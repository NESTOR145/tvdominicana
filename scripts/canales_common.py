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
  - Parseo de listas M3U.
  - Clasificación de categorías (DOMINICANOS -> ANIME-INFANTIL -> PREMIUM).
  - Normalización de URLs y de nombres para deduplicación robusta.
"""

import json
import os
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

# ---------- CONFIGURACIÓN COMPARTIDA ----------
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

TIMEOUT_VALIDACION = 15
TIMEOUT_DESCARGA_M3U = 30
MAX_HILOS = 30

CONTENT_TYPES_VALIDOS = (
    "video/",
    "audio/",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/mpegurl",
    "application/octet-stream",
    "binary/octet-stream",
)

FIRMAS_ERROR = (
    b"<!doctype html",
    b"<html",
    b"<?xml",
    b"{\"error",
    b"{\"status",
    b"{\"message",
    b"error",
    b"not found",
    b"no disponible",
    b"unavailable",
)

CAT_DOMINICANOS = "DOMINICANOS"
CAT_ANIME_INFANTIL = "ANIME-INFANTIL"
CAT_PREMIUM = "PREMIUM"

CAT_ORDER = {
    CAT_DOMINICANOS: 0,
    CAT_ANIME_INFANTIL: 1,
    CAT_PREMIUM: 2,
}

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

RE_ATTR = re.compile(r'([\w-]+)="([^"]*)"')

SUFIJOS_IGNORAR = re.compile(
    r"\b(hd|sd|fhd|uhd|4k|8k|h265|hevc|\+1|\+2|\+3|"
    r"latino|lat|español|esp|castellano|"
    r"opcion\s*\d+|opción\s*\d+)\b",
    re.IGNORECASE,
)


# ---------- LOGGING ----------
def log(msg: str) -> None:
    print(msg, flush=True)


# ---------- GUARDADO ATÓMICO ----------
def guardar_json_atomico(path: Path, data) -> None:
    """Escribe JSON de forma atómica: si el proceso muere, el original queda intacto."""
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
    """
    Carga JSON tolerante a fallos:
      - Archivo inexistente -> default
      - Archivo vacío -> default
      - BOM UTF-8 -> se elimina
      - JSON inválido -> lanza excepción
    """
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


# ---------- NORMALIZACIÓN DE URL ----------
def normalizar_url(u: str) -> str:
    """
    Normaliza URL: http==https, sin www, sin puerto por defecto,
    path en minúsculas y sin trailing slash, query ordenada, sin fragmento.
    """
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


# ---------- NORMALIZACIÓN DE NOMBRE ----------
def normalizar_nombre(n: str) -> str:
    """
    Normaliza nombre: minúsculas, sin tildes, sin paréntesis/corchetes,
    sin sufijos HD/SD/FHD/4K/+1/+2..., sin caracteres raros, SIN espacios.
    """
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
    """Tupla (url_norm, nombre_norm, categoria) para deduplicar."""
    return (
        normalizar_url(canal.get("url", "")),
        normalizar_nombre(canal.get("name", "")),
        canal.get("group-title", ""),
    )


# ---------- CLASIFICACIÓN ----------
def normalizar_categoria(valor: str, tvg_id: str = "", nombre: str = "") -> str:
    v = (valor or "").strip().upper()
    tid = (tvg_id or "").strip().lower()
    nom = (nombre or "").strip().lower()

    partes = [p.strip() for p in v.split(";") if p.strip()]

    if any(p in KIDS_KEYWORDS for p in partes):
        return CAT_ANIME_INFANTIL

    if ".do@" in tid or tid.endswith(".do"):
        return CAT_DOMINICANOS

    if any(p in DOM_GROUP_KEYWORDS for p in partes):
        return CAT_DOMINICANOS

    if any(h in nom for h in DOM_NAME_HINTS):
        return CAT_DOMINICANOS

    return CAT_PREMIUM


def clave_orden(canal: dict):
    """
    Ordena por categoría (DOMINICANOS -> ANIME-INFANTIL -> PREMIUM)
    y luego por nombre A→Z.
    """
    cat = normalizar_categoria(
        canal.get("group-title", ""),
        canal.get("_tvg_id", "") or canal.get("tvg_id", ""),
        canal.get("name", ""),
    )
    return (CAT_ORDER.get(cat, 99), (canal.get("name") or "").lower())


# ---------- PARSEO M3U ----------
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


# ---------- VALIDACIÓN REAL DE STREAM ----------
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
