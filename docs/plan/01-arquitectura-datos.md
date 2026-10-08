# 01 - Arquitectura, modelo de datos, auth, seguridad, deploy

Dominio: arquitectura general, layout, DB multi-tenant, auth admin, config/secretos, cifrado, auditoria, Ley 1581, Docker, testing.
Stack fijo: Python 3.12, FastAPI, SQLAlchemy 2 async + Alembic, PostgreSQL (SQLite en tests), Jinja2+HTMX (UI en espanol), Anthropic (`claude-sonnet-5-5`, configurable), Twilio WhatsApp, Google Calendar, Google Places API, httpx, pydantic-settings, arq/Redis, pytest, Docker compose.

## 1. Principios
- Monolito modular (un proceso web + un worker arq). Routers delgados -> services (transaccion) -> repositories/modelos. Sin logica de negocio en routers.
- Multi-tenant por columna `tenant_id` (shared schema). Todo dato de negocio cliente lleva `tenant_id`. Aislamiento forzado en capa de repositorio + RLS de Postgres como defensa en profundidad.
- Dos "mundos": (a) **Agencia** (owner/admins, leads, outreach, planes, bot factory) y (b) **Tenant** (negocio cliente: bot, contactos, citas). El "tenant" es el negocio cliente; la agencia es el operador de la plataforma (no tiene tenant_id en leads/outreach).
- Fail closed: webhook sin firma valida -> 403; tenant desconocido -> 404 sin filtrar info.
- Datos de salud: minimizacion. El bot NO pide ni almacena diagnosticos; solo nombre, telefono, servicio, fecha. Mensajes crudos con retencion limitada.

## 2. Layout del repo
```
Vendndores-AI/
  pyproject.toml  alembic.ini  Dockerfile  docker-compose.yml  .env.example  Makefile
  alembic/ versions/ env.py (async)
  docs/plan/
  app/
    main.py                 # create_app(), lifespan, middlewares, routers
    core/
      config.py             # Settings (pydantic-settings)
      security.py           # hash pw (argon2), tokens, csrf, constant-time
      crypto.py             # EnvelopeCrypto (Fernet/AES-GCM), key rotation
      logging.py            # structlog JSON + redaccion PII
      deps.py               # get_session, current_user, require_role, current_tenant
      errors.py rate_limit.py clock.py ids.py
    db/
      base.py               # DeclarativeBase, mixins (TimestampMixin, TenantMixin, SoftDelete)
      session.py            # async engine/sessionmaker, set_tenant_context()
      models/               # un archivo por agregado (ver s.4)
      repos/                # TenantScopedRepo[T]
    tenants/                # service.py router.py schemas.py (CRUD negocios, onboarding, secretos)
    bots/                   # factory.py (bot factory), brain.py (Claude), prompts/, kb.py, guardrails.py   [otros planes]
    channels/               # twilio.py (webhook+send), base.py Channel Protocol, voice/ (futuro)
    booking/                # calendar_google.py, availability.py, service.py
    leads/                  # places.py, scoring.py, csv_import.py, service.py
    outreach/               # campaigns.py, compliance.py, sender.py, optout.py
    plans/                  # catalog.py (Basico/Pro/Premium), entitlements.py, niche_templates/*.yaml
    privacy/                # consent.py, retention.py, dsar.py (Ley 1581)
    audit/                  # service.py (log_event), router.py
    web/
      auth.py router_*.py   # login, dashboard, tenants, leads, outreach, bots, citas, audit
      templates/ (base.html, partials/ HTMX, es/)  static/ (htmx.min.js vendored, css)
    worker.py               # arq WorkerSettings (jobs: followups, reminders, retention, scraping)
  tests/ unit/ integration/ e2e/ conftest.py factories.py
```
Regla de dependencias: web -> services; services -> db/repos, channels, booking; `core` no importa de otros paquetes; `bots` no importa `web`.

