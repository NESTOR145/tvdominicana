#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
limpiar_canales.py
------------------
Valida cada canal, elimina caídos tras MAX_FALLOS consecutivos y deduplica
de forma SEGURA (solo borra duplicados entre los canales que están online).
Además, corrige el campo 'country': solo los dominicanos llevan "DO".
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
    normalizar_categoria,
    normalizar_nombre,
    normalizar_url,
    probar_enlace,
)

JSON_PATH = Path("tvdominicana.json")
ESTADO_PATH = Path("estado_tvdominicana.json")
LOG_PATH = Path("canales_eliminados.log")

MAX_FALLOS = int(os.getenv("MAX_FALLOS", "6"))
UMBRAL_ABORTO = 0.9
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"


def cargar_estado():
    if not ESTADO_PATH.exists():
        return {}
    try:
        with open(ESTADO_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
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
    url = normalizar_url(canal.get("url", ""))
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

    total_original = len(canales)
    if total_original == 0:
        log("Lista vacía.")
        return

    # 1. Re-normalizar categorías de TODOS los canales
    log("Re-normalizando categorías...")
    cambios_cat = 0
    for c in canales:
        cat_original = c.get("group-title", "")
        tvg = c.get("tvg-id") or c.get("_tvg_id") or c.get("tvg_id") or ""
        nombre = c.get("name", "")
        cat_nueva = normalizar_categoria(cat_original, tvg, nombre)
        if cat_nueva != cat_original:
            cambios_cat += 1
        c["group-title"] = cat_nueva
        if tvg:
            c["tvg-id"] = tvg
        else:
            c.pop("tvg-id", None)
            c.pop("tvg_id", None)

    # --- CORREGIR CAMPO 'country' ---
    log("Corrigiendo campo 'country' (solo dominicanos llevan DO)...")
    for c in canales:
        cat = c.get("group-title", "")
        tvg = (c.get("tvg-id") or c.get("_tvg_id") or "").lower()
        
        if cat == "DOMINICANOS":
            if c.get("country") != "DO":
                c["country"] = "DO"
        else:
            if c.get("country") == "DO":
                if "." in tvg:
                    posible_pais = tvg.split(".")[-1].split("@")[0].upper()
                    if len(posible_pais) == 2 and posible_pais.isalpha() and posible_pais != "DO":
                        c["country"] = posible_pais
                    else:
                        c.pop("country", None)
                else:
                    c.pop("country", None)
    # --- FIN CORRECCIÓN 'country' ---

    estado = cargar_estado()
    log(f"Comprobando {total_original} canales (incluyendo duplicados)... (DRY_RUN={DRY_RUN}, MAX_FALLOS={MAX_FALLOS}, hilos={MAX_HILOS})")

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

    # 2. Clasificar resultados y procesar fallos
    canales_sobrevivientes = []
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
            canales_sobrevivientes.append(canal)
        elif res == "temporal":
            temporales += 1
            log(f"  TEMPORAL (no cuenta): {nombre}")
            canales_sobrevivientes.append(canal)
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
                canales_sobrevivientes.append(canal)

    validables = total_original - sin_url
    if validables > 0 and (caidos_reales / validables) > UMBRAL_ABORTO:
        log(f"::warning::ABORTADO: fallaron {caidos_reales}/{validables} canales validables. No se modifica nada.")
        sys.exit(0)

    # 3. DEDUPLICACIÓN SEGURA (Solo entre los que sobrevivieron y están online)
    log("Deduplicando canales online...")
    vistos_url = set()
    vistos_nombre = set()
    canales_finales = []
    n_dups = 0

    for c in canales_sobrevivientes:
        u = normalizar_url(c.get("url", ""))
        nom = normalizar_nombre(c.get("name", ""))
        clave_nombre = nom if nom else None

        dup = False
        if u and u in vistos_url:
            log(f"  DUPLICADO URL (se descarta): {c.get('name','?')}")
            dup = True
        elif clave_nombre and clave_nombre in vistos_nombre:
            log(f"  DUPLICADO NOMBRE (se descarta): {c.get('name','?')}")
            dup = True

        if dup:
            n_dups += 1
            continue

        if u:
            vistos_url.add(u)
        if clave_nombre:
            vistos_nombre.add(clave_nombre)
        canales_finales.append(c)

    # Limpiar estado huérfano
    ids_vivos = {id_canal(c) for c in canales_finales}
    huerfanos = [k for k in estado.keys() if k not in ids_vivos]
    for k in huerfanos:
        estado.pop(k, None)

    if eliminados and not DRY_RUN:
        anexar_log([f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {n}  {u}" for n, u in eliminados])

    if DRY_RUN:
        log(f"[DRY_RUN] Se eliminarían {len(eliminados)} caídos y {n_dups} duplicados.")
        return

    # Guardar cambios
    if eliminados or n_dups or cambios_cat or len(canales_finales) != total_original:
        canales_finales.sort(key=clave_orden)
        guardar_json_atomico(JSON_PATH, canales_finales)
        log(f"Se eliminaron {len(eliminados)} caídos y {n_dups} duplicados.")
    else:
        canales_finales.sort(key=clave_orden)
        guardar_json_atomico(JSON_PATH, canales_finales)
        log("No se eliminó ningún canal.")

    guardar_estado(estado)
    log(f"Resumen: ok={total_original - caidos_reales - temporales - sin_url}, "
        f"temporal={temporales}, fallo={caidos_reales}, sin_url={sin_url}, "
        f"eliminados={len(eliminados)}, duplicados_borrados={n_dups}")


if __name__ == "__main__":
    main()
