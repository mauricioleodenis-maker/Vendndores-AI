# 10 - Referencia Google Calendar por tenant (soporte)

Estado: referencia tecnica para planeacion y builders. Complementa docs/plan/03-canales-agenda.md.
Zona horaria fija: America/Bogota (UTC-5, sin DST). Idioma de UI: espanol.

## 1. OAuth2 (flujo web por tenant)

Scopes minimos:
- `https://www.googleapis.com/auth/calendar.freebusy` - consultar disponibilidad (solo lectura de busy/free, sin titulos).
- `https://www.googleapis.com/auth/calendar.events` - crear/editar/borrar eventos en calendarios que el tenant elige.
- NO pedir `https://www.googleapis.com/auth/calendar` (acceso total) ni `calendar.readonly` si no se usa.
- Scope OpenID opcional: `openid email` solo para identificar la cuenta conectada (sin datos de pacientes).

Parametros del flujo:
- `access_type=offline` (para recibir refresh_token) y `prompt=consent` (fuerza refresh_token en re-conexion).
- `include_granted_scopes=true` y `state` firmado (HMAC) con `tenant_id` + nonce, validado en el callback.
- PKCE recomendado aunque sea confidential client.
- Redirect URI fija por entorno, registrada en Google Cloud Console; nunca construida desde el header Host.
- Verificacion de la app de Google: los scopes `calendar.events`/`calendar.freebusy` son sensibles; planear revision de Google antes de produccion.

## 2. Almacenamiento y refresh de tokens

- Tabla `calendar_connections` (una fila por tenant + calendario):
  `id, tenant_id, provider='google', external_account_email, calendar_id, scopes, refresh_token_enc, access_token_enc, access_token_expires_at, status ('active'|'revoked'|'error'), created_at, updated_at`.
- Cifrado de `refresh_token` y `access_token` en reposo con AES-GCM (Fernet o `cryptography`), clave en variable de entorno/secret manager, no en BD ni en repo. Rotacion de clave con `key_id` en la fila.
- Nunca loguear tokens. Nunca devolver tokens en respuestas HTML/HTMX ni en JSON de API.
- Refresh: si `access_token_expires_at - now < 60s`, refrescar con `google.oauth2.credentials.Credentials.refresh(Request())` usando `refresh_token`. Guardar nuevo access token y expiracion en UTC.
- Concurrencia: refrescar con `SELECT ... FOR UPDATE` sobre la fila para evitar dos refrescos simultaneos en el mismo tenant.
- Errores `invalid_grant` (revocado o expirado): marcar `status='revoked'`, desactivar agenda automatica del tenant, notificar en el panel admin para re-conectar.
- Desconexion del tenant: llamar `https://oauth2.googleapis.com/revoke?token=...` y borrar tokens.
- Multi-tenant: toda consulta filtra por `tenant_id`; el callback valida que el `state` corresponda al tenant de la sesion.

## 3. Freebusy (disponibilidad)

Endpoint: `POST https://www.googleapis.com/calendar/v3/freeBusy`

```json
{
  "timeMin": "2026-10-12T08:00:00-05:00",
  "timeMax": "2026-10-12T18:00:00-05:00",
  "timeZone": "America/Bogota",
  "items": [{"id": "<calendar_id>"}]
}
```

- Enviar siempre `timeMin`/`timeMax` en RFC3339 con offset `-05:00` o en UTC `Z`; la respuesta trae `calendars[calendar_id].busy` como lista de `{start, end}` en UTC.
- Limite: rango maximo recomendado de 1-2 semanas por llamada; paginar por dia para agenda de la semana.
- Cachear resultados por 30-60 s por (tenant, calendario, dia) para no agotar cuota; invalidar al crear un evento.
- Calcular huecos en Python: slots de N minutos (duracion del servicio) dentro de horario laboral del tenant, menos `busy`, menos buffer configurable.
- Si el calendario no es accesible, la respuesta trae `errors` por calendario: tratar como "sin disponibilidad confiable" y caer al calendario interno (seccion 7).

## 4. Insert de evento con idempotencia

Endpoint: `POST https://www.googleapis.com/calendar/v3/calendars/{calendarId}/events?sendUpdates=none`

```json
{
  "id": "<idempotency_key base32hex>",
  "summary": "Cita: Juan Perez - Limpieza dental",
  "description": "Canal: WhatsApp. Lead: <lead_id>. Sin datos clinicos.",
  "start": {"dateTime": "2026-10-12T09:00:00", "timeZone": "America/Bogota"},
  "end":   {"dateTime": "2026-10-12T09:30:00", "timeZone": "America/Bogota"},
  "reminders": {"useDefault": false, "overrides": [{"method": "popup", "minutes": 60}]}
}
```

Reglas de idempotencia:
- `id` del evento = clave determinista derivada de `tenant_id + appointment_id` (hex minusculas 5-1024 chars, alfabeto `[a-v0-9]`, base32hex recomendado). Reintentos del mismo turno usan el mismo id.
- Si Google responde `409 Conflict` (id ya existe): hacer `GET .../events/{id}` y devolver ese evento como exito (idempotente).
- Guardar en BD `appointments.gcal_event_id` y `appointments.status` antes de confirmar al cliente por WhatsApp; si la llamada falla por red, reintentar con backoff exponencial (0.5 s, 1 s, 2 s, max 3) con el mismo id.
- Header `X-Idempotency`: no existe en Calendar API; la idempotencia depende del `id` del evento.
- No incluir datos de salud (diagnostico, tratamiento) en `summary`/`description`: Ley 1581. Solo nombre o alias y tipo de servicio, minimo necesario.
- Campo `attendees`: no usar (evita envio de invitaciones con datos del paciente).

