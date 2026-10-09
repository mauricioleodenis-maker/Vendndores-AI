# Integración: pendientes y notas entre módulos

Los constructores **agregan al final** (append) una sección `## <módulo> – <tema>` cuando necesitan un cambio fuera de
sus carpetas (dependencia nueva, columna de modelo, cambio de contrato). El integrador (B18) los resuelve.

---

## B0 Fundación – Guía rápida para los demás constructores

**Entorno de pruebas** (`tests/conftest.py`): `session`, `app`, `client`, `authenticated_client` (owner, cookie + `X-CSRF-Token`
ya puestos; `.user`, `.csrf_token`), `login(client, user)`, `make_user(role)`, `owner_user`, `tenant`, `make_tenant`, `fake_llm`
(`FakeLLM`, ya inyectado vía `dependency_overrides[get_llm]`), `fake_dns` (dict para simular DNS en `safe_fetch`).
La red está bloqueada (sockets y DNS lanzan `RuntimeError`); usa `respx` para HTTP. Para probar CSRF ausente:
`headers={"X-CSRF-Token": ""}`.

**Jobs**: `@register_job("nombre")` sobre `async def fn(ctx, *args, **kwargs)` en `app/<pkg>/jobs.py`; `app.worker` los descubre solo
(opcional `CRON_JOBS = [arq.cron.cron(...)]` en ese módulo). En `VAI_ENV=test` `enqueue()` NO ejecuta: acumula en
`app.core.jobs.ENQUEUED`; usa `await run_enqueued()` o llama la función directo.

**Routers**: `app/<pkg>/router.py` con `router = APIRouter(...)` (y opcional `routers = [...]`); `create_app()` los registra y les
aplica `csrf_guard` (todo POST/PUT/PATCH/DELETE bajo `/admin` y `/api` exige `X-CSRF-Token` o campo `csrf_token`; `/webhooks` exento).
Paquetes auto-registrados: tenants, plans, billing, niches, scraping, ai, factory, conversation, privacy, channels, booking,
reminders, leads, outreach, dashboard, saleskit, ventas (B17: usar `app/saleskit/router.py`). Un router de módulo con varios
archivos expone todos en `routers`. Las rutas del menú sin módulo muestran un marcador "Módulo en construcción" (el router real gana).

**Plantillas**: `from app.web.templating import render` → `render(request, "<modulo>/pagina.html", {...})` (inyecta `user`,
`csrf_token`, `nav`, `current_path`). Extiende `base.html` (`{% block title %}`, `{% block content %}`). Macros en
`partials/macros.html` (`badge`, `field`, `empty_state`, `pager`, `csrf`). Formularios HTML clásicos: `<input type="hidden" name="csrf_token" value="{{ csrf_token }}">`.
HTMX recibe el token automáticamente (`/static/app.js`). **CSP `script-src 'self'; style-src 'self'`: sin `<script>` ni `style=` inline**;
usa clases de `/static/app.css` (si necesitas CSS propio, pide añadirlo aquí o crea `app/web/static/<modulo>.css` y enlázalo).
Confirmaciones: `data-confirm="¿Seguro?"` en `<form>` o en elementos `hx-*`. Filtros: `cop`, `fecha`, `fecha_hora` (Bogotá), `hace`,
`tel_mask`, `estado_es`, `estado_clase`, `nicho_es`, `porcentaje`.

**Auth/roles**: `Depends(current_user)`, `Depends(require_role("admin"))` (owner siempre pasa), `Depends(get_session)` (commit al terminar).
Auditoría: `await log_event(session, actor=user, action="tenant.create", entity_type="tenant", entity_id=t.id, tenant_id=t.id, diff={...})`
(el diff se sanea: nunca valores de PII/secretos). Rate limit: `from app.core.rate_limit import enforce`.

**Teléfonos**: usar SIEMPRE `app.core.crypto.phone_hash(e164)` para `contacts.phone_hash`, `leads.phone_hash` y `suppression_list.phone_hash`
(mismo HMAC en toda la app). Cifrado de columnas: `EnvelopeCrypto.encrypt(...)` + `pack_blob/unpack_blob` (un solo `bytes`),
AAD `make_aad(tabla, tenant_id, columna)`. `tenant_secrets` guarda `ciphertext/nonce/key_version` por separado.

**Stubs de contrato** (firmas fijas de MAESTRO §5; el dueño reemplaza el cuerpo, sin cambiar firma):
`app/ai/client.py` (B5; **mantener `FakeLLM()` sin argumentos**, lo usa conftest; `get_llm` hoy lanza NotImplementedError),
`app/niches/loader.py`, `app/plans/entitlements.py` (stub permisivo), `app/scraping/crawler.py` (devuelve `[]`), `app/factory/service.py`,
`app/conversation/engine.py`, `app/privacy/service.py` (versión mínima funcional), `app/channels/sender.py` (`validate_twilio_signature` ya real),
`app/booking/{service,calendar_base}.py`, `app/reminders/scheduler.py` (no-op), `app/leads/{normalize,scoring}.py`.

**Cookies en dev**: `VAI_COOKIE_SECURE` es `true` por defecto (cookie `__Host-vai_session`); para HTTP local sin TLS poner `VAI_COOKIE_SECURE=false`
(B16: incluirlo en `.env.example` de dev; en prod debe ser `true`, el arranque falla si no).

**Modelos / decisiones tomadas por la fundación** (a tener en cuenta):
- `tenants.status` admite `draft, building, review, active, paused, offboarded` (se unificó `generating`→`building`, y se añadió `review`).
- `conversations.channel` admite `whatsapp, voice, sandbox, web`; `contact_id` es nullable (chat sandbox/demo sin contacto).
- `contacts.source` admite `inbound, import, demo`. `messages` tiene además `role`, `guardrail_flags`.
- `appointments` tiene `resource_key` (default `"default"`) y `resource_id`; índice único parcial `(tenant_id, resource_key, starts_at)` para estados
  `pending/confirmed`; la migración Postgres agrega la exclusión `gist` (rango `[)`). `idempotency_key` único por tenant.