## 3. Config y secretos (`app/core/config.py`)
`class Settings(BaseSettings)`, `model_config = SettingsConfigDict(env_file=".env", env_prefix="VAI_", extra="ignore")`. Campos:
`env: Literal["dev","test","prod"]`, `database_url: PostgresDsn|str`, `redis_url`, `secret_key: SecretStr` (firma sesiones, >=32 bytes), `master_keys: SecretStr` (JSON `{"v2":"<b64>","v1":"<b64>"}`, primera = activa), `anthropic_api_key`, `anthropic_model="claude-sonnet-5-5"`, `twilio_account_sid/auth_token` (cuenta de plataforma; los tenants pueden traer subcuenta propia), `google_places_api_key`, `google_oauth_client_id/secret`, `public_base_url` (para validar firma Twilio), `session_ttl_min=480`, `cookie_secure=True`, `allowed_hosts`, `default_tz="America/Bogota"`, `msg_retention_days=90`.
- `get_settings()` con `lru_cache`; validador: en `prod` falla el arranque si `secret_key` debil, `cookie_secure=False` o `master_keys` vacio.
- Secretos solo por env / Docker secrets; `.env` en `.gitignore`; `.env.example` sin valores. Gitleaks en CI + pre-commit. Nunca loguear `SecretStr`.

## 4. Modelo de datos (PostgreSQL; PK `uuid` v7 salvo nota; todas con `created_at`, `updated_at` timestamptz UTC)
**Plataforma / auth**
- `users`: id, email (unique citext), password_hash (argon2id), full_name, role enum(`owner`,`admin`,`operator`), is_active, failed_logins int, locked_until, totp_secret_enc (nullable, fase 2), last_login_at.
- `sessions`: id (hash sha256 del token, PK text), user_id FK, csrf_token, created_at, expires_at, last_seen_at, ip, user_agent, revoked_at. Index (user_id), (expires_at).
- `audit_log`: id bigserial, ts, actor_user_id null, actor_type enum(user,system,contact,webhook), tenant_id null, action text (`tenant.create`, `secret.update`, `lead.export`, `outreach.send`, `contact.erase`...), entity_type, entity_id, ip, request_id, diff jsonb (sin PII ni secretos; solo nombres de campos y hashes), prev_hash/hash (cadena HMAC para evidencia de manipulacion). Append-only: REVOKE UPDATE/DELETE al rol de la app.

**Planes**
- `plans`: id, code (`basico|pro|premium`), name, setup_fee_cop int, monthly_fee_cop int, limits jsonb (`max_conversations_month`, `max_calendars`, `voice_enabled`, `followups_enabled`, `max_services`), is_active. Seed: Basico 800k/250k, Pro 1.2M/400k, Premium 2M/600k.
- `subscriptions`: id, tenant_id FK, plan_id FK, status enum(trial,active,past_due,cancelled), started_at, current_period_end, custom_monthly_fee_cop null, notes.
- `usage_counters`: tenant_id, period (YYYY-MM), conversations int, messages_in int, messages_out int, llm_tokens_in/out bigint. PK (tenant_id, period).

**Tenants (negocios)**
- `tenants`: id, slug unique, name, niche enum(dentista,clinica_estetica,taller,restaurante,otro), niche_template_id FK null, status enum(draft,building,active,paused,offboarded), city, address, timezone, website_url, instagram_url, phone_contact, owner_name, created_by FK users, deleted_at.
- `tenant_profiles`: tenant_id PK/FK, hours jsonb (`{"mon":[["09:00","18:00"]],...}`), services jsonb ref opcional, tone text, languages text[], handoff_phone, scraped_summary text.
- `tenant_secrets`: id, tenant_id FK, kind enum(twilio_auth_token,twilio_sid,google_oauth_refresh,google_calendar_id,custom_api), ciphertext bytea, key_version text, nonce bytea, last4 text (para mostrar), rotated_at. Unique (tenant_id, kind). Nunca se devuelve en API/HTML; solo `last4`.
- `channel_accounts`: id, tenant_id, channel enum(whatsapp,voice), provider (`twilio`), phone_e164 unique (numero del bot; clave de ruteo de webhook), twilio_messaging_service_sid null, status, webhook_token_hash.
- `services`: id, tenant_id, name, description, price_cop int null, price_note text, duration_min int, requires_deposit bool, is_active, sort.
- `faqs`: id, tenant_id, question, answer, source enum(template,scrape,manual), is_active.
- `kb_documents`: id, tenant_id, source_url null, title, content text, content_hash, fetched_at (resultado de scraping del sitio, para el bot).
- `bot_configs`: id, tenant_id, version int, status enum(draft,published,archived), system_prompt text, booking_rules jsonb, guardrails jsonb (temas permitidos, handoff, no consejo medico), templates jsonb (saludo, confirmacion, recordatorio, followup, fuera de horario), model text, generated_by enum(factory,manual), source_inputs jsonb, published_at, published_by. Unique (tenant_id, version); una sola `published` por tenant (partial unique index).
- `niche_templates`: id, niche, name, version, payload jsonb (servicios tipicos, FAQs, prompt base, reglas de reserva), is_active. Global (sin tenant_id).

