# 03 - Canales (WhatsApp/Voz) y Agenda (Calendar, recordatorios, opt-out)

Dominio: `app/channels/`, `app/booking/`, `app/reminders/` (+ jobs en `app/worker.py`). Se apoya en el modelo de `01-arquitectura-datos.md` (tablas `channel_accounts`, `contacts`, `consents`, `conversations`, `messages`, `appointments`, `scheduled_jobs`, `tenant_secrets`, `suppression_list`, `message_templates`). Aqui solo se listan columnas NUEVAS o cambios. El "cerebro" (Claude, tools) esta en otro plan; este plan define la interfaz que consume (`BookingService`, `Channel`).

## 1. Principios
- Webhook = validar, persistir, encolar, responder 200 en < 1 s. Nada de LLM ni Google dentro del request.
- Idempotencia en todas las fronteras: `MessageSid` (inbound), `dedupe_key` (jobs), `idempotency_key` (reservas), `Idempotency` en eventos Google (`event.id` determinista).
- La fuente de verdad de las citas es NUESTRA DB (constraint anti-solape); Google Calendar es espejo + fuente de "ocupado" externo. Si Google cae, el bot sigue reservando contra la DB.
- Opt-out gana siempre: se chequea en cada envio saliente, no solo al encolar.
- Minimizacion de datos de salud: el evento de calendario lleva solo "Servicio - Nombre" y telefono; sin diagnostico/notas clinicas.

## 2. Layout
```
app/channels/
  base.py            # Protocol Channel, InboundMessage, OutboundMessage, SendResult, ChannelError
  twilio_wa.py       # TwilioWhatsAppChannel: send_text, send_template, parse_inbound, status mapping
  twilio_signature.py# validate_twilio_request(request, tenant_token) -> bool
  router.py          # POST /webhooks/twilio/whatsapp , /webhooks/twilio/status, /webhooks/twilio/voice (stub)
  inbound.py         # ingest_inbound(): resolve tenant, dedupe, persist, enqueue
  processor.py       # process_inbound(message_id): opt-out/STOP, ventana 24h, llama bots.brain, envia respuesta
  window.py          # session_window_open(conversation, now) -> bool ; ventana 24h
  optout.py          # keywords, apply_optout(), apply_optin(), is_suppressed()
  voice/             # base.py (VoiceChannel Protocol), twilio_voice.py (futuro), README de diseño
app/booking/
  models.py          # (modelos en db/models/): calendar_connections, working_hours, time_off, resources, booking_holds
  availability.py    # compute_slots(...)
  service.py         # BookingService: find_slots, hold, book, reschedule, cancel
  calendar_base.py   # Protocol CalendarProvider
  calendar_google.py # GoogleCalendarProvider
  calendar_local.py  # LocalCalendarProvider (fallback integrado, solo DB)
  oauth.py           # router /tenants/{id}/calendar/google/{connect,callback,disconnect}
  sync.py            # push/pull, channel watch, resync
app/reminders/
  scheduler.py       # schedule_for_appointment(), cancel_for_appointment(), enqueue_due()
  jobs.py            # send_reminder, send_followup, send_noshow_followup
  policies.py        # offsets por plan/nicho, quiet hours
```

