#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agregar_canales.py
------------------
Descarga múltiples listas M3U remotas, valida cada canal (HLS real) y agrega a
tvdominicana.json SOLO los canales que estén online y que aún no existan
(comparando por URL).

VALIDACIÓN MEJORADA (Opción B):
  - Si la URL es HLS (.m3u8 / mpegurl / contiene #EXTM3U):
      1. Descarga el master playlist.
      2. Si es master, sigue el PRIMER variant.
      3. Descarga el variant y verifica el PRIMER chunk (HEAD o GET).
      → Descarta playlists vacías, HTML de error, tokens expirados, etc.
  - Si la URL no es HLS (mp4, ts, etc.): chequeo ligero de status + bytes.
  - Fallback: acepta si el servidor devolvió algún byte válido.

Clasificación final (2 gratis + 9 premium):
  GRATIS:
    - ANIME-INFANTIL  (Kids, Animation, Cartoon, Children, Preschool, Baby, Toddler...)
    - DOMINICANOS     (tvg-id .do / group-title RD / nombre dominicano)
  PREMIUM (subdividido por género):
    - PELICULAS      (Movies, Classic)
    - SERIES         (Series)
    - NOTICIAS       (News)
    - DEPORTES       (Sports)
    - MUSICA         (Music)
    - RELIGION       (Religious)
    - DOCUMENTALES   (Documentary, Science)
    - COMEDIA        (Comedy)
    - VARIEDADES     (todo lo demás: General, Entertainment, Family, Shop, Auto, Weather...)

Garantías:
  - NO toca estado_tvdominicana.json.
  - NO toca canales_eliminados.log.
  - NO elimina ningún canal existente.
  - NO modifica canales ya presentes en el JSON.
  - Si todas las listas remotas fallan, sale sin modificar el JSON.
  - Si un canal nuevo está offline, no se agrega.
"""

import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ---------- CONFIGURACIÓN ----------
JSON_PATH = Path("tvdominicana.json")

URLS_REMOTAS_M3U = [
    "https://raw.githubusercontent.com/TVPRO20/Megatv23/master/tv23.m3u",
    "https://raw.githubusercontent.com/NESTOR145/miscanalesdetv/refs/heads/main/todosmiscanales.m3u",
    "https://iptv-org.github.io/iptv/countries/do.m3u",
]

# --- Ajustes optimizados para Opción B ---
TIMEOUT = 8                    # canales sanos responden <3s
REINTENTOS_POR_ENLACE = 1      # 1 intento (Opción B ya valida 3 capas)
PAUSA_ENTRE_REINTENTOS = 1
MAX_HILOS = 50                 # más que 30 sin floodear hosts
MAX_PROFUNDIDAD_HLS = 2        # master → variant → segmentos
BYTES_LECTURA_INICIAL = 16384  # 16 KB para detectar cabeceras HLS

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
# -----------------------------------

# ---- Categorías gratis (no se tocan) ----
CAT_DOMINICANOS = "DOMINICANOS"
CAT_ANIME_INFANTIL = "ANIME-INFANTIL"
FREE_CATEGORIES = {CAT_DOMINICANOS, CAT_ANIME_INFANTIL}

# ---- Categorías premium (subdivididas por género) ----
CAT_PELICULAS = "PELICULAS"
CAT_SERIES = "SERIES"
CAT_NOTICIAS = "NOTICIAS"
CAT_DEPORTES = "DEPORTES"
CAT_MUSICA = "MUSICA"
CAT_RELIGION = "RELIGION"
CAT_DOCUMENTALES = "DOCUMENTALES"
CAT_COMEDIA = "COMEDIA"
CAT_VARIEDADES = "VARIEDADES"   # ← fallback de todo lo demás

# Palabras clave para detectar Kids/Anime (gratis)
KIDS_KEYWORDS = {
    "ANIME-INFANTIL", "ANIME INFANTIL", "INFANTIL", "KIDS",
    "DIBUJOS ANIMADOS", "ANIME", "CARTOONS", "CARTOON",
    "ANIMATION", "CHILDREN", "PRESCHOOL", "BABY", "TODDLER", "TODDLERS",
}

# Palabras clave para detectar Dominicanos (gratis)
DOM_GROUP_KEYWORDS = {
    "DOMINICANOS", "DOMINICANA", "DOMINICANO", "RD",
    "REPUBLICA DOMINICANA", "REPÚBLICA DOMINICANA",
}

DOM_NAME_HINTS = ("dominicana", "dominicano", "quisqueya")

# MAPEO DE GÉNEROS: group-title de la M3U → categoría final
GENRE_MAP = {
    "MOVIES":       CAT_PELICULAS,
    "CLASSIC":      CAT_PELICULAS,
    "PELICULAS":    CAT_PELICULAS,
    "CINE":         CAT_PELICULAS,
    "SERIES":       CAT_SERIES,
    "NOVELAS":      CAT_SERIES,
    "NEWS":         CAT_NOTICIAS,
    "NOTICIAS":     CAT_NOTICIAS,
    "SPORTS":       CAT_DEPORTES,
    "DEPORTES":     CAT_DEPORTES,
    "MUSIC":        CAT_MUSICA,
    "MUSICA":       CAT_MUSICA,
    "RELIGIOUS":    CAT_RELIGION,
    "RELIGION":     CAT_RELIGION,
    "DOCUMENTARY":  CAT_DOCUMENTALES,
    "DOCUMENTALES": CAT_DOCUMENTALES,
    "SCIENCE":      CAT_DOCUMENTALES,
    "COMEDY":       CAT_COMEDIA,
    "COMEDIA":      CAT_COMEDIA,
}


def normalizar_categoria(valor: str, tvg_id: str = "", nombre: str = "") -> str:
    v = (valor or "").strip().upper()
    tid = (tvg_id or "").strip().lower()
    nom = (nombre or "").strip().lower()

    partes = [p.strip() for p in v.split(";") if p.strip()]

    # 1) Kids/Anime/Infantil -> GRATIS
    if any(p in KIDS_KEYWORDS for p in partes):
        return CAT_ANIME_INFANTIL

    # 2) Dominicano por tvg-id (.do@... o termina en .do) -> GRATIS
    if ".do@" in tid or tid.endswith(".do"):
        return CAT_DOMINICANOS

    # 3) Dominicano por group-title explícito -> GRATIS
    if any(p in DOM_GROUP_KEYWORDS for p in partes):
        return CAT_DOMINICANOS

    # 4) Dominicano por nombre -> GRATIS
    if any(h in nom for h in DOM_NAME_HINTS):
        return CAT_DOMINICANOS

    # 5) Género por group-title (primer match gana)
    for parte in partes:
        if parte in GENRE_MAP:
            return GENRE_MAP[parte]

    # 6) Fallback
    return CAT_VARIEDADES


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
                "_tvg_id": atributos.get("tvg-id", ""),
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


# ============================================================
# VALIDACIÓN MEJORADA (Opción B: HLS real)
# ============================================================

def _headers_para(canal_headers) -> dict:
    h = {"User-Agent": USER_AGENT}
    if isinstance(canal_headers, dict):
        for k, v in canal_headers.items():
            if isinstance(k, str) and isinstance(v, str):
                h[k] = v
    return h


def _verificar_chunk(url_chunk: str, headers: dict) -> bool:
    """
    Verifica que el primer segmento HLS exista.
    Intenta HEAD; si el servidor no lo soporta, cae a GET con stream.
    """
    try:
        r = requests.head(url_chunk, headers=headers, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code < 400:
            r.close()
            return True
        if r.status_code in (403, 405, 501):
            r.close()
            try:
                r2 = requests.get(url_chunk, headers=headers, timeout=TIMEOUT,
                                  stream=True, allow_redirects=True)
                ok = r2.status_code < 400
                try:
                    if ok:
                        r2.raw.read(1, decode_content=True)
                except Exception:
                    pass
                r2.close()
                return ok
            except Exception:
                return False
        r.close()
        return False
    except Exception:
        try:
            r = requests.get(url_chunk, headers=headers, timeout=TIMEOUT,
                             stream=True, allow_redirects=True)
            ok = r.status_code < 400
            r.close()
            return ok
        except Exception:
            return False


def _procesar_hls(url_base: str, texto: str, headers: dict, profundidad: int) -> bool:
    """
    Procesa contenido HLS:
      - Si es master (#EXT-X-STREAM-INF): sigue el primer variant.
      - Si es segmentos (#EXTINF): verifica el primer chunk.
      - Si no es ninguno: playlist vacía → inválido.
    """
    if profundidad > MAX_PROFUNDIDAD_HLS:
        return True  # demasiado profundo: no rechazar por precaución

    lineas = [l.strip() for l in texto.splitlines() if l.strip()]
    tiene_master = any(l.startswith("#EXT-X-STREAM-INF") for l in lineas)
    tiene_segmentos = any(l.startswith("#EXTINF") for l in lineas)

    if tiene_master:
        for i, l in enumerate(lineas):
            if l.startswith("#EXT-X-STREAM-INF"):
                for j in range(i + 1, len(lineas)):
                    v = lineas[j]
                    if v and not v.startswith("#"):
                        vurl = urljoin(url_base, v)
                        try:
                            r2 = requests.get(vurl, headers=headers, timeout=TIMEOUT,
                                              stream=True, allow_redirects=True)
                            if r2.status_code >= 400:
                                r2.close()
                                return False
                            try:
                                c2 = r2.raw.read(BYTES_LECTURA_INICIAL, decode_content=True)
                            except Exception:
                                c2 = b""
                            r2.close()
                        except Exception:
                            return False
                        return _procesar_hls(
                            vurl,
                            c2.decode("utf-8", errors="ignore"),
                            headers,
                            profundidad + 1,
                        )
                break
        return False

    if tiene_segmentos:
        for i, l in enumerate(lineas):
            if l.startswith("#EXTINF"):
                for j in range(i + 1, len(lineas)):
                    c = lineas[j]
                    if c and not c.startswith("#"):
                        return _verificar_chunk(urljoin(url_base, c), headers)
                break
        return False

    # m3u8 con #EXTM3U pero sin segmentos ni variants → vacía/muerta
    return False


def intento_unico(url: str, headers) -> bool:
    """
    Un intento de validación con Opción B:
      1. GET (stream) para detectar content-type y bytes.
      2. Si parece HLS → validación master→variant→chunk.
      3. Si no es HLS → chequeo ligero (status + bytes no vacíos).
    """
    h = _headers_para(headers)
    try:
        r = requests.get(url, headers=h, timeout=TIMEOUT, stream=True, allow_redirects=True)
        if r.status_code >= 400:
            r.close()
            return False

        try:
            chunk = r.raw.read(BYTES_LECTURA_INICIAL, decode_content=True)
        except Exception:
            chunk = b""

        ct = (r.headers.get("Content-Type") or "").lower()
        url_limpia = url.lower().split("?")[0]
        r.close()

        texto = chunk.decode("utf-8", errors="ignore")
        parece_hls = (
            "#EXTM3U" in texto
            or "mpegurl" in ct
            or url_limpia.endswith(".m3u8")
        )

        if parece_hls:
            return _procesar_hls(url, texto, h, 0)

        # No HLS: aceptar si devolvió algún byte o content-type de video/audio
        if chunk:
            return True
        if any(x in ct for x in ("video/", "audio/", "application/octet-stream", "mp2t")):
            return True
        return False

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


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    log("=" * 60)
    log("AGREGAR CANALES ONLINE (validación HLS mejorada)")
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

    log(f"Validando con {MAX_HILOS} hilos (HLS: master→variant→chunk)...")
    online = []
    t0 = time.time()
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

    log(f"Validación terminada en {time.time() - t0:.1f}s")
    log(f"Canales nuevos online encontrados: {len(online)}")

    if not online:
        log("No se agregó ningún canal nuevo.")
        return

    # Clasificar y limpiar campo interno _tvg_id
    for c in online:
        cat_original = c.get("group-title", "")
        tvg = c.pop("_tvg_id", "")
        nombre = c.get("name", "")
        c["group-title"] = normalizar_categoria(cat_original, tvg, nombre)

    # Reporte por categoría
    resumen = {}
    for c in online:
        cat = c["group-title"]
        resumen[cat] = resumen.get(cat, 0) + 1
    log("Resumen por categoría:")
    for cat, n in sorted(resumen.items(), key=lambda x: -x[1]):
        log(f"  {cat}: {n}")

    json_actual.extend(online)
    guardar_json(JSON_PATH, json_actual)
    log(f"JSON actualizado. Total de canales: {len(json_actual)}")
    log("=" * 60)


if __name__ == "__main__":
    main()
