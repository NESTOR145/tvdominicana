#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
canales_common.py
-----------------
Utilidades compartidas por agregar_canales.py y limpiar_canales.py.

Incluye:
  - Validación REAL de streams (Content-Type + primeros bytes + detección de HTML/JSON de error).
  - Guardado atómico de JSON (nunca corrompe).
  - Carga de JSON tolerante a archivo vacío, BOM y archivo inexistente.
  - Logging a stdout con flush.
  - Parseo de listas M3U (incluye headers inline del #EXTINF).
  - Clasificación en 10 categorías canónicas.
  - Prioridad de los 33 dominicanos.
"""

import json
import os
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

# ---------- CONFIGURACIÓN ----------
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

TIMEOUT_VALIDACION = 15
TIMEOUT_DESCARGA_M3U = 30
MAX_HILOS = 30

CONTENT_TYPES_VALIDOS = (
    "video/", "audio/",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/mpegurl",
    "application/octet-stream",
    "binary/octet-stream",
)

FIRMAS_ERROR = (
    b"<!doctype html", b"<html", b"<?xml",
    b"{\"error", b"{\"status", b"{\"message",
    b"error", b"not found", b"no disponible", b"unavailable",
)

# ---------- CATEGORÍAS (10) ----------
CAT_DOMINICANOS = "DOMINICANOS"
CAT_PELICULAS   = "PELICULAS Y SERIES"
CAT_ANIME       = "ANIME-INFANTIL"
CAT_NOTICIAS    = "NOTICIAS"
CAT_NOVELAS     = "NOVELAS"
CAT_DEPORTES    = "DEPORTES"
CAT_MUSICA      = "MUSICA"
CAT_RELIGION    = "RELIGION Y CULTURA"
CAT_VARIEDADES  = "VARIEDADES"
CAT_GENERAL     = "GENERAL"

CAT_ORDER = {
    CAT_DOMINICANOS: 0, CAT_PELICULAS: 1, CAT_ANIME: 2, CAT_NOTICIAS: 3,
    CAT_NOVELAS: 4, CAT_DEPORTES: 5, CAT_MUSICA: 6, CAT_RELIGION: 7,
    CAT_VARIEDADES: 8, CAT_GENERAL: 9,
}

GROUP_TO_CATEGORY = {
    "movies": CAT_PELICULAS, "series": CAT_PELICULAS, "classic": CAT_PELICULAS,
    "peliculas": CAT_PELICULAS, "película": CAT_PELICULAS, "cine": CAT_PELICULAS,
    "animation": CAT_ANIME, "kids": CAT_ANIME, "family": CAT_ANIME,
    "infantil": CAT_ANIME, "ninos": CAT_ANIME, "dibujos": CAT_ANIME,
    "news": CAT_NOTICIAS, "noticias": CAT_NOTICIAS,
    "sports": CAT_DEPORTES, "deportes": CAT_DEPORTES,
    "music": CAT_MUSICA, "musica": CAT_MUSICA, "música": CAT_MUSICA,
    "religious": CAT_RELIGION, "religion": CAT_RELIGION,
    "culture": CAT_RELIGION, "cultura": CAT_RELIGION,
    "entertainment": CAT_VARIEDADES, "entretenimiento": CAT_VARIEDADES,
    "comedy": CAT_VARIEDADES, "lifestyle": CAT_VARIEDADES,
    "cooking": CAT_VARIEDADES, "travel": CAT_VARIEDADES,
    "auto": CAT_VARIEDADES, "business": CAT_VARIEDADES,
    "education": CAT_VARIEDADES, "science": CAT_VARIEDADES,
    "documentary": CAT_VARIEDADES, "relax": CAT_VARIEDADES,
    "general": CAT_GENERAL, "public": CAT_GENERAL,
    "legislative": CAT_GENERAL, "weather": CAT_GENERAL,
    "shop": CAT_GENERAL, "undefined": CAT_GENERAL,
}

NAME_FALLBACKS = [
    ("novela", CAT_NOVELAS), ("telenovela", CAT_NOVELAS),
    ("pelicula", CAT_PELICULAS), ("película", CAT_PELICULAS),
    ("movie", CAT_PELICULAS), ("cinema", CAT_PELICULAS),
    ("anime", CAT_ANIME), ("infantil", CAT_ANIME),
    ("kids", CAT_ANIME), ("cartoon", CAT_ANIME),
    ("noticia", CAT_NOTICIAS), ("news", CAT_NOTICIAS),
    ("deporte", CAT_DEPORTES), ("sport", CAT_DEPORTES), ("futbol", CAT_DEPORTES),
    ("musica", CAT_MUSICA), ("música", CAT_MUSICA), ("music", CAT_MUSICA),
    ("religi", CAT_RELIGION), ("cristo", CAT_RELIGION),
    ("iglesia", CAT_RELIGION), ("cultura", CAT_RELIGION),
]

DOM_GROUP_KEYWORDS = {
    "DOMINICANOS", "DOMINICANA", "DOMINICANO", "RD",
    "REPUBLICA DOMINICANA", "REPÚBLICA DOMINICANA",
}
DOM_NAME_HINTS = ("dominicana", "dominicano", "quisqueya")

RE_ATTR = re.compile(r'([\w-]+)="([^"]*)"')

SUFIJOS_IGNORAR = re.compile(
    r"\b(hd|sd|fhd|uhd|4k|8k|h265|hevc|\+1|\+2|\+3|"
    r"latino|lat|español|esp|castellano|"
    r"opcion\s*\d+|opción\s*\d+)\b",
    re.IGNORECASE,
)

PRIORITY_DOMINICANOS = [
    "AME TV", "RTVD-R.D", "TELEMICRO", "DIGITAL 15", "TELECENTRO",
    "ANTENA 7", "ANTENA 21", "TELESISTEMA", "TELE ANTILLAS", "COLOR VISION",
    "CINEVISION", "ALCARRIZOS TV", "CONTANZA TV", "TELEALTURA", "TV MONTAÑA",
    "AGROTENNDENCIAS TV", "TS TELESIGLO", "SANTIAGO TV", "CDN", "NOTICIAS 16",
    "CDN DEPORTES", "CORAL TV", "NARANJA TV", "RNN CANAL 37", "VISION TV",
    "PERAVIA VISION", "BANI VISION", "TLSD", "READY HD", "HERMANAS MIRABAL TV",
    "DIGITAL 809", "CHINOLA TV", "BELLAVISION",
]

_PRIORITY_KEYS = [(i, None) for i, _ in enumerate(PRIORITY_DOMINICANOS)]  # placeholder


def log(msg: str) -> None:
    print(msg, flush=True)


def guardar_json_atomico(path: Path, data) -> None:
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def cargar_json_seguro(path: Path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        texto = path.read_text(encoding="utf-8-sig").strip()
        if not texto:
            log(f"AVISO: {path} está vacío. Se usa default ({default!r}).")
            return default
        return json.loads(texto)
    except json.JSONDecodeError as e:
        log(f"ERROR: {path} no es JSON válido: {e}")
        raise


def normalizar_url(u: str) -> str:
    if not u:
        return ""
    try:
        s = urlsplit(u.strip())
        scheme = s.scheme.lower()
        if scheme in ("http", "https"):
            scheme = "https"
        host = s.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if host.endswith(":80") or host.endswith(":443"):
            host = host.rsplit(":", 1)[0]
        path = s.path.lower().rstrip("/") or "/"
        query = s.query
        if query:
            query = "&".join(sorted(query.split("&")))
        return urlunsplit((scheme, host, path, query, ""))
    except Exception:
        return u.strip()


def normalizar_nombre(n: str) -> str:
    if not n:
        return ""
    n = unicodedata.normalize("NFKD", n)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower()
    n = re.sub(r"\([^)]*\)", " ", n)
    n = re.sub(r"\[[^\]]*\]", " ", n)
    n = SUFIJOS_IGNORAR.sub(" ", n)
    n = re.sub(r"[^a-z0-9\s]", " ", n)
    return "".join(n.split())


def clave_dedupe(canal: dict):
    return (
        normalizar_url(canal.get("url", "")),
        normalizar_nombre(canal.get("name", "")),
        canal.get("group-title", ""),
    )


def normalizar_categoria(valor: str, tvg_id: str = "", nombre: str = "") -> str:
    v = (valor or "").strip().upper()
    tid = (tvg_id or "").strip().lower()
    nom = (nombre or "").strip().lower()

    # 1) DOMINICANOS (se comprueba primero)
    if ".do@" in tid or tid.endswith(".do"):
        return CAT_DOMINICANOS
    partes = [p.strip() for p in v.split(";") if p.strip()]
    if any(p in DOM_GROUP_KEYWORDS for p in partes):
        return CAT_DOMINICANOS
    if any(h in nom for h in DOM_NAME_HINTS):
        return CAT_DOMINICANOS

    # 2) group-title multi-valor
    for p in partes:
        p_norm = unicodedata.normalize("NFKD", p).encode("ascii", "ignore").decode().lower().strip()
        cat = GROUP_TO_CATEGORY.get(p_norm)
        if cat:
            return cat

    # 3) Fallback por nombre
    for key, cat in NAME_FALLBACKS:
        if key in nom:
            return cat

    # 4) Catch-all
    return CAT_GENERAL


def _build_priority_keys():
    global _PRIORITY_KEYS
    _PRIORITY_KEYS = [(i, normalizar_nombre(p)) for i, p in enumerate(PRIORITY_DOMINICANOS)]


_build_priority_keys()


def priority_index(canal: dict) -> int:
    tid = (canal.get("tvg-id") or canal.get("_tvg_id") or canal.get("tvg_id") or "").lower()
    id_prefix = normalizar_nombre(tid.split(".")[0])
    nombre = normalizar_nombre(canal.get("name", ""))

    for idx, key in _PRIORITY_KEYS:
        if id_prefix and id_prefix == key:
            return idx
    for idx, key in _PRIORITY_KEYS:
        if nombre and nombre == key:
            return idx

    best_idx, best_len = -1, 0
    for source in (id_prefix, nombre):
        if not source:
            continue
        for idx, key in _PRIORITY_KEYS:
            if len(key) >= 3 and key in source and len(key) > best_len:
                best_idx, best_len = idx, len(key)
    return best_idx if best_len >= 3 else -1


def clave_orden(canal: dict):
    tvg = (canal.get("tvg-id") or canal.get("_tvg_id") or canal.get("tvg_id") or "")
    cat = normalizar_categoria(canal.get("group-title", ""), tvg, canal.get("name", ""))
    prio = priority_index(canal) if cat == CAT_DOMINICANOS else -1
    prio_key = prio if prio >= 0 else 999
    return (CAT_ORDER.get(cat, 99), prio_key, (canal.get("name") or "").lower())


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
            if not nombre:
                nombre = atributos.get("tvg-name", "").strip()

            canal_actual = {
                "name": nombre,
                "logo": atributos.get("tvg-logo", ""),
                "url": "",
                "_tvg_id": atributos.get("tvg-id", ""),
            }
            group = atributos.get("group-title", "")
            if group:
                canal_actual["group-title"] = group

            # Headers inline del EXTINF
            ref_inline = atributos.get("http-referrer", "").strip()
            ua_inline  = atributos.get("http-user-agent", "").strip()
            if ref_inline or ua_inline:
                canal_actual.setdefault("headers", {})
                if ref_inline:
                    canal_actual["headers"]["Referer"] = ref_inline
                if ua_inline:
                    canal_actual["headers"]["User-Agent"] = ua_inline

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


def _headers_para(url: str, headers) -> dict:
    h = {"User-Agent": USER_AGENT}
    if isinstance(headers, dict):
        for k, v in headers.items():
            if isinstance(k, str) and isinstance(v, str):
                h[k] = v
    return h


def _primeros_bytes_validos(chunk: bytes, content_type: str) -> bool:
    if not chunk:
        return any(ct in content_type for ct in CONTENT_TYPES_VALIDOS)
    chunk_lower = chunk[:2048].lower().lstrip()
    for firma in FIRMAS_ERROR:
        if chunk_lower.startswith(firma):
            return False
    if chunk_lower.startswith(b"#extm3u"):
        return True
    if chunk[:1] == b"\x47":
        return True
    if any(ct in content_type for ct in CONTENT_TYPES_VALIDOS):
        if b"<html" in chunk_lower or b"<!doctype" in chunk_lower:
            return False
        return True
    return False


def validar_stream(url: str, headers=None) -> str:
    h = _headers_para(url, headers)
    try:
        r = requests.get(url, headers=h, timeout=TIMEOUT_VALIDACION, stream=True)
    except (requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError):
        return "temporal"
    except Exception:
        return "fallo"

    try:
        code = r.status_code
        content_type = (r.headers.get("Content-Type") or "").lower()

        if code in (403, 429):
            return "temporal"
        if 500 <= code < 600:
            return "temporal"
        if code >= 400:
            return "fallo"
        if code < 200 or code >= 300:
            return "fallo"

        try:
            chunk = next(r.iter_content(chunk_size=2048), b"")
        except (requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError):
            return "temporal"
        except Exception:
            return "fallo"

        if _primeros_bytes_validos(chunk, content_type):
            return "ok"
        return "fallo"
    finally:
        try:
            r.close()
        except Exception:
            pass


def probar_enlace(url: str, headers=None, intentos: int = 3, pausa: float = 3.0) -> str:
    temporal_visto = False
    for i in range(intentos):
        res = validar_stream(url, headers)
        if res == "ok":
            return "ok"
        if res == "temporal":
            temporal_visto = True
        if i < intentos - 1:
            time.sleep(pausa)
    return "temporal" if temporal_visto else "fallo"
