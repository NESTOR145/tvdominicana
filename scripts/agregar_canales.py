
name: Agregar canales online desde lista remota

on:
  schedule:
    - cron: '0 2,8,14,20 * * *'
  workflow_dispatch:

permissions:
  contents: write

# Mismo grupo en ambos workflows para que agregar y limpiar no se ejecuten juntos.
concurrency:
  group: canales
  cancel-in-progress: false

jobs:
  agregar:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    if: github.ref == 'refs/heads/main'

    steps:
      - name: Descargar repositorio
        uses: actions/checkout@v4
        with:
          ref: main
          fetch-depth: 0

      - name: Instalar Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: pip

      - name: Instalar dependencias
        run: python -m pip install --disable-pip-version-check "requests==2.32.3"

      - name: Ejecutar agregación
        env:
          PYTHONUNBUFFERED: "1"
        run: python scripts/agregar_canales.py

      - name: Subir resultado
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: agregar-resultado
          path: tvdominicana.json
          if-no-files-found: ignore
          retention-days: 14

      - name: Guardar cambios
        if: success()
        shell: bash
        run: |
          set -euo pipefail
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add -- tvdominicana.json

          if git diff --cached --quiet; then
            echo "Sin cambios que guardar."
            exit 0
          fi

          git commit -m "Auto: agregar canales online desde lista remota"

          for i in 1 2 3; do
            if git push origin HEAD:main; then
              echo "Push correcto en intento $i."
              exit 0
            fi

            echo "Push falló; actualizando main e intentando rebase."
            git fetch origin main

            if ! git rebase origin/main; then
              git rebase --abort || true
              echo "::error::Conflicto al integrar cambios remotos. No se fuerza el push."
              exit 1
            fi

            sleep 3
          done

          echo "::error::No se pudo hacer push tras 3 intentos."
          exit 1
