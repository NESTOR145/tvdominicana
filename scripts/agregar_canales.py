
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agrega canales online de fuentes M3U sin duplicar y sin sobrescribir datos válidos."""

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
    clave_dedupe,
    clave_nombre_categoria,
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
    "https://raw.githubusercontent.com/TVPRO20/Megatv23/master/tv23.m3u",
    "https://raw.githubusercontent.com/NESTOR145/miscanalesdetv/refs/heads/main/todosmiscanales.m3u",
    "https://iptv-org.github.io/iptv/countries/do.m3u",
]


def descargar_m3u(url: str):
    try:
        log(f"→ Descargando lista M3U: {url}")
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT_DESCARGA_M3U,
        )
        response.raise_for_status()
        contenido = response.text

        if not contenido.strip() or "#EXTINF" not in contenido.upper():
            log(f"  AVISO: la respuesta de {url} no parece una lista M3U.")
            return None

        log(f"  OK: {len(contenido):,} caracteres recibidos.")
        return contenido

    except requests.RequestException as exc:
        log(f"  ERROR descargando lista: {exc}")
        return None


def validar_canal(canal):
    url = canal.get("url")
    if not isinstance(url, str) or not url.strip():
        return None

    return canal if probar_enlace(url, canal.get("headers")) == "ok" else None


def canal_valido(c):
    return (
        isinstance(c, dict)
        and isinstance(c.get("name", ""), str)
        and isinstance(c.get("url", ""), str)
    )


def main() -> None:
    log("=" * 60)
    log("AGREGAR CANALES ONLINE DESDE MÚLTIPLES LISTAS M3U")
    log("=" * 60)

    try:
        json_actual = cargar_json_seguro(JSON_PATH, default=[])
    except (OSError, ValueError) as exc:
        log(f"ERROR seguro leyendo {JSON_PATH}: {exc}")
        sys.exit(1)

    if not isinstance(json_actual, list) or any(
        not canal_valido(c) for c in json_actual
    ):
        log(
            f"ERROR: {JSON_PATH} debe ser una lista de objetos "
            "de canal válidos. No se modifica."
        )
        sys.exit(1)

    log(f"Canales en JSON actual: {len(json_actual)}")

    todos = []
    listas_ok = 0

    for url_remota in URLS_REMOTAS_M3U:
        contenido = descargar_m3u(url_remota)
        if contenido is None:
            continue

        canales = parsear_m3u(contenido)
        if canales:
            listas_ok += 1
            log(f"Canales encontrados: {len(canales)}")
            todos.extend(canales)

    # Si ninguna fuente produjo canales parseables, no hacer ningún cambio.
    if listas_ok == 0 or not todos:
        log("Ninguna lista remota produjo canales utilizables. No se modifica el JSON.")
        return

    urls_existentes = set()
    nombres_categoria_existentes = set()
    pares_existentes = set()

    for c in json_actual:
        url_norm = normalizar_url(c.get("url", ""))
        nombre_norm = normalizar_nombre(c.get("name", ""))

        if url_norm:
            urls_existentes.add(url_norm)
        if nombre_norm:
            nombres_categoria_existentes.add(clave_nombre_categoria(c))

        pares_existentes.add(clave_dedupe(c))

    candidatos = []
    urls_vistas = set()
    nombres_vistos = set()
    pares_vistos = set()
    duplicados = 0

    for original in todos:
        c = dict(original)
        url_norm = normalizar_url(c.get("url", ""))
        nombre_norm = normalizar_nombre(c.get("name", ""))

        if not url_norm or not nombre_norm:
            continue

        # Se evita duplicado por URL, por nombre+categoría y por nombre+URL.
        par = clave_dedupe(c)
        nombre_cat = clave_nombre_categoria(c)

        if (
            url_norm in urls_existentes
            or url_norm in urls_vistas
            or par in pares_existentes
            or par in pares_vistos
            or nombre_cat in nombres_categoria_existentes
            or nombre_cat in nombres_vistos
        ):
            duplicados += 1
            continue

        urls_vistas.add(url_norm)
        pares_vistos.add(par)
        nombres_vistos.add(nombre_cat)
        candidatos.append(c)

    log(
        f"Candidatos únicos para validar: {len(candidatos)}; "
        f"duplicados descartados: {duplicados}"
    )

    if not candidatos:
        log("No hay canales nuevos que agregar.")
        return

    online = []

    with ThreadPoolExecutor(max_workers=MAX_HILOS) as pool:
        futures = {
            pool.submit(validar_canal, c): c
            for c in candidatos
        }

        for future in as_completed(futures):
            canal = futures[future]

            try:
                result = future.result()
            except Exception as exc:
                log(f"ERROR validando {canal.get('name', '?')}: {exc}")
                result = None

            if result:
                online.append(result)
                log(f"  ONLINE: {canal.get('name', '(sin nombre)')}")
            else:
                log(f"  NO VALIDADO: {canal.get('name', '(sin nombre)')}")

    if not online:
        log("No se agregó ningún canal: ninguno pasó la validación.")
        return

    # Segunda deduplicación antes de escribir, por defensa en profundidad.
    existentes_urls = {
        normalizar_url(c.get("url", ""))
        for c in json_actual
        if c.get("url")
    }
    existentes_nombres = {
        clave_nombre_categoria(c)
        for c in json_actual
    }

    salida_nueva = []

    for c in online:
        tvg = c.pop("_tvg_id", "")
        c["group-title"] = normalizar_categoria(
            c.get("group-title", ""),
            tvg,
            c.get("name", ""),
        )

        if not c.get("headers"):
            c.pop("headers", None)

        un = normalizar_url(c.get("url", ""))
        nc = clave_nombre_categoria(c)

        if un in existentes_urls or nc in existentes_nombres:
            continue

        existentes_urls.add(un)
        existentes_nombres.add(nc)
        salida_nueva.append(c)

    if not salida_nueva:
        log("Tras la comprobación final no quedan canales nuevos.")
        return

    # Conserva canales actuales y solo agrega nuevos.
    resultado = list(json_actual) + salida_nueva
    resultado.sort(key=clave_orden)

    try:
        guardar_json_atomico(JSON_PATH, resultado)
    except OSError as exc:
        log(f"ERROR guardando JSON de forma atómica: {exc}")
        sys.exit(1)

    log(
        f"JSON actualizado. Agregados: {len(salida_nueva)}; "
        f"total: {len(resultado)}."
    )
    log("=" * 60)


if __name__ == "__main__":
    main()
