#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agregar_canales.py
------------------
Descarga la lista M3U remota, valida cada canal y agrega a tvdominicana.json
SOLO los canales que estén online y que aún no existan (comparando por URL).

Garantías:
  - NO toca estado_tvdominicana.json.
  - NO toca canales_eliminados.log.
  - NO elimina ningún canal existente.
  - Si la lista remota falla, sale sin modificar el JSON.
  - Si un canal nuevo está offline, no se agrega.
  - Cada canal nuevo se agrega con su campo 'group-title' normalizado:
      * "DOMINICANOS"     -> gratis
      * "ANIME-INFANTIL"  -> gratis
      * "PREMIUM"         -> premium (cualquier otra categoría)
"""

import json
import re
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ---------- CONFIGURACIÓN ----------
JSON_PATH = Path("tvdominicana.json")

URL_REMOTA_M3U = "https://raw.githubusercontent.com/TVPRO20/Megatv23/master/tv23.m3u"

TIMEOUT = 15                    # segundos por intento
REINTENTOS_POR_ENLACE = 2       # intentos por canal
PAUSA_ENTRE_REINTENTOS = 2      # segundos entre intentos
MAX_HILOS = 30                  # validaciones simultáneas

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
# -----------------------------------

# ---------- CATEGORÍAS VÁLIDAS ----------
CAT_DOMINICANOS = "DOMINICANOS"
CAT_ANIME_INFANTIL = "ANIME-INFANTIL"
CAT_PREMIUM = "PREMIUM"

FREE_CATEGORIES = {CAT_DOMINICANOS, CAT_ANIME_INFANTIL}


def normalizar_categoria(valor: str) -> str:
    """
    Normaliza el group-title recibido a uno de los 3 valores canónicos.
    Solo DOMINICANOS y ANIME-INFANTIL son gratis. Todo lo demás es PREMIUM.
    """
    if not valor:
        return CAT_PREMIUM

    v = valor.strip().upper()

    if v in ("DOMINICANOS", "DOMINICANA", "RD", "REPUBLICA DOMINICANA",
             "REPÚBLICA DOMINICANA"):
        return CAT_DOMINICANOS

    if v in ("ANIME-INFANTIL", "ANIME INFANTIL", "INFANTIL", "KIDS",
             "DIBUJOS ANIMADOS", "ANIME", "CARTOONS", "CARTOON"):
        return CAT_ANIME_INFANTIL

    return CAT_PREMIUM


# ---------- LOG ----------
def log(msg: str) -> None:
    print(msg, flush=True)


# ---------- JSON ----------
def cargar_json(path: Path):
    if not path.exists():
        log(f"AVISO: no existe {path}. Se creará al final.")
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            log(f"ERROR: {path} no contiene una lista plana.")
            sys.exit(1)
        return data
    except json.JSONDecodeError as e:
        log(f"ERROR: {path} no es JSON válido: {e}")
        sys.exit(1)


def guardar_json(path: Path, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---------- DESCARGA M3U ----------
def descargar_m3u(url: str):
    try:
        log(f"→ Descargando lista M3U: {url}")
        headers = {"User-Agent": USER_AGENT}
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        log(f"  OK: {len(r.text):,} bytes recibidos.")
        return r.text
    except requests.RequestException as e:
        log(f"  ERROR al descargar la lista: {e}")
        return None


# ---------- PARSER M3U ----------
RE_ATTR = re.compile(r'([\w-]+)="([^"]*)"')


def parsear_m3u(contenido: str):
    canales = []
    canal_actual = None

    for linea in contenido.splitlines():
        linea = linea.strip()
        if not linea:
            continue

        if linea.startswith("#EXTM3U"):
            continue

        if linea.startswith("#EXTINF:"):
            info = linea[len("#EXTINF:"):]
            atributos = dict(RE_ATTR.findall(info))

            sin_attrs = RE_ATTR.sub("", info)
            nombre = ""
            if "," in sin_attrs:
                nombre = sin_attrs.split(",", 1)[1].strip()

            canal_actual = {
                "name": nombre,
                "logo": atributos.get("tvg-logo", ""),
                "url": "",
            }
            group = atributos.get("group-title", "")
            if group:
                canal_actual["group-title"] = group

        elif linea.startswith("#EXTVLCOPT:"):
            if canal_actual is not None:
                opt = linea[len("#EXTVLCOPT:"):]
                if "=" in opt:
                    k, v = opt.split("=", 1)
                    k, v = k.strip(), v.strip()
                    if k == "http-user-agent":
                        canal_actual.setdefault("headers", {})["User-Agent"] = v
                    elif k == "http-referrer":
                        canal_actual.setdefault("headers", {})["Referer"] = v

        elif linea.startswith("#"):
            continue

        else:
            if canal_actual is not None:
                canal_actual["url"] = linea
                if canal_actual["url"]:
                    canales.append(canal_actual)
                canal_actual = None

    return canales


# ---------- VALIDACIÓN ----------
def intento_unico(url: str, headers) -> bool:
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
        return code < 400
    except Exception:
        return False


def probar_enlace(url: str, headers) -> bool:
    for i in range(REINTENTOS_POR_ENLACE):
        if intento_unico(url, headers):
            return True
        if i < REINTENTOS_POR_ENLACE - 1:
            time.sleep(PAUSA_ENTRE_REINTENTOS)
    return False


def validar_canal(canal):
    url = canal.get("url")
    if not url:
        return None
    if probar_enlace(url, canal.get("headers")):
        return canal
    return None


# ---------- MAIN ----------
def main() -> None:
    log("=" * 60)
    log("AGREGAR CANALES ONLINE DESDE LISTA REMOTA")
    log("=" * 60)

    json_actual = cargar_json(JSON_PATH)
    log(f"Canales en el JSON actual: {len(json_actual)}")

    contenido = descargar_m3u(URL_REMOTA_M3U)
    if not contenido:
        log("No se pudo obtener la lista. Se conserva el JSON sin cambios.")
        sys.exit(0)

    canales_m3u = parsear_m3u(contenido)
    log(f"Canales encontrados en el M3U: {len(canales_m3u)}")

    if not canales_m3u:
        log("El M3U no contiene canales. Nada que hacer.")
        return

    urls_existentes = {c.get("url") for c in json_actual if c.get("url")}
    vistos = set()
    candidatos = []
    for c in canales_m3u:
        u = c.get("url")
        if not u or u in urls_existentes or u in vistos:
            continue
        vistos.add(u)
        candidatos.append(c)

    log(f"Canales nuevos por validar: {len(candidatos)}")

    if not candidatos:
        log("No hay canales nuevos. Nada que agregar.")
        return

    log(f"Validando con {MAX_HILOS} hilos...")
    online = []
    with ThreadPoolExecutor(max_workers=MAX_HILOS) as executor:
        futuros = {executor.submit(validar_canal, c): c for c in candidatos}
        for futuro in as_completed(futuros):
            canal = futuros[futuro]
            try:
                resultado = futuro.result()
            except Exception as e:
                log(f"  ERROR validando {canal.get('name', '?')}: {e}")
                resultado = None
            if resultado:
                online.append(resultado)
                log(f"  ✅ {canal.get('name', '(sin nombre)')}")
            else:
                log(f"  ❌ {canal.get('name', '(sin nombre)')}")

    log(f"Canales nuevos online encontrados: {len(online)}")

    if not online:
        log("No se agregó ningún canal nuevo.")
        return

    # Normalizar la categoría (group-title) de cada canal nuevo
    for c in online:
        cat_original = c.get("group-title", "")
        c["group-title"] = normalizar_categoria(cat_original)

    # Merge y guardado
    json_actual.extend(online)
    guardar_json(JSON_PATH, json_actual)
    log(f"JSON actualizado. Total de canales: {len(json_actual)}")
    log("=" * 60)


if __name__ == "__main__":
    main()
