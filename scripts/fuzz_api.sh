#!/usr/bin/env bash
# Fuzzing de la API desde su OpenAPI con Schemathesis: busca respuestas 500 y respuestas
# que no cumplen el esquema que el propio backend declara.
#
#   uv run --group fuzz bash scripts/fuzz_api.sh [max_ejemplos]
#
# FUZZ_SCOPE deja solo una parte, para repetir una falla en local más rápido: la API del
# local en dos mitades de tamaño parecido, `operacion` (inventario, pedidos, carta, mesas,
# reservas y caja) y `gestion` (el resto), y `plataforma`. Sin él (`todo`), corre todo,
# como en el CI: partida, las cadenas con estado no cruzan de una mitad a la otra.
#
# Levanta la aplicación real (DEBUG activo, para que publique /api/v1/openapi.json) sobre
# una SQLite nueva, migrada y sembrada, entra con las cuentas de demostración y corre
# `schemathesis run` dos veces: con la sesión del encargado contra la API del local y con
# la de la administración del sistema contra `/platform`. Es el mismo arranque que usa
# la verificación del contrato (Pact), sin la ruta de estados.
set -euo pipefail

MAX_EXAMPLES=${1:-25}
PORT=${FUZZ_PORT:-8204}
SCOPE=${FUZZ_SCOPE:-todo}
case "$SCOPE" in
  todo | operacion | gestion | plataforma) ;;
  *) echo "FUZZ_SCOPE=$SCOPE: se espera todo, operacion, gestion o plataforma" >&2; exit 2 ;;
esac
PASSWORD=resthub123
ROOT=$(cd "$(dirname "$0")/.." && pwd)
WORK=$(mktemp -d)
SERVER_PID=""
# Cuatro comprobaciones que no aplican a esta API. La aceptación de datos válidos falla en las
# reglas entre campos que OpenAPI no expresa («un pedido en mesa necesita la mesa»); el rechazo de
# datos inválidos, en la coerción de Pydantic (un `0` se lee como fecha en el modo laxo); los
# métodos no soportados y la cabecera `Allow`, en el enrutador de Starlette, que devuelve un
# `Allow` incompleto cuando GET y POST de la misma ruta son operaciones distintas y un 422 en
# `PATCH /tables/order` (la ruta con parámetro atrapa la estática).
EXCLUDED_CHECKS=positive_data_acceptance,negative_data_rejection,unsupported_method,allow_header_conformance

cleanup() {
  if [ -n "$SERVER_PID" ]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

cd "$ROOT"
export DATABASE_URL="sqlite+aiosqlite:///$WORK/fuzz.db"
export DEBUG=true
export LOG_JSON=false
export LOG_LEVEL=WARNING
export JWT_SECRET_KEY=fuzz-solo-pruebas-locales-0000000000000000
export OPENROUTER_API_KEY=
export OBSERVABILITY_ENABLED=false
# El fuzzing manda cientos de peticiones por minuto desde una sola IP.
export RATE_LIMIT_PER_MINUTE=0
# Las acciones críticas piden la contraseña confirmada (RFS-21): se confirma una vez
# al entrar y dura toda la corrida.
export REAUTHENTICATION_WINDOW_SECONDS=7200
# La administración del sistema entra solo con la contraseña, como en las pruebas y en
# Pact: con los dos pasos obligatorios (RFS-22) toda `/platform` respondería 403.
export PLATFORM_TWO_FACTOR_REQUIRED=false
export CORS_ALLOWED_ORIGINS='["http://localhost:5203"]'

python -m alembic upgrade head
python scripts/seed_dev.py

python -m uvicorn resthub.main:app --host 127.0.0.1 --port "$PORT" --log-level warning &
SERVER_PID=$!

for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$PORT/api/v1/health" >/dev/null 2>&1; then break; fi
  sleep 0.5
done

login() {
  curl -fsS -X POST "http://127.0.0.1:$PORT/api/v1/$1" -H 'Content-Type: application/json' \
    -d "{\"email\": \"$2\", \"password\": \"$PASSWORD\"}" |
    python -c 'import json, sys; print(json.load(sys.stdin)["access_token"])'
}

OWNER_TOKEN=$(login auth/login admin@resthub.dev)
PLATFORM_TOKEN=$(login auth/login plataforma@resthub.dev)
for token in "$OWNER_TOKEN" "$PLATFORM_TOKEN"; do
  curl -fsS -o /dev/null -X POST "http://127.0.0.1:$PORT/api/v1/auth/me/reauthenticate" \
    -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
    -d "{\"password\": \"$PASSWORD\"}"
done
SCHEMA="http://127.0.0.1:$PORT/api/v1/openapi.json"
STATUS=0
# Lo que cierra o bloquea la sesión con la que se hace el fuzzing: salir, bloquearla
# por inactividad o cerrar las sesiones de una cuenta (el encargado sembrado podría
# ser el número que toca). El resto de la corrida quedaría en 401 o 423.
SESSION_ENDERS='auth/logout|auth/me/lock|staff/\{user_id\}/sessions'

# Las rutas de la mitad `operacion` de la API del local.
OPERATION='/api/v1/(inventory|orders|menu|tables|reservations|cash)'
local_api() {
  echo "== API del local (sesión del encargado)$1"
  schemathesis run "$SCHEMA" \
    -H "Authorization: Bearer $OWNER_TOKEN" \
    "${@:2}" \
    --exclude-checks "$EXCLUDED_CHECKS" \
    --max-examples "$MAX_EXAMPLES" || STATUS=1
}
case "$SCOPE" in
  todo) local_api "" --exclude-path-regex "/(platform|auth/login|auth/preview|$SESSION_ENDERS)" ;;
  operacion) local_api ", operación" --include-path-regex "$OPERATION" ;;
  gestion)
    local_api ", gestión" \
      --exclude-path-regex "/(platform|auth/login|auth/preview|$SESSION_ENDERS)|$OPERATION"
    ;;
esac

if [ "$SCOPE" = todo ] || [ "$SCOPE" = plataforma ]; then
  echo "== Administración del sistema (/platform)"
  schemathesis run "$SCHEMA" \
    -H "Authorization: Bearer $PLATFORM_TOKEN" \
    --include-path-regex '/platform' \
    --exclude-path-regex '/platform/auth/login' \
    --exclude-checks "$EXCLUDED_CHECKS" \
    --max-examples "$MAX_EXAMPLES" || STATUS=1
fi

exit "$STATUS"
