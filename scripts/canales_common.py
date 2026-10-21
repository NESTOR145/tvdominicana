
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Utilidades compartidas para agregar_canales.py y limpiar_canales.py."""

import hashlib
import json
import os
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
TIMEOUT_VALIDACION = 15
TIMEOUT_DESCARGA_M3U = 30
MAX_HILOS = 20
INTENTOS_VALIDACION = 2
PAUSA_VALIDACION = 2.0

CONTENT_TYPES_VALIDOS = (
    "video/", "audio/", "application/vnd.apple.mpegurl",
    "application/x-mpegurl", "application/mpegurl",
    "application/octet-stream", "binary/octet-stream",
)
FIRMAS_ERROR = (
    b"<!doctype html", b"<html", b'{"error', b'{"status',
    b'{"message', b"access denied", b"not found",
    b"no disponible", b"unavailable",
)
CAT_DOMINICANOS = "DOMINICANOS"
CAT_ANIME_INFANTIL = "ANIME-INFANTIL"
CAT_PREMIUM = "PREMIUM"
CAT_ORDER = {CAT_DOMINICANOS: 0, CAT_ANIME_INFANTIL: 1, CAT_PREMIUM: 2}
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
    r"latino|lat|español|esp|castellano|opcion\s*\d+|opci[oó]n\s*\d+)\b",
    re.IGNORECASE,
)


def log(msg: str) -> None:
    print(msg, flush=True)


def guardar_json_atomico(path: Path, data) -> None:
    """Guarda en temporal dentro del mismo directorio y reemplaza atómicamente."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def cargar_json_seguro(path: Path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    texto = path.read_text(encoding="utf-8-sig").strip()
    if not texto:
        raise ValueError(f"{path} está vacío; se aborta para proteger los datos.")
    try:
        return json.loads(texto)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} no contiene JSON válido: {exc}") from exc


def normalizar_url(url: str) -> str:
    """Normaliza host/esquema/query sin alterar mayúsculas del path ni valores de query."""
    if not isinstance(url, str) or not url.strip():
        return ""
    raw = url.strip()
    try:
        s = urlsplit(raw)
        if s.scheme.lower() not in ("http", "https") or not s.hostname:
            return raw
        scheme = s.scheme.lower()
        host = s.hostname.lower()
        if host.startswith("www."):
            host = host[4:]
        try:
            port = s.port
        except ValueError:
            return raw
        if port and not (
            (scheme == "http" and port == 80)
            or (scheme == "https" and port == 443)
        ):
            host = f"{host}:{port}"
        # No se equiparan http y https: algunos proveedores los sirven de forma distinta.
        path = s.path or "/"
        if path != "/":
            path = path.rstrip("/") or "/"
        query = urlencode(sorted(parse_qsl(s.query, keep_blank_values=True)), doseq=True)
        return urlunsplit((scheme, host, path, query, ""))
    except Exception:
        return raw


def normalizar_nombre(nombre: str) -> str:
    if not isinstance(nombre, str) or not nombre.strip():
        return ""
    n = unicodedata.normalize("NFKD", nombre)
    n = "".join(c for c in n if not unicodedata.combining(c)).lower()
    n = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", n)
    n = SUFIJOS_IGNORAR.sub(" ", n)
    n = re.sub(r"[^a-z0-9\s]", " ", n)
    return " ".join(n.split())


def clave_dedupe(canal: dict):
    """Clave de identidad: nombre normalizado + URL normalizada."""
    return (
        normalizar_nombre(canal.get("name", "")),
        normalizar_url(canal.get("url", "")),
    )


def clave_nombre_categoria(canal: dict):
    nombre = normalizar_nombre(canal.get("name", ""))
    categoria = normalizar_categoria(
        canal.get("group-title", ""),
        canal.get("_tvg_id", "") or canal.get("tvg_id", ""),
        canal.get("name", ""),
    )
    return (nombre, categoria) if nombre else None


def id_canal(canal: dict) -> str:
    """Identificador estable para el estado de fallos."""
    url = normalizar_url(canal.get("url", ""))
    if url:
        base = "url:" + url
    else:
        base = (
            "sinurl:"
            + normalizar_nombre(canal.get("name", ""))
            + "|"
            + str(canal.get("logo", ""))
        )
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


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
    cat = normalizar_categoria(
        canal.get("group-title", ""),
        canal.get("_tvg_id", "") or canal.get("tvg_id", ""),
        canal.get("name", ""),
    )
    return (
        CAT_ORDER.get(cat, 99),
        normalizar_nombre(canal.get("name", "")),
        str(canal.get("name", "")).lower(),
    )


