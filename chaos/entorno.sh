#!/usr/bin/env bash
# Entorno local de los experimentos de caos de RestHub.
#
# Levanta, en puertos propios y sin root:
#   PostgreSQL 55205 ─► Toxiproxy «postgres» 55305 ─► backend 8205
#   backend 8205 ─► Toxiproxy «backend» 8306 ─► proxy de fallas HTTP 8307 ─► frontend 5205
# La API de Toxiproxy escucha en 8305. El frontend habla con el backend solo a
# través de los dos proxies, y el backend con la base solo a través de
# Toxiproxy, así que cada experimento puede cortar, demorar o falsear cualquiera
# de los dos tramos sin tocar la aplicación.
#
# Nunca apunta a Railway ni a Vercel: todo corre en 127.0.0.1.
#
# Uso: chaos/entorno.sh arrancar | parar | estado | backend-arrancar | backend-parar
#                       | frontend-construir | base-arrancar | base-parar
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONT="${RESTHUB_CHAOS_FRONT:-$HOME/github/wt-pruebas-caos-front}"
RUN="${RESTHUB_CHAOS_RUN:-/tmp/caos-resthub}"
PGDATA="${RESTHUB_CHAOS_PGDATA:-/tmp/pg-caos}"
TOXIPROXY="${TOXIPROXY_SERVER:-toxiproxy-server}"

PG_PORT=55205
PG_PROXY_PORT=55305
BACKEND_PORT=8205
TOXI_API_PORT=8305
BACKEND_PROXY_PORT=8306
FALLAS_PORT=8307
FRONT_PORT=5205

mkdir -p "$RUN"
# shellcheck disable=SC1091
source "$HOME/.local/pgsql/env.sh"

export DATABASE_URL="postgresql+asyncpg://postgres@127.0.0.1:${PG_PROXY_PORT}/resthub"
# LOG_JSON=true como en el despliegue: con la consola de desarrollo, cada traza
# se dibuja con rich (con variables locales) y eso solo ya frena el proceso.
export DEBUG=true LOG_JSON=true LOG_LEVEL=INFO
export JWT_SECRET_KEY="pruebas-locales-solo-desarrollo-000000000000"
export OPENROUTER_API_KEY="" TYPESAFE_API_KEY=""
export CORS_ALLOWED_ORIGINS="[\"http://localhost:${FRONT_PORT}\"]"
export FRONTEND_BASE_URL="http://localhost:${FRONT_PORT}"

esperar_puerto() {
  local puerto=$1 nombre=$2
  for _ in $(seq 1 120); do
    if (echo >"/dev/tcp/127.0.0.1/$puerto") 2>/dev/null; then
      return 0
    fi
    sleep 0.5
  done
  echo "No respondió $nombre en el puerto $puerto" >&2
  return 1
}

base_arrancar() {
  if [ ! -d "$PGDATA" ]; then
    initdb -D "$PGDATA" -U postgres -A trust >/dev/null
  fi
  if ! pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
    pg_ctl -D "$PGDATA" -o "-p $PG_PORT -k /tmp" -l "$RUN/postgres.log" -w start >/dev/null
  fi
  psql -h /tmp -p "$PG_PORT" -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='resthub'" \
    | grep -q 1 || createdb -h /tmp -p "$PG_PORT" -U postgres resthub
}

base_parar() {
  pg_ctl -D "$PGDATA" -m fast stop >/dev/null 2>&1 || true
}