- `suppression_list`: único `(phone_hash, scope, tenant_key)`; `scope` global|tenant|agency; `tenant_key` = `''` o id del tenant.
- `bot_configs` tiene `config` (JSON completo generado) y `generation_meta`; una sola `published` por tenant (índice único parcial).
- `leads`: `stage`/`disposition` (plan 04), único parcial por `phone_hash` salvo `perdido`. `leads.phone_enc` cifrado opcional.
- `services.needs_review` y `faqs.needs_review` agregados para el flujo de revisión del bot.
- `scheduled_jobs.kind` incluye los de plan 03 más `reminder`/`followup` genéricos; `dedupe_key` único.
- Migración inicial: `alembic/versions/0001_esquema_inicial.py`. Los módulos que necesiten columnas nuevas: anótenlo abajo; el integrador
  regenera/ajusta la migración (`alembic revision --autogenerate`). Comando: `VAI_DATABASE_URL=sqlite+aiosqlite:///x.db alembic upgrade head`.
- Seed: `python -m app.cli seed` siembra planes (MAESTRO §7: Pro 390.000) y oferta `fundador` (100 % setup, 5 cupos). Si existen `app.plans.catalog.seed(session)`
  o `app.niches.loader.seed(session)` (async) los invoca después.
- `rotate-keys` solo re-cifra `tenant_secrets`; cada módulo con columnas cifradas propias (mensajes, contactos…) debe aportar su rotación.
- RLS de Postgres: pendiente (hardening posterior, MAESTRO §2). `set_tenant_context()` ya existe en `app/db/session.py`.
- Se omitió `mypy --strict` fuera de `app/core` y `app/db` (limpios); `ruff` limpio en todo el repo.

---

## B1 (tenants)
- Worker: el job `tenants.generate_bot` se registra con `@register_job` en `app/tenants/service.py`; `app/worker.py` debe importar `app.tenants.service` para que arq lo tenga en `JOB_REGISTRY` (B10/integrador).
- El wizard redirige (HX-Redirect y enlace) a `/admin/negocios/{id}/bot` al terminar; esa ruta (revisión del bot) la expone B4/factory. `build_bot` debe dejar `tenant.status` en `review` (si no lo hace, el job lo pasa de `building` a `review`). Si `build_bot` falla, la empresa vuelve a `draft` y el wizard muestra "Reintentar".
- Prefill del paso 2 usa `app.niches.loader.get_niche_template(niche).typical_services` (items dict con `name` y `duration_min`/`default_duration_min`); si B2 usa otra forma, ajustar `_niche_defaults` en `app/tenants/router.py`.
- Columna Plan en la lista de empresas: no incluida (depende de `plans`); B2/B15 pueden agregarla.
- Otros módulos que necesiten secretos del tenant deben usar `app.tenants.service.get_secret_value(session, tenant_id, kind)` (descifra con AAD tenant+kind) y `list_secret_meta` para mostrar solo last4.
- `app.tenants.service` importa `app.factory.service` (contrato B4); evitar que factory importe `app.tenants.service` a nivel de módulo (import circular).
- `tests/web/test_web.py::test_placeholders_for_unbuilt_modules` asume que `/admin/negocios` es un marcador; ahora lo sirve tenants. Quitar esa ruta de la lista del test (integrador).

## B2 Niches/Plans – notas para el integrador
- **Siembra**: `app/cli.py seed` ya llama los hooks `app.plans.catalog.seed(session)` y `app.niches.loader.seed(session)` (existen). El catalogo de B2 pisa los `limits` minimos de `PLAN_SEED` de cli.py (mismos precios/topes de MAESTRO §7); puede eliminarse `PLAN_SEED` de cli.py. `GET /api/plans` y `/admin/planes` siembran solos si `plans` esta vacia.
- **Nicho `taller`**: el YAML de soporte usa `taller_mecanico`; el codigo canonico es `taller` (enum de `tenants.niche`); `get_niche_template("taller_mecanico")` funciona como alias.
- **Esquema `NicheTemplate`** (`app/niches/schema.py`, `extra=forbid`): campos `persona, typical_services, faq_seeds, booking_rules, required_questions, forbidden_topics, escalation_triggers, message_templates, compliance, required_variables, off_topic_response, sensitive_response, prompt_base, extras`. Marcador unico del nombre del negocio: `{{negocio}}`; variables de ejecucion: nombre, servicio, fecha, hora, fecha_hora, personas, numero, total, domicilio, tiempo. `render_placeholders(text, values)` deja intactos los desconocidos. B4/B5 deben consumirlo desde `app.niches.loader`.
- **Entitlements**: sin suscripcion rige `basico` (activo). Suscripcion `cancelled` => `assert_within_limit` lanza `PlanLimitExceeded("suscripcion_cancelada")`. Metricas soportadas: `conversations`, `services`, `calendars` (semantica "cabe una mas"). Otros modulos deben llamar `assert_within_limit(session, tenant_id, "services")` al crear servicios (B1) y `"calendars"` al conectar calendario (B9), y `record_usage(...)` al abrir conversacion/mensajes (B5/B6). Periodo mensual en hora de Bogota.
- **Contrato de planes**: MAESTRO manda sobre support/05 (Pro 390.000; 500/1.500/4.000 conversaciones; calendarios 1/3/10; Premium con `voice_enabled=true` aunque el canal de voz aun no exista: validar antes de venderlo). Fundador = 5 cupos, setup 100% off.
- **Billing**: `create_subscription` crea un `BillingRecord(kind="setup", status="pendiente")` solo si el setup neto > 0. La mensualidad y el registro `ajuste` por cambio de plan los gestiona B15 (`change_plan` no prorratea ni crea `ajuste`).
- **Para B13 (convertir lead)**: usar `app.plans.service.create_subscription(session, tenant_id, plan_code, offer_code=..., actor=user)`.
- **CSS/UI**: `/admin/planes` reutiliza `.kpis/.kpi/.card/.table-wrap` de app.css (sin CSS propio). Los precios del cotizador usan el filtro `cop`.
- **Tests de concurrencia**: el cupo Fundador usa un UPDATE condicional atomico; el test usa SQLite en archivo. En Postgres no requiere `FOR UPDATE`.
- **Test de fundacion obsoleto**: `tests/test_fixtures.py::test_stub_contracts_import` espera `get_niche_template("dentista")` -> `NotImplementedError`; B2 ya lo implementa (devuelve la plantilla). Quitar esa asercion (y la del crawler, de B3) al integrar.

