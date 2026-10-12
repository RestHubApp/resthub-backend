#!/usr/bin/env bash
# Corre un experimento con Chaos Toolkit y guarda el journal JSON y la salida.
# Uso: chaos/run.sh chaos/experiments/01-database-down.json <etiqueta> [carpeta]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXPERIMENT="$1"
LABEL="${2:-corrida}"
OUTPUT="${3:-${RESTHUB_CHAOS_REPORTES:-$ROOT/chaos/journals}}"
mkdir -p "$OUTPUT"
NAME="$(basename "$EXPERIMENT" .json)-$LABEL"
cd "$ROOT"
# `--rollback-strategy always`: los rollbacks corren aunque la hipótesis se
# desvíe, así un experimento fallido no deja la base caída ni un tóxico puesto.
PYTHONPATH="$ROOT/chaos" uv run --group chaos chaos --log-file "$OUTPUT/$NAME.log" run \
  --rollback-strategy always --journal-path "$OUTPUT/$NAME.journal.json" "$EXPERIMENT" \
  2>&1 | tee "$OUTPUT/$NAME.txt"