toxiproxy_arrancar() {
  if ! (echo >"/dev/tcp/127.0.0.1/$TOXI_API_PORT") 2>/dev/null; then
    nohup "$TOXIPROXY" -host 127.0.0.1 -port "$TOXI_API_PORT" >"$RUN/toxiproxy.log" 2>&1 &
    echo $! >"$RUN/toxiproxy.pid"
    esperar_puerto "$TOXI_API_PORT" Toxiproxy
  fi
  local api="http://127.0.0.1:${TOXI_API_PORT}"
  curl -sf -X POST "$api/populate" -H 'Content-Type: application/json' -d "[
    {\"name\": \"postgres\", \"listen\": \"127.0.0.1:${PG_PROXY_PORT}\", \"upstream\": \"127.0.0.1:${PG_PORT}\", \"enabled\": true},
    {\"name\": \"backend\", \"listen\": \"127.0.0.1:${BACKEND_PROXY_PORT}\", \"upstream\": \"127.0.0.1:${BACKEND_PORT}\", \"enabled\": true}
  ]" >/dev/null
}

backend_arrancar() {
  if [ -f "$RUN/backend.pid" ] && kill -0 "$(cat "$RUN/backend.pid")" 2>/dev/null; then
    return 0
  fi
  cd "$RAIZ"
  # `.venv/bin/uvicorn` y no `uv run`: así el PID es el del servidor y un
  # SIGKILL lo mata a él, no a un proceso intermedio.
  nohup .venv/bin/uvicorn resthub.main:app --host 127.0.0.1 --port "$BACKEND_PORT" \
    >>"$RUN/backend.log" 2>&1 &
  echo $! >"$RUN/backend.pid"
  esperar_puerto "$BACKEND_PORT" backend
}

backend_parar() {
  if [ -f "$RUN/backend.pid" ]; then
    kill "$(cat "$RUN/backend.pid")" 2>/dev/null || true
    rm -f "$RUN/backend.pid"
  fi
}

fallas_arrancar() {
  if [ -f "$RUN/fallas.pid" ] && kill -0 "$(cat "$RUN/fallas.pid")" 2>/dev/null; then
    return 0
  fi
  cd "$RAIZ"
  RESTHUB_CHAOS_UPSTREAM="http://127.0.0.1:${BACKEND_PROXY_PORT}" \
    nohup .venv/bin/uvicorn --app-dir chaos resthub_chaos.proxy_fallas:app --host 127.0.0.1 --port "$FALLAS_PORT" \
    --log-level warning >"$RUN/fallas.log" 2>&1 &
  echo $! >"$RUN/fallas.pid"
  esperar_puerto "$FALLAS_PORT" "proxy de fallas"
}

frontend_construir() {
  cd "$FRONT"
  VITE_API_URL="http://localhost:${FALLAS_PORT}" pnpm build >"$RUN/frontend-build.log" 2>&1
}

frontend_arrancar() {
  if [ -f "$RUN/frontend.pid" ] && kill -0 "$(cat "$RUN/frontend.pid")" 2>/dev/null; then
    return 0
  fi
  cd "$FRONT"
  [ -d dist ] || frontend_construir
  nohup pnpm exec vite preview --port "$FRONT_PORT" --strictPort >"$RUN/frontend.log" 2>&1 &
  echo $! >"$RUN/frontend.pid"
  esperar_puerto "$FRONT_PORT" frontend
}

parar_pid() {
  local archivo="$RUN/$1.pid"
  if [ -f "$archivo" ]; then
    pkill -P "$(cat "$archivo")" 2>/dev/null || true
    kill "$(cat "$archivo")" 2>/dev/null || true
    rm -f "$archivo"
  fi
}

case "${1:-}" in
  arrancar)
    base_arrancar
    toxiproxy_arrancar
    cd "$RAIZ"
    .venv/bin/alembic upgrade head >"$RUN/alembic.log" 2>&1
    .venv/bin/python scripts/seed_dev.py >"$RUN/seed.log" 2>&1
    backend_arrancar
    fallas_arrancar
    frontend_arrancar
    echo "Entorno de caos arriba: frontend http://localhost:${FRONT_PORT}"
    ;;
  parar)
    parar_pid frontend
    parar_pid fallas
    backend_parar
    parar_pid toxiproxy
    base_parar
    ;;
  estado)
    for p in "$PG_PORT postgres" "$TOXI_API_PORT toxiproxy" "$BACKEND_PORT backend" \
             "$FALLAS_PORT proxy-fallas" "$FRONT_PORT frontend"; do
      set -- $p
      if (echo >"/dev/tcp/127.0.0.1/$1") 2>/dev/null; then echo "$2 ($1): arriba"; else echo "$2 ($1): abajo"; fi
    done
    ;;
  backend-arrancar) backend_arrancar ;;
  backend-parar) backend_parar ;;
  base-arrancar) base_arrancar ;;
  base-parar) base_parar ;;
  frontend-construir) frontend_construir ;;
  *)
    echo "Uso: $0 arrancar | parar | estado | backend-arrancar | backend-parar | base-arrancar | base-parar | frontend-construir" >&2
    exit 2
    ;;
esac