## 3. Tablas nuevas / cambios (Alembic migration `0003_channels_booking`)
- `channel_accounts` (+): `twilio_subaccount_sid null`, `whatsapp_sender_status enum(sandbox,pending,approved,rejected)`, `voice_enabled bool default false`, `webhook_token_hash` (token aleatorio 32B en la URL del tenant, ver 4.1).
- `webhook_events`: id, provider (`twilio`), provider_sid text, kind (`inbound`,`status`), tenant_id null, received_at, payload_hash, status enum(received,processed,ignored,failed), error. Unique (provider, kind, provider_sid, status_value). Sirve de dedupe + auditoria; sin cuerpo del mensaje.
- `messages` (+): `status enum(received,queued,sent,delivered,read,failed,undelivered)`, `error_code text null`, `template_name null`, `window_exempt bool` (envio con plantilla), `processed_at null`. Estado inbound inicial `received`; `processed_at` marca fin de proceso (permite reintento de huerfanos).
- `conversations` (+): `last_inbound_at timestamptz` (base de la ventana 24h), `locale`, `bot_paused_until null` (handoff humano).
- `contacts` (+): `opt_out_source enum(keyword,manual,complaint) null`, `opt_out_keyword text null`.
- `calendar_connections`: id, tenant_id, provider enum(google,local), status enum(active,needs_reauth,revoked,error), google_calendar_id, google_account_email_enc, scopes text[], sync_token_enc null, watch_channel_id null, watch_resource_id null, watch_expires_at null, last_sync_at, last_error. Refresh token en `tenant_secrets(kind=google_oauth_refresh)`. Unique (tenant_id) WHERE status<>'revoked' (1 calendario activo por tenant en MVP).
- `resources`: id, tenant_id, name (ej. "Silla 1", "Dra. Perez", "Bahia 2"), kind, capacity int default 1, is_active. `appointments` gana `resource_id` y la exclusion pasa a `(tenant_id =, resource_id =, tstzrange(starts_at,ends_at,'[)') &&) WHERE status IN ('pending','confirmed')`. Cada tenant crea un resource "default" al activarse.
- `working_hours`: id, tenant_id, resource_id null, weekday smallint 0-6, start_time, end_time (varias filas por dia = franjas/almuerzo). Se siembran desde `tenant_profiles.hours`.
- `time_off`: id, tenant_id, resource_id null, starts_at, ends_at, reason (festivos Colombia sembrados via lib `holidays` CO, vacaciones).
- `booking_holds`: id, tenant_id, resource_id, contact_id, starts_at, ends_at, expires_at (TTL 10 min), idempotency_key. Exclusion igual a appointments con `WHERE expires_at > now()` no es posible en gist -> los holds viven en `appointments` con `status='pending'` + `hold_expires_at`; un job barre los vencidos a `cancelled`. (Se elimina tabla separada: una sola constraint.)
- `appointments` (+): `hold_expires_at`, `idempotency_key unique (tenant_id,key)`, `google_event_etag`, `sync_status enum(local_only,synced,pending_push,failed)`, `cancelled_by enum(contact,tenant,system)`, `confirmed_by_contact_at`, `reminder_24h_sent_at`, `reminder_2h_sent_at`.
- `scheduled_jobs` (+): `locked_at`, `locked_by`, `last_error`, `next_attempt_at`; index (status, run_at). `kind` enum: `reminder_24h`, `reminder_2h`, `confirm_request`, `followup_unbooked`, `followup_noshow`, `followup_post_visit`, `hold_expire`.
- `message_templates` (scope=tenant) (+): `category enum(utility,marketing,authentication)`, `language`, `variables text[]`, `twilio_content_sid`, `approval_status`. Plantillas base globales por nicho (ver 8).

## 4. WhatsApp inbound (Twilio)
### 4.1 Endpoint
`POST /webhooks/twilio/whatsapp` (form-urlencoded). Alternativa multi-tenant: `POST /webhooks/twilio/whatsapp/{token}`; el token (32B urlsafe) es capa extra de ruteo, NO sustituye la firma. Ruteo primario: campo `To` (`whatsapp:+57...`) -> `channel_accounts.phone_e164`.
Pasos de `ingest_inbound(request) -> Response`:
1. Leer body crudo con limite 64 KB (middleware); parsear form a `dict[str,str]`.
2. Resolver `channel_account` por `To`; no existe -> 404 generico (sin detalle).
3. Descifrar auth token del tenant (`tenant_secrets`) o de plataforma si usa subcuenta/Messaging Service de la agencia.
4. `validate_twilio_request(url, params, signature, token)`: usa `twilio.request_validator.RequestValidator`; URL reconstruida desde `settings.public_base_url` + path (NO desde `Host` del request, por proxy); comparacion constant-time (la lib ya la hace). Falla -> 403 + audit `webhook.invalid_signature` (rate-limited log, sin eco del body). Tambien soportar `X-Twilio-Signature` con `bodySHA256` para JSON (no usado ahora).
5. Dedupe: `INSERT INTO webhook_events ... ON CONFLICT DO NOTHING`; si ya existia -> 200 vacio (retry de Twilio).
6. Upsert `contact` (por `phone_hash` HMAC de `From`), `conversation` abierta (una por contacto+canal; reabre si `closed`); actualizar `last_inbound_at`.
7. Insertar `messages(direction=in, provider_sid=MessageSid, body_enc, status=received)`. `UNIQUE(provider_sid)` es el cinturon: `IntegrityError` -> 200.
8. Medios: `NumMedia>0` -> NO descargar en MVP; el bot responde "por ahora solo puedo leer texto" (evita malware/imagenes de salud). Guardar solo `media_count`. Ubicacion/contactos: ignorar.
9. Encolar `process_inbound(message_id)` en arq (`_job_id=f"in:{message_id}"` = dedupe de cola). Responder `200` con `<Response/>` TwiML vacio (la respuesta va por API de mensajes, no TwiML, para soportar latencia LLM).
10. Fallback sin Redis (dev/test): `BackgroundTasks` ejecuta el mismo `process_inbound`.
Huerfanos: job periodico `reap_unprocessed_inbound` (cada 2 min) re-encola mensajes `received` con `processed_at null` y > 2 min.