---

## B3 (scraping)
- API: `app.scraping.scrape_and_store(session, tenant_id, website_url, *, instagram_url=None, max_pages=10)` crawlea y guarda `kb_documents` (dedupe `(tenant_id, content_hash)`; NO hace commit, solo flush). `crawl_business_site(url, max_pages)` mantiene la firma del contrato. `extract_hints(text)` devuelve telefonos/correos/horarios/precios como pistas (no verdad) y `wrap_untrusted(url, text)` delimita contenido scrapeado para el LLM; B4 debe usarlos.
- B4/factory: `build_bot(scrape=True)` puede llamar `scrape_and_store` y luego leer `KbDocument` del tenant; el texto ya viene saneado (patrones de inyeccion filtrados) pero sigue siendo NO CONFIABLE.
- Crawl secuencial con pausa de 1 s entre peticiones (`CRAWL_DELAY_SECONDS`), max 20 paginas duras; con 10 paginas tarda ~10 s: ejecutar en job, no en request web.
- Dominio registrable aproximado (sin lista PSL, maneja `.com.co`); si se quiere exactitud, agregar `tldextract` a pyproject.

## B4 factory – notas de integración
- **Contrato ampliado (compatible)**: `publish_bot(session, bot_config_id, *, actor, require_sandbox=False, enforce_review=True)`.
  Flujos automáticos (bot demo de B13) deben llamar `publish_bot(..., enforce_review=False)`: limpia `needs_review` y publica sin
  exigir confirmación humana. La UI usa `require_sandbox=True` (exige usar el chat de prueba al menos una vez).
  También: `approve_draft`, `rollback_bot(session, tenant_id, version, *, actor)`, `FactoryGenerationError` (AppError 502).
- **B1/tenants**: `build_bot` NO hace commit y deja la empresa en `tenant.status` sin tocarla; `publish_bot`/`rollback_bot` ponen
  `tenant.status="active"` si estaba en draft/building/review. La UI de revisión vive en `GET /admin/negocios/{id}/bot`
  (la ruta a la que ya redirige `/estado`). `FactoryInput` se re-exporta desde `app.factory.service` (definido en `app/factory/schemas.py`).
- **B5/conversation**: `sandbox_reply(..., bot_config_id=<borrador>)` debe aceptar versiones `draft` y leer `BotConfig.system_prompt`,
  `config`, `guardrails`, `templates`, `booking_rules` (`working_hours` = `{"mon": [["08:00","17:00"]]}`). El catálogo vigente está
  en las tablas `services`/`faqs` (copia de trabajo) y la foto versionada en `BotConfig.config["services"|"faqs"]`.
  El `system_prompt` lo renderiza `app/factory/prompts.py::render_system_prompt` (reglas fijas + FAQs); B5 puede anteponer su propio bloque.
- **CSS**: `app/web/static/factory.css` (cargado con `<link>` en `factory/bot.html`).

## B5 ai/conversation – notas de integración
- **Contrato B6 (canal)**: `ConversationEngine.handle_inbound` reutiliza el último `Message` entrante con `processed_at IS NULL` si su
  texto descifrado coincide (AAD `make_aad("messages", tenant_id, "body_enc")` + `pack_blob`); si no existe lo crea. B6 debe guardar el
  entrante (idempotencia por `provider_sid`) y NO marcar `processed_at`. El engine hace `flush`, no `commit`: el llamador hace commit.
  Devuelve `[]` cuando no hay que responder (opt-out, humano a cargo, rate limit 30 msgs/h por contacto). Los mensajes salientes se
  guardan `status="queued"`, `role="assistant"`; B6 los envía y actualiza estado.
- **Conteo de uso**: el engine llama `record_usage(conversations=1)` en el primer turno de cada conversación (y `assert_within_limit(...,
  "conversations")` antes; si falla responde `limit_reply`) y tokens en cada turno. B6 NO debe contar `conversations`; `messages_in/out` los
  deja a B6.
- **Estado de slots ofrecidos**: no hay `conversations.state_json`; los slots ofrecidos se guardan en `messages.llm_usage["offered_slots"]`
  del mensaje saliente (se leen los últimos 8). Si se agrega la columna, migrar `ConversationEngine._recent_offered`.
- **BotConfig (B4)**: el engine lee `bot.system_prompt` (como "notas", saneado), `bot.booking_rules["max_days_ahead"]`, `bot.templates`
  (claves opcionales: `out_of_scope_reply`, `handoff_reply`, `fallback_reply`, `safe_reply`, `price_unknown_reply`, `limit_reply`...),
  `bot.config["hours"]` (`{weekly:{mon:[{open,close}]}}`), `bot.config["tone"]["register"|"emoji_level"]`. Precios y FAQs se toman de las
  tablas `services`/`faqs` (los marcados `needs_review` se ocultan o muestran sin precio).
- **B8 (booking)**: se usan `find_slots`, `book` y `cancel` (by="contact"). `book` debe lanzar `AppError` (p.ej. `ConflictError`) cuando el
  horario ya no está libre; `find_slots` debe respetar min_notice/max_days_ahead/festivos. `Slot.starts_at` debe ser aware.
- **Notificación al staff en handoff**: `open_handoff` solo crea la fila `handoffs`, marca `conversation.status="handoff"` y
  `bot_paused_until = now+12h`. Falta (B15/B6) notificar por WhatsApp/email al staff y reanudar el bot al resolver
  (`app.conversation.handoff.resume_bot(conversation)`).
