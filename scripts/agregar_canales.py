#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agregar_canales.py
------------------
Descarga listas M3U remotas, valida cada canal con validación REAL y agrega a
tvdominicana.json SOLO los canales online que aún no existan.
Si un canal ya existe pero cambió de URL, actualiza la URL automáticamente.
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
    normalizar_nombre,
    normalizar_url,
    parsear_m3u,
    probar_enlace,
)

JSON_PATH = Path("tvdominicana.json")

URLS_REMOTAS_M3U = [
    "https://raw.githubusercontent.com/NESTOR145/miscanalesdetv/refs/heads/main/todosmiscanales.m3u",
    "https://iptv-org.github.io/iptv/countries/do.m3u",
]


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

    # --- PREPARAR ÍNDICES DE LO QUE YA EXISTE ---
    urls_existentes = {normalizar_url(c.get("url", "")) for c in json_actual if c.get("url")}
    
    # Mapa para buscar canales existentes por nombre+categoría y actualizar su URL
    mapa_existentes = {}
    for c in json_actual:
        nom = normalizar_nombre(c.get("name", ""))
        cat = c.get("group-title", "")
        if nom:
            mapa_existentes[(nom, cat)] = c

    vistos_url = set()
    vistos_nombre = set()
    candidatos = []
    actualizados_url = 0
    descartados_dup = 0

    for c in todos_los_candidatos:
        u = c.get("url")
        if not u:
            continue
        un = normalizar_url(u)
        nom = normalizar_nombre(c.get("name", ""))
        cat = c.get("group-title", "")
        clave_nombre = (nom, cat) if nom else None

        # 1. ¿Ya existe exactamente igual en el JSON? -> Ignorar
        if un in urls_existentes or un in vistos_url:
            descartados_dup += 1
            continue

        # 2. ¿Existe el nombre pero con otra URL? -> ACTUALIZAR URL
        if clave_nombre and clave_nombre in mapa_existentes:
            existente = mapa_existentes[clave_nombre]
            log(f"  🔄 Actualizando URL de {c.get('name')}: {existente['url']} -> {u}")
            existente["url"] = u
            # Actualizar tvg-id si viene en la nueva M3U
            tvg_nuevo = c.get("_tvg_id", "")
            if tvg_nuevo:
                existente["tvg-id"] = tvg_nuevo
            # Actualizar headers si los trae
            if c.get("headers"):
                existente["headers"] = c["headers"]
            
            actualizados_url += 1
            continue # No lo agregamos a la lista de nuevos

        # 3. ¿Es un duplicado dentro de la misma lista remota? -> Ignorar
        if clave_nombre and clave_nombre in vistos_nombre:
            descartados_dup += 1
            continue

        # 4. Si llega aquí, es un canal NUEVO
        vistos_url.add(un)
        if clave_nombre:
            vistos_nombre.add(clave_nombre)
        candidatos.append(c)

    log(f"Canales nuevos únicos por validar: {len(candidatos)}")
    log(f"URLs actualizadas: {actualizados_url}")
    log(f"Descartados por duplicado: {descartados_dup}")

    if not candidatos and actualizados_url == 0:
        log("No hay canales nuevos ni actualizaciones. Nada que hacer.")
        return

    # --- VALIDAR Y AGREGAR LOS NUEVOS ---
    if candidatos:
        log(f"Validando con {MAX_HILOS} hilos (validación REAL)...")
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
        
        for c in online:
            cat_original = c.get("group-title", "")
            tvg = c.pop("_tvg_id", "")
            nombre = c.get("name", "")
            c["group-title"] = normalizar_categoria(cat_original, tvg, nombre)
            if tvg:
                c["tvg-id"] = tvg
            if not c.get("headers"):
                c.pop("headers", None)
        
        json_actual.extend(online)

    # --- GUARDAR SIEMPRE ---
    if candidatos or actualizados_url > 0:
        json_actual.sort(key=clave_orden)
        guardar_json_atomico(JSON_PATH, json_actual)
        log(f"JSON actualizado. Total de canales: {len(json_actual)}")
    
    log("=" * 60)


if __name__ == "__main__":
    main()