### 4.2 Status callback
`POST /webhooks/twilio/status` (mismas validaciones). Actualiza `messages.status` por `MessageSid` con maquina de estados monotona (queued<sent<delivered<read; `failed/undelivered` terminales); ignora retrocesos (callbacks desordenados). Codigos de error relevantes: 63016 (fuera de ventana/plantilla requerida), 63024 (destinatario invalido), 63032 (opt-out de usuario a nivel WhatsApp) -> `63032`/`21610` marcan contacto `opted_out` automaticamente. Outreach (agencia) reutiliza el mismo callback via `outreach_messages.provider_sid`.

### 4.3 Procesamiento `process_inbound(message_id)` (arq)
1. Cargar mensaje + contacto + conversacion (sesion con `set_tenant_context`). Si `processed_at` ya set -> return.
2. **Opt-out/in**: `optout.classify(body) -> OptOut|OptIn|None` (normaliza: lower, sin tildes, trim, palabra completa). Keywords STOP: `stop, parar, para, baja, cancelar suscripcion, no mas, no quiero mensajes, desuscribir, salir, unsubscribe`; START: `start, iniciar, activar, suscribir, si quiero`. Ojo: "cancelar" solo NO es opt-out (puede cancelar cita) -> lo maneja el bot; "para" solo cuando es el mensaje completo. STOP -> `apply_optout` y respuesta unica de confirmacion (permitida, es respuesta a mensaje del usuario) y NO pasar al LLM.
3. Si `contact.opted_out` y llega mensaje normal: se atiende conversacion entrante (el usuario escribio, ventana abierta) pero NO se programan recordatorios/follow-ups; responder solo a lo que pregunta. (Decision: opt-out = marketing/proactivo; atencion reactiva sigue. Documentar en politica.)
4. Rate limit por contacto (ej. 20 msg/10 min) y por tenant; excedido -> un aviso y silencio. Tope mensual del plan (`usage_counters`) -> mensaje de cortesia + alerta al owner.
5. `bot_paused_until` activo (handoff) -> notificar al humano, no responder.
6. Llamar `bots.brain.respond(tenant_id, conversation_id, history, tools=BookingTools)`; tools inyectan `tenant_id` server-side. Guardrails de tema (Meta policy) viven en brain.
7. Enviar respuesta con `channel.send_text` (dentro de ventana por definicion), persistir `messages(out)`, set `processed_at`. Errores del LLM -> mensaje de disculpa fijo + handoff + alerta; reintento arq max 3 con backoff exponencial; tras agotar, marcar `failed` y crear tarea de revision en dashboard.
8. Escribir "typing"/read receipt: Twilio WhatsApp no lo expone de forma fiable -> omitir.

### 4.4 Ventana de 24 h y plantillas
- `window.session_window_open(conv, now) = now - conv.last_inbound_at < 24h` (usar 23h30m como margen). El Protocol `Channel.send` recibe `OutboundMessage(kind='text'|'template')`; `twilio_wa.send` consulta `window.py` y, si cerrada y `kind='text'`, lanza `WindowClosedError` (NUNCA intenta; Meta cobra/falla). El caller (reminders) debe usar `send_template`.
- `send_template(to, content_sid, variables: dict[str,str])` usa Twilio Content API (`client.messages.create(content_sid=..., content_variables=json.dumps(vars), messaging_service_sid|from_=...)`).
- Categorias: recordatorios/confirmaciones = `utility` (aprobables, fuera de ventana); follow-ups de reactivacion tipo promocional = `marketing` (requieren consentimiento `marketing` en `consents` y cuestan mas). Si la conversacion esta fuera de ventana y el contacto no tiene consentimiento del proposito -> no se envia, se registra `skipped_no_consent`.
- Registro y aprobacion: `POST /tenants/{id}/templates/{tid}/submit` crea Content + solicita aprobacion WhatsApp (`ContentAndApprovals`); estado se sincroniza por job `sync_template_status` cada hora. Un tenant sin plantilla aprobada solo hace recordatorios si el cliente respondio en 24h (degradacion documentada en UI).
- Numero: cada tenant usa su numero WhatsApp Business (sender de Twilio, perfil verificado por Meta). Sandbox solo en dev (`whatsapp_sender_status=sandbox`, join code).

