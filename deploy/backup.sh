#!/usr/bin/env sh
# Respaldo de Postgres cifrable. Uso: deploy/backup.sh [directorio]  (cron diario recomendado)
set -eu
DEST="${1:-./backups}"
mkdir -p "$DEST"
FILE="$DEST/vendedores-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
docker compose exec -T db pg_dump -U vendedores -d vendedores --no-owner | gzip -9 > "$FILE"
chmod 600 "$FILE"
find "$DEST" -name 'vendedores-*.sql.gz' -mtime +30 -delete
echo "Respaldo creado: $FILE"
