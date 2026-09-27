#!/usr/bin/env bash
# Muestrea, cada N segundos, la memoria residente (RSS) y el uso de CPU del
# proceso de uvicorn, sus hilos y las conexiones abiertas en PostgreSQL.
#
#   tests/load/medir_recursos.sh <pid de uvicorn> <salida.csv> [intervalo_s] [puerto_pg]
#
# Se corre en segundo plano durante la prueba y se detiene con kill. Necesita
# `psql` en el PATH (por ejemplo, `source ~/.local/pgsql/env.sh`).
set -u
PID=$1
SALIDA=$2
INTERVALO=${3:-5}
PUERTO=${4:-55204}
TICKS=$(getconf CLK_TCK)

echo "epoch,rss_mb,cpu_pct,hilos,pg_conexiones,pg_activas,pg_esperando_bloqueo" > "$SALIDA"
prev_t=$(date +%s.%N)
prev_c=$(awk '{print $14+$15}' "/proc/$PID/stat")
while kill -0 "$PID" 2>/dev/null; do
  sleep "$INTERVALO"
  t=$(date +%s.%N)
  c=$(awk '{print $14+$15}' "/proc/$PID/stat" 2>/dev/null) || break
  rss=$(awk '/VmRSS/ {printf "%.1f", $2/1024}' "/proc/$PID/status")
  hilos=$(awk '/Threads/ {print $2}' "/proc/$PID/status")
  cpu=$(awk -v c="$c" -v pc="$prev_c" -v t="$t" -v pt="$prev_t" -v k="$TICKS" \
    'BEGIN {printf "%.1f", 100 * (c - pc) / k / (t - pt)}')
  pg=$(psql -h /tmp -p "$PUERTO" -U postgres -d resthub -tAF, -c \
    "select count(*), count(*) filter (where state = 'active'), count(*) filter (where wait_event_type = 'Lock') from pg_stat_activity where datname = 'resthub' and pid <> pg_backend_pid()" 2>/dev/null)
  echo "$(date +%s),$rss,$cpu,$hilos,$pg" >> "$SALIDA"
  prev_t=$t
  prev_c=$c
done
