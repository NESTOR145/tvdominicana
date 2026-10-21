
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Limpia canales tras fallos consecutivos; prioriza conservar el catálogo ante dudas."""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from canales_common import (
    MAX_HILOS,
    cargar_json_seguro,
    guardar_json_atomico,
    id_canal,
    log,
    normalizar_nombre,
    normalizar_url,
    probar_enlace,
)

JSON_PATH = Path("tvdominicana.json")
ESTADO_PATH = Path("estado_tvdominicana.json")
LOG_PATH = Path("canales_eliminados.log")

MAX_FALLOS = 6
UMBRAL_ABORTO = 0.90
DRY_RUN = os.getenv("DRY_RUN", "0").strip().lower() in (
    "1", "true", "yes", "si"
)


def cargar_estado():
    if not ESTADO_PATH.exists():
        return {}

    try:
        data = cargar_json_seguro(ESTADO_PATH, default={})

        if not isinstance(data, dict):
            raise ValueError("el estado debe ser un objeto JSON")

        estado = {}

        for k, v in data.items():
            try:
                n = int(v)
                estado[str(k)] = max(0, n)
            except (TypeError, ValueError):
                log(f"AVISO: contador de estado inválido para {k!r}; se ignora.")

        return estado

    except (OSError, ValueError) as exc:
        # No renombrar ni sobrescribir estado corrupto automáticamente.
        raise ValueError(
            f"Estado ilegible ({exc}). Corrige o restaura "
            f"{ESTADO_PATH}; no se modifica nada."
        ) from exc


def anexar_log(lineas):
    if not lineas:
        return

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    with LOG_PATH.open("a", encoding="utf-8") as f:
        for linea in lineas:
            f.write(linea + "\n")

        f.flush()
        os.fsync(f.fileno())


def clave_legacy(canal):
    """Permite recuperar contadores del formato antiguo."""
    url = canal.get("url", "")
    if url:
        return str(url)

    return f"{canal.get('name', '')}|{canal.get('logo', '')}"


def deduplicar(canales):
    """Quita URL repetida o nombre normalizado repetido dentro de la categoría."""
    salida = []
    urls = set()
    nombres = set()
    pares = set()
    descartados = 0

    for canal in canales:
        u = normalizar_url(canal.get("url", ""))
        n = normalizar_nombre(canal.get("name", ""))
        cat = str(canal.get("group-title", "")).strip().upper()

        par = (n, u)
        nc = (n, cat) if n else None

        if (
            (u and u in urls)
            or par in pares
            or (nc and nc in nombres)
        ):
            log(
                f"  DUPLICADO descartado: "
                f"{canal.get('name', '?')} | {canal.get('url', '')}"
            )
            descartados += 1
            continue

        if u:
            urls.add(u)

        pares.add(par)

        if nc:
            nombres.add(nc)

        salida.append(canal)

    return salida, descartados


def main():
    if not JSON_PATH.exists():
        log(f"ERROR: no existe {JSON_PATH}; no se modifica nada.")
        sys.exit(1)

    try:
        canales_originales = cargar_json_seguro(JSON_PATH)
    except (OSError, ValueError) as exc:
        log(f"ERROR seguro leyendo {JSON_PATH}: {exc}")
        sys.exit(1)

    if not isinstance(canales_originales, list) or any(
        not isinstance(c, dict) for c in canales_originales
    ):
        log(
            f"ERROR: {JSON_PATH} debe ser una lista de objetos. "
            "No se modifica."
        )
        sys.exit(1)

    if not canales_originales:
        log("La lista está vacía; no se modifica el catálogo ni el estado.")
        return

    try:
        estado = cargar_estado()
    except ValueError as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)

    canales, n_dups = deduplicar(canales_originales)

    log(
        f"Canales originales={len(canales_originales)}; "
        f"tras deduplicar={len(canales)}; DRY_RUN={DRY_RUN}"
    )

    resultados = {}

    with ThreadPoolExecutor(max_workers=MAX_HILOS) as pool:
        futures = {}

        for canal in canales:
            url = canal.get("url", "")

            # Un canal sin URL no se elimina automáticamente.
            if not isinstance(url, str) or not url.strip():
                resultados[id_canal(canal)] = "sin_url"
            else:
                futures[
                    pool.submit(probar_enlace, url, canal.get("headers"))
                ] = canal

        for future in as_completed(futures):
            canal = futures[future]

            try:
                resultados[id_canal(canal)] = future.result()
            except Exception as exc:
                log(
                    f"ERROR validando {canal.get('name', '?')}: {exc}; "
                    "se trata como temporal."
                )
                resultados[id_canal(canal)] = "temporal"

    nuevos = []
    eliminados = []

    caidos_reales = 0
    temporales = 0
    sin_url = 0

    nuevo_estado = dict(estado)

    for canal in canales:
        cid = id_canal(canal)
        res = resultados.get(cid, "temporal")

        nombre = canal.get("name", "desconocido")
        url = canal.get("url", "")
        legacy = clave_legacy(canal)

        if res == "sin_url":
            sin_url += 1
            log(
                f"  SIN URL: {nombre} "
                "(se conserva por seguridad; revisar manualmente)"
            )
            nuevos.append(canal)
            continue

        if res == "ok":
            log(f"  OK: {nombre}")
            nuevo_estado[cid] = 0
            nuevo_estado.pop(legacy, None)
            nuevos.append(canal)

        elif res == "temporal":
            temporales += 1
            log(f"  TEMPORAL: {nombre}; no suma fallos.")
            nuevos.append(canal)

        else:
            caidos_reales += 1

            previo = nuevo_estado.get(cid, nuevo_estado.get(legacy, 0))

            try:
                fallos = max(0, int(previo)) + 1
            except (TypeError, ValueError):
                fallos = 1

            nuevo_estado[cid] = fallos
            nuevo_estado.pop(legacy, None)

            if fallos >= MAX_FALLOS:
                log(
                    f"  ELIMINADO: {nombre}; "
                    f"{fallos} fallos consecutivos."
                )
                eliminados.append((nombre, url))
                nuevo_estado.pop(cid, None)
            else:
                log(
                    f"  FALLO {fallos}/{MAX_FALLOS}: "
                    f"{nombre}; se conserva."
                )
                nuevos.append(canal)

    validables = len(canales) - sin_url

    if (
        validables > 0
        and caidos_reales / validables > UMBRAL_ABORTO
    ):
        log(
            f"ABORTADO por caída masiva: "
            f"{caidos_reales}/{validables} fallos. "
            "No se guarda ningún cambio."
        )
        sys.exit(1)

    ids_vivos = {id_canal(c) for c in nuevos}

    nuevo_estado = {
        k: v for k, v in nuevo_estado.items()
        if k in ids_vivos
    }

    if DRY_RUN:
        log(
            f"[DRY_RUN] Se quitarían {len(eliminados)} canales caídos "
            f"y {n_dups} duplicados."
        )
        log(
            f"[DRY_RUN] Se conservarían {len(nuevos)} canales. "
            "No se escribió ningún archivo."
        )
        return

    cambios_catalogo = bool(eliminados or n_dups)

    try:
        # No se reescribe el catálogo si no hubo cambios estructurales.
        if cambios_catalogo:
            guardar_json_atomico(JSON_PATH, nuevos)

        guardar_json_atomico(ESTADO_PATH, nuevo_estado)

        if eliminados:
            marca = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.gmtime()
            ) + " UTC"

            anexar_log([
                f"{marca} | {nombre} | {url}"
                for nombre, url in eliminados
            ])

    except OSError as exc:
        log(f"ERROR escribiendo archivos: {exc}")
        sys.exit(1)

    log(
        f"Resumen: conservados={len(nuevos)}, "
        f"eliminados={len(eliminados)}, duplicados={n_dups}, "
        f"temporal={temporales}, fallos_reales={caidos_reales}, "
        f"sin_url_conservados={sin_url}."
    )


if __name__ == "__main__":
    main()
