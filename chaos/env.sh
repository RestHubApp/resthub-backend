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
# Uso: chaos/env.sh start | stop | status | backend-start | backend-stop
#                       | frontend-build | database-start | database-stop
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONT="${RESTHUB_CHAOS_FRONT:-$HOME/github/wt-pruebas-caos-front}"
RUN="${RESTHUB_CHAOS_RUN:-/tmp/caos-resthub}"
PGDATA="${RESTHUB_CHAOS_PGDATA:-/tmp/pg-caos}"
TOXIPROXY="${TOXIPROXY_SERVER:-toxiproxy-server}"

PG_PORT=55205
PG_PROXY_PORT=55305
BACKEND_PORT=8205
TOXI_API_PORT=8305
BACKEND_PROXY_PORT=8306
FAULT_PROXY_PORT=8307
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

wait_for_port() {
  local port=$1 name=$2
  for _ in $(seq 1 120); do
    if (echo >"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
      return 0
    fi
    sleep 0.5
  done
  echo "No respondió $name en el port $port" >&2
  return 1
}

database_start() {
  if [ ! -d "$PGDATA" ]; then
    initdb -D "$PGDATA" -U postgres -A trust >/dev/null
  fi
  if ! pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
    pg_ctl -D "$PGDATA" -o "-p $PG_PORT -k /tmp" -l "$RUN/postgres.log" -w start >/dev/null
  fi
  psql -h /tmp -p "$PG_PORT" -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='resthub'" \
    | grep -q 1 || createdb -h /tmp -p "$PG_PORT" -U postgres resthub
}

database_stop() {
  pg_ctl -D "$PGDATA" -m fast stop >/dev/null 2>&1 || true
}

toxiproxy_start() {
  if ! (echo >"/dev/tcp/127.0.0.1/$TOXI_API_PORT") 2>/dev/null; then
    nohup "$TOXIPROXY" -host 127.0.0.1 -port "$TOXI_API_PORT" >"$RUN/toxiproxy.log" 2>&1 &
    echo $! >"$RUN/toxiproxy.pid"
    wait_for_port "$TOXI_API_PORT" Toxiproxy
  fi
  local api="http://127.0.0.1:${TOXI_API_PORT}"
  curl -sf -X POST "$api/populate" -H 'Content-Type: application/json' -d "[
    {\"name\": \"postgres\", \"listen\": \"127.0.0.1:${PG_PROXY_PORT}\", \"upstream\": \"127.0.0.1:${PG_PORT}\", \"enabled\": true},
    {\"name\": \"backend\", \"listen\": \"127.0.0.1:${BACKEND_PROXY_PORT}\", \"upstream\": \"127.0.0.1:${BACKEND_PORT}\", \"enabled\": true}
  ]" >/dev/null
}

backend_start() {
  if [ -f "$RUN/backend.pid" ] && kill -0 "$(cat "$RUN/backend.pid")" 2>/dev/null; then
    return 0
  fi
  cd "$ROOT"
  # `.venv/bin/uvicorn` y no `uv run`: así el PID es el del servidor y un
  # SIGKILL lo mata a él, no a un proceso intermedio.
  nohup .venv/bin/uvicorn resthub.main:app --host 127.0.0.1 --port "$BACKEND_PORT" \
    >>"$RUN/backend.log" 2>&1 &
  echo $! >"$RUN/backend.pid"
  wait_for_port "$BACKEND_PORT" backend
}

backend_stop() {
  if [ -f "$RUN/backend.pid" ]; then
    kill "$(cat "$RUN/backend.pid")" 2>/dev/null || true
    rm -f "$RUN/backend.pid"
  fi
}

fault_proxy_start() {
  if [ -f "$RUN/fault_proxy.pid" ] && kill -0 "$(cat "$RUN/fault_proxy.pid")" 2>/dev/null; then
    return 0
  fi
  cd "$ROOT"
  RESTHUB_CHAOS_UPSTREAM="http://127.0.0.1:${BACKEND_PROXY_PORT}" \
    nohup .venv/bin/uvicorn --app-dir chaos resthub_chaos.fault_proxy:app --host 127.0.0.1 --port "$FAULT_PROXY_PORT" \
    --log-level warning >"$RUN/fault_proxy.log" 2>&1 &
  echo $! >"$RUN/fault_proxy.pid"
  wait_for_port "$FAULT_PROXY_PORT" "proxy de fallas"
}

frontend_build() {
  cd "$FRONT"
  VITE_API_URL="http://localhost:${FAULT_PROXY_PORT}" pnpm build >"$RUN/frontend-build.log" 2>&1
}

frontend_start() {
  if [ -f "$RUN/frontend.pid" ] && kill -0 "$(cat "$RUN/frontend.pid")" 2>/dev/null; then
    return 0
  fi
  cd "$FRONT"
  [ -d dist ] || frontend_build
  nohup pnpm exec vite preview --port "$FRONT_PORT" --strictPort >"$RUN/frontend.log" 2>&1 &
  echo $! >"$RUN/frontend.pid"
  wait_for_port "$FRONT_PORT" frontend
}

stop_pid() {
  local pid_file="$RUN/$1.pid"
  if [ -f "$pid_file" ]; then
    pkill -P "$(cat "$pid_file")" 2>/dev/null || true
    kill "$(cat "$pid_file")" 2>/dev/null || true
    rm -f "$pid_file"
  fi
}

case "${1:-}" in
  start)
    database_start
    toxiproxy_start
    cd "$ROOT"
    .venv/bin/alembic upgrade head >"$RUN/alembic.log" 2>&1
    .venv/bin/python scripts/seed_dev.py >"$RUN/seed.log" 2>&1
    backend_start
    fault_proxy_start
    frontend_start
    echo "Entorno de caos arriba: frontend http://localhost:${FRONT_PORT}"
    ;;
  stop)
    stop_pid frontend
    stop_pid fault_proxy
    backend_stop
    stop_pid toxiproxy
    database_stop
    ;;
  status)
    for p in "$PG_PORT postgres" "$TOXI_API_PORT toxiproxy" "$BACKEND_PORT backend" \
             "$FAULT_PROXY_PORT fault-proxy" "$FRONT_PORT frontend"; do
      set -- $p
      if (echo >"/dev/tcp/127.0.0.1/$1") 2>/dev/null; then echo "$2 ($1): arriba"; else echo "$2 ($1): abajo"; fi
    done
    ;;
  backend-start) backend_start ;;
  backend-stop) backend_stop ;;
  database-start) database_start ;;
  database-stop) database_stop ;;
  frontend-build) frontend_build ;;
  *)
    echo "Uso: $0 start | stop | status | backend-start | backend-stop | database-start | database-stop | frontend-build" >&2
    exit 2
    ;;
esac
