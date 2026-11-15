#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
limpiar_canales.py
------------------
Valida cada canal, elimina caídos tras MAX_FALLOS consecutivos y deduplica.
Sin blindaje: cualquier canal que falle MAX_FALLOS veces se elimina.
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
        return data if isinstance(data, dict) else {}
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
    return url if url else f"{canal.get('name','')}|{canal.get('logo','')}"


def main():
    if not JSON_PATH.exists():
        log("ERROR: no existe tvdominicana.json")
        sys.exit(1)

    canales = cargar_json_seguro(JSON_PATH)
    if not isinstance(canales, list):
        log("ERROR: JSON no es lista plana.")
        sys.exit(1)

    total = len(canales)
    if total == 0:
        log("Lista vacía.")
        return

    # 1. Re-normalizar categorías
    log("Re-normalizando categorías...")
    cambios = 0
    for c in canales:
        cat_o = c.get("group-title", "")
        tvg = c.get("tvg-id") or c.get("_tvg_id") or c.get("tvg_id") or ""
        cat_n = normalizar_categoria(cat_o, tvg, c.get("name", ""))
        if cat_n != cat_o:
            cambios += 1
        c["group-title"] = cat_n
        if tvg:
            c["tvg-id"] = tvg
        else:
            c.pop("tvg-id", None)
            c.pop("tvg_id", None)

    # 2. Corregir 'country'
    log("Corrigiendo 'country'...")
    for c in canales:
        cat = c.get("group-title", "")
        tvg = (c.get("tvg-id") or c.get("_tvg_id") or "").lower()
        if cat == "DOMINICANOS":
            if c.get("country") != "DO":
                c["country"] = "DO"
        else:
            if c.get("country") == "DO":
                if "." in tvg:
                    p = tvg.split(".")[-1].split("@")[0].upper()
                    if len(p) == 2 and p.isalpha() and p != "DO":
                        c["country"] = p
                    else:
                        c.pop("country", None)
                else:
                    c.pop("country", None)

    estado = cargar_estado()
    log(f"Comprobando {total} canales... (DRY_RUN={DRY_RUN}, MAX_FALLOS={MAX_FALLOS}, hilos={MAX_HILOS})")

    # 3. Validar todos en paralelo
    resultados = {}
    with ThreadPoolExecutor(max_workers=MAX_HILOS) as ex:
        futuros = {}
        for canal in canales:
            url = canal.get("url", "")
            if not url:
                resultados[id_canal(canal)] = (canal, "sin_url")
                continue
            futuros[ex.submit(probar_enlace, url, canal.get("headers"))] = canal

        for f in as_completed(futuros):
            canal = futuros[f]
            try:
                res = f.result()
            except Exception as e:
                log(f"  ERROR {canal.get('name','?')}: {e}")
                res = "fallo"
            resultados[id_canal(canal)] = (canal, res)

    # 4. Clasificar resultados
    sobrevivientes = []
    eliminados = []
    caidos = 0
    temporales = 0
    sin_url = 0

    for canal in canales:
        cid = id_canal(canal)
        nombre = canal.get("name", "desconocido")
        url = canal.get("url", "")
        _, res = resultados.get(cid, (canal, "fallo"))

        if res == "sin_url":
            sin_url += 1
            log(f"  SIN URL (eliminado): {nombre}")
            eliminados.append((nombre, ""))
            estado.pop(cid, None)
        elif res == "ok":
            log(f"  OK: {nombre}")
            estado[cid] = 0
            sobrevivientes.append(canal)
        elif res == "temporal":
            temporales += 1
            log(f"  TEMPORAL: {nombre}")
            sobrevivientes.append(canal)
        else:
            caidos += 1
            fallos = estado.get(cid, 0) + 1
            if fallos >= MAX_FALLOS:
                log(f"  ELIMINADO ({fallos} fallos): {nombre}")
                eliminados.append((nombre, url))
                estado.pop(cid, None)
            else:
                estado[cid] = fallos
                log(f"  FALLO {fallos}/{MAX_FALLOS}: {nombre}")
                sobrevivientes.append(canal)

    # 5. Umbral de aborto
    validables = total - sin_url
    if validables > 0 and (caidos / validables) > UMBRAL_ABORTO:
        log(f"::warning::ABORTADO: {caidos}/{validables} fallaron.")
        sys.exit(0)

    # 6. Deduplicar
    log("Deduplicando...")
    vistos_url = set()
    vistos_nombre = set()
    finales = []
    n_dups = 0

    for c in sobrevivientes:
        u = normalizar_url(c.get("url", ""))
        n = normalizar_nombre(c.get("name", ""))
        clave = n if n else None
        dup = False
        if u and u in vistos_url:
            log(f"  DUP URL: {c.get('name','?')}")
            dup = True
        elif clave and clave in vistos_nombre:
            log(f"  DUP NOMBRE: {c.get('name','?')}")
            dup = True
        if dup:
            n_dups += 1
            continue
        if u:
            vistos_url.add(u)
        if clave:
            vistos_nombre.add(clave)
        finales.append(c)

    # 7. Limpiar estado huérfano
    ids_vivos = {id_canal(c) for c in finales}
    for k in [k for k in estado.keys() if k not in ids_vivos]:
        estado.pop(k, None)

    if eliminados and not DRY_RUN:
        anexar_log([f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {n}  {u}" for n, u in eliminados])

    if DRY_RUN:
        log(f"[DRY_RUN] Se eliminarían {len(eliminados)} caídos y {n_dups} duplicados.")
        return

    # 8. Guardar
    finales.sort(key=clave_orden)
    guardar_json_atomico(JSON_PATH, finales)
    guardar_estado(estado)
    log(f"Resumen: ok={total - caidos - temporales - sin_url}, temporal={temporales}, "
        f"fallo={caidos}, sin_url={sin_url}, eliminados={len(eliminados)}, dups={n_dups}")


if __name__ == "__main__":
    main()