- **Job**: `conversation.summarize` (app/conversation/memory.py) se auto-registra en `JOB_REGISTRY`; el engine lo encola cuando hay > 20
  mensajes sin resumir. Requiere que `app.worker` importe `app.conversation.memory` (hoy lo descubre por `app/<pkg>/jobs.py`: si no
  lo encuentra, crear `app/conversation/jobs.py` que re-exporte, o importar el módulo en worker.py).
- **sandbox_reply** acepta además `llm: LLMClient | None = None` (kw opcional; por defecto `get_llm()`). B4 puede inyectar el `llm` de su
  dependencia. Registra `llm_usage` con `purpose="sandbox"` (sin contar el plan) y hace `flush`.
- **Prompt**: plantilla en `app/ai/prompts/runtime.md.j2` (`runtime@1.0.0`). `AnthropicClient` parte el system en bloque estable
  (cache_control efímero) y dinámico con `SYSTEM_SPLIT`.
- **tests/test_fixtures.py (B0)**: `test_real_get_llm_is_stub` espera `NotImplementedError`; ahora `get_llm()` lanza
  `AppError("llm_not_configured", 503)` sin `VAI_ANTHROPIC_API_KEY`. `test_stub_contracts_import` también quedó obsoleto
  (crawler/niches ya implementados). El integrador debe actualizar o eliminar esos dos tests.

## B6 (channels) - notas de integracion
- **Rutas**: `POST /webhooks/twilio/whatsapp`, `/webhooks/twilio/status`, `/webhooks/twilio/voice`, `/webhooks/twilio/voice/status` (auto-registradas via `app.channels.router`). Job: `channels.process_inbound` (`app/channels/jobs.py`, ya descubierto por `worker.py`). Firma validada contra `VAI_PUBLIC_BASE_URL + path (+ query)`: en prod esa URL debe coincidir EXACTO con la configurada en Twilio.
- **Credenciales**: token del tenant = `tenant_secrets.kind="twilio_auth_token"` (AAD `tenant_secrets:<tenant_id>:<kind>`, como `tenants.service.set_secret`); sin secreto, cae al token de la plataforma. Envio por tenant usa `twilio_sid` + `twilio_auth_token` si ambos existen y `channel_accounts.phone_e164` como remitente (nunca el numero de la agencia).
- **B1/tenants**: `contacts.phone_enc` lo cifra B6 con AAD `contacts:<tenant_id>:phone_enc` y `display_name_enc` con `contacts:<tenant_id>:display_name_enc` (`app.channels.service.encrypt_phone/decrypt_phone`). Cualquier otro modulo que cree o lea contactos debe usar el mismo AAD.
- **B7/privacy**: B6 llama `is_optout_message`, `is_optin_message`, `apply_optout`, `record_consent`, `redact_pii`. Para el opt-in tras STOP, B6 busca `app.privacy.service.apply_optin(session, *, phone_e164, tenant_id, evidence)` (opcional via getattr); si B7 lo expone, se quita la supresion; si no, solo se limpia `contacts.opted_out` y `suppression_list` sigue bloqueando el envio. El aviso de privacidad corto (support/12 §2) esta en `app/channels/jobs.py::privacy_notice`; B7 puede reemplazarlo. La pagina `/privacidad` debe existir (el aviso la enlaza).
- **B14/outreach**: los envios con `tenant_id=None` usan `StatusCallback=/webhooks/twilio/status`. Si el `From` no es de un tenant y la firma valida con el token de plataforma, se invocan los handlers de `app.channels.service.STATUS_FALLBACKS` (`async def fn(session, params)`); B14 debe registrar ahi su actualizacion de `outreach_messages`. El inbound hacia el numero de la agencia (respuestas a outreach) hoy responde 403 (no hay `channel_account`): B14 debe agregar su ruta propia o un fallback equivalente.
- **Contrato ampliado (compatible)**: `send_whatsapp_text(..., bypass_suppression=False)` (solo para la confirmacion de STOP). Texto libre fuera de la ventana de 24 h devuelve `SendResult(ok=False, error="63016")`; B10 (recordatorios) debe usar `send_whatsapp_template` fuera de ventana. Helpers: `window_is_open`, `failure_reason(code)`, `sign_twilio` (tests).
- **B5/engine**: el webhook guarda el entrante (`processed_at=None`) y el motor lo reutiliza; el job envia los salientes `queued` sin `provider_sid` que deja el motor y les pone SID/estado. `ConversationEngine()` sin LLM usa `get_llm()` (los jobs no pasan por `dependency_overrides`; en tests parchear `app.conversation.engine.get_llm`).
- **Pendiente futuro**: descarga de media (`MediaUrl0..N`, Basic auth) no implementada (solo se cuenta `NumMedia`); el bot pide texto. Voz: solo stub TwiML.

## B7 privacy
- **Firma ampliada** `app.privacy.service.is_suppressed(session, phone, *, tenant_id=None)`: sin `tenant_id` cualquier entrada cuenta (conservador); con `tenant_id` solo globales/agencia y las de ese tenant. B6/B14 deberían pasar `tenant_id` en envíos del bot. Nuevas: `apply_optin`, `revoke_consent`, `has_consent`, `is_erase_request`, `is_rights_request` (BORRAR MIS DATOS / DERECHOS), `app.privacy.texts.privacy_notice(purpose, negocio=, agencia=, url=)` y `REPLY_*` para respuestas de STOP/SI/BORRAR.
- **B6 (pipeline inbound)**: antes del LLM llamar `is_optout_message` -> `apply_optout` + `REPLY_OPTOUT`; `is_optin_message` -> `apply_optin`; `is_erase_request` -> notificar al staff (la supresión real es manual vía `POST /api/privacidad/{tenant_id}/contactos/{contact_id}/borrar`). Al crear mensajes, setear `purge_after=retention.default_purge_after()` (el job diario `privacy.purge_retention` también rellena los faltantes).
- **Job/cron**: `app/privacy/jobs.py` define `privacy.purge_retention` y `CRON_JOBS` (08:30 UTC); lo descubre `app.worker`.
- **UI admin**: no hay página `/admin/privacidad` (solo API DSAR owner/admin). Opcional: agregar botones Exportar/Borrar en el detalle de conversación (B15).
- **Textos**: `/privacidad` y `/privacidad/{slug}` muestran el texto marcado "Borrador para revisión legal"; completar placeholders [NOMBRE AGENCIA], [NIT], etc. tras revisión del abogado. Falta `tenants.dpa_*` gating (B1).