### 4.5 Outbound
`TwilioWhatsAppChannel.send_text(account, to_phone, body, *, conversation_id, idempotency_key) -> SendResult`:
- Pre-checks en orden: `is_suppressed(phone_hash)` (suppression_list + contact.opted_out segun proposito) -> `SuppressedError`; ventana; rate limit de salida del numero (token bucket Redis, defecto 1 msg/s por sender, 80 msg/s limite Twilio); longitud <= 1600 chars (dividir por parrafos).
- Cliente `twilio.rest.Client` es sincrono: ejecutar en `asyncio.to_thread` o usar `httpx` directo contra `https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json` con Basic auth (preferido: async nativo, timeout 10 s, 2 reintentos solo en 5xx/timeout con el mismo `idempotency_key`; 4xx no se reintenta). Header `I-Twilio-Idempotency-Token` si disponible; si no, dedupe propio por `messages.idempotency_key`.
- `StatusCallback={public_base_url}/webhooks/twilio/status`.
- Persistir `messages(out, status=queued, provider_sid)` en la misma transaccion que el resultado de la API (si la API OK y commit falla -> job de conciliacion por status callback que crea el registro faltante marcado `reconciled`).

## 5. Voz (Twilio Voice, fase posterior) - solo interfaz ahora
```python
class VoiceChannel(Protocol):
    async def handle_incoming_call(self, call: IncomingCall) -> VoiceInstruction: ...  # TwiML / Media Streams
    async def place_call(self, account, to_phone, purpose, *, idempotency_key) -> CallResult: ...
    async def on_transcript(self, call_id, utterance: str) -> BotReply: ...  # misma BotBrain
```
- Ruta reservada `POST /webhooks/twilio/voice` (devuelve `<Say>` "Este canal aun no esta disponible, escribanos por WhatsApp" + `<Hangup/>`; firma validada igual). Flag por plan `limits.voice_enabled` y `channel_accounts.voice_enabled`.
- Diseno futuro: Twilio Media Streams (WebSocket `/ws/voice/{call_sid}`) -> STT -> `bots.brain.respond(channel='voice')` -> TTS (es-CO). Mismo `BookingService` y mismas tools; el brain recibe `channel` para respuestas cortas. Grabacion de llamadas desactivada por defecto (Ley 1581: consentimiento + aviso al inicio). Tabla futura `calls` (id, tenant_id, call_sid unique, direction, duration_s, status, transcript_enc).
- `Channel` y `VoiceChannel` comparten `InboundMessage` normalizado (`tenant_id, contact_phone, text, channel, provider_sid, received_at`) para que `processor` no dependa de Twilio.

## 6. Opt-out y consentimiento
- `optout.apply_optout(contact, source, keyword)`: transaccion que (a) `contacts.opted_out=true, opted_out_at`, (b) `consents.revoked_at` para `recordatorios` y `marketing` (no `atencion`), (c) inserta `suppression_list(phone_hash, reason=opt_out)` (cubre tambien outreach de la agencia: el mismo humano que dice STOP a un tenant NO es suprimido globalmente salvo el pitch de agencia; por eso `suppression_list` gana columna `scope enum(global,tenant,agency)` + `tenant_id null`), (d) cancela `scheduled_jobs` pendientes del contacto (`status='cancelled'`), (e) audit `contact.optout`.
- `is_suppressed(phone_hash, purpose, tenant_id)` consulta suppression_list + contacto; usado por `channel.send`, `scheduler`, y outreach. Cache Redis 60 s con invalidacion en apply_optout.
- Opt-in explicito: primer mensaje del contacto = base `atencion` (aviso de privacidad en el primer saludo con link a politica: "Tratamos tus datos para gestionar tu cita. Responde STOP para no recibir recordatorios"). `recordatorios` se consienten al reservar (el bot lo pregunta, `consents.evidence` guarda mensaje+ts). `marketing` solo con "si" explicito; nunca inferido.
- Complaint/bloqueo: error 63032 / usuario bloquea -> `reason=complaint`.
- Pantalla dashboard "Contactos": ver estado de consentimiento, exportar/borrar (DSAR, ver plan 01 s.9).

