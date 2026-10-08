# 00 — Plan maestro consolidado (fuente de verdad)

Este documento consolida `01`–`04` y `support/*`. Si algo contradice a otro plan, **gana este**.
Los planes 01–04 siguen siendo la referencia de detalle (columnas, reglas, tests).

## 1. Producto
Plataforma de la agencia para vender **Recepcionista IA** (WhatsApp vía Twilio; voz después) a negocios
pequeños (dentista, clínica estética, taller, restaurante). Tres pilares:
1. **Fábrica de bots**: el operador escribe unos datos del negocio → la IA arma todo el bot (servicios,
   precios, FAQs, reglas de agenda, plantillas, prompt) desde la plantilla de nicho + scraping de su web.
2. **Planes listos**: Básico / Pro / Premium (COP), oferta Fundador, garantía 30 días, facturación simple.
3. **Buscador de leads**: Google Places API + import CSV del Maps-Scraper, scoring, prueba secreta,
   pipeline, lead → bot demo en un clic, campañas de outreach con cumplimiento.

## 2. Decisiones finales (simplificaciones sobre 01–04)
- Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, Postgres (prod) / SQLite aiosqlite (tests y dev rápido).
- UI: Jinja2 + HTMX, en español, servida por la misma app. HTMX vendorizado en `app/web/static/`.
- Jobs: **arq + Redis** en prod; toda función de job es una `async def` pura que recibe `ctx` y se puede
  llamar directo en tests. Si `VAI_REDIS_URL` no está, `app/core/jobs.enqueue()` ejecuta en
  `BackgroundTasks`/`asyncio.create_task` (modo dev).
- Rate limit: `app/core/rate_limit.py` con backend en memoria (dev/test) y Redis (prod), misma interfaz.
- Multi-tenant: columna `tenant_id` + `TenantScopedRepo`. RLS de Postgres queda como hardening posterior
  (documentado), no bloquea el MVP.
- Cifrado: AES-256-GCM `EnvelopeCrypto` (01 §7) + `blind_index` HMAC para teléfonos.
- LLM: Anthropic SDK oficial (`anthropic`), modelo `VAI_ANTHROPIC_MODEL` (default `claude-sonnet-5-5`).
  Todo acceso a Claude pasa por `app/ai/client.py` (Protocol `LLMClient` + `FakeLLM` para tests).
- IDs: `uuid4` (`uuid.UUID`, tipo `Uuid` de SQLAlchemy). Dinero COP como `int`. Fechas `timestamptz` UTC;
  zona de negocio `America/Bogota`.
- Ningún test toca la red. Externos (Anthropic, Twilio, Google) con fakes/respx.

## 3. Layout definitivo
```
app/
  main.py            create_app(): middlewares, static, auto-registro de routers
  cli.py             create-owner, seed, rotate-keys
  worker.py          arq WorkerSettings (lista de jobs + cron)
  core/              config, security, crypto, logging, errors, http (safe_fetch), rate_limit, clock, jobs, deps
  db/                base.py, session.py, repo.py (TenantScopedRepo), models/ (un archivo por dominio)
  auth/              login/logout, sesiones, CSRF, usuarios (router + service)
  audit/             log_event + vista
  tenants/           empresas, perfil, secretos, canales, wizard “Nueva empresa”
  plans/             catalog.py (+ YAML), entitlements, offers, quote, suscripciones
  billing/           registros de cobro, MRR
  niches/            templates/*.yaml (dentista, clinica_estetica, taller, restaurante) + loader/schema
  scraping/          crawl del sitio del negocio → kb_documents (usa core.http.safe_fetch)
  ai/                client.py (LLMClient, AnthropicClient, FakeLLM), prompts/
  factory/           build_bot(): genera BotConfig con Claude, revisión/publicación, UI
  conversation/      ConversationEngine, tools, guardrails, memoria, handoff, chat sandbox
  privacy/           consentimientos, opt-out, supresión, DSAR, retención, redacción PII
  channels/          Twilio WhatsApp (webhook, firma, envío, status, ventana 24h), voz stub
  booking/           disponibilidad, BookingService, calendario local, Google Calendar + OAuth
  reminders/         programación de recordatorios/seguimientos y jobs
  leads/             normalize, dedupe, scoring, csv_import, places, pipeline, secret_shop, demo
  outreach/          plantillas, ComplianceGate, campañas, sender, webhooks
  dashboard/         Inicio (KPIs), Conversaciones, Citas
  web/               templates/base.html, partials, static/ (htmx, css), filtros Jinja
tests/               conftest.py (app, db, client autenticado, fakes), unit/, integration/
```
**Regla de propiedad**: cada constructor solo crea/edita archivos dentro de sus carpetas asignadas
(+ sus tests en `tests/<modulo>/`). Si necesita algo de otro módulo, usa el **contrato** (§5); si el
contrato no alcanza, lo anota en `docs/plan/integracion-pendientes.md` (append) y el integrador lo resuelve.

