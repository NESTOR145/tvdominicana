#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generar_epg.py
--------------
Guía de Programación (EPG) multi-fuente con fusión inteligente.

Fuentes soportadas (en orden de PRIORIDAD):
1. epgshare01 ES1     (España - muy completa, ~370 canales)
2. Open-EPG Spain     (España - muy completa, ~380 canales)
3. TDTChannels        (Fuente original)

Estrategia de fusión:
- Se descargan todas las fuentes en paralelo.
- Para cada canal, se toma la PRIMERA fuente (según prioridad) que tenga datos.
- Se deduplican programas por (channelId, startTime).
- Se guardan solo programas desde 2h atrás hasta 48h adelante.

Tolerancia a fallos:
- Si una fuente falla, se continúa con las demás.
- Si TODAS fallan, se conserva el epg.json anterior (no se sobrescribe).
- Si no hay datos para un canal, no se rompe nada: simplemente se omite.
"""

import gzip
import io
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from canales_common import (
    cargar_json_seguro,
    generar_channel_id,
    guardar_json_atomico,
    log,
)

# ============================================================
# CONFIGURACIÓN
# ============================================================
# Lista de fuentes EPG en orden de PRIORIDAD (primera = más prioritaria).
# Si quieres añadir más fuentes, simplemente agrega un dict a esta lista.
# Documentación:
#   - epgshare01: https://epgshare01.online/
#   - open-epg:   https://www.open-epg.com/
#   - tdtchannels: https://www.tdtchannels.com/epg/
FUENTES_EPG = [
    {
        "nombre": "epgshare01-ES1",
        "url": "https://epgshare01.online/epgshare01/epg_ripper_ES1.xml.gz",
        "gzip": True,
    },
    {
        "nombre": "open-epg-spain",
        "url": "https://www.open-epg.com/files/spain.xml.gz",
        "gzip": True,
    },
    {
        "nombre": "tdtchannels",
        "url": "https://www.tdtchannels.com/epg/TV.xml",
        "gzip": False,
    },
]

CANALES_JSON_PATH = Path("tvdominicana.json")
EPG_JSON_PATH = Path("epg.json")

# Rango temporal a extraer
HORAS_ATRAS = 2
HORAS_ADELANTE = 48

# Timeouts
TIMEOUT_DESCARGA = 90  # segundos por fuente
MAX_DESCARGA_BYTES = 200 * 1024 * 1024  # 200 MB máximo por fuente

# Hilos para descarga en paralelo
MAX_HILOS_DESCARGA = 3


# ============================================================
# PARSEO DE FECHAS XMLTV
# ============================================================
def parsear_fecha_xmltv(fecha_str: str) -> int | None:
    """Convierte una fecha XMLTV ("20261031140000 +0000") a timestamp Unix (ms)."""
    if not fecha_str:
        return None
    try:
        fecha_limpia = fecha_str.strip().split(" ")[0]
        if len(fecha_limpia) < 14:
            return None

        dt = datetime.strptime(fecha_limpia, "%Y%m%d%H%M%S")

        partes = fecha_str.strip().split(" ")
        if len(partes) > 1 and len(partes[1]) == 5:
            zona_str = partes[1]
            signo = 1 if zona_str[0] == "+" else -1
            horas = int(zona_str[1:3])
            minutos = int(zona_str[3:5])
            offset = timezone(timedelta(hours=signo * horas, minutes=signo * minutos))
            dt = dt.replace(tzinfo=offset)
        else:
            dt = dt.replace(tzinfo=timezone.utc)

        return int(dt.timestamp() * 1000)
    except Exception:
        return None


# ============================================================
# DESCARGA DE FUENTES
# ============================================================
def descargar_fuente(fuente: dict) -> bytes | None:
    """Descarga una fuente EPG con timeout y límite de tamaño."""
    nombre = fuente["nombre"]
    url = fuente["url"]
    log(f"→ Descargando [{nombre}]: {url}")

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; DominiTV-EPG-Bot/2.0)"
        }
        r = requests.get(url, headers=headers, timeout=TIMEOUT_DESCARGA, stream=True)
        r.raise_for_status()

        contenido = io.BytesIO()
        descargado = 0
        for chunk in r.iter_content(chunk_size=64 * 1024):
            descargado += len(chunk)
            if descargado > MAX_DESCARGA_BYTES:
                log(f"  ADVERTENCIA [{nombre}]: excede {MAX_DESCARGA_BYTES // (1024*1024)} MB, se corta.")
                break
            contenido.write(chunk)

        data = contenido.getvalue()
        log(f"  OK [{nombre}]: {len(data):,} bytes")
        return data

    except requests.RequestException as e:
        log(f"  ERROR [{nombre}]: {e}")
        return None
    except Exception as e:
        log(f"  ERROR inesperado [{nombre}]: {e}")
        return None


def descomprimir_si_necesario(data: bytes, es_gzip: bool) -> bytes | None:
    """Descomprime el contenido si es gzip (con auto-detección por firma)."""
    # Auto-detección: si empieza con 0x1f 0x8b, es gzip
    es_gzip_real = data[:2] == b"\x1f\x8b"

    if not es_gzip and not es_gzip_real:
        return data

    try:
        return gzip.decompress(data)
    except Exception as e:
        log(f"  AVISO: no se pudo descomprimir ({e}).")
        if es_gzip_real:
            return None  # Era gzip pero no se pudo descomprimir
        return data  # No era gzip, devolver tal cual


# ============================================================
# PARSEO Y EXTRACCIÓN
# ============================================================
def extraer_programas(
    xml_bytes: bytes,
    mapa_tvg: dict,
    desde_ms: int,
    hasta_ms: int,
    nombre_fuente: str,
) -> dict[str, list[dict]]:
    """Extrae programas relevantes del XMLTV. Devuelve {channelId: [programas]}."""
    log(f"  Parseando XML [{nombre_fuente}] ({len(xml_bytes):,} bytes)...")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        log(f"  ERROR [{nombre_fuente}]: XML mal formado: {e}")
        return {}

    programas_por_canal = {}
    total = 0

    for prog in root.findall("programme"):
        channel_attr = prog.get("channel", "").strip()
        if not channel_attr or channel_attr not in mapa_tvg:
            continue

        start_ms = parsear_fecha_xmltv(prog.get("start", ""))
        stop_ms = parsear_fecha_xmltv(prog.get("stop", ""))
        if start_ms is None or stop_ms is None:
            continue

        if stop_ms < desde_ms or start_ms > hasta_ms:
            continue

        titulo_elem = prog.find("title")
        titulo = titulo_elem.text.strip() if titulo_elem is not None and titulo_elem.text else "Sin título"

        desc_elem = prog.find("desc")
        descripcion = desc_elem.text.strip() if desc_elem is not None and desc_elem.text else None

        icon_elem = prog.find("icon")
        icon_url = icon_elem.get("src") if icon_elem is not None else None

        _, _, channel_id = mapa_tvg[channel_attr]

        programa = {
            "channelId": channel_id,
            "title": titulo,
            "description": descripcion,
            "startTime": start_ms,
            "endTime": stop_ms,
            "iconUrl": icon_url,
        }

        programas_por_canal.setdefault(channel_id, []).append(programa)
        total += 1

    log(f"  [{nombre_fuente}]: {total} programas en {len(programas_por_canal)} canales.")
    return programas_por_canal


# ============================================================
# FUSIÓN DE FUENTES
# ============================================================
def fusionar_fuentes(fuentes_programas: list[dict[str, list[dict]]]) -> list[dict]:
    """
    Fusiona fuentes respetando prioridad (primera > segunda > tercera).
    Deduplica por (channelId, startTime).
    """
    canales_finales: dict[str, dict[int, dict]] = {}

    for programas_fuente in fuentes_programas:
        for channel_id, programas in programas_fuente.items():
            # Primera fuente que tenga este canal gana
            if channel_id in canales_finales and len(canales_finales[channel_id]) > 0:
                continue

            canales_finales[channel_id] = {}
            for prog in programas:
                start = prog["startTime"]
                if start not in canales_finales[channel_id]:
                    canales_finales[channel_id][start] = prog

    # Aplanar y ordenar
    resultado = []
    for programas in canales_finales.values():
        resultado.extend(programas.values())

    resultado.sort(key=lambda p: (p["channelId"], p["startTime"]))
    return resultado


# ============================================================
# MAIN
# ============================================================
def main() -> None:
    log("=" * 60)
    log("GENERAR EPG MULTI-FUENTE (ROBUSTO)")
    log("=" * 60)

    # 1. Cargar canales
    canales = cargar_json_seguro(CANALES_JSON_PATH, default=[])
    if not isinstance(canales, list) or not canales:
        log("ERROR: tvdominicana.json está vacío o no es una lista.")
        sys.exit(1)

    log(f"Canales cargados: {len(canales)}")

    # 2. Mapa tvg-id → (url, name, channelId)
    mapa_tvg = {}
    sin_tvg = 0
    for canal in canales:
        tvg_id = (canal.get("tvg-id") or "").strip()
        if not tvg_id:
            sin_tvg += 1
            continue
        url = canal.get("url", "")
        name = canal.get("name", "")
        channel_id = generar_channel_id(url, name)
        mapa_tvg[tvg_id] = (url, name, channel_id)

    log(f"Canales con tvg-id: {len(mapa_tvg)} | Sin tvg-id: {sin_tvg}")

    if not mapa_tvg:
        log("ADVERTENCIA: Ningún canal tiene tvg-id. No se puede generar EPG.")
        guardar_json_atomico(EPG_JSON_PATH, [])
        return

    # 3. Rango temporal
    ahora = datetime.now(timezone.utc)
    desde_ms = int((ahora - timedelta(hours=HORAS_ATRAS)).timestamp() * 1000)
    hasta_ms = int((ahora + timedelta(hours=HORAS_ADELANTE)).timestamp() * 1000)
    log(f"Rango: {datetime.fromtimestamp(desde_ms/1000, tz=timezone.utc)} → {datetime.fromtimestamp(hasta_ms/1000, tz=timezone.utc)}")

    # 4. Descargar todas las fuentes en paralelo
    log(f"Descargando {len(FUENTES_EPG)} fuentes en paralelo...")
    descargas: dict[int, bytes | None] = {}

    with ThreadPoolExecutor(max_workers=MAX_HILOS_DESCARGA) as executor:
        futuros = {
            executor.submit(descargar_fuente, fuente): idx
            for idx, fuente in enumerate(FUENTES_EPG)
        }
        for futuro in as_completed(futuros):
            idx = futuros[futuro]
            try:
                descargas[idx] = futuro.result()
            except Exception as e:
                log(f"  ERROR inesperado en fuente {idx}: {e}")
                descargas[idx] = None

    # 5. Extraer programas respetando prioridad
    fuentes_programas: list[dict[str, list[dict]]] = []
    fuentes_exitosas = 0

    for idx, fuente in enumerate(FUENTES_EPG):
        data = descargas.get(idx)
        if data is None:
            log(f"[{fuente['nombre']}] DESCARTADA (no se descargó).")
            fuentes_programas.append({})
            continue

        xml_bytes = descomprimir_si_necesario(data, fuente.get("gzip", False))
        del data  # liberar memoria

        if xml_bytes is None:
            fuentes_programas.append({})
            continue

        try:
            programas = extraer_programas(xml_bytes, mapa_tvg, desde_ms, hasta_ms, fuente["nombre"])
            del xml_bytes  # liberar memoria
            fuentes_programas.append(programas)
            if programas:
                fuentes_exitosas += 1
        except Exception as e:
            log(f"[{fuente['nombre']}] ERROR en extracción: {e}")
            fuentes_programas.append({})

    log(f"Fuentes exitosas: {fuentes_exitosas}/{len(FUENTES_EPG)}")

    # 6. Fusión
    log("Fusionando fuentes (prioridad: 1ª > 2ª > 3ª)...")
    lista_final = fusionar_fuentes(fuentes_programas)

    if not lista_final:
        log("ADVERTENCIA: No se encontraron programas en ninguna fuente.")
        log("Se conserva el epg.json anterior (si existe).")
        log("=" * 60)
        return

    # 7. Guardar
    guardar_json_atomico(EPG_JSON_PATH, lista_final)

    canales_con_epg = len({p["channelId"] for p in lista_final})
    log(f"EPG guardada: {len(lista_final)} programas, {canales_con_epg} canales con guía.")
    log(f"Canales sin EPG: {len(mapa_tvg) - canales_con_epg}")
    log("=" * 60)


if __name__ == "__main__":
    main()
