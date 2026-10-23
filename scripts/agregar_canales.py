#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agregar_canales.py
------------------
Descarga listas M3U remotas, valida cada canal con validación REAL y agrega a
tvdominicana.json SOLO los canales online que aún no existan.

Reglas de actualización:
- Si un canal ya existe (mismo nombre + categoría) pero cambió de URL,
  SOLO se actualiza si la nueva URL responde correctamente (validación real).
- Las URLs nuevas que estén caídas se descartan y se conserva la URL anterior.
- Las URLs temporales (timeout, 403, 5xx) también se descartan por seguridad.
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
    """Solo devuelve el canal si está 100% online. Si está caído o temporal, devuelve None."""
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

    # --- PREPARAR ÍNDICES ---
    urls_existentes = {normalizar_url(c.get("url", "")) for c in json_actual if c.get("url")}

    mapa_existentes = {}
    for c in json_actual:
        nom = normalizar_nombre(c.get("name", ""))
        cat = c.get("group-title", "")
        if nom:
            mapa_existentes[(nom, cat)] = c

    # --- CLASIFICAR: nuevos y actualizaciones pendientes ---
    vistos_url = set()
    vistos_nombre = set()
    # Cada entrada: (canal_remoto, es_actualizacion, existente_o_None)
    a_validar = []
    descartados_dup = 0

    for c in todos_los_candidatos:
        u = c.get("url")
        if not u:
            continue
        un = normalizar_url(u)
        nom = normalizar_nombre(c.get("name", ""))
        cat = c.get("group-title", "")
        clave_nombre = (nom, cat) if nom else None

        # 1. URL ya existe exactamente -> ignorar
        if un in urls_existentes or un in vistos_url:
            descartados_dup += 1
            continue

        # 2. Mismo nombre pero URL distinta -> candidato a actualización (requiere validación)
        if clave_nombre and clave_nombre in mapa_existentes:
            existente = mapa_existentes[clave_nombre]
            vistos_url.add(un)
            a_validar.append((c, True, existente))
            continue

        # 3. Duplicado dentro de las listas remotas -> ignorar
        if clave_nombre and clave_nombre in vistos_nombre:
            descartados_dup += 1
            continue

        # 4. Canal nuevo
        vistos_url.add(un)
        if clave_nombre:
            vistos_nombre.add(clave_nombre)
        a_validar.append((c, False, None))

    if not a_validar:
        log(f"Nada que validar. Descartados por duplicado: {descartados_dup}")
        log("=" * 60)
        return

    # --- VALIDAR TODO EN PARALELO (nuevos + actualizaciones) ---
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
                    # Actualización validada OK -> aplicar
                    log(f"  🔄 {canal.get('name', '?')}: URL actualizada (validada OK)")
                    existente["url"] = canal["url"]
                    tvg_nuevo = canal.get("_tvg_id", "")
                    if tvg_nuevo:
                        existente["tvg-id"] = tvg_nuevo
                    if canal.get("headers"):
                        existente["headers"] = canal["headers"]
                    actualizados_url += 1
                else:
                    # Canal nuevo validado OK -> agregar
                    online_nuevos.append(resultado)
                    log(f"  ✅ {canal.get('name', '(sin nombre)')}")
            else:
                if es_upd:
                    log(f"  ⏭️ {canal.get('name', '?')}: nueva URL caída. Se conserva la anterior.")
                else:
                    log(f"  ❌ {canal.get('name', '(sin nombre)')}")
                descartados_por_caida += 1

    # --- PROCESAR NUEVOS (normalizar categoría + headers) ---
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
