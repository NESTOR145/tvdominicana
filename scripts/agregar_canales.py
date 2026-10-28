#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agregar_canales.py
------------------
Descarga listas M3U remotas, valida cada canal con validación REAL y agrega a
tvdominicana.json SOLO los canales online que aún no existan.
"""

import sys
import unicodedata
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

# --- CONFIGURACIÓN DE LISTAS REMOTAS ---
URLS_SIN_FILTRO = [
    "https://raw.githubusercontent.com/NESTOR145/miscanalesdetv/refs/heads/main/todosmiscanales.m3u",
    "https://iptv-org.github.io/iptv/countries/do.m3u",
]

URLS_CON_FILTRO = [
    "https://iptv-org.github.io/iptv/languages/spa.m3u",
]

# --- FILTRO DE GRUPOS PERMITIDOS ---
GRUPOS_PERMITIDOS = {
    "movies", "movie", "series", "classic", "cine", "peliculas", "película",
    "animation", "anime", "kids", "family", "infantil", "ninos", "dibujos",
    "music", "musica", "música", "sports", "deportes",
    "documentary", "documental", "news", "noticias",
    "dominicano", "dominicana"
}

def es_grupo_permitido(group_title: str) -> bool:
    if not group_title:
        return False
    partes = group_title.split(";")
    for p in partes:
        p_norm = unicodedata.normalize("NFKD", p).encode("ascii", "ignore").decode().lower().strip()
        if p_norm in GRUPOS_PERMITIDOS:
            return True
    return False


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

    for url_remota in URLS_SIN_FILTRO:
        contenido = descargar_m3u(url_remota)
        if contenido:
            canales_m3u = parsear_m3u(contenido)
            log(f"Canales encontrados en {url_remota}: {len(canales_m3u)} (sin filtro)")
            todos_los_candidatos.extend((c, False) for c in canales_m3u)

    for url_remota in URLS_CON_FILTRO:
        contenido = descargar_m3u(url_remota)
        if contenido:
            canales_m3u = parsear_m3u(contenido)
            log(f"Canales encontrados en {url_remota}: {len(canales_m3u)} (con filtro)")
            todos_los_candidatos.extend((c, True) for c in canales_m3u)

    if not todos_los_candidatos:
        log("Ninguna lista devolvió canales. Nada que hacer.")
        return

    # --- PREPARAR ÍNDICES (Solo por nombre y url) ---
    urls_existentes = {normalizar_url(c.get("url", "")) for c in json_actual if c.get("url")}

    mapa_existentes = {}
    for c in json_actual:
        nom_limpio = normalizar_nombre(c.get("name", ""))
        if nom_limpio:
            mapa_existentes[nom_limpio] = c

    # --- CLASIFICAR ---
    vistos_url = set()
    vistos_nombre = set()
    a_validar = []
    descartados_dup = 0
    ignorados_por_categoria = 0

    for c, aplica_filtro in todos_los_candidatos:
        u = c.get("url")
        if not u:
            continue
        
        un = normalizar_url(u)
        nom_limpio = normalizar_nombre(c.get("name", ""))
        cat = c.get("group-title", "")
        
        clave_nombre = nom_limpio if nom_limpio else un

        if un in urls_existentes or un in vistos_url:
            descartados_dup += 1
            continue

        if clave_nombre in mapa_existentes:
            existente = mapa_existentes[clave_nombre]
            vistos_url.add(un)
            a_validar.append((c, True, existente))
            continue

        if clave_nombre in vistos_nombre:
            descartados_dup += 1
            continue

        if aplica_filtro and not es_grupo_permitido(cat):
            ignorados_por_categoria += 1
            continue

        vistos_url.add(un)
        if clave_nombre:
            vistos_nombre.add(clave_nombre)
        a_validar.append((c, False, None))

    log(f"Candidatos: {len(a_validar)} | Ignorados por categoría: {ignorados_por_categoria} | Duplicados: {descartados_dup}")

    if not a_validar:
        log("Nada que validar. Nada que hacer.")
        log("=" * 60)
        return

    # --- VALIDAR TODO EN PARALELO ---
    log(f"Validando {len(a_validar)} canales (nuevos + actualizaciones) con {MAX_HILOS} hilos...")
    online_nuevos = []
    actualizados_url = 0
    descartados_por_caida = 0

    with ThreadPoolExecutor(max_workers=MAX_HILOS) as executor:
        futuros = {
            executor.submit(validar_canal, canal): (canal, es_upd, existente)
            for canal, es_upd, existente in a_validar
        }
        for futuro in as_completed(futuros):
            canal, es_upd, existente = futuros[futuro]
            try:
                resultado = futuro.result()
            except Exception as e:
                log(f"  ERROR validando {canal.get('name', '?')}: {e}")
                resultado = None

            if resultado:
                if es_upd:
                    log(f"  🔄 {canal.get('name', '?')}: URL actualizada (validada OK)")
                    existente["url"] = canal["url"]
                    tvg_nuevo = canal.get("_tvg_id", "")
                    if tvg_nuevo:
                        existente["tvg-id"] = tvg_nuevo
                    if canal.get("headers"):
                        existente["headers"] = canal["headers"]
                    actualizados_url += 1
                else:
                    online_nuevos.append(resultado)
                    log(f"  ✅ {canal.get('name', '(sin nombre)')}")
            else:
                if es_upd:
                    log(f"  ⏭️ {canal.get('name', '?')}: nueva URL caída. Se conserva la anterior.")
                else:
                    log(f"  ❌ {canal.get('name', '(sin nombre)')}")
                descartados_por_caida += 1

    # --- PROCESAR NUEVOS ---
    for c in online_nuevos:
        cat_original = c.get("group-title", "")
        tvg = c.pop("_tvg_id", "")
        nombre = c.get("name", "")
        c["group-title"] = normalizar_categoria(cat_original, tvg, nombre)
        if tvg:
            c["tvg-id"] = tvg
        if not c.get("headers"):
            c.pop("headers", None)

    json_actual.extend(online_nuevos)

    # --- RESUMEN ---
    log("=" * 60)
    log("Resumen:")
    log(f"  Nuevos agregados:        {len(online_nuevos)}")
    log(f"  URLs actualizadas:       {actualizados_url}")
    log(f"  Ignorados por categoría: {ignorados_por_categoria}")
    log(f"  Descartados por caída:   {descartados_por_caida}")
    log(f"  Descartados por dup:     {descartados_dup}")

    # --- GUARDAR SI HAY CAMBIOS ---
    if online_nuevos or actualizados_url > 0:
        json_actual.sort(key=clave_orden)
        guardar_json_atomico(JSON_PATH, json_actual)
        log(f"JSON actualizado. Total de canales: {len(json_actual)}")
    else:
        log("Sin cambios. JSON no se modificó.")

    log("=" * 60)

if __name__ == "__main__":
    main()
