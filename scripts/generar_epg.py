#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generar_epg.py
--------------
Guía de Programación (EPG) multi-fuente con fusión inteligente.

Fuentes soportadas (en orden de PRIORIDAD):
1. iptv-epg DO        (República Dominicana)
2. epgshare01 DO1     (República Dominicana)
3. iptv-epg MX        (México - para HBO, Star, Disney, etc.)
4. iptv-epg CO        (Colombia - para Warner, Win Sports, etc.)
5. iptv-epg AR        (Argentina - para TyC, Fox, etc.)
6. iptv-epg CL        (Chile - para TVN3, etc.)
7. iptv-epg ES        (España - para Clan, Eurosport, etc.)
8. TDTChannels EPG    (España - fuente oficial de TDTChannels)
9. epgshare01 US2     (Estados Unidos / Panregional - para canales US)

Estrategia de fusión:
- Se descargan todas las fuentes en paralelo.
- Para cada canal, se toma la PRIMERA fuente (según prioridad) que tenga datos.
- Se deduplican programas por (channelId, startTime).
- Se guardan solo programas desde 2h atrás hasta 48h adelante.

Matching de canales:
- Los `tvg-id` del JSON (tvdominicana.json) suelen tener formatos como
  "telemicro.do@SD", "rtvd", "colorvision9.do", mientras que las fuentes
  usan formatos como "Telemicro.dr" o "Canal.5.(Telemicro).do".
- Este script aplica un mapeo directo para los IDs conocidos y un matching
  en cascada (normalización + alias + fuzzy substring) para el resto.