## 7. Agenda
### 7.1 Protocol
```python
class CalendarProvider(Protocol):
    async def busy_intervals(self, conn, start: datetime, end: datetime) -> list[Interval]: ...
    async def create_event(self, conn, appt: Appointment) -> ExternalEvent: ...
    async def update_event(self, conn, appt: Appointment) -> ExternalEvent: ...
    async def delete_event(self, conn, appt: Appointment) -> None: ...
    async def sync_changes(self, conn) -> list[ExternalChange]: ...
```
Fabrica `get_provider(tenant_id)` -> `GoogleCalendarProvider` si hay `calendar_connections.active(google)`, si no `LocalCalendarProvider` (busy = [], eventos solo en DB; el dashboard `/citas` muestra vista semana/dia con HTMX y permite crear/mover/cancelar manualmente).

### 7.2 OAuth Google por tenant (`booking/oauth.py`)
- Scopes minimos: `https://www.googleapis.com/auth/calendar.events` y `calendar.freebusy`... (MVP: `calendar.events` + `calendar.readonly` para listar calendarios). NO usar scope `calendar` completo. App en modo produccion con verificacion de scopes sensibles (planificar semanas de revision; mientras tanto "Testing" con usuarios de prueba <= 100).
- Flujo (Authorization Code + PKCE + `access_type=offline`, `prompt=consent`): `GET /tenants/{id}/calendar/google/connect` (requiere rol admin y CSRF) genera `state` firmado (itsdangerous, 10 min, contiene tenant_id+user_id+nonce guardado en sesion) -> redirect a `https://accounts.google.com/o/oauth2/v2/auth`. `GET /calendar/google/callback`: valida `state` y nonce, intercambia `code` en `https://oauth2.googleapis.com/token` (httpx), valida `id_token` (email), guarda refresh token cifrado en `tenant_secrets`. Redirect URI fijo en settings (`google_oauth_redirect_uri`).
- El dueno del negocio puede autorizar con un link de un solo uso (`/onboarding/calendar/{signed_token}`, TTL 72 h) enviado por la agencia, sin cuenta en el dashboard.
- Tras conectar: elegir calendario destino (`calendarList.list`) o crear secundario "Citas - {Negocio}" (`calendars.insert`) recomendado para no mezclar eventos personales; busy se consulta contra TODOS los calendarios que el dueno marque ("considerar ocupado").
- Tokens: access token en cache Redis (TTL = expires_in - 60 s); refresh con `invalid_grant` -> `status=needs_reauth`, alerta en dashboard y al owner de agencia, y el bot degrada a calendario local (no reserva a ciegas: ver 7.5).
- Revocar: `/calendar/google/disconnect` llama `https://oauth2.googleapis.com/revoke`, borra secreto, `status=revoked`, borra watch channel. Eventos ya creados permanecen.

### 7.3 Disponibilidad (`availability.compute_slots`)
```python
async def compute_slots(session, tenant_id, service_id, date_from: date, date_to: date, *,
                        resource_id: UUID | None = None, now: datetime | None = None,
                        max_slots: int = 12) -> list[Slot]
```
Algoritmo (zona `tenant.timezone`, por defecto America/Bogota; todo se guarda UTC): (1) ventana = `working_hours` menos `time_off`/festivos CO; (2) restar `appointments` activas (`pending` no vencidas, `confirmed`) del resource; (3) restar `provider.busy_intervals` (Google `freebusy.query`, cache 30-60 s por tenant+rango, invalidada en push de webhook); (4) aplicar reglas de `bot_configs.booking_rules`: duracion = `service.duration_min`, `buffer_min`, `min_notice_h` (def. 2 h), `max_advance_days` (def. 30), `slot_step_min` (def. 15-30), citas simultaneas por `resource.capacity` (restaurante: mesas -> capacity); (5) devolver como maximo N slots repartidos (manana/tarde/dias distintos) para que el bot ofrezca 3-4 opciones. DST no aplica en Colombia pero usar `zoneinfo` siempre. Funcion pura sobre intervalos (`merge_intervals`, `subtract_intervals`) para TDD.

