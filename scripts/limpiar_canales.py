#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
limpiar_canales.py
------------------
Valida cada canal con validación REAL, elimina los caídos tras MAX_FALLOS
consecutivos y elimina duplicados existentes al inicio.

Reordena TODO el JSON al final: DOMINICANOS -> ANIME-INFANTIL -> PREMIUM, A→Z.

Reglas:
  - "ok"       -> resetea fallos a 0, conserva canal.
  - "temporal" -> NO cuenta como fallo. Conserva canal y estado.
  - "fallo"    -> suma 1 a fallos. Si llega a MAX_FALLOS, elimina.
  - "sin_url"  -> se elimina directamente.

Garantías:
  - Aborta si >UMBRAL_ABORTO de canales validables fallan.
  - Guardado atómico de JSON y estado.
  - DRY_RUN=1 no modifica nada.
  - Log de eliminados con timestamp.
  - Limpia entradas huérfanas del estado.
  - Deduplica al inicio y reordena al final.
"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from canales_common import (
    MAX_HILOS,
    cargar_json_seguro,
    clave_orden,
    guardar_json_atomico,
    log,
    normalizar_nombre,
    normalizar_url,
    probar_enlace,
)

# ---------- CONFIGURACIÓN ----------
JSON_PATH = Path("tvdominicana.json")
ESTADO_PATH = Path("estado_tvdominicana.json")
LOG_PATH = Path("canales_eliminados.log")

# MAX_FALLOS configurable por env: por defecto 6 fallos consecutivos para eliminar.
MAX_FALLOS = int(os.getenv("MAX_FALLOS", "6"))
UMBRAL_ABORTO = 0.9
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"
# -----------------------------------