**Contactos y conversaciones (por tenant)**
- `contacts`: id, tenant_id, phone_e164 (cifrado deterministico no; se guarda `phone_hash` HMAC para busqueda + `phone_enc`), display_name_enc, first_seen_at, opted_out bool, opted_out_at, source enum(inbound,import), erased_at. Unique (tenant_id, phone_hash).
- `consents`: id, tenant_id, contact_id, purpose enum(atencion,recordatorios,marketing), policy_version, granted_at, revoked_at, evidence text (mensaje/timestamp), channel.
- `conversations`: id, tenant_id, contact_id, channel_account_id, status enum(open,handoff,closed), last_message_at, summary text, handoff_reason null.
- `messages`: id, tenant_id, conversation_id, direction enum(in,out), provider_sid unique (idempotencia Twilio `MessageSid`), body_enc bytea (cifrado), body_redacted text null, media_count, llm_usage jsonb, created_at, purge_after date (retencion). Index (tenant_id, conversation_id, created_at).
- `appointments`: id, tenant_id, contact_id, service_id, starts_at, ends_at (timestamptz), status enum(pending,confirmed,cancelled,no_show,done), google_event_id, reminder_sent_at, source enum(bot,manual), notes_enc null. Exclusion constraint `EXCLUDE USING gist (tenant_id WITH =, tstzrange(starts_at,ends_at) WITH &&) WHERE status IN ('pending','confirmed')` (anti doble reserva; si hay varios recursos, agregar `resource_id`).
- `scheduled_jobs`: id, tenant_id, kind (reminder,followup), run_at, payload jsonb, status, attempts, dedupe_key unique.

**Leads / outreach (mundo agencia, sin tenant_id)**
- `lead_sources`: id, kind enum(places_api,csv_import,manual), label, params jsonb, created_by, created_at.
- `leads`: id, source_id FK, place_id unique null (Google), name, niche, city, address, phone_e164, phone_hash, website, instagram, rating numeric(2,1), review_count int, signals jsonb (resenas "no contestan"), score smallint, score_breakdown jsonb, status enum(new,contacted,replied,interested,won,lost,do_not_contact), converted_tenant_id FK null, notes, last_contacted_at. Index (status, score desc), (niche, city).
- `campaigns`: id, name, niche, message_template_id, daily_limit, send_window jsonb (08:00-19:00 L-S Bogota), status, created_by.
- `outreach_messages`: id, campaign_id, lead_id, channel, template_name, body, status enum(queued,sent,delivered,read,failed,replied), provider_sid, sent_at, error.
- `suppression_list`: id, phone_hash unique, reason enum(opt_out,complaint,manual,bounced), created_at. Consultada SIEMPRE antes de enviar (leads y contactos).
- `message_templates`: id, scope enum(outreach,tenant), name, body, twilio_content_sid null, approved bool.

Convenciones: FK con `ON DELETE RESTRICT` salvo hijos puros (CASCADE); `CHECK` para enums como `native_enum=False`; indices compuestos que empiezan por `tenant_id`; dinero en COP como `int` (sin decimales).

