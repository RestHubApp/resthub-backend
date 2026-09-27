#!/usr/bin/env bash
# Corre un experimento con Chaos Toolkit y guarda el journal JSON y la salida.
# Uso: chaos/correr.sh chaos/experimentos/01-base-cae.json <etiqueta> [carpeta]
set -euo pipefail
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXPERIMENTO="$1"
ETIQUETA="${2:-corrida}"
SALIDA="${3:-${RESTHUB_CHAOS_REPORTES:-$RAIZ/chaos/journals}}"
mkdir -p "$SALIDA"
NOMBRE="$(basename "$EXPERIMENTO" .json)-$ETIQUETA"
cd "$RAIZ"
# `--rollback-strategy always`: los rollbacks corren aunque la hipótesis se
# desvíe, así un experimento fallido no deja la base caída ni un tóxico puesto.
PYTHONPATH="$RAIZ/chaos" uv run --group chaos chaos --log-file "$SALIDA/$NOMBRE.log" run \
  --rollback-strategy always --journal-path "$SALIDA/$NOMBRE.journal.json" "$EXPERIMENTO" \
  2>&1 | tee "$SALIDA/$NOMBRE.txt"