def parsear_m3u(contenido: str):
    canales = []
    canal_actual = None

    for linea in contenido.lstrip("\ufeff").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#EXTM3U"):
            continue

        if linea.startswith("#EXTINF:"):
            info = linea[len("#EXTINF:"):]
            atributos = dict(RE_ATTR.findall(info))
            sin_attrs = RE_ATTR.sub("", info)
            nombre = sin_attrs.split(",", 1)[1].strip() if "," in sin_attrs else ""
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
                    k, v = (x.strip() for x in opt.split("=", 1))
                    if k == "http-user-agent":
                        canal_actual.setdefault("headers", {})["User-Agent"] = v
                    elif k == "http-referrer":
                        canal_actual.setdefault("headers", {})["Referer"] = v

        elif linea.startswith("#"):
            continue

        elif canal_actual is not None:
            canal_actual["url"] = linea
            if (
                linea.startswith(("http://", "https://"))
                and (canal_actual.get("name") or canal_actual.get("logo"))
            ):
                canales.append(canal_actual)
            canal_actual = None

    return canales


def _headers_para(headers) -> dict:
    result = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Connection": "close",
    }
    if isinstance(headers, dict):
        for k, v in headers.items():
            if (
                isinstance(k, str)
                and isinstance(v, str)
                and k.strip()
                and "\n" not in k + v
            ):
                result[k] = v
    return result


def _primeros_bytes_validos(chunk: bytes, content_type: str) -> bool:
    if not chunk:
        return any(ct in content_type for ct in CONTENT_TYPES_VALIDOS)

    sample = chunk[:2048].lower().lstrip()

    if any(sample.startswith(sig) for sig in FIRMAS_ERROR):
        return False

    if sample.startswith(b"#extm3u") or chunk[:1] == b"\x47":
        return True

    if any(ct in content_type for ct in CONTENT_TYPES_VALIDOS):
        if b"<html" in sample or b"<!doctype" in sample or b"<body" in sample:
            return False
        return True

    # Algunos proveedores no envían Content-Type, pero TS/MP4 tienen firmas típicas.
    if len(chunk) >= 8 and (
        chunk[4:8] == b"ftyp" or chunk[:4] == b"\x00\x00\x00\x01"
    ):
        return True

    return False


def validar_stream(url: str, headers=None) -> str:
    """Retorna ok, temporal o fallo. Los errores de red se consideran temporales."""
    if not isinstance(url, str) or not url.strip():
        return "fallo"
    if not url.lower().startswith(("http://", "https://")):
        return "fallo"

    response = None
    try:
        response = requests.get(
            url.strip(),
            headers=_headers_para(headers),
            timeout=(8, TIMEOUT_VALIDACION),
            stream=True,
            allow_redirects=True,
        )
        code = response.status_code

        if code in (401, 403, 408, 425, 429):
            return "temporal"
        if 500 <= code <= 599:
            return "temporal"
        if code >= 400 or not 200 <= code < 300:
            return "fallo"

        ctype = (response.headers.get("Content-Type") or "").lower()

        try:
            chunk = next(response.iter_content(chunk_size=2048), b"")
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.ChunkedEncodingError,
        ):
            return "temporal"
        except requests.RequestException:
            return "temporal"

        return "ok" if _primeros_bytes_validos(chunk, ctype) else "fallo"

    except (
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.RetryError,
    ):
        return "temporal"
    except requests.RequestException:
        return "temporal"
    except Exception:
        return "temporal"
    finally:
        if response is not None:
            response.close()


def probar_enlace(
    url: str,
    headers=None,
    intentos: int = INTENTOS_VALIDACION,
    pausa: float = PAUSA_VALIDACION,
) -> str:
    temporal_visto = False

    for i in range(max(1, intentos)):
        resultado = validar_stream(url, headers)

        if resultado == "ok":
            return "ok"
        if resultado == "temporal":
            temporal_visto = True

        if i + 1 < max(1, intentos):
            time.sleep(max(0, pausa))

    # Ante una mezcla de fallo y error temporal, no contamos un fallo definitivo.
    if temporal_visto:
        return "temporal"
    return "fallo"