### 7.4 Reserva sin doble booking (`BookingService`)
```python
async def hold(self, tenant_id, contact_id, service_id, starts_at, *, idempotency_key) -> Appointment  # pending, hold_expires_at=+10min
async def confirm(self, tenant_id, appointment_id, *, idempotency_key) -> Appointment                  # confirmed + push a Google
async def reschedule(self, tenant_id, appointment_id, new_starts_at, *, by) -> Appointment
async def cancel(self, tenant_id, appointment_id, *, by, reason=None) -> Appointment
```
- Garantia primaria: constraint `EXCLUDE USING gist` (requiere `CREATE EXTENSION btree_gist`) -> `IntegrityError`/`ExclusionViolation` -> `SlotTakenError` y el bot ofrece nuevos slots (re-ejecuta `compute_slots`). NO se confia en "chequear y luego insertar".
- SQLite (tests): sin exclusion; `BookingService` hace `SELECT ... overlap` dentro de `BEGIN IMMEDIATE` y la suite de concurrencia/constraint corre en Postgres real (marcador `@pytest.mark.pg`).
- Orden en `confirm`: (1) tx DB: pasar a `confirmed`, `sync_status=pending_push`; commit. (2) Despues, `provider.busy_intervals` de ultimo momento NO bloquea (evita 2PC), pero si Google reporta evento solapado externo creado entre medias, se crea igual y se marca conflicto en dashboard ("conflicto con evento externo") -> notificacion al negocio. Para minimizar: antes de `hold` se consulta freebusy fresco (sin cache) para ese slot exacto.
- (3) Job `push_to_google(appointment_id)`: `events.insert` con `id` determinista (base32hex de uuid sin guiones -> permite reintentos idempotentes; 409 = ya existe), `summary="{servicio} - {nombre}"`, `description` con telefono y link a la cita en dashboard (sin datos clinicos), `reminders.useDefault=false`, `extendedProperties.private={appt_id, tenant_id}`, `sendUpdates="none"`. Fallo -> `sync_status=failed`, reintento backoff (1,5,15,60 min, 5 intentos), alerta.
- Cancelacion/reprogramacion: libera el rango en la misma tx (status `cancelled` sale de la constraint), luego `events.delete/patch` por `google_event_id`. Politica por nicho: `min_cancel_notice_h` (ej. dentista 4 h) -> si menor, el bot deriva a humano.
- Idempotencia: `UNIQUE(tenant_id, idempotency_key)`; la key la deriva el tool-call (`hash(conversation_id, message_id, accion)`) para que un reintento del LLM no cree dos citas.
- Un contacto no puede tener > N citas futuras activas (def. 2) ni citas solapadas entre si.
- Holds: job `hold_expire` por cita pendiente (`run_at=hold_expires_at`) + barrido cada minuto.

### 7.5 Sincronizacion desde Google (cambios externos)
- `events.watch` (push notifications) en `POST /webhooks/google/calendar` con header `X-Goog-Channel-Token` (secreto por conexion, comparar constant-time) y `X-Goog-Resource-State`; solo dispara `sync_calendar(tenant_id)` en cola. Canal expira (~7 dias): job diario renueva (`watch_expires_at < now+2d`). Si no hay URL publica (dev) -> polling cada 5 min.
- `sync_calendar`: `events.list(syncToken=...)`; 410 GONE -> full resync. Evento nuestro movido/borrado por el dueno desde Google (`extendedProperties.private.appt_id`) -> actualiza/cancela `appointment` y notifica al contacto con plantilla de reprogramacion. Eventos ajenos -> solo cuentan como "ocupado".
- Degradacion: con `needs_reauth` o Google caido (>3 fallos), `BookingService` opera contra DB y marca citas `pending_push`; el bot dice "tu cita queda registrada y el negocio la confirmara" solo si la politica del tenant lo permite (`booking_rules.allow_unsynced=true`); si no, ofrece handoff. Se retoma el push al recuperar.
- Cuotas: Calendar API ~ 1M req/dia por proyecto; usar `fields=` parciales, backoff exponencial ante 403 `rateLimitExceeded`/429.

