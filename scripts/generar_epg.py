#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generar_epg.py
--------------
Descarga el XMLTV de TDTChannels, cruza los datos con tvdominicana.json
usando el campo `tvg-id` y genera `epg.json` con el formato que espera
la app Android (ProgramDTO).

Lógica de cruce:
1.  Para cada canal en tvdominicana.json, se obtiene su `tvg-id`.
2.  Se busca en el XMLTV un <programme> cuyo atributo `channel` coincida
    con ese `tvg-id`.
3.  Si hay coincidencia, se extraen los programas y se calcula el `channelId`
    (SHA-1 de "url|name") para que la app los asocie al canal correcto.

Solo se guardan programas desde 2 horas atrás hasta 48 horas adelante
para mantener el archivo ligero.
"""

import sys
import xml.etree.ElementTree as ET
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
# Fuente oficial de la EPG de TDTChannels
EPG_XML_URL = "https://www.tdtchannels.com/epg/TV.xml"

CANALES_JSON_PATH = Path("tvdominicana.json")
EPG_JSON_PATH = Path("epg.json")

# Rango de tiempo a extraer (horas)
HORAS_ATRAS = 2
HORAS_ADELANTE = 48

# Timeout para la descarga (segundos)
TIMEOUT_DESCARGA = 60


# ============================================================
# PARSEO DE FECHAS XMLTV
# ============================================================
def parsear_fecha_xmltv(fecha_str: str) -> int | None:
    """
    Convierte una fecha XMLTV (formato "20261031140000 +0000")
    a timestamp Unix en milisegundos.

    Retorna None si el formato no es válido.
    """
    if not fecha_str:
        return None
    try:
        # El formato es: YYYYMMDDHHMMSS +HHMM
        # Ejemplo: 20261031140000 +0000
        fecha_limpia = fecha_str.strip().split(" ")[0]
        if len(fecha_limpia) < 14:
            return None

        dt = datetime.strptime(fecha_limpia, "%Y%m%d%H%M%S")

        # Extraer zona horaria si existe
        partes = fecha_str.strip().split(" ")
        if len(partes) > 1 and len(partes[1]) == 5:
            zona_str = partes[1]  # Ej: +0000
            signo = 1 if zona_str[0] == "+" else -1
            horas = int(zona_str[1:3])
            minutos = int(zona_str[3:5])
            offset = timezone(timedelta(hours=signo * horas, minutes=signo * minutos))
            dt = dt.replace(tzinfo=offset)
        else:
            # Si no hay zona, asumir UTC
            dt = dt.replace(tzinfo=timezone.utc)

        return int(dt.timestamp() * 1000)
    except Exception:
        return None


# ============================================================
# DESCARGA DEL XMLTV
# ============================================================
def descargar_xmltv(url: str) -> bytes | None:
    """Descarga el XMLTV desde la URL indicada."""
    log(f"→ Descargando XMLTV desde: {url}")
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; DominiTV-EPG-Bot/1.0)"
        }
        r = requests.get(url, headers=headers, timeout=TIMEOUT_DESCARGA)
        r.raise_for_status()
        log(f"  OK: {len(r.content):,} bytes recibidos.")
        return r.content
    except requests.RequestException as e:
        log(f"  ERROR al descargar XMLTV: {e}")
        return None


# ============================================================
# GENERACIÓN DE LA EPG
# ============================================================
def main() -> None:
    log("=" * 60)
    log("GENERAR GUÍA DE PROGRAMACIÓN (EPG) DESDE TDTCHANNELS")
    log("=" * 60)

    # 1. Cargar el JSON de canales
    canales = cargar_json_seguro(CANALES_JSON_PATH, default=[])
    if not isinstance(canales, list) or not canales:
        log("ERROR: tvdominicana.json está vacío o no es una lista.")
        sys.exit(1)

    log(f"Canales cargados: {len(canales)}")

    # 2. Construir un mapa de tvg-id -> (url, name, channelId)
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
        # Crear un JSON vacío para que la app no falle
        guardar_json_atomico(EPG_JSON_PATH, [])
        return

    # 3. Descargar el XMLTV
    xml_bytes = descargar_xmltv(EPG_XML_URL)
    if xml_bytes is None:
        log("ERROR: No se pudo descargar el XMLTV. Se conserva el epg.json anterior.")
        # No sobrescribir si falla la descarga
        return

    # 4. Parsear el XMLTV
    log("Parseando XMLTV...")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        log(f"ERROR: XMLTV mal formado: {e}")
        return

    # 5. Calcular rango de tiempo
    ahora = datetime.now(timezone.utc)
    desde_ms = int((ahora - timedelta(hours=HORAS_ATRAS)).timestamp() * 1000)
    hasta_ms = int((ahora + timedelta(hours=HORAS_ADELANTE)).timestamp() * 1000)

    # 6. Iterar sobre los <programme> del XMLTV
    log(f"Buscando programas entre {datetime.fromtimestamp(desde_ms / 1000, tz=timezone.utc)} y {datetime.fromtimestamp(hasta_ms / 1000, tz=timezone.utc)}...")

    programas_por_canal = {}
    total_programas = 0
    matches = 0

    for prog in root.findall("programme"):
        channel_attr = prog.get("channel", "").strip()
        if not channel_attr:
            continue

        # Verificar si el tvg-id está en nuestro mapa
        if channel_attr not in mapa_tvg:
            continue

        start_str = prog.get("start", "")
        stop_str = prog.get("stop", "")

        start_ms = parsear_fecha_xmltv(start_str)
        stop_ms = parsear_fecha_xmltv(stop_str)

        if start_ms is None or stop_ms is None:
            continue

        # Filtrar por rango de tiempo
        if stop_ms < desde_ms or start_ms > hasta_ms:
            continue

        # Extraer datos del programa
        titulo_elem = prog.find("title")
        titulo = titulo_elem.text.strip() if titulo_elem is not None and titulo_elem.text else "Sin título"

        desc_elem = prog.find("desc")
        descripcion = desc_elem.text.strip() if desc_elem is not None and desc_elem.text else None

        icon_elem = prog.find("icon")
        icon_url = icon_elem.get("src") if icon_elem is not None else None

        url, name, channel_id = mapa_tvg[channel_attr]

        programa = {
            "channelId": channel_id,
            "title": titulo,
            "description": descripcion,
            "startTime": start_ms,
            "endTime": stop_ms,
            "iconUrl": icon_url,
        }

        programas_por_canal.setdefault(channel_id, []).append(programa)
        total_programas += 1
        matches += 1

    log(f"Programas encontrados en el rango: {total_programas}")
    log(f"Canales con EPG: {len(programas_por_canal)}")

    # 7. Aplanar la lista de programas
    lista_final = []
    for programas in programas_por_canal.values():
        lista_final.extend(programas)

    # 8. Ordenar por startTime para que la app los muestre en orden
    lista_final.sort(key=lambda p: p["startTime"])

    # 9. Guardar el JSON
    if lista_final:
        guardar_json_atomico(EPG_JSON_PATH, lista_final)
        log(f"EPG guardada: {len(lista_final)} programas en {EPG_JSON_PATH}")
    else:
        log("ADVERTENCIA: No se encontraron programas. Se conserva el epg.json anterior.")
        # No sobrescribir si no hay datos, para no dejar a la app sin guía

    log("=" * 60)


if __name__ == "__main__":
    main()
