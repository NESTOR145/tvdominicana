#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Comprueba cada canal de tvdominicana.json y elimina los que estén caídos
tras 3 fallos consecutivos.
"""

import json
import sys
import time
from pathlib import Path
import requests

# ---------- CONFIGURACIÓN ----------
JSON_PATH = Path("tvdominicana.json")
ESTADO_PATH = Path("estado_tvdominicana.json")
LOG_PATH = Path("canales_eliminados.log")

MAX_FALLOS = 3
TIMEOUT = 15
REINTENTOS_POR_ENLACE = 3
PAUSA_ENTRE_REINTENTOS = 3
UMBRAL_ABORTO = 0.9
DRY_RUN = False     # False = borra de verdad. True = solo informa.

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
# -----------------------------------


def cargar_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def guardar_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def cargar_estado():
    if ESTADO_PATH.exists():
        try:
            with open(ESTADO_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def guardar_estado(estado):
    with open(ESTADO_PATH, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=2)


def anexar_log(lineas):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        for l in lineas:
            f.write(l + "\n")


def id_canal(canal):
    return f"{canal.get('name','')}||{canal.get('url','')}"


def intento_unico(url, headers):
    h = {"User-Agent": USER_AGENT}
    if isinstance(headers, dict):
        for k, v in headers.items():
            if isinstance(k, str) and isinstance(v, str):
                h[k] = v
    try:
        r = requests.get(url, headers=h, timeout=TIMEOUT, stream=True)
        code = r.status_code
        try:
            r.close()
        except Exception:
            pass
        if code < 400:
            return "ok"
        if code in (403, 429) or 500 <= code < 600:
            return "temporal"
        return "fallo"
    except (requests.exceptions.Timeout,
            requests.exceptions.ConnectionError):
        return "temporal"
    except Exception:
        return "fallo"


def probar_enlace(url, headers):
    for i in range(REINTENTOS_POR_ENLACE):
        res = intento_unico(url, headers)
        if res == "ok":
            return True
        if i < REINTENTOS_POR_ENLACE - 1:
            time.sleep(PAUSA_ENTRE_REINTENTOS)
    return False


def main():
    if not JSON_PATH.exists():
        print("ERROR: no existe tvdominicana.json")
        sys.exit(1)

    canales = cargar_json(JSON_PATH)
    if not isinstance(canales, list):
        print("ERROR: tvdominicana.json no es una lista plana.")
        sys.exit(1)

    total = len(canales)
    if total == 0:
        print("Lista vacía.")
        return

    estado = cargar_estado()
    nuevos_canales = []
    eliminados = []
    fallidos = 0

    print(f"Comprobando {total} canales... (DRY_RUN={DRY_RUN})")

    for canal in canales:
        nombre = canal.get("name", "desconocido")
        url = canal.get("url", "")
        headers = canal.get("headers")
        cid = id_canal(canal)

        if not url:
            print(f"  SIN URL: {nombre}")
            fallidos += 1
            continue

        vivo = probar_enlace(url, headers)

        if vivo:
            print(f"  OK: {nombre}")
            estado[cid] = 0
            nuevos_canales.append(canal)
        else:
            fallidos += 1
            fallos = estado.get(cid, 0) + 1
            estado[cid] = fallos

            if fallos >= MAX_FALLOS:
                print(f"  ELIMINADO (falló {fallos} veces): {nombre}")
                eliminados.append((nombre, url))
                estado.pop(cid, None)
            else:
                print(f"  FALLO {fallos}/{MAX_FALLOS}: {nombre}")
                nuevos_canales.append(canal)

    if total > 0 and (fallidos / total) > UMBRAL_ABORTO:
        print(f"ABORTADO: fallaron {fallidos}/{total}. No se modifica el JSON.")
        sys.exit(0)

    if eliminados:
        anexar_log([f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {n}  {u}"
                    for n, u in eliminados])

        if DRY_RUN:
            print(f"[DRY_RUN] Se eliminarían {len(eliminados)} canales. No se modifica nada.")
        else:
            guardar_json(JSON_PATH, nuevos_canales)
            print(f"Se eliminaron {len(eliminados)} canales.")
    else:
        print("No se eliminó ningún canal.")

    if not DRY_RUN:
        guardar_estado(estado)


if __name__ == "__main__":
    main()