## 4. Convenciones de código (las verifica el revisor)
- Routers delgados → `service.py` (transacción) → modelos/repos. Pydantic v2 para entrada/salida.
- Cada módulo con rutas expone `router = APIRouter(...)` en `app/<modulo>/router.py`; `main.py` los
  auto-registra (importa `app.<pkg>.router` si existe). Páginas HTML bajo `/admin/...`, JSON bajo `/api/...`,
  webhooks bajo `/webhooks/...`.
- Templates del módulo en `app/web/templates/<modulo>/*.html`, extienden `base.html`.
- Auth: `Depends(current_user)`, `Depends(require_role("owner","admin"))`, `Depends(verify_csrf)` en métodos
  que mutan (lo aplica un dependency global en routers `/admin` y `/api`; webhooks exentos, con firma).
- Sesión DB: `Depends(get_session)` → `AsyncSession`. Servicios reciben `session` explícita.
- Auditoría: `await audit.log_event(session, actor=..., action="tenant.create", entity_type=..., entity_id=..., tenant_id=...)`.
- Errores: `app.core.errors.AppError(code, message, status)` → handler JSON RFC7807 / partial HTML.
- Logs: `structlog` vía `app.core.logging.get_logger`; nunca loguear PII ni secretos.
- Tipado completo, `ruff` limpio, sin `print`. Tests pytest-asyncio (`asyncio_mode=auto`).

## 5. Contratos entre módulos (firmas que todos pueden importar)
La fundación crea estos archivos con la firma y un cuerpo `raise NotImplementedError` o implementación
mínima; el dueño del módulo los completa **sin cambiar la firma**.