Tolerancia a fallos:
- Si una fuente falla, se continúa con las demás.
- Si TODAS fallan, se conserva el epg.json anterior (no se sobrescribe).
"""

import gzip
import io
import re
import sys
import unicodedata
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
# CONFIGURACIÓN DE FUENTES (9 guías seleccionadas)
# ============================================================
FUENTES_EPG = [
    # --- 1. DOMINICANAS (Prioridad Máxima) ---
    {
        "nombre": "iptv-epg-DO",
        "url": "https://iptv-epg.org/files/epg-do.xml",
        "gzip": False,
    },
    {
        "nombre": "epgshare01-DO1",
        "url": "https://epgshare01.online/epgshare01/epg_ripper_DO1.xml.gz",
        "gzip": True,
    },
    # --- 2. MÉXICO (Para Star Channel, Azteca, Claro, HBO MX, etc.) ---
    {
        "nombre": "iptv-epg-MX",
        "url": "https://iptv-epg.org/files/epg-mx.xml",
        "gzip": False,
    },
    # --- 3. COLOMBIA (Para Win Sports, Warner CO, ESPN CO, etc.) ---
    {
        "nombre": "iptv-epg-CO",
        "url": "https://iptv-epg.org/files/epg-co.xml",
        "gzip": False,
    },
    # --- 4. ARGENTINA (Para TyC, Fox Sports AR, etc.) ---
    {
        "nombre": "iptv-epg-AR",
        "url": "https://iptv-epg.org/files/epg-ar.xml",
        "gzip": False,
    },
    # --- 5. CHILE (Para TVN3, Star Channel CL, etc.) ---
    {
        "nombre": "iptv-epg-CL",
        "url": "https://iptv-epg.org/files/epg-cl.xml",
        "gzip": False,
    },
    # --- 6. ESPAÑA (Guía genérica de iptv-epg) ---
    {
        "nombre": "iptv-epg-ES",
        "url": "https://iptv-epg.org/files/epg-es.xml",
        "gzip": False,
    },
    # --- 7. TDTCHANNELS (Fuente oficial para España) ---
    {
        "nombre": "tdtchannels-ES",
        "url": "https://www.tdtchannels.com/epg/TV.xml.gz",
        "gzip": True,
    },
    # --- 8. ESTADOS UNIDOS / PANREGIONAL (Guía ligera de epgshare01) ---
    {
        "nombre": "epgshare01-US2",
        "url": "https://epgshare01.online/epgshare01/epg_ripper_US2.xml.gz",
        "gzip": True,
    },
]

# ============================================================
# LISTA NEGRA: Canales con datos incorrectos en fuentes específicas
# ============================================================
IGNORAR_FUENTE = {
    # En iptv-epg-DO, Telemicro tiene la programación de Digital 15 por error.
    # Lo ignoramos en esa fuente para que use la siguiente (epgshare01-DO1).
    "iptv-epg-DO": ["Telemicro.dr", "telemicro.do@SD"],
}

CANALES_JSON_PATH = Path("tvdominicana.json")
EPG_JSON_PATH = Path("epg.json")

# Rango temporal a extraer
HORAS_ATRAS = 2
HORAS_ADELANTE = 48

# Timeouts
TIMEOUT_DESCARGA = 90  # segundos por fuente
MAX_DESCARGA_BYTES = 500 * 1024 * 1024  # 500 MB máximo por fuente
MAX_HILOS_DESCARGA = 3

# User-Agent tipo navegador (algunos servidores bloquean "Bot" en el UA)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ------------------------------------------------------------
# MAPEO DIRECTO DE IDS (OPCIÓN A)
# Traduce los tvg-id de tu JSON exactamente a los de la guía.
# ------------------------------------------------------------
TVG_ID_MAP = {
    # --- DOMINICANOS ---
    "telemicro.do@SD":       "Telemicro.dr",
    "telesistema11.do@SD":   "Telesistema11.dr",
    "telecentro.do@SD":      "Telecentro.dr",
    "digital15.do@SD":       "Canal.15.(Digital.Quince.Telemicro).(República.Dominicana).do",
    "teleantillas.do@SD":    "TeleAntillas.dr",
    "colorvision9.do@SD":    "ColorVision.dr",
    "cdn.do@SD":             "CDN.dr",
    "cdndeportes.do@SD":     "CDNDeportes.dr",
    "teleuniverso.do@SD":    "TeleUniverso.dr",
    "teleunion.do@SD":       "Teleunion.dr",
    
    # --- PELICULAS Y SERIES (Feeds México .mx) ---
    "AMCLatinAmerica.us@Panregional":      "AMC.mx",
    "AXNLatinAmerica.us@CentralAmerica":   "AXN.mx",
    "Cinecanal.us@Panregional":            "Cinecanal.mx",
    "CinemaxLatinAmerica.us@SD":           "Cinemax.mx",
    "CNNenEspanol.us@SD":                  "CNNenEspanol.mx",
    "DiscoveryKidsLatinAmerica.us@Panregional": "DiscoveryKids.mx",
    "DisneyChannelLatinAmerica.ar@Panregional": "DisneyChannel.mx",
    "DisneyJrLatinAmerica.ar@North":       "DisneyJunior.mx",
    "DisneyXD.us@East":                    "DisneyXD.mx",
    "HBO.us@East":                         "HBO.mx",
    "HBO2LatinAmerica.us@HD":              "HBO2.mx",
    "HBOFamilyLatinAmerica.us@SD":         "HBOFamily.mx",
    "HistoryLatinAmerica.us@Panregional":  "History.mx",
    "History2LatinAmerica.us@Panregional": "History2.mx",
    "NationalGeographicLatinAmerica.us@Panregional": "NationalGeographic.mx",
    "StarChannelLatinAmerica.us@Panregional": "StarChannel.mx",
    "TNTSeriesLatinAmerica.us@Panregional": "TNTSeries.mx",
    "UniversalTVLatinAmerica.us@Panregional": "UniversalTV.mx",
    "StudioUniversalLatinAmerica.us@Panregional": "StudioUniversal.mx",
    "AELatinAmerica.us@Panregional":       "AAndE.mx",
    "WarnerChannel.us@Colombia":           "WarnerChannel.co",
    "WarnerChannel.us@South":              "WarnerChannel.ar",
    
    # --- DEPORTES ---
    "ESPNLatinAmerica.us@South":           "ESPN.co",
    "ESPN2LatinAmerica.us@South":          "ESPN2.co",
    "ESPN3LatinAmerica.us@South":          "ESPN3.co",
    "ESPN4LatinAmerica.us@South":          "ESPN4.co",

    # --- ESTADOS UNIDOS (Red de seguridad para epgshare01) ---
    "HBOHits.us@East":                     "HBOHits.us",
    "CinemaxHits.us@East":                 "CinemaxHits.us",
    "StarzKidsFamily.us@East":             "StarzKidsFamily.us",
    "FoxDeportes.us@SD":                   "FoxDeportes.us",
    
    # --- TDTCHANNELS (ESPAÑA) ---
    "La1.TV":          "LA1.es",
    "La2.TV":          "LA2.es",
    "24Horas.TV":      "24Horas.es",
    "Clan.TV":         "ClanTVE.es",
    "TDP.TV":          "Teledeporte.es",
    "SX3.TV":          "Super3.es",      # TDTChannels lo llama SX3, la guía Super3
    "13.TV":           "TRECE.es",       # TRECE
    "ElToroTV.TV":     "ElToroTV.es",
    "RMTV.TV":         "RealMadridTV.es",
    "Euronews.TV":     "Euronews.es",
    "324.TV":          "324.es",
    "E3.TV":           "Esport3.es",
    "CanalSurA.TV":    "CanalSurAndalucia.es",
    "ETBD.TV":         "ETB1.es",
}

# ------------------------------------------------------------
# ALIAS: mapea tvg-id "normalizado" del JSON -> alias en las fuentes.
# ------------------------------------------------------------
ALIASES_TVG = {
    "antena7":      {"canal7", "antena7", "antenalatin"},
    "antenalatina": {"canal7", "antenalatin"},
    "antenalatin":  {"canal7", "antenalatin"},
    "digital15":    {"canal15", "digital15", "digitalquince"},
    "digitalquince":{"canal15", "digital15", "digitalquince"},
    "canal15":      {"canal15", "digital15", "digitalquince"},
    "colorvision9": {"colorvision"},
    "telecentro13": {"telecentro"},
    "canal29":      {"teleuniverso"},
}


# ============================================================
# PARSEO DE FECHAS XMLTV
# ============================================================
def parsear_fecha_xmltv(fecha_str: str) -> int | None:
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
# NORMALIZACIÓN / MATCHING DE IDs DE CANAL
# ============================================================
def _normalizar_id_canal(s: str) -> str:
    if not s:
        return ""
    s = s.strip().lower()
    s = s.split("@")[0]
    s = re.sub(r"\.(do|dr|com|tv|net|org|pr|us|mx|ar|co|es|lat|eu)$", "", s)
    s = re.sub(r"[.\s_\-\(\)&/]+", "", s)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return s


def _build_match_index(mapa_tvg: dict) -> list:
    idx = []
    for tvg_id, (_url, nombre, channel_id) in mapa_tvg.items():
        base_key = _normalizar_id_canal(tvg_id)
        nombre_key = _normalizar_id_canal(nombre)

        keys = set()
        if base_key:
            keys.add(base_key)
        if nombre_key:
            keys.add(nombre_key)
        # Alias: buscar por base_key y por nombre_key
        for k in (base_key, nombre_key):
            if k and k in ALIASES_TVG:
                keys |= ALIASES_TVG[k]

        if keys:
            idx.append((channel_id, keys, base_key, nombre_key))
    return idx


def _resolver_channel_id(
    channel_attr: str,
    display_name: str,
    mapa_tvg: dict,
    match_index: list,
) -> tuple[str | None, int]:
    # Match exacto (rápido)
    if channel_attr in mapa_tvg:
        return mapa_tvg[channel_attr][2], 10_000

    cx = _normalizar_id_canal(channel_attr)
    dx = _normalizar_id_canal(display_name)
    if not cx and not dx:
        return None, 0

    best_id = None
    best_score = 0

    for channel_id, keys, base_key, nombre_key in match_index:
        for key in keys:
            if cx and cx == key:
                score = 2000 if key == base_key else 1900
            elif dx and dx == key:
                score = 1800
            elif cx and len(key) >= 4 and key in cx:
                score = 700 + len(key)
            elif cx and len(cx) >= 4 and cx in key:
                score = 600 + len(cx)
            elif dx and len(key) >= 4 and key in dx:
                score = 500 + len(key)
            else:
                continue

            if score > best_score:
                best_score = score
                best_id = channel_id

    # Umbral mínimo: exigimos al menos substring de 4 caracteres
    if best_score < 500:
        return None, 0
    return best_id, best_score


# ============================================================
# DESCARGA DE FUENTES
# ============================================================
def descargar_fuente(fuente: dict) -> bytes | None:
    nombre = fuente["nombre"]
    url = fuente["url"]
    log(f"→ Descargando [{nombre}]: {url}")

    try:
        headers = {"User-Agent": USER_AGENT}
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
    es_gzip_real = data[:2] == b"\x1f\x8b"
    if not es_gzip and not es_gzip_real:
        return data
    try:
        return gzip.decompress(data)
    except Exception as e:
        log(f"  AVISO: no se pudo descomprimir ({e}).")
        if es_gzip_real:
            return None
        return data


# ============================================================
# PARSEO Y EXTRACCIÓN
# ============================================================
def extraer_programas(
    xml_bytes: bytes,
    mapa_tvg: dict,
    desde_ms: int,
    hasta_ms: int,
    nombre_fuente: str,
    match_index: list,
) -> dict[str, list[dict]]:
    log(f"  Parseando XML [{nombre_fuente}] ({len(xml_bytes):,} bytes)...")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        log(f"  ERROR [{nombre_fuente}]: XML mal formado: {e}")
        return {}

    display_map = {}
    for ch in root.findall("channel"):
        cid = ch.get("id", "")
        dn = ch.find("display-name")
        if cid and dn is not None and dn.text:
            display_map[cid] = dn.text.strip()

    programas_por_canal: dict[str, list[dict]] = {}
    total = 0
    no_match = set()
    no_fecha = 0
    fuera_rango = 0

    for prog in root.findall("programme"):
        channel_attr = prog.get("channel", "").strip()
        if not channel_attr:
            continue

        # 🚨 CORRECCIÓN: Ignorar canales en la lista negra de esta fuente
        if channel_attr in IGNORAR_FUENTE.get(nombre_fuente, []):
            continue

        channel_id, _score = _resolver_channel_id(
            channel_attr,
            display_map.get(channel_attr, ""),
            mapa_tvg,
            match_index,
        )
        if not channel_id:
            no_match.add(channel_attr)
            continue

        start_ms = parsear_fecha_xmltv(prog.get("start", ""))
        stop_ms = parsear_fecha_xmltv(prog.get("stop", ""))
        if start_ms is None or stop_ms is None:
            no_fecha += 1
            continue

        if stop_ms < desde_ms or start_ms > hasta_ms:
            fuera_rango += 1
            continue

        title_el = prog.find("title")
        titulo = title_el.text.strip() if title_el is not None and title_el.text else "Sin título"

        desc_el = prog.find("desc")
        descripcion = desc_el.text.strip() if desc_el is not None and desc_el.text else None

        icon_el = prog.find("icon")
        icon_url = icon_el.get("src") if icon_el is not None else None

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

    log(f"  [{nombre_fuente}]: {total} programas en {len(programas_por_canal)} canales "
        f"(sin match: {len(no_match)}, sin fecha: {no_fecha}, fuera de rango: {fuera_rango})")

    if no_match and total == 0:
        log(f"  PISTA [{nombre_fuente}]: ningún channel id matchea. "
            f"Ejemplos del XML que no se resolvieron: {list(no_match)[:8]}")
        log(f"  PISTA [{nombre_fuente}]: tus tvg-id del JSON son: "
            f"{list(mapa_tvg.keys())[:8]}")

    return programas_por_canal


# ============================================================
# FUSIÓN DE FUENTES
# ============================================================
def fusionar_fuentes(fuentes_programas: list[dict[str, list[dict]]]) -> list[dict]:
    canales_finales: dict[str, dict[int, dict]] = {}

    for programas_fuente in fuentes_programas:
        for channel_id, programas in programas_fuente.items():
            if channel_id in canales_finales and len(canales_finales[channel_id]) > 0:
                continue
            canales_finales[channel_id] = {}
            for prog in programas:
                start = prog["startTime"]
                if start not in canales_finales[channel_id]:
                    canales_finales[channel_id][start] = prog

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
    mapeados_directos = 0
    
    for canal in canales:
        tvg_id = (canal.get("tvg-id") or "").strip()
        
        # --- APLICACIÓN DEL MAPEO DIRECTO (OPCIÓN A) ---
        if tvg_id in TVG_ID_MAP:
            tvg_id_original = tvg_id
            tvg_id = TVG_ID_MAP[tvg_id]
            canal["tvg-id"] = tvg_id  # Actualizamos el diccionario en memoria
            log(f"  🔄 Mapeado: {tvg_id_original} -> {tvg_id}")
            mapeados_directos += 1
        # ------------------------------------------------

        if not tvg_id:
            sin_tvg += 1
            continue
            
        url = canal.get("url", "")
        name = canal.get("name", "")
        channel_id = generar_channel_id(url, name)
        mapa_tvg[tvg_id] = (url, name, channel_id)

    log(f"Canales con tvg-id: {len(mapa_tvg)} | Sin tvg-id: {sin_tvg} | Mapeados directos: {mapeados_directos}")

    if not mapa_tvg:
        log("ADVERTENCIA: Ningún canal tiene tvg-id. No se puede generar EPG.")
        guardar_json_atomico(EPG_JSON_PATH, [])
        return

    # 3. Construir índice de matching (para los que no están en el mapa directo)
    match_index = _build_match_index(mapa_tvg)
    log(f"Índice de matching: {len(match_index)} canales con claves de búsqueda")

    # 4. Rango temporal
    ahora = datetime.now(timezone.utc)
    desde_ms = int((ahora - timedelta(hours=HORAS_ATRAS)).timestamp() * 1000)
    hasta_ms = int((ahora + timedelta(hours=HORAS_ADELANTE)).timestamp() * 1000)
    log(f"Rango: {datetime.fromtimestamp(desde_ms/1000, tz=timezone.utc)} "
        f"→ {datetime.fromtimestamp(hasta_ms/1000, tz=timezone.utc)}")

    # 5. Descargar todas las fuentes en paralelo
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

    # 6. Extraer programas respetando prioridad
    fuentes_programas: list[dict[str, list[dict]]] = []
    fuentes_exitosas = 0

    for idx, fuente in enumerate(FUENTES_EPG):
        data = descargas.get(idx)
        if data is None:
            log(f"[{fuente['nombre']}] DESCARTADA (no se descargó).")
            fuentes_programas.append({})
            continue

        xml_bytes = descomprimir_si_necesario(data, fuente.get("gzip", False))
        del data

        if xml_bytes is None:
            fuentes_programas.append({})
            continue

        try:
            programas = extraer_programas(
                xml_bytes, mapa_tvg, desde_ms, hasta_ms,
                fuente["nombre"], match_index,
            )
            del xml_bytes
            fuentes_programas.append(programas)
            if programas:
                fuentes_exitosas += 1
        except Exception as e:
            log(f"[{fuente['nombre']}] ERROR en extracción: {e}")
            fuentes_programas.append({})

    log(f"Fuentes exitosas: {fuentes_exitosas}/{len(FUENTES_EPG)}")

    # 7. Fusión
    log("Fusionando fuentes (prioridad: 1ª > 2ª > ...)...")
    lista_final = fusionar_fuentes(fuentes_programas)

    if not lista_final:
        log("ADVERTENCIA: No se encontraron programas en ninguna fuente.")
        log("Se conserva el epg.json anterior (si existe).")
        log("=" * 60)
        return

    # 8. Guardar
    guardar_json_atomico(EPG_JSON_PATH, lista_final)

    canales_con_epg = {p["channelId"] for p in lista_final}
    log(f"EPG guardada: {len(lista_final)} programas, {len(canales_con_epg)} canales con guía.")

    # 9. Diagnóstico: qué canales del JSON se quedaron sin EPG
    con_epg = set()
    for tvg_id, (url, name, cid) in mapa_tvg.items():
        if cid in canales_con_epg:
            con_epg.add(tvg_id)
    sin_epg = sorted(set(mapa_tvg.keys()) - con_epg)
    if sin_epg:
        log(f"Canales SIN EPG ({len(sin_epg)}):")
        for tvg_id in sin_epg:
            _, nombre, _ = mapa_tvg[tvg_id]
            log(f"    - {nombre}  (tvg-id: {tvg_id})")
    log("=" * 60)


if __name__ == "__main__":
    main()
