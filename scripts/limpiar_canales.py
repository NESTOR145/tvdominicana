#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
limpiar_canales.py
------------------
Comprueba cada canal de tvdominicana.json con validación REAL
(Content-Type + primeros bytes + detección de HTML/JSON de error) y elimina
los que estén realmente caídos tras MAX_FALLOS consecutivos.

Reglas:
  - "ok"       -> resetea fallos a 0, conserva canal.
  - "temporal" -> NO cuenta como fallo (403/429/5xx/timeout). Conserva canal y estado.
  - "fallo"    -> suma 1 a fallos. Si llega a MAX_FALLOS, elimina.
  - "sin_url"  -> se elimina directamente (un canal sin URL no sirve).

Garantías:
  - Aborta (sin modificar nada) si >UMBRAL_ABORTO de canales validables fallan.
  - Guardado atómico de JSON y estado.
  - DRY_RUN vía variable de entorno DRY_RUN=1 (no modifica JSON, estado ni log).
  - Log de eliminados con timestamp.
  - Limpia entradas huérfanas del estado.
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
    guardar_json_atomico,
    log,
    probar_enlace,
)

# ---------- CONFIGURACIÓN ----------
JSON_PATH = Path("tvdominicana.json")
ESTADO_PATH = Path("estado_tvdominicana.json")
LOG_PATH = Path("canales_eliminados.log")

MAX_FALLOS = 6
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
    """ID estable y NO colisionable. Si no hay URL, usa name+logo."""
    url = canal.get("url", "")
    if url:
        return url
    return f"{canal.get('name','')}|{canal.get('logo','')}"


def main():
    if not JSON_PATH.exists():
        log("ERROR: no existe tvdominicana.json")
        sys.exit(1)

    canales = cargar_json_seguro(JSON_PATH)
    if not isinstance(canales, list):
        log("ERROR: tvdominicana.json no es una lista plana.")
        sys.exit(1)

    total = len(canales)
    if total == 0:
        log("Lista vacía.")
        return

    estado = cargar_estado()
    log(f"Comprobando {total} canales... (DRY_RUN={DRY_RUN}, MAX_FALLOS={MAX_FALLOS}, hilos={MAX_HILOS})")

    # Validar en paralelo: cada futuro devuelve (canal, resultado)
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

    # Procesar en el orden original del JSON para preservar orden relativo
    for canal in canales:
        cid = id_canal(canal)
        nombre = canal.get("name", "desconocido")
        url = canal.get("url", "")
        _, res = resultados.get(cid, (canal, "fallo"))

        if res == "sin_url":
            sin_url += 1
            log(f"  SIN URL (se elimina): {nombre}")
            # Un canal sin URL no sirve: se elimina directamente.
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
            # Conserva canal y estado tal cual (no incrementa fallos)
            nuevos_canales.append(canal)

        else:  # "fallo"
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

    # Aborto por caída masiva (solo cuenta fallos reales, excluyendo sin_url)
    validables = total - sin_url
    if validables > 0 and (caidos_reales / validables) > UMBRAL_ABORTO:
        log(f"::warning::ABORTADO: fallaron {caidos_reales}/{validables} canales validables. No se modifica nada.")
        sys.exit(0)

    # Limpiar huérfanos del estado (canales que ya no están en el JSON)
    ids_vivos = {id_canal(c) for c in nuevos_canales}
    huerfanos = [k for k in estado.keys() if k not in ids_vivos]
    for k in huerfanos:
        estado.pop(k, None)
    if huerfanos:
        log(f"Estado: {len(huerfanos)} entradas huérfanas eliminadas.")

    # Aplicar cambios
    if eliminados and not DRY_RUN:
        anexar_log([f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {n}  {u}"
                    for n, u in eliminados])

    if DRY_RUN:
        log(f"[DRY_RUN] Se eliminarían {len(eliminados)} canales. No se modifica nada.")
        log(f"[DRY_RUN] Estado que se guardaría: {len(estado)} entradas.")
        return

    if eliminados:
        guardar_json_atomico(JSON_PATH, nuevos_canales)
        log(f"Se eliminaron {len(eliminados)} canales.")
    else:
        log("No se eliminó ningún canal.")

    guardar_estado(estado)

    log(f"Resumen: ok={total - caidos_reales - temporales - sin_url}, "
        f"temporal={temporales}, fallo={caidos_reales}, sin_url={sin_url}, "
        f"eliminados={len(eliminados)}")


if __name__ == "__main__":
    main()