```python
# app/core/http.py (fundación, implementación completa)
async def safe_fetch(url: str, *, max_bytes: int = 2_000_000, timeout: float = 8.0, max_redirects: int = 3) -> FetchResult
#   FetchResult(url: str, status: int, content_type: str, text: str)  ; lanza UnsafeURLError / FetchError

# app/core/jobs.py (fundación)
async def enqueue(job_name: str, *args, _defer_by: timedelta | None = None, _job_id: str | None = None, **kwargs) -> None
JOB_REGISTRY: dict[str, Callable]   # register_job(name)(fn) decorador; worker.py lo usa

# app/ai/client.py  (dueño: B5)
class LLMClient(Protocol):
    async def complete(self, *, system: str, messages: list[dict], tools: list[dict] | None = None,
                       tool_choice: dict | None = None, max_tokens: int = 1024, temperature: float = 0.2) -> LLMResponse
# LLMResponse(text: str, tool_calls: list[ToolCall(id, name, input: dict)], stop_reason: str, usage: dict)
def get_llm() -> LLMClient            # dependency; tests lo sobreescriben con FakeLLM

# app/niches/loader.py (dueño: B2)
def list_niches() -> list[str]; def get_niche_template(niche: str) -> NicheTemplate (pydantic)

# app/plans/entitlements.py (dueño: B2)
async def get_entitlements(session, tenant_id) -> Entitlements
async def assert_within_limit(session, tenant_id, metric: str) -> None   # PlanLimitExceeded
async def record_usage(session, tenant_id, *, conversations=0, messages_in=0, messages_out=0, tokens_in=0, tokens_out=0) -> None

# app/scraping/crawler.py (dueño: B3)
async def crawl_business_site(url: str, *, max_pages: int = 10) -> list[ScrapedPage(url, title, text)]

# app/factory/service.py (dueño: B4)
async def build_bot(session, tenant_id, inputs: FactoryInput, *, scrape: bool = True, llm: LLMClient | None = None) -> BotConfig
async def publish_bot(session, bot_config_id, *, actor) -> BotConfig

# app/conversation/engine.py (dueño: B5)
class ConversationEngine:
    async def handle_inbound(self, session, *, tenant_id, conversation_id, text: str) -> list[str]  # respuestas a enviar
async def sandbox_reply(session, tenant_id, history: list[dict], text: str, *, bot_config_id=None) -> str

# app/privacy/service.py (dueño: B7)
def is_optout_message(text: str) -> bool ; def is_optin_message(text: str) -> bool
async def apply_optout(session, *, phone_e164: str, tenant_id=None, evidence: str) -> None
async def is_suppressed(session, phone_e164: str) -> bool
def redact_pii(text: str) -> str
async def record_consent(session, tenant_id, contact_id, purpose: str, evidence: str) -> None

# app/channels/sender.py (dueño: B6)
async def send_whatsapp_text(session, *, tenant_id, to_e164: str, body: str) -> SendResult
async def send_whatsapp_template(session, *, tenant_id: UUID | None, to_e164: str, content_sid: str,
                                 variables: dict[str, str], from_override: str | None = None) -> SendResult
#   tenant_id=None => cuenta de la agencia (outreach). Respeta is_suppressed y VAI_TWILIO_DRY_RUN.
def validate_twilio_signature(url: str, params: dict, signature: str, auth_token: str) -> bool

# app/booking/service.py (dueño: B8)
class BookingService:
    async def find_slots(self, session, tenant_id, service_id, date_from: date, date_to: date, *, limit: int = 6) -> list[Slot]
    async def book(self, session, tenant_id, *, contact_id, service_id, starts_at: datetime, idempotency_key: str, source="bot") -> Appointment
    async def cancel(self, session, tenant_id, appointment_id, *, by: str, reason: str | None = None) -> Appointment
    async def reschedule(self, session, tenant_id, appointment_id, new_starts_at: datetime, *, by: str) -> Appointment
# app/booking/calendar_base.py: CalendarProvider Protocol (busy_intervals, create_event, update_event, delete_event)

# app/reminders/scheduler.py (dueño: B10)
async def schedule_for_appointment(session, appointment) -> None ; async def cancel_for_appointment(session, appointment_id) -> None

# app/leads/normalize.py (dueño: B11)
def to_e164_co(raw: str | None) -> tuple[str | None, str]  # (e164, phone_type: mobile|landline|unknown)
# app/leads/scoring.py (B11): def score_lead(lead, signals, secret_test, cfg=DEFAULT) -> ScoreResult
```

## 6. Modelos (los crea la fundación; nombres de tabla definitivos)
Basado en 01 §4 + ajustes de 03/04: `users, sessions, audit_log, plans, offers, subscriptions,
billing_records, usage_counters, niche_templates (opcional; YAML es la fuente), tenants (+is_demo,
dpa_accepted_at), tenant_profiles, tenant_secrets, channel_accounts, services, faqs, kb_documents,
bot_configs, contacts, consents, conversations, messages, handoffs, appointments, calendar_connections,
working_hours, time_off, scheduled_jobs, webhook_events, suppression_list, lead_sources, leads,
review_signals, lead_events, secret_shop_tests, message_templates, campaigns, campaign_targets,
outreach_messages, llm_usage`.
- Enums como `String` + `CheckConstraint` (portables SQLite/Postgres). JSON con `sa.JSON` (JSONB variant en PG).
- Anti doble reserva: en SQLite se garantiza con verificación transaccional en `BookingService` +
  índice único `(tenant_id, resource_key, starts_at)` para estados activos; en PG la migración agrega la
  exclusion constraint gist.

## 7. Planes comerciales (seed, ver support/05)
| code | Setup COP | Mensual COP | Conversaciones/mes | Calendarios | Seguimientos | Voz |
|---|---|---|---|---|---|---|
| basico | 800.000 | 250.000 | 500 | 1 | no | no |
| pro | 1.200.000 | 390.000 | 1.500 | 3 | sí | no |
| premium | 2.000.000 | 600.000 | 4.000 | 10 | sí | sí |
Oferta `fundador`: primeros 5 clientes, setup 100% off a cambio de testimonio en video. Garantía 30 días.

