#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agregar_canales.py
------------------
Descarga múltiples listas M3U remotas, valida cada canal y agrega a tvdominicana.json
SOLO los canales que estén online y que aún no existan (comparando por URL).

Clasificación de group-title (solo 3 valores posibles):
  1) Anime/Infantil/Kids/Animation/Cartoon/Children/Preschool/Baby/Toddler -> ANIME-INFANTIL (gratis)
  2) tvg-id dominicano (.do@... o termina en .do)                          -> DOMINICANOS    (gratis)
  3) group-title explícito dominicano (DOMINICANOS, RD, REPUBLICA DOMINICANA...) -> DOMINICANOS (gratis)
  4) nombre con "dominicana/dominicano/quisqueya" (fallback seguro)        -> DOMINICANOS    (gratis)
  5) Todo lo demás                                                          -> PREMIUM

Garantías:
  - NO toca estado_tvdominicana.json.
  - NO toca canales_eliminados.log.
  - NO elimina ningún canal existente.
  - Si todas las listas remotas fallan, sale sin modificar el JSON.
  - Si un canal nuevo está offline, no se agrega.
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

URLS_REMOTAS_M3U = [
    "https://raw.githubusercontent.com/TVPRO20/Megatv23/master/tv23.m3u",
    "https://raw.githubusercontent.com/NESTOR145/miscanalesdetv/refs/heads/main/todosmiscanales.m3u",
    "https://iptv-org.github.io/iptv/countries/do.m3u",
]

TIMEOUT = 15
REINTENTOS_POR_ENLACE = 2
PAUSA_ENTRE_REINTENTOS = 2
MAX_HILOS = 30

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
# -----------------------------------

CAT_DOMINICANOS = "DOMINICANOS"
CAT_ANIME_INFANTIL = "ANIME-INFANTIL"
CAT_PREMIUM = "PREMIUM"

FREE_CATEGORIES = {CAT_DOMINICANOS, CAT_ANIME_INFANTIL}

KIDS_KEYWORDS = {
    "ANIME-INFANTIL", "ANIME INFANTIL", "INFANTIL", "KIDS",
    "DIBUJOS ANIMADOS", "ANIME", "CARTOONS", "CARTOON",
    "ANIMATION", "CHILDREN", "PRESCHOOL", "BABY", "TODDLER", "TODDLERS",
}

DOM_GROUP_KEYWORDS = {
    "DOMINICANOS", "DOMINICANA", "DOMINICANO", "RD",
    "REPUBLICA DOMINICANA", "REPÚBLICA DOMINICANA",
}

DOM_NAME_HINTS = ("dominicana", "dominicano", "quisqueya")


def normalizar_categoria(valor: str, tvg_id: str = "", nombre: str = "") -> str:
    v = (valor or "").strip().upper()
    tid = (tvg_id or "").strip().lower()
    nom = (nombre or "").strip().lower()

    partes = [p.strip() for p in v.split(";") if p.strip()]

    # 1) Anime / Infantil / Kids -> gratis
    if any(p in KIDS_KEYWORDS for p in partes):
        return CAT_ANIME_INFANTIL

    # 2) Dominicano por tvg-id (.do@... o termina en .do)
    if ".do@" in tid or tid.endswith(".do"):
        return CAT_DOMINICANOS

    # 3) Dominicano por group-title explícito
    if any(p in DOM_GROUP_KEYWORDS for p in partes):
        return CAT_DOMINICANOS

    # 4) Fallback por nombre
    if any(h in nom for h in DOM_NAME_HINTS):
        return CAT_DOMINICANOS

    # 5) Todo lo demás -> PREMIUM
    return CAT_PREMIUM


def log(msg: str) -> None:
    print(msg, flush=True)


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
                "_tvg_id": atributos.get("tvg-id", ""),  # interno
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


def main() -> None:
    log("=" * 60)
    log("AGREGAR CANALES ONLINE DESDE MÚLTIPLES LISTAS M3U")
    log("=" * 60)

    json_actual = cargar_json(JSON_PATH)
    log(f"Canales en el JSON actual: {len(json_actual)}")

    todos_los_candidatos = []

    for url_remota in URLS_REMOTAS_M3U:
        contenido = descargar_m3u(url_remota)
        if contenido:
            canales_m3u = parsear_m3u(contenido)
            log(f"Canales encontrados en {url_remota}: {len(canales_m3u)}")
            todos_los_candidatos.extend(canales_m3u)

    if not todos_los_candidatos:
        log("Ninguna lista devolvió canales. Nada que hacer.")
        return

    urls_existentes = {c.get("url") for c in json_actual if c.get("url")}
    vistos = set()
    candidatos = []
    for c in todos_los_candidatos:
        u = c.get("url")
        if not u or u in urls_existentes or u in vistos:
            continue
        vistos.add(u)
        candidatos.append(c)

    log(f"Canales nuevos únicos por validar: {len(candidatos)}")

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

    for c in online:
        cat_original = c.get("group-title", "")
        tvg = c.pop("_tvg_id", "")
        nombre = c.get("name", "")
        c["group-title"] = normalizar_categoria(cat_original, tvg, nombre)

    json_actual.extend(online)
    guardar_json(JSON_PATH, json_actual)
    log(f"JSON actualizado. Total de canales: {len(json_actual)}")
    log("=" * 60)


if __name__ == "__main__":
    main()
