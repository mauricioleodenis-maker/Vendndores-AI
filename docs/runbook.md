# Runbook de operación

## Salud y logs
- Vivo: `GET /healthz`. Listo (DB/Redis): `GET /readyz`.
- Logs: `make logs` (JSON estructurado; sin PII ni secretos).

## Respaldos
- Diario: `sh deploy/backup.sh /ruta/backups` (pg_dump comprimido, retención 30 días; el script sale con error y no purga si el volcado falla o está vacío, así que monitoree el código de salida del cron). Programe en cron y copie fuera del servidor (almacenamiento cifrado).
- Respalde también `.env` (en gestor de secretos) y **`VAI_MASTER_KEYS`**: sin ellas los secretos cifrados son irrecuperables.
- Restaurar:
  ```bash
  docker compose stop web worker
  gunzip -c backup.sql.gz | docker compose exec -T db psql -U vendedores -d vendedores
  docker compose up -d
  ```
- Pruebe una restauración en un entorno aparte cada mes.

## Despliegue y migraciones
`git pull && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`. El servicio `migrate` ejecuta `alembic upgrade head` antes de `web`/`worker`. Antes de migrar, haga un respaldo. Rollback: restaure el respaldo y despliegue la imagen anterior.

## Rotación de claves
- **Clave maestra**: genere `python -m app.cli gen-key`; en `VAI_MASTER_KEYS` agregue la nueva **primero** (`{"v2":"nueva","v1":"vieja"}`), reinicie, ejecute `python -m app.cli rotate-keys`, verifique, y solo después retire la vieja.
- **VAI_SECRET_KEY**: cambiarla cierra todas las sesiones y invalida estados OAuth pendientes; hágalo en horario de baja carga.
- **VAI_PHONE_HASH_KEY**: no rotar sin reindexar teléfonos (rompe búsquedas y supresión). Planifique migración.
- **Twilio / Anthropic / Google**: cree la credencial nueva, actualice `.env`, reinicie `web` y `worker`, revoque la anterior.

## Incidentes
1. **Contener**: `VAI_OUTREACH_ENABLED=false` (kill switch) y/o `VAI_TWILIO_DRY_RUN=true`; reinicie `web` y `worker`. Pause campañas en la UI.
2. **Evaluar**: revise la bitácora de auditoría (`/admin/auditoria`) y los logs; determine datos y negocios afectados.
3. **Credenciales expuestas**: rote de inmediato (sección anterior) y revoque tokens (Google/Twilio).
4. **Datos personales**: si hubo acceso no autorizado, notifique a los titulares y a la SIC dentro de los plazos de la Ley 1581 (reporte de incidente en el RNBD).
5. **Recuperar**: restaure desde respaldo si hay corrupción; confirme `/readyz`.
6. **Postmortem**: documente causa, impacto, línea de tiempo y acciones.

## Problemas comunes
- La app no arranca en prod: revise el mensaje "Configuracion de produccion invalida" (secretos faltantes/débiles).
- Webhook Twilio 403: `VAI_PUBLIC_BASE_URL` no coincide exactamente con la URL configurada en Twilio.
- Jobs no corren: verifique `worker` y `VAI_REDIS_URL`.
- Costos de Places altos: baje `VAI_GOOGLE_PLACES_BUDGET_USD_MONTH`.