## B9 (Google Calendar / OAuth)
- **Router**: las rutas viven en `app/booking/oauth.py` (`router`). `main.py` solo descubre `app.booking.router`; B8 debe exponer en `app/booking/router.py`: `from app.booking.oauth import router as google_oauth_router` y `routers = [google_oauth_router]`. Rutas: `GET /admin/negocios/{id}/calendario/google` (pagina), `POST .../connect` y `.../disconnect` (CSRF), `GET /admin/calendario/google/callback` (redirect URI = `VAI_PUBLIC_BASE_URL` + ese path; registrarlo en Google Cloud Console). Variables: `VAI_GOOGLE_OAUTH_CLIENT_ID/SECRET`.
- **B1/tenants**: incluir en la pestana de calendario/canales `{% include "booking/_google_calendar.html" %}` con el contexto de `await app.booking.oauth.calendar_context(session, tenant_id)` (clave `gcal`) mas `tenant`; o enlazar a `/admin/negocios/{id}/calendario/google`.
- **B8/BookingService**: `from app.booking.google_calendar import active_provider, CalendarError, CalendarAuthError`. `await active_provider(session, tenant_id)` devuelve `GoogleCalendarProvider` o `None` (usar Local). `create_event/update_event/delete_event` ya fijan `appointment.google_event_id/etag/sync_status="synced"`; lanzan `CalendarError` (red/5xx tras 3 reintentos) o `CalendarAuthError` (revocado; la conexion pasa a `needs_reauth`): capturar y marcar `sync_status="pending_push"`/caer a local. `busy_intervals` cachea 45 s y se invalida al escribir eventos.
- **Datos**: el access token solo vive en memoria; refresh token cifrado en `tenant_secrets(kind=google_oauth_refresh)`; email de la cuenta cifrado en `calendar_connections.google_account_email_enc`. Sin cambios de modelo requeridos. Dependencia `itsdangerous` (viene con starlette; confirmar en pyproject).

## B8 booking (nucleo)
- **tests/web/test_web.py::test_placeholders_for_unbuilt_modules**: `/admin/citas` ya no es placeholder (B8 lo sirve en `app/booking/router.py`); quitarlo de la lista del test.
- **Rutas**: `/admin/citas` (dia/semana, `?tenant_id=&dia=YYYY-MM-DD`), `POST /admin/citas/nueva`, `POST /admin/citas/{id}/cancelar`, `/admin/citas/horarios` (editor, solo admin) y `/admin/citas/ausencias`. Selector de empresa por query (sin tenant en sesion). Plantillas en `app/web/templates/booking/`.
- **Reglas de agenda**: `compute_slots` lee `bot_configs.booking_rules` del BotConfig *published*: `slot_minutes`, `buffer_min`, `min_notice_hours`, `max_days_ahead`, y extras `closed_on_holidays` (def. true), `max_active_per_contact` (def. 2). Sin BotConfig publicado usa defaults. Horario laboral = `working_hours` con `resource_id IS NULL` (un solo recurso `default`; `capacity>1` no soportado por el indice unico `(tenant, resource_key, starts_at)`).
- **Contrato ampliado (compatible)**: `BookingService(provider=None)`; errores `SlotTakenError` (409 `slot_taken`) y `SlotUnavailableError` (422 `slot_unavailable`), ambos `AppError`. `book(source="bot")` exige que `starts_at` este en la grilla calculada; `source="manual"` solo valida pasado + choque. Citas se crean `confirmed` (holds `pending` no implementados; los vencidos si se ignoran al calcular). `by` en cancel/reschedule: `contact|bot|tenant|system`. `audit_log.actor_type` solo admite user/system/webhook/contact, por eso las acciones del staff se registran como `system`.
- **B9**: `providers.get_provider()` importa `app.booking.google_calendar.GoogleCalendarProvider()` (constructor sin args) cuando hay `calendar_connections` google `active`; si falla el import usa el local. Los fallos de `create/update/delete_event` marcan `sync_status=pending_push` sin revertir la cita.
- **B10**: BookingService llama `schedule_for_appointment` tras crear/mover y `cancel_for_appointment` al cancelar/mover (flush, sin commit; commit lo hace `get_session`/el llamador). En PG falta la exclusion constraint gist en la migracion (integrador).
- Tests de B8 en `tests/booking/core/` (conftest propio; coincide con tests/booking de B9). Cobertura ~96% de availability/service/router/providers.

