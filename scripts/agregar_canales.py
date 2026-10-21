#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agregar_canales.py
------------------
Descarga múltiples listas M3U remotas, valida cada canal con validación REAL
(Content-Type + primeros bytes + detección de HTML/JSON de error) y agrega a
tvdominicana.json SOLO los canales que estén realmente online y que aún no
existan (comparando por URL normalizada).

Reordena TODO el JSON por categoría:
    DOMINICANOS -> ANIME-INFANTIL -> PREMIUM
y dentro de cada categoría, alfabético por nombre (A→Z).

Garantías:
  - NO toca estado_tvdominicana.json.
  - NO toca canales_eliminados.log.
  - NO elimina ningún canal existente (solo lo reordena).
  - Si todas las listas remotas fallan, sale sin modificar el JSON.
  - Si un canal nuevo está offline (o devuelve HTML/JSON de error), NO se agrega.
"""

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from canales_common import (
    MAX_HILOS,
    TIMEOUT_DESCARGA_M3U,
    USER_AGENT,
    cargar_json_seguro,
    clave_orden,
    guardar_json_atomico,
    log,
    normalizar_categoria,
    normalizar_url,
    parsear_m3u,
    probar_enlace,
)

# ---------- CONFIGURACIÓN ----------
JSON_PATH = Path("tvdominicana.json")

URLS_REMOTAS_M3U = [
    "https://raw.githubusercontent.com/TVPRO20/Megatv23/master/tv23.m3u",
    "https://raw.githubusercontent.com/NESTOR145/miscanalesdetv/refs/heads/main/todosmiscanales.m3u",
    "https://iptv-org.github.io/iptv/countries/do.m3u",
]
# -----------------------------------


def descargar_m3u(url: str):
    try:
        log(f"→ Descargando lista M3U: {url}")
        headers = {"User-Agent": USER_AGENT}
        r = requests.get(url, headers=headers, timeout=TIMEOUT_DESCARGA_M3U)
        r.raise_for_status()
        log(f"  OK: {len(r.text):,} bytes recibidos.")
        return r.text
    except requests.RequestException as e:
        log(f"  ERROR al descargar la lista: {e}")
        return None


def validar_canal(canal):
    """Devuelve el canal si está online, None si no."""
    url = canal.get("url")
    if not url:
        return None
    if probar_enlace(url, canal.get("headers")) == "ok":
        return canal
    return None


def main() -> None:
    log("=" * 60)
    log("AGREGAR CANALES ONLINE DESDE MÚLTIPLES LISTAS M3U")
    log("=" * 60)

    json_actual = cargar_json_seguro(JSON_PATH, default=[])
    if json_actual is None:
        json_actual = []
    if not isinstance(json_actual, list):
        log(f"ERROR: {JSON_PATH} no contiene una lista plana.")
        sys.exit(1)

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

    # Deduplicación por URL normalizada (evita http vs https, trailing slash, etc.)
    urls_existentes = {normalizar_url(c.get("url", "")) for c in json_actual if c.get("url")}
    vistos = set()
    candidatos = []
    for c in todos_los_candidatos:
        u = c.get("url")
        if not u:
            continue
        un = normalizar_url(u)
        if un in urls_existentes or un in vistos:
            continue
        vistos.add(un)
        candidatos.append(c)

    log(f"Canales nuevos únicos por validar: {len(candidatos)}")
    if not candidatos:
        log("No hay canales nuevos. Nada que agregar.")
        return

    log(f"Validando con {MAX_HILOS} hilos (validación REAL: Content-Type + primeros bytes)...")
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

    log(f"Canales nuevos ONLINE REALES encontrados: {len(online)}")
    if not online:
        log("No se agregó ningún canal nuevo.")
        return

    # Clasificar nuevos
    for c in online:
        cat_original = c.get("group-title", "")
        tvg = c.pop("_tvg_id", "")
        nombre = c.get("name", "")
        c["group-title"] = normalizar_categoria(cat_original, tvg, nombre)
        # Limpiar headers vacíos
        if not c.get("headers"):
            c.pop("headers", None)

    json_actual.extend(online)
    json_actual.sort(key=clave_orden)

    guardar_json_atomico(JSON_PATH, json_actual)
    log(f"JSON actualizado y reordenado. Total de canales: {len(json_actual)}")
    log("=" * 60)


if __name__ == "__main__":
    main()
