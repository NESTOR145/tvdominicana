#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agregar_canales.py
------------------
Descarga listas M3U remotas, valida cada canal y agrega a tvdominicana.json
solo los canales online que aún no existan.
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
    # Listas originales
    "https://raw.githubusercontent.com/NESTOR145/miscanalesdetv/refs/heads/main/todosmiscanales.m3u",
    "https://iptv-org.github.io/iptv/countries/do.m3u",
    # --- 17 listas iptv-org por país (sin filtro) ---
    "https://iptv-org.github.io/iptv/countries/pe.m3u",
    "https://iptv-org.github.io/iptv/countries/cl.m3u",
    "https://iptv-org.github.io/iptv/countries/ec.m3u",
    "https://iptv-org.github.io/iptv/countries/pr.m3u",
    "https://iptv-org.github.io/iptv/countries/ar.m3u",
    "https://iptv-org.github.io/iptv/countries/co.m3u",
    "https://iptv-org.github.io/iptv/countries/ve.m3u",
    "https://iptv-org.github.io/iptv/countries/cu.m3u",
    "https://iptv-org.github.io/iptv/countries/gt.m3u",
    "https://iptv-org.github.io/iptv/countries/hn.m3u",
    "https://iptv-org.github.io/iptv/countries/sv.m3u",
    "https://iptv-org.github.io/iptv/countries/ni.m3u",
    "https://iptv-org.github.io/iptv/countries/cr.m3u",
    "https://iptv-org.github.io/iptv/countries/pa.m3u",
    "https://iptv-org.github.io/iptv/countries/uy.m3u",
    "https://iptv-org.github.io/iptv/countries/py.m3u",
    "https://iptv-org.github.io/iptv/countries/bo.m3u",
]

URL_MEXICO = "https://iptv-org.github.io/iptv/countries/mx.m3u"

URLS_CON_FILTRO = [
    "https://iptv-org.github.io/iptv/languages/spa.m3u",
    "https://www.tdtchannels.com/lists/tv.m3u8",
    URL_MEXICO,
]

# --- FILTRO DE GRUPOS PERMITIDOS (para SPA y TDTChannels) ---
# Nota: los nombres aquí se comparan en minúsculas y sin acentos.
GRUPOS_PERMITIDOS = {
    # Películas y series
    "movies", "movie", "series", "classic", "cine", "peliculas", "película",
    "cineseries", "cine-series",
    # Infantil / anime
    "animation", "anime", "kids", "family", "infantil", "infantiles",
    "ninos", "dibujos",
    # Música
    "music", "musica", "música", "musicales",
    # Deportes
    "sports", "deportes", "deportivos",
    # Noticias
    "news", "noticias", "informativos",
    # Novelas (IMPRESCINDIBLE: sin esto los canales de spa.m3u y TDT se descartan)
    "novela", "novelas", "telenovela", "telenovelas",
    "novelas turcas", "telenovelas turcas",
    # Religión / cultura
    "religion", "religious", "religioso", "religiosos",
    "culture", "cultura", "cristiano", "cristianos",
    # Variedades
    "entertainment", "entretenimiento", "comedy", "lifestyle",
    "cooking", "travel", "auto", "business", "education",
    "science", "documentary", "documental", "relax", "outdoor",
    # General
    "general", "public", "generalistas", "eventuales",
    # Dominicanos
    "dominicano", "dominicana", "dominicanos",
}


def es_grupo_permitido(group_title: str) -> bool:
    if not group_title:
        return False
    for p in group_title.split(";"):
        p_norm = unicodedata.normalize("NFKD", p).encode("ascii", "ignore").decode().lower().strip()
        if p_norm in GRUPOS_PERMITIDOS:
            return True
    return False


def es_canal_valido_mexico(canal: dict) -> bool:
    """Filtro específico para México: sin geo-blocked, tvg-id limpio y .m3u8."""
    nombre = canal.get("name", "")
    if "[Geo-blocked]" in nombre or "[Geo-bloqueado]" in nombre:
        return False
    tvg_id = canal.get("_tvg_id", "").strip()
    if not tvg_id or any(ch in tvg_id for ch in "[]() "):
        return False
    url = canal.get("url", "").strip()
    if not url.startswith("http") or ".m3u8" not in url:
        return False
    return True


def descargar_m3u(url: str):
    try:
        log(f"→ Descargando: {url}")
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_DESCARGA_M3U)
        r.raise_for_status()
        log(f"  OK: {len(r.text):,} bytes")
        return r.text
    except requests.RequestException as e:
        log(f"  ERROR: {e}")
        return None


def validar_canal_nuevo(canal):
    url = canal.get("url")
    if not url:
        return None
    if probar_enlace(url, canal.get("headers")) == "ok":
        return canal
    return None


def validar_actualizacion(canal_nuevo, canal_existente):
    url_vieja = canal_existente.get("url", "")
    if url_vieja and probar_enlace(url_vieja, canal_existente.get("headers"), intentos=2, pausa=2.0) == "ok":
        return "conservar"
    if probar_enlace(canal_nuevo.get("url"), canal_nuevo.get("headers")) == "ok":
        return "actualizar"
    return "conservar_caida"