## B11 (leads: datos)
- **API**: `normalize` (`to_e164_co`, `phone_type`, `name_key`, `normalize_website`, `website_domain`, `instagram_handle`); `dedupe` (`async find_duplicate(session, place_id=, phone_hash=, website_domain=, name_key=, city=) -> DuplicateMatch(lead, kind, auto_merge)`, `async merge_leads(session, winner, loser)`); `scoring` (`score_lead` -> `ScoreResult(score, breakdown, band caliente|tibio|frio, disposition="perdido" si no opera)`, `review_signal_scan(list[str]) -> list[SignalMatch]`, `ScoringConfig`); `csv_import` (`read_headers`, `detect_columns`, `parse_csv`, `import_csv(session, source, candidates, dry_run=)`, `file_sha256`, `find_reupload`; errores `CsvImportError(AppError)`); `export.export_leads_csv(leads)` (UTF-8 BOM, anti formula-injection).
- **B12/router**: flujo preview = leer bytes -> `read_headers` -> `detect_columns` (editable) -> `parse_csv` + `import_csv(dry_run=True)`; commit = crear `LeadSource(kind="csv_import", params={"sha256": ...})`, repetir con `dry_run=False` y hacer commit. Los limites (5 MB / 5.000 filas) se aplican dentro de `decode_bytes`/`parse_csv` (413). Chequear Content-Type en el router.
- **Senales web para scoring**: `lead.signals["web_whatsapp"]` / `["web_booking"]` (bool) evitan el +5 de "web sin boton"; quien haga el crawl de la web del lead debe poblarlos.
- **Dedupe**: sin columna `possible_duplicate_of`; el import marca tag `posible_duplicado` y `signals["duplicate_of"]=<id>`. `phone_enc` se cifra con aad `make_aad("leads", None, "phone")`.
- Nota: se ejecuto `ruff format app` (global) una vez; si algun archivo ajeno cambio solo de formato, es inofensivo.

## B10 Recordatorios – notas para el integrador
- **Plantillas Twilio**: `app/reminders/service.py` busca en `message_templates` filas `name in (cita_recordatorio_24h, cita_recordatorio_2h, cita_no_show)` con `approval_status='approved'` y `twilio_content_sid` (tenant propio gana a global `tenant_id NULL`). Sin plantilla y fuera de la ventana de 24 h => job `cancelled` con `last_error=skipped_no_template`. Hay que sembrar/registrar esas plantillas (B14/B1).
- **No-show**: B8 debe llamar `app.reminders.scheduler.schedule_noshow_followup(session, appt)` al marcar `no_show` (hoy nadie lo llama). Reactivacion: `schedule_unbooked_followup(session, tenant_id=, contact_id=, conversation_id=)` desde el motor/B5 cuando se cotiza y no se agenda (max 2 por conversacion).
- **Cron esperados por `app/worker.py`** (aviso `missing_crons` en el log de arranque si faltan): `leads.secret_shop_timeout` (B13), `outreach.dispatch` (B14), `billing.generate_monthly_records` (B15), `privacy.purge_retention` (ok). Cada duenio debe declarar `CRON_JOBS = [cron(fn, name="<nombre exacto>")]` en su `app/<pkg>/jobs.py` y `@register_job` con el mismo nombre.
- `worker.py` ademas importa `app.tenants.service` y `app.conversation.memory` (registran jobs fuera de `jobs.py`). Si otros modulos registran jobs en otros archivos, agregarlos a `EXTRA_JOB_MODULES`.
- Los recordatorios enviados no se guardan como `messages` de la conversacion (el sender no lo hace); si el dashboard debe mostrarlos, B6 puede exponer un helper publico.
- `tests/test_fixtures.py::test_real_get_llm_is_stub` falla en la suite global (no es de B10; depende de B5).

## B13 Leads ventas – wiring y pendientes
- **Registro de rutas**: `app/leads/router.py` (B12) solo expone `router`. Para activar las rutas de ventas
  (`/api/leads/{id}/secret-shop`, `/api/secret-shop/{id}/reply`, `/pitch-evidence`, `/demo-bot`, `/convert`,
  `/admin/leads/{id}/ventas*` y la pagina PUBLICA `/demo/{token}` + `/demo/{token}/mensaje`) agregar en `app/leads/router.py`:
  `from app.leads.sales_router import router as sales_router` y `routers = [router, sales_router]`.
  `/demo/*` no cuelga de `/admin` ni `/api`: no exige sesion ni CSRF (el token firmado + tope de mensajes lo protegen).
- **Jobs**: crear `app/leads/jobs.py` con `from app.leads.secret_shop import secret_shop_timeout_job`
  y `CRON_JOBS = [cron(secret_shop_timeout_job, minute={0, 15, 30, 45}, name="leads.secret_shop_timeout")]`
  (el worker espera ese nombre). Pendiente (spec §12): `purge_expired_demos_job` (borrar tenants `is_demo` vencidos sin conversion;
  vencimiento = `created_at + VAI_DEMO_TOKEN_TTL_DAYS`).
- **Etapas**: `listing.move_stage` (B12) deberia delegar en `app.leads.pipeline.transition_stage(session, lead, stage, actor=user)`
  para validar `ALLOWED`, crear `lead_event` y `audit_log` (no duplique la tabla de transiciones). Para `no_contactar/perdido`
  usar `pipeline.set_disposition`.
- **Demo por WhatsApp**: `demo_links()` genera `https://wa.me/<VAI_DEMO_WHATSAPP_NUMBER>?text=DEMO-<slug>`, pero el ruteo
  inbound del codigo `DEMO-<slug>` hacia el tenant demo (channels, B6) NO esta implementado: no se crea `channel_accounts`
  (phone_e164 es unique y el numero demo es compartido). Hasta entonces funciona solo la pagina web `/demo/{token}`.
- **Tope de mensajes de demo**: usa `app.core.rate_limit.enforce("demo-msgs:<tenant_id>")` (memoria en dev/test, Redis en prod).
- **Plantillas**: el boton/acciones se incrustan con `hx-get="/admin/leads/{id}/ventas" hx-trigger="load"` o `{% include %}` no
  posible (necesita contexto); sugerido en `leads/detalle.html`: `<div hx-get="/admin/leads/{{ lead.id }}/ventas" hx-trigger="load"></div>`.
  CSS propio: `app/web/static/leads_sales.css` (enlazado desde los parciales).
- Demo no crea suscripcion trial (el `trial` de plan basico del spec se omite: los tenants demo no consumen cuotas porque usan `sandbox_reply`).

