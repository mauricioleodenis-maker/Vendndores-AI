#!/usr/bin/env sh
# Respaldo de Postgres. Uso: deploy/backup.sh [directorio]  (cron diario recomendado)
# Falla en voz alta: si pg_dump falla no se deja un archivo vacio ni se purgan respaldos viejos.
set -eu
umask 077
DEST="${1:-./backups}"
mkdir -p "$DEST"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FILE="$DEST/vendedores-$STAMP.sql.gz"
TMP="$DEST/.vendedores-$STAMP.sql.tmp"
trap 'rm -f "$TMP" "$TMP.gz"' EXIT
if ! docker compose exec -T db pg_dump -U vendedores -d vendedores --no-owner > "$TMP"; then
  echo "ERROR: pg_dump fallo; no se creo respaldo" >&2
  exit 1
fi
if [ ! -s "$TMP" ]; then
  echo "ERROR: el volcado esta vacio" >&2
  exit 1
fi
gzip -9 -c "$TMP" > "$TMP.gz"
gzip -t "$TMP.gz"
mv "$TMP.gz" "$FILE"
chmod 600 "$FILE"
find "$DEST" -name 'vendedores-*.sql.gz' -mtime +30 -delete
echo "Respaldo creado: $FILE"