## 8. Recordatorios y follow-ups
`reminders/scheduler.py`:
- `schedule_for_appointment(appt)` inserta filas `scheduled_jobs` con `dedupe_key=f"{kind}:{appt.id}:{starts_at.isoformat()}"` (ON CONFLICT DO NOTHING; al reprogramar se cancelan las viejas y se crean nuevas). Defaults por plan/nicho en `policies.py`: `reminder_24h` (todos), `reminder_2h` (Pro+), `confirm_request` (24 h antes, pide "responde 1 para confirmar, 2 para reprogramar"; Pro+), `followup_post_visit` (Premium: 24 h despues, "como te fue?" + pedir resena Google), `followup_noshow` (2 h despues si `no_show`: ofrecer reagendar), `followup_unbooked` (conversacion donde se cotizo y no se agendo: +24 h dentro de ventana (texto libre) y +3 dias solo con plantilla utility y consentimiento; maximo 2 intentos por conversacion).
- Ejecutor: cron arq cada minuto `enqueue_due()` -> `SELECT ... FROM scheduled_jobs WHERE status='pending' AND run_at<=now() ORDER BY run_at LIMIT 200 FOR UPDATE SKIP LOCKED`; marca `locked_at/by`, encola `run_job(job_id)`. Lock vencido (>5 min) se reclama. Reintentos max 3. Esto da exactly-once-ish: dedupe_key + `messages.idempotency_key` evitan doble envio.
- `send_reminder(job_id)` re-verifica al ejecutar: cita sigue `confirmed`, no paso la hora, contacto no `opted_out`, dentro de quiet hours del tenant (08:00-20:00 `America/Bogota`; fuera -> se pospone a la ventana, nunca de madrugada), no se envio ya (`reminder_*_sent_at`). Elige canal: dentro de ventana -> texto libre con plantilla del tenant; si no -> `send_template`. Sin plantilla aprobada -> `skipped_no_template` + alerta.
- Respuestas al recordatorio ("1", "confirmo", "cancelar") entran por el flujo inbound normal: el brain tiene el contexto de la cita proxima en el prompt (`upcoming_appointments`) y usa tools `confirm_attendance`/`cancel`/`reschedule`.
- Plantillas base (Content API, es-CO, utility) sembradas por nicho en `plans/niche_templates/*.yaml` (`message_templates`): `recordatorio_cita` ("Hola {{1}}, te recordamos tu cita de {{2}} el {{3}} a las {{4}} en {{5}}. Responde 1 para confirmar o 2 para reprogramar."), `confirmacion_cita`, `reprogramacion`, `followup_no_asistio`, `post_visita`, `confirmar_reserva_mesa` (restaurante), `vehiculo_listo` (taller). Sin datos clinicos en variables (usar nombre del servicio generico, no diagnostico).
- Limite de costo: contador por tenant/mes de plantillas enviadas en `usage_counters` (anadir columna `templates_sent`) y tope por plan.

## 9. Seguridad y cumplimiento (alineado con `security-review`)
- Firma Twilio obligatoria en TODOS los webhooks; en `dev` se puede desactivar solo con `VAI_ENV=dev` + flag explicito, imposible en prod (validador de Settings). Webhook Google: token de canal + dedupe por `X-Goog-Message-Number`.
- Secrets: tokens Twilio/Google solo en `tenant_secrets` cifrados; refresh tokens nunca en logs; redaccion de `From/To` en logs (ultimos 4).
- OAuth: `state` firmado + nonce de sesion (anti-CSRF), PKCE, redirect URI exacto, scopes minimos, tokens por tenant sin cruce (la conexion se busca por `tenant_id` del state, verificado contra el usuario).
- Aislamiento: todas las consultas via `TenantScopedRepo`; workers fijan `set_tenant_context` al iniciar cada job; test que verifica que `compute_slots(tenant A)` ignora citas de tenant B.
- Inyeccion de prompt via mensajes: tools reciben argumentos tipados validados (service_id perteneciente al tenant, fecha dentro de `compute_slots` ofrecidos); `book` rechaza `starts_at` que no este en la lista de slots recientes de la conversacion (cache `offered_slots` 15 min) -> el LLM no puede inventar horarios.
- Abuso: rate limit por contacto/tenant/IP del webhook (solo Twilio IPs no es fiable; firma basta), limite de tamano, rechazo de `From` no `whatsapp:`.
- Salud (Ley 1581): datos sensibles minimizados; aviso/consentimiento en primer contacto; mensajes cifrados `body_enc` con retencion `purge_after`; eventos de calendario sin diagnosticos; transferencias a Google/Twilio/Anthropic declaradas en la politica de tratamiento (transferencia internacional, art. 26 Ley 1581).
- Meta: el bot rechaza temas fuera del negocio (en brain), plantillas veraces, sin spam: tope diario y quiet hours.