## B12 (leads: Places + UI)
- **Job de busqueda**: `app/leads/search.py` registra `leads.search_places` con `@register_job`, pero el worker solo importa `app.<pkg>.jobs`. Debe existir `app/leads/jobs.py` que haga `import app.leads.search  # noqa: F401` (y los jobs de B13). Sin eso el job encolado no corre en el worker arq.
- **Macro `csrf()` (foundation)**: `partials/macros.html::csrf()` lee `csrf_token` del contexto, pero con `{% from ... import csrf %}` Jinja NO pasa el contexto y el input sale con `value=""` (formularios sin JS fallan CSRF; HTMX no, porque usa el header del meta). B12 importa con `{% from "partials/macros.html" import csrf with context %}`. Revisar `auth/ajustes.html`, `booking/*`, etc., o cambiar la macro a `{% macro csrf() %}` + importar siempre `with context`.
- **Estilos**: CSP `style-src 'self'` prohibe `<style>` inline; B12 agrego `app/web/static/leads.css` (kanban, timeline) enlazado desde `leads/_tabs.html`. Considerar moverlo a `app.css`.
- **Config nueva (sin cambios de codigo)**: `VAI_GOOGLE_PLACES_API_KEY` y `VAI_GOOGLE_PLACES_BUDGET_USD_MONTH`. Tope diario = 2x (mensual/30), contadores en el rate limiter compartido (memoria en dev, Redis en prod). Costos por solicitud son constantes orientativas en `places.py` (`COST_USD_PER_SEARCH_REQUEST=0.035`); verificar en la consola de Google.
- **Etapas**: `listing.move_stage` cambia `Lead.stage` + `LeadEvent(stage_changed)` + auditoria desde kanban/detalle. Si B13 (`pipeline.py`) impone transiciones validas, hacer que `listing.move_stage` delegue en esa funcion.
- **Places details/reviews**: `PlacesClient.get_details` existe y esta probado, pero el job no lo llama (costo y ToS de cache de resenas). Para `review_signal_scan` usar el CSV del Maps-Scraper o llamarlo bajo demanda.
- **Dry-run del buscador** = solo estimado (0 llamadas a Google, nada se guarda).
- **CSV preview**: el archivo subido se guarda en `<tmp>/vai-csv-import/<sha256>.csv` (0600, TTL 1 h) para el paso de confirmacion; con varios hosts web usar volumen compartido o mover a Redis.
- **Rutas**: `/admin/leads` (tabla), `/admin/leads/kanban`, `/buscar`, `/importar`, `/export.csv`, `/{id}`; API `/api/leads/search|sources/{id}/status|import/preview|import/commit|{id}/stage`. Cuidado con colision de rutas `/admin/leads/{lead_id}` con las de B13 (`sales_router`): las estaticas de B12 se registran antes.
- **tests/web/test_web.py::test_placeholders_for_unbuilt_modules**: `/admin/leads` ya no es placeholder (lo sirve B12); quitarlo de la lista del test. (`tests/test_fixtures.py::test_real_get_llm_is_stub` y `::test_stub_contracts_import` fallan por B5/otros, no por leads.)

## B14 Outreach – notas de integración
- **Seed**: `app.outreach.templates.seed_templates(session)` crea las 4 plantillas (borrador, sin aprobar). Se auto-ejecuta al abrir
  `/admin/campanas/plantillas` o `GET /api/outreach/templates`; `app.cli seed` puede llamarla también.
- **Aprobación Meta**: no hay llamada a Content API; tras aprobar en Twilio se registra el `ContentSid` con
  `POST /api/outreach/templates/{id}/approval` (admin). Sin ContentSid aprobado, el ComplianceGate niega y `start` falla.
- **Dry-run**: se usa `VAI_TWILIO_DRY_RUN` (ya existe; el sender devuelve SID `DRYRUN...`). No se agregó `OUTREACH_DRY_RUN` ni
  `OUTREACH_MAX_DAILY` a `Settings`: el tope (80/día) y la rampa (20 +10/día) son constantes duras en `app/outreach/compliance.py`.
  `VAI_OUTREACH_ENABLED=false` bloquea start/resume, el gate y el job `outreach.dispatch` (cron cada minuto, autodescubierto).
- **StatusCallback**: `channels.sender._status_callback()` apunta a `/webhooks/twilio/status`; outreach se engancha vía
  `channels.service.STATUS_FALLBACKS` (se registra al importar `app.outreach.webhooks`). `/webhooks/twilio/outreach-status` también existe.
- **Opt-out por STOP** responde con TwiML `<Message>` (no usa `send_whatsapp_template`, que bloquea números suprimidos).
- Webhooks usan el token de la plataforma (`VAI_TWILIO_AUTH_TOKEN`) y `VAI_PUBLIC_BASE_URL` para validar la firma.
- Sugerencia B18: enlazar "Campañas" desde la ficha del lead (`/admin/leads/{id}`) y migración Alembic: sin cambios de esquema.

## B17 (kit de ventas)
- Router en `app/saleskit/router.py` (paquete existente de la fundación; no se creó `app/sales_kit`). Estáticos propios: `app/web/static/saleskit.css` y `saleskit.js` (botón Copiar, cargados desde las plantillas).
- Los precios de `docs/ventas/06-oferta-y-planes.md` siguen MAESTRO §7 (Pro 390.000; fundador = 5 clientes, setup 100% off), no `support/05`. Si B2 cambia el seed, actualizar ese documento.
- Sin dependencias nuevas.

---

## B18 Integrador – resolución (HECHO)
- HECHO: `app/booking/router.py` expone `routers=[google_oauth_router]`; `app/leads/router.py` expone `routers=[sales_router]`; nuevo `app/leads/jobs.py` (importa `leads.search`, cron `leads.secret_shop_timeout`).
- HECHO: macro `csrf` importada `with context` en todas las plantillas (tenants wizard, outreach, booking).
- HECHO: `app.cli seed` llama `seed_templates` de outreach. Tests obsoletos de B0/web actualizados (`test_fixtures.py`, `test_web.py`).
- HECHO: brecha real corregida: sin `working_hours` la disponibilidad usa `bot_configs.config["hours"]["weekly"]` del bot publicado (`availability._bot_hours`).
- HECHO: migración `0001` verificada contra los modelos (autogenerate sin diferencias) y `upgrade head` en SQLite OK.
- HECHO: humo e2e en `tests/integration/test_e2e_smoke.py` (wizard -> build_bot FakeLLM -> publicar -> Twilio firmado -> cita; CSV -> score -> bot demo).
- PENDIENTE (no bloqueante): `listing.move_stage` aún no delega en `pipeline.transition_stage`; notificación al staff en handoff; plantillas Twilio aprobadas; RLS Postgres; descarga de media; ruteo `DEMO-<slug>` por WhatsApp; `purge_expired_demos_job`.

