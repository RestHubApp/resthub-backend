#!/usr/bin/env bash
# Restaura un respaldo cifrado (`resthub-*.dump.age`) en una base PostgreSQL y mide cuánto tarda.
#
#   bash scripts/restore_backup.sh <respaldo.dump.age> <llave-age.txt> <postgresql://destino>
#
# El destino debe ser una base vacía o de prueba: `pg_restore --clean` reemplaza lo que haya.
# La llave privada de age no vive en el repositorio ni en Railway. La meta de recuperación
# (RNFS-12) es de 4 horas o menos, de punta a punta: bajar el artefacto, descifrar y restaurar.
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "Uso: $0 <respaldo.dump.age> <llave-age.txt> <postgresql://destino>" >&2
  exit 2
fi
backup="$1"
identity="$2"
target="$3"

started=$(date +%s)
age --decrypt --identity "$identity" "$backup" \
  | pg_restore --clean --if-exists --no-owner --no-privileges --exit-on-error --dbname "$target"
finished=$(date +%s)

elapsed=$((finished - started))
echo "Restaurado en ${elapsed} s ($(date -u -d "@${elapsed}" +%H:%M:%S))."
echo "Tablas restauradas:"
psql "$target" --no-psqlrc --tuples-only --command \
  "SELECT relname || ': ' || n_live_tup FROM pg_stat_user_tables ORDER BY relname"
