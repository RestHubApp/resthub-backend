#!/usr/bin/env bash
# Corre un escenario de carga completo contra el backend local:
# prepara los datos, muestrea recursos, corre k6 con el dashboard web y guarda
# todo en $REPORTS_DIR (por omisión tests/load/reportes).
#
#   tests/load/run.sh <escenario> [nombre]      # escenario: nominal, stress, peak, soak
#
# Variables: BASE_URL (http://localhost:8204), UVICORN_PID (pid del servidor,
# para medir su memoria), PG_PORT (55204), K6 (k6-sse), REPORTS_DIR.
#
# El servidor se levanta con RATE_LIMIT_PER_MINUTE=0: todos los usuarios virtuales
# de k6 salen de la misma IP y el límite por IP los cortaría.
set -euo pipefail
cd "$(dirname "$0")/../.."

SCENARIO=$1
RUN_NAME=${2:-$1}
K6=${K6:-k6-sse}
REPORTS_DIR=${REPORTS_DIR:-tests/load/reportes}
BASE_URL=${BASE_URL:-http://localhost:8204}
case "$BASE_URL" in
  http://localhost:*|http://127.0.0.1:*) ;;
  *) echo "Solo contra un backend local: BASE_URL=$BASE_URL" >&2; exit 1 ;;
esac
mkdir -p "$REPORTS_DIR"
export BASE_URL REPORTS_DIR RUN_NAME

"$K6" run -q tests/load/prepare.js > "$REPORTS_DIR/$RUN_NAME-preparacion.txt" 2>&1
# La máquina es compartida: se deja constancia de su carga antes y después.
{ date; uptime; } > "$REPORTS_DIR/$RUN_NAME-maquina.txt"

MONITOR=""
if [ -n "${UVICORN_PID:-}" ]; then
  tests/load/measure_resources.sh "$UVICORN_PID" "$REPORTS_DIR/$RUN_NAME-recursos.csv" 5 "${PG_PORT:-55204}" &
  MONITOR=$!
fi

set +e
K6_WEB_DASHBOARD=true \
K6_WEB_DASHBOARD_PORT=${K6_WEB_DASHBOARD_PORT:-5665} \
K6_WEB_DASHBOARD_PERIOD=${K6_WEB_DASHBOARD_PERIOD:-5s} \
K6_WEB_DASHBOARD_EXPORT="$REPORTS_DIR/$RUN_NAME-dashboard.html" \
  "$K6" run -q --out "csv=$REPORTS_DIR/$RUN_NAME-muestras.csv.gz" "tests/load/$SCENARIO.js" 2>&1 \
  | tee "$REPORTS_DIR/$RUN_NAME-terminal.txt"
STATUS=${PIPESTATUS[0]}
set -e

[ -n "$MONITOR" ] && kill "$MONITOR" 2>/dev/null || true
{ date; uptime; } >> "$REPORTS_DIR/$RUN_NAME-maquina.txt"
echo "k6 terminó con código $STATUS (99 = algún umbral no se cumplió)"
exit "$STATUS"