## Mejoras (plans+niches)
- `subscriptions`: falta índice único parcial `(tenant_id) WHERE status != 'cancelled'` para garantizar una sola suscripción vigente por tenant a nivel BD (el servicio ya serializa con `FOR UPDATE` sobre el tenant en Postgres).

## Mejoras (tenants)
- BD: añadir índice `lower(tenants.name)`/`pg_trgm` si la búsqueda por nombre/ciudad en `/admin/negocios` crece (hoy `LIKE '%q%'` con scan); índice parcial `tenants(created_at desc) WHERE deleted_at IS NULL`.
- `tenant_secrets.last4` expone todo el valor si el secreto mide <=4 caracteres; considerar guardar vacío para secretos cortos.
- Flash por query string (`?ok=`/`?error=`) permite mensajes falsificables en un enlace; migrar a cookie de sesión firmada o `HX-Trigger` toast.

## Mejoras (scraping)
- El modulo scraping no tiene UI propia (no existe `app/web/templates/scraping/`); el estado del scraping se muestra desde factory (`scrape_error`). Sugerencia para factory/UI: mostrar el conteo de `kb_documents` nuevos y un toast si `scrape_error` no es None.
- `kb_documents` ya tiene unique (tenant_id, content_hash); sin indices adicionales necesarios.

### factory (revision haiku-factory)
- CRITICO (fuera de modulo): `app/conversation/context.py:155-158` debe filtrar `Service.needs_review.is_(False)` y `app/booking/*` igual; hoy los cambios de borrador (descartar/editar/regenerar) en las tablas `services`/`faqs` llegan al bot en vivo antes de publicar. Solucion de fondo: catalogo de borrador ligado a `bot_config_id` o runtime leyendo `BotConfig.config` publicado.
- `app/factory/router.py::regenerate` mantiene la sesion abierta durante scraping + LLM; requiere dividir en fases con sesiones cortas (cambia `get_session` en `app/core/deps.py`).
- Pendiente: `app/web/static/factory.css` (ahora redundante con `app.css`: `.fb-grid`, `.fb-item`, `.fb-chat`) puede simplificarse; no se edito por no ser del modulo.

## Mejoras (ai+conversation)
- UI: `app/web/templates/conversation/` no tiene plantillas; las vistas de conversaciones viven en `app/web/templates/dashboard/conversacion*.html` (otro modulo). Deben usar `bubble()`, `.chat`, `badge()` y `empty_state()` del sistema de diseno, y mostrar toast (`HX-Trigger`) al tomar/devolver una conversacion.
- `context.py` mantiene servicios con `needs_review` (precio oculto por `price_text`, descripcion omitida en `get_business_info`); el catalogo de borrador sigue sin separarse del publicado (ver factory).
- Rate limit por contacto (30 msg/h) responde en silencio (solo flag `rate_limited` + log): considerar aviso al operador. Concurrencia: dos mensajes simultaneos de una conversacion pueden duplicar el handoff (anadir unique parcial `handoffs(conversation_id) WHERE status IN ('open','claimed')`).
- Indices existentes (`ix_messages_tenant_conv_created`) cubren las consultas del motor; sin indices nuevos.

### channels (fortificación)
- Pendiente (decisión de producto): hoy el motor responde en el primer turno junto con el aviso de privacidad (hallazgo haiku-channels #2); bloquear hasta el SI cambiaría el flujo MAESTRO §4 B6.
- BORRAR MIS DATOS responde con enlace a /privacidad (sin borrado automático por falta de verificación de identidad); integrar `dsar.erase_contact` con confirmación en privacy.
- Sin límite de mensajes por contacto/plan (`entitlements._LIMIT_KEYS` sin `messages`) y envío HTTP dentro de la transacción del job: pasar a outbox.
- Índice parcial único de conversaciones abiertas (carrera en get_or_create_conversation) en app/db/models/conversations.py.
- channels no tiene UI propia (templates/channels vacío); no hay pantallas que migrar al sistema de diseño.

## Mejoras (privacy)
- Retención: `Conversation.summary`, `Handoff.summary` y `Appointment.notes_enc` no se purgan por `purge_after`; definir política.
- Erasure: no cancela el evento de Google Calendar (`Appointment.google_event_id`); requiere job en booking.
- `Consent`: añadir índice único parcial `(tenant_id, contact_id, purpose) WHERE revoked_at IS NULL` (migración, módulo db) para evitar duplicados concurrentes.
- `app/channels/jobs.py:206-211`: no poner `opt_out_source=None` antes de `apply_optin`; llamar directo a `apply_optin`.
- Cron `privacy.purge_retention` corre 08:30 UTC (03:30 Bogotá); ajustar zona en worker si se desea.
- DSAR: owner/admin accede a cualquier tenant por diseño; evaluar rol `dpo`.
- `/privacidad` muestra placeholders legales ([NIT], [CORREO_DPO]) hasta que se complete la configuración.

## Mejoras

- booking: `app/web/static/app.js` no autoenvía selects; el selector de empresa de Citas/Horarios ya no usa `onchange` inline (CSP) y muestra botón "Cambiar empresa" (solo si hay >1 empresa). Opcional: soportar `data-autosubmit` en app.js.
- booking (DB): considerar índice `appointments(tenant_id, starts_at)` y `time_off(tenant_id, ends_at)` si no existen (consultas de agenda diaria/semanal y disponibilidad).