## 5. Zona horaria America/Bogota

- Colombia no tiene horario de verano: offset fijo `-05:00`. Usar `zoneinfo.ZoneInfo("America/Bogota")` (Python 3.12) y no offsets hardcodeados en codigo de negocio.
- Guardar siempre en BD `timestamptz` en UTC; convertir a Bogota solo para UI y para construir `dateTime` de Google con `timeZone` explicito.
- Horario laboral y festivos colombianos se guardan por tenant (tabla `business_hours`, `holidays`), no en el calendario de Google.
- Al parsear respuestas de Google: `dateTime` trae offset; `date` (eventos de dia completo) no tiene hora: no usarlos para disponibilidad sin tratarlos como bloqueo de todo el dia.

## 6. Double-booking (carreras)

Riesgo: dos conversaciones de WhatsApp reservan el mismo slot a la vez, o Google tiene un evento creado manualmente no reflejado.

Capas de defensa:
1. Reserva local atomica en Postgres antes de llamar a Google:
   - Tabla `slot_reservations(tenant_id, calendar_id, starts_at, ends_at, appointment_id, status, expires_at)` con `EXCLUDE USING gist (tenant_id WITH =, calendar_id WITH =, tstzrange(starts_at, ends_at) WITH &&) WHERE (status IN ('held','confirmed'))`.
   - Requiere extension `btree_gist`. Un conflicto -> `IntegrityError` -> el slot ya no esta disponible.
2. Hold temporal: estado `held` con `expires_at = now() + 5 min` mientras el paciente confirma. Job de limpieza libera holds vencidos.
3. Flujo:
   a. Freebusy + BD local -> slot candidato.
   b. Insert en `slot_reservations` como `held` (falla si hay choque).
   c. Insert de evento en Google con id idempotente.
   d. Actualizar reserva a `confirmed` y guardar `gcal_event_id`.
   e. Si (c) falla de forma definitiva: borrar hold y avisar al paciente de reintentar.
4. Transaccion: el paso (b) y la creacion de la cita en `appointments` van en la misma transaccion (`SELECT ... FOR UPDATE` no es necesario si se usa el EXCLUDE constraint).
5. Verificacion previa a confirmar: volver a consultar freebusy justo antes de (c); si aparece busy inesperado, liberar hold y ofrecer otro slot.
6. Sincronizacion inversa: webhook de Google (`events.watch`, canal con `token` firmado por tenant) o polling cada 5-10 min para detectar eventos borrados/movidos manualmente y liberar o marcar conflicto.
7. Concurrencia del bot: un solo consumidor por conversacion (lock por `conversation_id` con `pg_advisory_xact_lock`) para no procesar dos mensajes del mismo paciente en paralelo.

## 7. Calendario interno de respaldo (fallback)

Cuando el tenant no conecta Google, o el token esta revocado, el agendamiento sigue funcionando con calendario propio.

Modelo:
- Tabla `internal_calendar_events`: `id, tenant_id, starts_at (timestamptz), ends_at, service_code, status ('held'|'confirmed'|'cancelled'|'no_show'|'done'), patient_name, patient_phone, source ('bot'|'admin'|'google_sync'), gcal_event_id NULL, created_at`.
- Misma restriccion EXCLUDE de la seccion 6 sobre `(tenant_id, tstzrange)` para `status IN ('held','confirmed')`.
- Disponibilidad = horario laboral del tenant - festivos - eventos internos - eventos Google (si conectado, via freebusy).
- Vista admin en Jinja2+HTMX: agenda por dia/semana, con bloqueo manual y cancelacion; HTMX para refrescar un fragmento de la agenda.
- Modo mixto: si Google esta activo, el evento se crea en Google y se espeja en la tabla interna (`gcal_event_id` no nulo). Si Google falla, la cita queda interna y se marca `sync_pending` para reintento.
- Migracion: al conectar Google despues, importar eventos futuros por freebusy y crear bloqueos internos; no duplicar si ya hay `gcal_event_id`.
- Recordatorios: job programado (cron/worker) que envia WhatsApp 24 h y 2 h antes, usando la tabla interna como fuente unica de verdad.

## 8. Checklist para builders

- [ ] Scopes exactos: `calendar.freebusy` y `calendar.events`.
- [ ] `access_type=offline`, `prompt=consent`, `state` firmado por tenant.
- [ ] Tokens cifrados; refresh con lock por fila; `invalid_grant` -> status `revoked`.
- [ ] Freebusy con `timeZone=America/Bogota`; cache 30-60 s.
- [ ] `event.id` determinista; manejo de 409; reintentos con backoff.
- [ ] Exclusion constraint en Postgres (`btree_gist`) para doble reserva.
- [ ] Hold con expiracion y job de limpieza.
- [ ] Eventos sin datos clinicos; sin `attendees`.
- [ ] Calendario interno como fallback con la misma regla de no-solapamiento.
- [ ] Tests: 409 idempotente, dos reservas concurrentes (solo una gana), token revocado, cambio de dia sin DST.
