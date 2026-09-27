#!/usr/bin/env bash
# Corre un escenario de carga completo contra el backend local:
# prepara los datos, muestrea recursos, corre k6 con el dashboard web y guarda
# todo en $REPORTES (por omisión tests/load/reportes).
#
#   tests/load/correr.sh <escenario> [nombre]      # escenario: nominal, estres, pico, resistencia
#
# Variables: BASE_URL (http://localhost:8204), UVICORN_PID (pid del servidor,
# para medir su memoria), PG_PUERTO (55204), K6 (k6-sse), REPORTES.
set -euo pipefail
cd "$(dirname "$0")/../.."

ESCENARIO=$1
NOMBRE=${2:-$1}
K6=${K6:-k6-sse}
REPORTES=${REPORTES:-tests/load/reportes}
BASE_URL=${BASE_URL:-http://localhost:8204}
case "$BASE_URL" in
  http://localhost:*|http://127.0.0.1:*) ;;
  *) echo "Solo contra un backend local: BASE_URL=$BASE_URL" >&2; exit 1 ;;
esac
mkdir -p "$REPORTES"
export BASE_URL REPORTES NOMBRE

"$K6" run -q tests/load/preparar.js > "$REPORTES/$NOMBRE-preparacion.txt" 2>&1
# La máquina es compartida: se deja constancia de su carga antes y después.
{ date; uptime; } > "$REPORTES/$NOMBRE-maquina.txt"

MONITOR=""
if [ -n "${UVICORN_PID:-}" ]; then
  tests/load/medir_recursos.sh "$UVICORN_PID" "$REPORTES/$NOMBRE-recursos.csv" 5 "${PG_PUERTO:-55204}" &
  MONITOR=$!
fi

set +e
K6_WEB_DASHBOARD=true \
K6_WEB_DASHBOARD_PORT=${K6_WEB_DASHBOARD_PORT:-5665} \
K6_WEB_DASHBOARD_PERIOD=${K6_WEB_DASHBOARD_PERIOD:-5s} \
K6_WEB_DASHBOARD_EXPORT="$REPORTES/$NOMBRE-dashboard.html" \
  "$K6" run -q --out "csv=$REPORTES/$NOMBRE-muestras.csv.gz" "tests/load/$ESCENARIO.js" 2>&1 \
  | tee "$REPORTES/$NOMBRE-terminal.txt"
ESTADO=${PIPESTATUS[0]}
set -e

[ -n "$MONITOR" ] && kill "$MONITOR" 2>/dev/null || true
{ date; uptime; } >> "$REPORTES/$NOMBRE-maquina.txt"
echo "k6 terminó con código $ESTADO (99 = algún umbral no se cumplió)"
exit "$ESTADO"