def cargar_estado():
    if not ESTADO_PATH.exists():
        return {}
    try:
        with open(ESTADO_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            log("AVISO: estado no es dict. Se reinicia.")
            return {}
        return data
    except Exception as e:
        log(f"AVISO: estado corrupto ({e}). Se respalda y reinicia.")
        try:
            ESTADO_PATH.rename(ESTADO_PATH.with_suffix(".corrupto.json"))
        except OSError:
            pass
        return {}


def guardar_estado(estado):
    guardar_json_atomico(ESTADO_PATH, estado)


def anexar_log(lineas):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        for l in lineas:
            f.write(l + "\n")


def id_canal(canal):
    """ID estable y NO colisionable. Usa URL normalizada si existe."""
    url = normalizar_url(canal.get("url", ""))
    if url:
        return url
    return f"{canal.get('name','')}|{canal.get('logo','')}"


def deduplicar(canales):
    """Elimina duplicados por URL normalizada y por nombre normalizado + categoría."""
    vistos_url = set()
    vistos_nombre = set()
    salida = []
    descartados = 0
    for c in canales:
        u = normalizar_url(c.get("url", ""))
        nom = normalizar_nombre(c.get("name", ""))
        cat = c.get("group-title", "")
        clave_nombre = (nom, cat) if nom else None

        dup = False
        if u and u in vistos_url:
            log(f"  DUPLICADO URL (se descarta): {c.get('name','?')}")
            dup = True
        elif clave_nombre and clave_nombre in vistos_nombre:
            log(f"  DUPLICADO NOMBRE (se descarta): {c.get('name','?')}")
            dup = True

        if dup:
            descartados += 1
            continue

        if u:
            vistos_url.add(u)
        if clave_nombre:
            vistos_nombre.add(clave_nombre)
        salida.append(c)
    return salida, descartados


def main():
    if not JSON_PATH.exists():
        log("ERROR: no existe tvdominicana.json")
        sys.exit(1)

    canales = cargar_json_seguro(JSON_PATH)
    if not isinstance(canales, list):
        log("ERROR: tvdominicana.json no es una lista plana.")
        sys.exit(1)

    total_original = len(canales)
    if total_original == 0:
        log("Lista vacía.")
        return

    canales, n_dups = deduplicar(canales)
    if n_dups:
        log(f"Duplicados eliminados al inicio: {n_dups} "
            f"({total_original} -> {len(canales)} canales).")

    total = len(canales)
    if total == 0:
        log("Tras deduplicar no queda ningún canal.")
        if not DRY_RUN:
            guardar_json_atomico(JSON_PATH, [])
            guardar_estado({})
        return

    estado = cargar_estado()
    log(f"Comprobando {total} canales... (DRY_RUN={DRY_RUN}, MAX_FALLOS={MAX_FALLOS}, hilos={MAX_HILOS})")

    resultados = {}
    with ThreadPoolExecutor(max_workers=MAX_HILOS) as executor:
        futuros = {}
        for canal in canales:
            url = canal.get("url", "")
            if not url:
                resultados[id_canal(canal)] = (canal, "sin_url")
                continue
            futuros[executor.submit(probar_enlace, url, canal.get("headers"))] = canal

        for futuro in as_completed(futuros):
            canal = futuros[futuro]
            try:
                res = futuro.result()
            except Exception as e:
                log(f"  ERROR validando {canal.get('name','?')}: {e}")
                res = "fallo"
            resultados[id_canal(canal)] = (canal, res)

    nuevos_canales = []
    eliminados = []
    caidos_reales = 0
    temporales = 0
    sin_url = 0

    for canal in canales:
        cid = id_canal(canal)
        nombre = canal.get("name", "desconocido")
        url = canal.get("url", "")
        _, res = resultados.get(cid, (canal, "fallo"))

        if res == "sin_url":
            sin_url += 1
            log(f"  SIN URL (se elimina): {nombre}")
            eliminados.append((nombre, ""))
            estado.pop(cid, None)
            continue

        if res == "ok":
            log(f"  OK: {nombre}")
            estado[cid] = 0
            nuevos_canales.append(canal)

        elif res == "temporal":
            temporales += 1
            log(f"  TEMPORAL (no cuenta): {nombre}")
            nuevos_canales.append(canal)

        else:
            caidos_reales += 1
            fallos = estado.get(cid, 0) + 1
            estado[cid] = fallos

            if fallos >= MAX_FALLOS:
                log(f"  ELIMINADO (falló {fallos} veces): {nombre}")
                eliminados.append((nombre, url))
                estado.pop(cid, None)
            else:
                log(f"  FALLO {fallos}/{MAX_FALLOS}: {nombre}")
                nuevos_canales.append(canal)

    validables = total - sin_url
    if validables > 0 and (caidos_reales / validables) > UMBRAL_ABORTO:
        log(f"::warning::ABORTADO: fallaron {caidos_reales}/{validables} canales validables. No se modifica nada.")
        sys.exit(0)

    ids_vivos = {id_canal(c) for c in nuevos_canales}
    huerfanos = [k for k in estado.keys() if k not in ids_vivos]
    for k in huerfanos:
        estado.pop(k, None)
    if huerfanos:
        log(f"Estado: {len(huerfanos)} entradas huérfanas eliminadas.")

    if eliminados and not DRY_RUN:
        anexar_log([f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {n}  {u}"
                    for n, u in eliminados])

    if DRY_RUN:
        log(f"[DRY_RUN] Se eliminarían {len(eliminados)} canales (incluye {n_dups} duplicados iniciales).")
        log(f"[DRY_RUN] Estado que se guardaría: {len(estado)} entradas.")
        return

    if eliminados or n_dups:
        # Reordenar SIEMPRE para garantizar DOMINICANOS -> ANIME-INFANTIL -> PREMIUM
        nuevos_canales.sort(key=clave_orden)
        guardar_json_atomico(JSON_PATH, nuevos_canales)
        log(f"Se eliminaron {len(eliminados)} canales y {n_dups} duplicados iniciales.")
    else:
        # Aunque no se elimine nada, reordenar por si el JSON venía desordenado
        nuevos_canales.sort(key=clave_orden)
        guardar_json_atomico(JSON_PATH, nuevos_canales)
        log("No se eliminó ningún canal.")

    guardar_estado(estado)

    log(f"Resumen: ok={total - caidos_reales - temporales - sin_url}, "
        f"temporal={temporales}, fallo={caidos_reales}, sin_url={sin_url}, "
        f"eliminados={len(eliminados)}, duplicados_iniciales={n_dups}")


if __name__ == "__main__":
    main()