## 5. Aislamiento multi-tenant
1. `TenantMixin` agrega `tenant_id` NOT NULL FK + index.
2. `TenantScopedRepo[T]`: constructor exige `tenant_id`; todo `select()` agrega `.where(T.tenant_id == self.tenant_id)`; `add()` fija tenant_id; imposible construirlo sin tenant. Prueba de arquitectura (pytest) que escanea `app/` y falla si hay `select(` de modelo con TenantMixin fuera de repos.
3. RLS en Postgres: `ALTER TABLE ... ENABLE/FORCE ROW LEVEL SECURITY; CREATE POLICY tenant_isolation USING (tenant_id = current_setting('app.tenant_id')::uuid)`. `session.set_tenant_context(tenant_id)` ejecuta `SET LOCAL app.tenant_id` al inicio de cada transaccion. Rol `app_user` (sin BYPASSRLS) para runtime, `app_migrator` para Alembic. Paneles de agencia que cruzan tenants usan funciones/servicios con sesion `agency_scope` explicita y auditada. En SQLite (tests) RLS no existe: la capa de repo es la garantia + tests de aislamiento; una suite adicional corre contra Postgres real en CI.
4. Ruteo de webhook: `To` (numero del bot) -> `channel_accounts.phone_e164` -> tenant. Nunca confiar en tenant_id enviado por el cliente.

## 6. Autenticacion del dashboard (`app/web/auth.py`, `app/core/security.py`)
- Login email+password; hash **argon2id** (argon2-cffi), politica min 12 chars; rehash on login si cambian parametros.
- Sesion server-side: token aleatorio 256 bits (`secrets.token_urlsafe(32)`) en cookie `vai_session` (`HttpOnly; Secure; SameSite=Lax; Path=/`, prefijo `__Host-` en prod); en DB solo sha256 del token. TTL deslizante 8h, absoluto 7d; rotar token al login (anti fixation); logout revoca; cambio de password revoca otras sesiones.
- **CSRF**: token por sesion (`sessions.csrf_token`) renderizado en `<meta name="csrf-token">`; HTMX lo envia con `hx-headers='{"X-CSRF-Token": ...}'` via `htmx:configRequest`; formularios clasicos con input hidden. Dependencia `verify_csrf` obligatoria en POST/PUT/PATCH/DELETE (compare_digest) + chequeo de `Origin/Host`. Webhooks (Twilio) exentos de CSRF pero con validacion de firma.
- Fuerza bruta: `rate_limit` por IP+email (Redis, 5 intentos/15 min), bloqueo progresivo `locked_until`, mensaje generico, audit de fallos. Timing uniforme (hash dummy si el usuario no existe).
- Roles: `owner` (todo, usuarios, secretos, borrado), `admin` (tenants, bots, leads, outreach), `operator` (solo ver/editar conversaciones y citas). `require_role(*roles)` como dependency. Primer owner via comando CLI `python -m app.cli create-owner` (no hay registro publico).
- Headers (middleware): CSP (`default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'`), HSTS, `X-Content-Type-Options`, `Referrer-Policy: same-origin`, `Cache-Control: no-store` en vistas autenticadas. HTMX y CSS servidos locales (sin CDN). Jinja2 autoescape ON; prohibido `|safe` salvo helper revisado.
- Fase 2: TOTP obligatorio para owner/admin (`totp_secret_enc`).

## 7. Cifrado de secretos y PII en reposo (`app/core/crypto.py`)
```python
class EnvelopeCrypto:
    def __init__(self, keys: dict[str, bytes], active: str): ...
    def encrypt(self, plaintext: bytes, *, aad: bytes) -> EncryptedBlob  # (ciphertext, nonce, key_version)
    def decrypt(self, blob: EncryptedBlob, *, aad: bytes) -> bytes
    def blind_index(self, value: str, *, purpose: str) -> str  # HMAC-SHA256 para busqueda (phone_hash)
    def rotate(self, blob, *, aad) -> EncryptedBlob
```
- AES-256-GCM (`cryptography`), nonce 96 bit aleatorio, `aad = f"{table}:{tenant_id}:{column}"` (enlaza el dato a su tenant/columna). Claves maestras desde env `master_keys` (con version); en prod migrable a KMS (GCP KMS / AWS KMS) sin cambiar la interfaz.
- Se cifran: `tenant_secrets.*`, `messages.body_enc`, `contacts.phone_enc/display_name_enc`, `appointments.notes_enc`, `users.totp_secret_enc`, tokens OAuth de Google. Busqueda por telefono via `blind_index` con clave HMAC separada.
- Rotacion: comando `python -m app.cli rotate-keys` re-cifra por lotes (job arq) a la version activa; claves viejas se mantienen hasta terminar. UI muestra solo `last4`; ver/rotar secreto exige rol owner y se audita.
- Postgres con disco cifrado (volumen/proveedor) + TLS `sslmode=require` hacia la DB y Redis en prod.