def main() -> None:
    log("=" * 60)
    log("AGREGAR CANALES ONLINE")
    log("=" * 60)

    json_actual = cargar_json_seguro(JSON_PATH, default=[])
    if json_actual is None:
        json_actual = []
    if not isinstance(json_actual, list):
        log("ERROR: JSON no es lista plana.")
        sys.exit(1)

    log(f"Canales en JSON actual: {len(json_actual)}")

    candidatos = []

    for url in URLS_SIN_FILTRO:
        contenido = descargar_m3u(url)
        if contenido:
            canales = parsear_m3u(contenido)
            log(f"  {url}: {len(canales)} canales (sin filtro)")
            candidatos.extend((c, None) for c in canales)

    for url in URLS_CON_FILTRO:
        contenido = descargar_m3u(url)
        if contenido:
            canales = parsear_m3u(contenido)
            tipo = "mexico" if url == URL_MEXICO else "categoria"
            log(f"  {url}: {len(canales)} canales (filtro {tipo})")
            candidatos.extend((c, tipo) for c in canales)

    if not candidatos:
        log("Nada que procesar.")
        return

    urls_existentes = {normalizar_url(c.get("url", "")) for c in json_actual if c.get("url")}
    mapa_existentes = {}
    for c in json_actual:
        n = normalizar_nombre(c.get("name", ""))
        if n:
            mapa_existentes[n] = c

    vistos_url = set()
    vistos_nombre = set()
    a_validar = []
    dup = 0
    ign_filtro = 0

    for c, tipo in candidatos:
        u = c.get("url")
        if not u:
            continue
        un = normalizar_url(u)
        nom = normalizar_nombre(c.get("name", ""))
        cat = c.get("group-title", "")
        clave = nom if nom else un

        if un in urls_existentes or un in vistos_url:
            dup += 1
            continue

        if clave in mapa_existentes:
            vistos_url.add(un)
            a_validar.append((c, "upd", mapa_existentes[clave]))
            continue

        if clave in vistos_nombre:
            dup += 1
            continue

        if tipo == "categoria" and not es_grupo_permitido(cat):
            ign_filtro += 1
            continue
        if tipo == "mexico" and not es_canal_valido_mexico(c):
            ign_filtro += 1
            continue

        vistos_url.add(un)
        if clave:
            vistos_nombre.add(clave)
        a_validar.append((c, "nuevo", None))

    log(f"Candidatos: {len(a_validar)} | Ignorados: {ign_filtro} | Duplicados: {dup}")

    if not a_validar:
        log("Nada que validar.")
        return

    log(f"Validando {len(a_validar)} canales...")
    nuevos = []
    upd_url = 0
    conservados = 0
    caidos = 0

    with ThreadPoolExecutor(max_workers=MAX_HILOS) as ex:
        futuros = {}
        for canal, modo, existente in a_validar:
            if modo == "upd":
                futuros[ex.submit(validar_actualizacion, canal, existente)] = (canal, modo, existente)
            else:
                futuros[ex.submit(validar_canal_nuevo, canal)] = (canal, modo, existente)

        for f in as_completed(futuros):
            canal, modo, existente = futuros[f]
            try:
                res = f.result()
            except Exception as e:
                log(f"  ERROR {canal.get('name','?')}: {e}")
                res = None

            if modo == "upd":
                if res == "actualizar":
                    log(f"  🔄 {canal.get('name','?')}")
                    existente["url"] = canal["url"]
                    if canal.get("_tvg_id"):
                        existente["tvg-id"] = canal["_tvg_id"]
                    if canal.get("headers"):
                        existente["headers"] = canal["headers"]
                    upd_url += 1
                else:
                    conservados += 1
            else:
                if res:
                    nuevos.append(res)
                    log(f"  ✅ {canal.get('name','(sin nombre)')}")
                else:
                    log(f"  ❌ {canal.get('name','(sin nombre)')}")
                    caidos += 1

    for c in nuevos:
        cat_o = c.get("group-title", "")
        tvg = c.pop("_tvg_id", "")
        c["group-title"] = normalizar_categoria(cat_o, tvg, c.get("name", ""))
        if tvg:
            c["tvg-id"] = tvg
        if not c.get("headers"):
            c.pop("headers", None)

    json_actual.extend(nuevos)

    # --- RE-CLASIFICACIÓN FINAL: corrige canales históricos mal clasificados ---
    # (TNT Novelas, TNT Novelas México, Pluto TV Novelas… que estuvieran en
    # PELICULAS Y SERIES o VARIEDADES por un group-title genérico).
    reclasificados = 0
    for c in json_actual:
        cat_actual = c.get("group-title", "")
        tvg = c.get("tvg-id") or c.get("_tvg_id") or c.get("tvg_id") or ""
        nueva = normalizar_categoria(cat_actual, tvg, c.get("name", ""))
        if nueva != cat_actual:
            c["group-title"] = nueva
            reclasificados += 1
    if reclasificados:
        log(f"Re-clasificados: {reclasificados} canales")

    log("=" * 60)
    log(f"Nuevos: {len(nuevos)} | URLs actualizadas: {upd_url} | Conservados: {conservados}")
    log(f"Ignorados filtro: {ign_filtro} | Caídos: {caidos} | Duplicados: {dup}")

    if nuevos or upd_url > 0 or reclasificados > 0:
        json_actual.sort(key=clave_orden)
        guardar_json_atomico(JSON_PATH, json_actual)
        log(f"JSON actualizado. Total: {len(json_actual)}")
    else:
        log("Sin cambios.")

    log("=" * 60)


if __name__ == "__main__":
    main()