## 10. Endpoints (resumen)
| Metodo | Ruta | Auth | Nota |
|---|---|---|---|
| POST | /webhooks/twilio/whatsapp | firma Twilio | inbound |
| POST | /webhooks/twilio/status | firma Twilio | estados |
| POST | /webhooks/twilio/voice | firma Twilio | stub |
| POST | /webhooks/google/calendar | channel token | dispara sync |
| GET | /tenants/{id}/calendar | sesion admin | estado conexion |
| GET | /tenants/{id}/calendar/google/connect, /callback | sesion + state | OAuth |
| POST | /tenants/{id}/calendar/google/disconnect | sesion + CSRF | revoca |
| GET/POST | /tenants/{id}/citas, /citas/{aid}/(mover\|cancelar\|confirmar) | sesion | calendario local (HTMX) |
| GET/POST | /tenants/{id}/horarios, /recursos, /bloqueos | sesion | working_hours/resources/time_off |
| GET/POST | /tenants/{id}/plantillas, /plantillas/{tid}/submit | sesion | plantillas WA |
| GET | /tenants/{id}/contactos/{cid} ; POST .../optout | sesion | consentimiento |
| GET | /health/channels | interno | estado colas, ultimo webhook |

## 11. Config nueva (Settings)
`twilio_validate_signature=True`, `wa_session_window_h=24`, `wa_send_rate_per_s=1`, `quiet_hours=("08:00","20:00")`, `hold_ttl_min=10`, `google_oauth_redirect_uri`, `google_watch_base_url`, `reminder_offsets_default`, `max_followups_per_conversation=2`, `max_active_appointments_per_contact=2`.

## 12. Tests (TDD; objetivo >= 80%, 100% en rutas de dinero/seguridad)
- Unit: `test_twilio_signature` (valida/invalida/URL proxied/params extra); `test_window` (23h59/24h01); `test_optout_classify` (STOP, "para", "no me para bolas" no matchea, tildes/mayusculas, "cancelar" no es opt-out); `test_availability` (intervalos puros: solapes, buffers, min_notice, festivos, almuerzo, capacity); `test_scheduler_policies`; `test_status_state_machine`.
- Integracion (httpx AsyncClient + SQLite): webhook inbound happy path, firma invalida 403, tenant desconocido 404, replay mismo `MessageSid` -> un solo `messages`, encola una vez; STOP -> suppression + jobs cancelados; status callbacks desordenados.
- Postgres real (`pg` marker): 50 `hold` concurrentes al mismo slot -> exactamente 1 exito (asyncio.gather); RLS impide ver citas de otro tenant; `FOR UPDATE SKIP LOCKED` con 2 workers no duplica recordatorios.
- Google/Twilio: mocks con `respx` (httpx): refresh token `invalid_grant` -> `needs_reauth`; 409 en insert idempotente; 410 syncToken -> full resync; 429 backoff; Twilio 63016 -> sin reintento, marca `window_closed`.
- Seguridad: log no contiene token ni telefono completo; `state` OAuth manipulado -> 400; `book` con slot no ofrecido -> rechazado.
- Fakes: `FakeChannel`, `FakeCalendarProvider`, `FrozenClock` (`core/clock.py`) para tiempo determinista.

## 13. Hitos
1. `Channel` base + inbound webhook + firma + dedupe + processor con brain fake (sandbox Twilio).
2. Opt-out/consentimiento + suppression + status callbacks.
3. Agenda local: working_hours/resources, `compute_slots`, `BookingService` + constraint, vista `/citas`.
4. Recordatorios (scheduler arq) con ventana 24h y plantillas Content API.
5. Google Calendar: OAuth, push, freebusy, watch/sync, degradacion.
6. Follow-ups (unbooked/no-show/post-visita) por plan.
7. Voz: stub + interfaz; implementacion real despues.

## 14. Riesgos / decisiones abiertas
- Aprobacion de plantillas y del sender WhatsApp por Meta toma dias: iniciar en onboarding del primer cliente. Verificacion de scopes Google tarda semanas: usar modo Testing/cuentas de prueba al inicio, o usar calendario local como default y Google como "Pro".
- Un numero WhatsApp por tenant (costo/gestion) vs numero compartido con ruteo por palabra clave: se asume 1 numero por tenant (mas limpio para Ley 1581 y marca).
- Modelo de resource unico en MVP; multi-profesional (dentistas varios) via `resources` ya soportado en esquema, UI en fase 2.
- Opt-out = solo proactivo (decision s.4.3.3): confirmar con asesor legal.
- Costo conversaciones WhatsApp iniciadas por el negocio (utility/marketing) debe reflejarse en el precio del plan o como excedente.