## 8. Auditoria (`app/audit`)
`await audit.log_event(session, actor, action, entity_type, entity_id, tenant_id=None, diff=None)` se llama en el service dentro de la misma transaccion (si falla el audit, falla la operacion). Eventos obligatorios: login ok/fail/lockout, cambios de usuarios/roles, CRUD tenants, publicacion de bot_config, lectura/rotacion de secretos, exportacion de leads/contactos, envios masivos de outreach, opt-outs, solicitudes DSAR, borrados. `diff` nunca contiene valores de PII/secretos. Cadena de hash HMAC (`hash = HMAC(prev_hash || canonical_json)`); job nocturno `verify_audit_chain`. UI `/admin/auditoria` con filtros (solo owner). Retencion 2 anos.

## 9. Ley 1581 de 2012 / Decreto 1377 (habeas data)
- Rol: la agencia es **Encargada** del tratamiento y el negocio cliente **Responsable** para los datos de sus pacientes/clientes; contrato de transmision de datos (DPA) como parte del onboarding (checkbox + version guardada en `tenants.dpa_version/dpa_accepted_at`). Para datos de leads (B2B) la agencia es Responsable.
- Autorizacion previa: el primer mensaje del bot incluye aviso de privacidad corto + link a politica; se registra `consents(purpose=atencion)` al continuar; `recordatorios` y `marketing` se piden por separado. Sin consentimiento de marketing, no se envian promociones.
- Datos sensibles (salud): el prompt prohibe solicitar/inferir diagnosticos o sintomas detallados; si el usuario los escribe, el pipeline `redact_sensitive()` los enmascara antes de logs/analitica y el bot deriva a humano. Solo se guarda lo necesario para la cita.
- Derechos del titular (conocer, actualizar, suprimir, revocar): comando "BAJA"/"STOP" -> `opted_out` + `suppression_list` inmediato; `privacy/dsar.py`: `export_contact_data(tenant_id, contact_id)` y `erase_contact(tenant_id, contact_id)` (anonimiza: borra `phone_enc`, nombre, mensajes; conserva cita agregada sin PII), respuesta en <= 15 dias habiles; todo auditado.
- Retencion: `messages.purge_after = created_at + msg_retention_days` (90 d); job diario `retention.purge()`. Leads sin interes se purgan a 12 meses.
- Transferencia internacional: Anthropic/Twilio/Google procesan fuera de Colombia -> declarar en politica; enviar al LLM solo el contexto minimo, sin telefono ni nombre completo (usar alias). Evaluar registro de bases de datos en el RNBD (SIC) si aplica por tamano de la agencia.
- Politica de privacidad y aviso publicados en `/privacidad` (plantilla editable por tenant).

## 10. Webhooks y seguridad transversal
- Twilio: `RequestValidator(auth_token).validate(url, params, X-Twilio-Signature)` usando `public_base_url`; auth token del tenant (descifrado) o de plataforma. Idempotencia por `provider_sid`. Respuesta 200 rapida; procesamiento LLM en arq (`process_inbound(message_id)`).
- Rate limit por contacto y por tenant (Redis token bucket) para frenar abuso y costo LLM; tope mensual por plan (`usage_counters`).
- SSRF: el scraper de sitios (bot factory) usa `safe_fetch()` en `core/http.py`: solo http/https, resuelve DNS y bloquea IP privadas/loopback/link-local/metadata, limite 2 MB, timeout 10 s, max 3 redirects revalidados.
- Prompt injection: contenido scrapeado/mensajes de usuario van como datos delimitados; el bot solo tiene tools acotadas (consultar servicios, disponibilidad, reservar) con `tenant_id` inyectado por el servidor, nunca por el modelo.
- Validacion: Pydantic v2 estricta (E.164 con `phonenumbers`), SQL solo parametrizado, subida CSV con limite de tamano y sanitizacion de celdas (anti CSV-injection al exportar: prefijar `'` a `=,+,-,@`).
- Dependencias: `pip-audit` + `ruff` + `bandit` en CI; versiones fijadas con `uv.lock`.
- Logs: structlog JSON con `request_id`; filtro que enmascara telefonos/emails/tokens.