## 8. Asignación de los 20 constructores (Sonnet)
**Fase A — Fundación (1 agente, secuencial):**
- **B0 Fundación**: pyproject (deps), `app/core/*`, `app/db/*` (todos los modelos de §6), `app/auth`,
  `app/audit`, `app/main.py`, `app/cli.py`, `app/worker.py` (esqueleto), `app/web` (base.html con menú
  completo, static, filtros), stubs de contratos §5, `tests/conftest.py` + tests de core/auth/audit, Alembic.

**Fase B — Módulos en paralelo (17 agentes):**
| id | Carpetas | Alcance |
|---|---|---|
| B1 | tenants/ | CRUD empresas, perfil/horarios, servicios/FAQs editables, secretos cifrados (solo last4), channel_accounts, **wizard Nueva empresa (3 pasos) que llama `factory.build_bot`** |
| B2 | niches/, plans/ | 4 YAML de nicho (de support/01–04) + schema/loader; catálogo de planes, entitlements, uso, ofertas, cotizador, suscripciones, UI “Planes” y cotizador |
| B3 | scraping/ | crawler SSRF-safe de la web del negocio (robots, mismo dominio, límite páginas), extracción de texto/precios/horarios, guarda `kb_documents` |
| B4 | factory/ | generación del BotConfig con Claude (tool forzada con JSON schema), grounding de precios, versiones draft→published, rollback, UI revisión + chat de prueba (usa `sandbox_reply`) |
| B5 | ai/, conversation/ | `LLMClient`/`AnthropicClient`/`FakeLLM`, ConversationEngine (tool loop ≤4, tools check_availability/book/cancel/get_business_info/handoff, memoria 12 msgs + resumen), system prompt fijo, guardrails on-topic, anti prompt-injection, handoffs |
| B6 | channels/ | webhook Twilio WhatsApp (firma, idempotencia por MessageSid, ruteo por número), status callback, ventana 24h, envío texto/plantilla (httpx, dry-run), procesamiento inbound → privacy → engine → envío, voz stub |
| B7 | privacy/ | opt-out/opt-in, supresión, consentimientos, aviso de privacidad, DSAR export/erase, retención (job), `redact_pii`, página pública `/privacidad` (textos de support/12) |
| B8 | booking/ (núcleo) | `compute_slots` (horarios, festivos CO, buffers), BookingService, LocalCalendarProvider, anti doble reserva, UI Citas del tenant |
| B9 | booking/google* | GoogleCalendarProvider (httpx, freebusy, events), OAuth por tenant (state firmado, tokens cifrados, refresh), conectar/desconectar en UI |
| B10 | reminders/, worker.py (lista de jobs) | recordatorios 24h/2h, no-show, reactivación según plan, quiet hours, cron de jobs |
| B11 | leads/ (datos) | normalize, dedupe/merge, scoring + `review_signal_scan`, import CSV con preview y anti CSV-injection, export CSV |
| B12 | leads/places.py, leads/router + UI | Places API (New) con field mask, presupuesto, búsqueda por nicho+ciudad+barrios, job, UI buscador + tabla + kanban |
| B13 | leads/ (ventas) | pipeline/transiciones, prueba secreta (registro, tiempos, timeout 24h), pitch-evidence, **lead → bot demo en un clic** + página `/demo/{token}`, convertir a cliente |
| B14 | outreach/ | plantillas (support/07), ComplianceGate, campañas (preview/start/pause), dispatch job, webhooks inbound/status de outreach, kill switch + dry-run, UI Campañas |
| B15 | billing/, dashboard/ | registros de cobro, mensualidades, MRR; Inicio con KPIs; Conversaciones (lista + detalle + tomar control) |
| B16 | deploy | Dockerfile, docker-compose(.prod), Caddy, Makefile, CI GitHub Actions, `.env.example`, README en español (guía de uso paso a paso), `docs/runbook.md` |
| B17 | docs/ventas | `docs/ventas/` con guion, objeciones, plantillas y plan comercial listos para usar (de support/05–07,12) + página `/admin/kit-ventas` (lectura) |

**Fase C — Consolidación (2 agentes):**
- **B18 Integrador**: corre toda la suite, resuelve `integracion-pendientes.md`, genera migración Alembic,
  arranca la app y recorre las páginas, corrige wiring.
- **B19 Revisor seguridad/calidad** (ECC `security-reviewer` + `fastapi-reviewer` + `python-reviewer`):
  audita y **corrige** hallazgos críticos/altos, deja informe en `docs/revision-seguridad.md`.