## 11. Docker / deploy
- `Dockerfile` multi-stage (builder uv -> runtime `python:3.12-slim`), usuario no-root, `HEALTHCHECK` a `/healthz`.
- `docker-compose.yml`: `web` (uvicorn, workers 2), `worker` (arq), `db` (postgres:16, volumen, `POSTGRES_PASSWORD` via secret), `redis` (7, appendonly), `migrate` (one-shot `alembic upgrade head`, `depends_on` db healthy), `caddy`/`traefik` (TLS automatico Let's Encrypt) en `docker-compose.prod.yml`. Redes: `web`/`worker` en red interna; solo el proxy expone 443. `docker-compose.override.yml` para dev con reload y mailhog-like no necesario.
- Endpoints: `/healthz` (liveness), `/readyz` (DB+Redis). Backups: `pg_dump` cifrado diario a bucket + prueba de restauracion mensual (runbook en `docs/runbook.md`).
- Despliegue inicial: un VPS (Hetzner/DigitalOcean/Contabo) o Cloud Run + Cloud SQL; webhook publico via dominio propio con HTTPS (requisito Twilio). CI GitHub Actions: lint -> tests -> build image -> deploy manual (tag).
- Migraciones: Alembic async con `compare_type`, un cambio por revision, nunca editar revisiones aplicadas; datos semilla (`plans`, `niche_templates`) en `app/cli.py seed` idempotente, no en migraciones.

## 12. Estrategia de testing (TDD, cobertura >= 80%; `rules/common`)
- `pytest` + `pytest-asyncio` (auto mode) + `httpx.AsyncClient(ASGITransport)`; SQLite `aiosqlite` en memoria por defecto (fixtures `engine`, `session` con rollback por test); suite `-m pg` contra Postgres (service container en CI) para RLS, exclusion constraint de citas, JSONB.
- Factories (`tests/factories.py`) para tenant, user, lead, appointment. `freezegun`/`Clock` inyectable para recordatorios y ventanas de envio.
- Externos siempre mockeados: `respx` para Anthropic/Places/Twilio REST; `FakeChannel`, `FakeCalendar`, `FakeLLM` (Protocols en `app/*/base.py`). Ningun test toca red (conftest bloquea sockets).
- Tests clave: aislamiento tenant A vs B en cada repo y endpoint (parametrizado); CSRF requerido; cookie flags; lockout de login; cifrado round-trip + AAD incorrecto falla + rotacion; auditoria append-only + cadena; webhook con firma invalida -> 403 y duplicado idempotente; opt-out bloquea envio; DSAR export/erase; purga de retencion; anti SSRF (IPs privadas); secretos nunca aparecen en respuestas/logs (assert).
- Pruebas de arquitectura: ningun `select` crudo sobre tablas tenant fuera de repos; `core` no importa paquetes de dominio.
- CI: `ruff check/format`, `mypy --strict app/core app/db`, `bandit`, `pip-audit`, `pytest --cov`. E2E ligero con Playwright opcional para login + HTMX.

## 13. Hitos (orden de construccion de esta capa)
1. Scaffold: pyproject, config, session, base models, Alembic, Docker, CI, `/healthz`.
2. `crypto`, `audit`, `users/sessions`, login+CSRF+headers, CLI create-owner.
3. `tenants`, `tenant_secrets`, `channel_accounts`, `plans/subscriptions`, seeds.
4. RLS + `TenantScopedRepo` + tests de aislamiento.
5. `contacts/conversations/messages/consents`, privacy (opt-out, DSAR, retencion).
6. Integracion con planes 02-06 (bots, channels, booking, leads, outreach) sobre estos contratos.

## 14. Riesgos / decisiones abiertas
- Costo LLM por tenant: medir `usage_counters` desde el dia 1; tope duro por plan.
- Politica Meta/WhatsApp: plantillas aprobadas para mensajes iniciados por el negocio fuera de ventana 24 h; outreach en frio por WhatsApp puede violar ToS -> campanas con limites estrictos, opt-in cuando exista, alternativa email/llamada (ver plan outreach).
- Una cuenta Twilio por tenant vs subcuentas: el modelo (`tenant_secrets`, `channel_accounts`) soporta ambas; decidir al contratar.
- KMS gestionado vs clave en env: empezar con env, migrar antes de manejar mas de ~10 tenants de salud.
