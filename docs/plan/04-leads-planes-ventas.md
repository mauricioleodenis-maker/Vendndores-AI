# 04 - Leads, outreach, prueba secreta, planes, facturacion, lead -> demo bot

Dominio: `app/leads`, `app/outreach`, `app/plans`, `app/billing`. Mundo **agencia** (sin tenant_id salvo `subscriptions`/`billing_records`). Reutiliza de 01: `suppression_list`, `audit_log`, `crypto.py` (phone_hash HMAC), `rate_limit.py`, arq. Reutiliza de 02: `bots.factory.build_bot(...)`.
Referencias ECC seguidas: data-scraper-agent (capas COLLECT->ENRICH->STORE, dedupe, scoring por lotes, feedback), security-review (SSRF, secretos, rate limit), api-design, fastapi-patterns.

## 1. Cambios a 01 (reconciliacion)
- `leads.status` pasa a **pipeline en espanol**: `nuevo, prueba_secreta, contactado, demo, cerrado` + estados terminales ortogonales `perdido`, `no_contactar` (columna separada `stage` y `disposition` enum(`activo`,`perdido`,`no_contactar`)). Los valores de 01 (`replied`, etc.) se eliminan; "respondio" vive en `outreach_messages.status`.
- `campaigns.send_window` y `daily_limit` se conservan. `message_templates.scope=outreach` se usa aqui.
- `plans.limits` se amplia (ver s.9). Seed de planes vive en `app/plans/catalog.py` y se aplica por migracion de datos idempotente (upsert por `code`).

## 2. Estructura de modulos
```
app/leads/
  models.py        # Lead, LeadSource, LeadEvent, SecretShopTest, ReviewSignal
  schemas.py       # pydantic v2 (LeadOut, LeadCreate, PlacesSearchIn, CsvImportReport)
  places.py        # PlacesClient (Google Places API New, httpx async)
  csv_import.py    # parse + column mapping + report
  normalize.py     # to_e164_co(), normalize_url(), name_key(), phone_hash()
  dedupe.py        # find_duplicate(), merge_leads()
  scoring.py       # score_lead(), review_signal_scan()
  pipeline.py      # transiciones validas, LeadEvent
  secret_shop.py   # prueba secreta: registro y medicion de tiempos
  demo.py          # lead_to_demo_bot()
  service.py       # LeadService (orquesta, transacciones)
  router.py        # /api/leads, /admin/leads (HTMX)
app/outreach/
  models.py campaigns.py templates.py sender.py compliance.py optout.py webhooks.py router.py
app/plans/
  catalog.py entitlements.py offers.py service.py router.py
app/billing/
  models.py service.py router.py   # registros simples, sin pasarela en MVP
app/workers/leads_jobs.py outreach_jobs.py  # funciones arq
templates/admin/leads/*.html, outreach/*.html, plans/*.html  # Jinja2+HTMX en espanol
```

## 3. Modelo de datos (adiciones/ajustes; Alembic revision `0004_leads_sales`)
- `lead_sources` (01) + `params jsonb` (query, city, radius, niche, fileName, sha256 del CSV), `stats jsonb` (found, new, duplicates, rejected).
- `leads` (01) ajustado: `stage` enum(nuevo,prueba_secreta,contactado,demo,cerrado) default nuevo; `disposition` enum(activo,perdido,no_contactar) default activo; `place_id` unique null; `maps_url`; `phone_e164` null; `phone_hash` (HMAC-SHA256 con `PHONE_HASH_KEY`), `phone_enc` (cifrado); `phone_type` enum(mobile,landline,unknown) (WhatsApp solo movil CO: +57 3xx); `name_key` (normalizado sin tildes/forma juridica) ; `website_domain`; `instagram_handle`; `business_status` (OPERATIONAL/CLOSED); `price_level` smallint null; `owner_user_id` FK users null; `score`, `score_breakdown jsonb`, `score_version` int; `tags text[]`; `converted_tenant_id` FK null; `demo_tenant_id` FK null; `last_contacted_at`; `next_action_at`. Indices: unique(phone_hash) WHERE phone_hash IS NOT NULL AND disposition<>'perdido'... (parcial, ver s.5), (stage, score desc), (niche, city), GIN(tags), pg_trgm GIN(name).
- `review_signals`: id, lead_id FK, source enum(places,csv), review_ref (hash del texto), rating smallint, text_excerpt (<=280 chars), published_at, matched_patterns text[], created_at. Solo se guardan resenas con match negativo "no contestan" (minimizacion).
- `lead_events`: id, lead_id, ts, actor_user_id null, kind enum(created,imported,scored,stage_changed,note,outreach_sent,reply_received,opt_out,secret_shop_sent,secret_shop_replied,demo_created,converted), data jsonb. Append-only (timeline en UI).
- `secret_shop_tests`: id, lead_id, created_by, channel enum(whatsapp,instagram,llamada,web_form), sent_at (timestamptz, el momento real, editable por el operador), scenario enum(precio,cita,horario,urgencia), message_text, first_reply_at null, first_reply_excerpt, response_seconds int null (columna generada o calculada), after_hours bool (calculado con horario del lead o 08-18 Bogota), outcome enum(sin_respuesta,respuesta_lenta,respuesta_ok,respuesta_con_cierre), notes. Regla: `sin_respuesta` si no hay respuesta a las 24h (job). Un test cuenta como "dolor" si response_seconds > 900 (15 min en horario) o sin_respuesta.
- `campaigns` (01) + `stage_target` enum, `template_id` FK, `audience_filter jsonb` (niche, city, min_score, stage, tags), `paused_reason`, `stats jsonb`. `campaign_targets`: campaign_id, lead_id, status, unique(campaign_id, lead_id) (idempotencia al "congelar" la audiencia).
- `outreach_messages` (01) + `idempotency_key` unique (`{campaign_id}:{lead_id}:{step}`), `step` smallint (1=pitch, 2=seguimiento), `delivery_error_code`, `replied_at`, `content_variables jsonb`.
- `message_templates` (01) + `kind` enum(pitch,seguimiento,optout_confirm,prueba_secreta_hint), `variables text[]`, `twilio_content_sid`, `approval_status` enum(draft,submitted,approved,rejected), `locale` es_CO. Versionado: unique(name, version).
- `suppression_list` (01) + `source_lead_id`, `evidence` (texto del STOP). `phone_hash` unique.
- `plans` (01) + `description`, `features jsonb` (lista de textos UI), `sort`, `is_public`. `offers`: id, code (`fundador`), name, discount_type enum(setup_pct,monthly_pct,monthly_fixed_months_free), value, max_redemptions, redeemed int, valid_until, applies_to_plan_codes text[], conditions text (ej. "caso de estudio + testimonio"), is_active. `subscriptions` (01) + `offer_id` FK null, `guarantee_until` date, `guarantee_status` enum(vigente,reclamada,vencida).
- `billing_records`: id, tenant_id FK, subscription_id FK, kind enum(setup,mensualidad,ajuste,reembolso,descuento), period (YYYY-MM null), amount_cop int (negativo para reembolso/descuento), iva_cop int (19%; flag `prices_include_iva` en settings), status enum(pendiente,pagado,vencido,anulado), due_date, paid_at, method enum(transferencia,nequi,daviplata,pse,efectivo,otro), reference text, external_invoice_no text null (factura electronica se emite fuera: Siigo/Alegra, MVP solo referencia), notes, created_by. Unique(subscription_id, kind, period) WHERE kind='mensualidad'.

## 4. Google Places API (`app/leads/places.py`)
Usar **Places API (New)** `https://places.googleapis.com/v1/`. Solo API oficial; sin scraping de HTML de Maps. Clave `GOOGLE_PLACES_API_KEY` en env/secret manager (nunca en repo, logs ni templates); restringida por IP en GCP.
```python
class PlacesClient:
    def __init__(self, api_key: SecretStr, http: httpx.AsyncClient, limiter: AsyncRateLimiter): ...
    async def text_search(self, query: str, *, location_bias: LatLngCircle | None, page_token: str | None = None, page_size: int = 20) -> PlacesPage
    async def details(self, place_id: str) -> PlaceDetails
    async def search_all(self, niche: str, city: str, *, max_results: int = 60) -> AsyncIterator[PlaceSummary]
```
- Text Search: `POST places:searchText`, body `{"textQuery": "dentista en Cali, Colombia", "languageCode":"es","regionCode":"CO","pageSize":20,"pageToken":...}`; header `X-Goog-Api-Key`, `X-Goog-FieldMask: places.id,places.displayName,places.formattedAddress,places.rating,places.userRatingCount,places.nationalPhoneNumber,places.internationalPhoneNumber,places.websiteUri,places.googleMapsUri,places.businessStatus,places.priceLevel,nextPageToken`. El **field mask es obligatorio** y controla el costo (SKU Pro/Enterprise por campos; telefono, website y rating caen en Enterprise: calcular costo por busqueda y mostrar estimado en UI antes de ejecutar).
- Details: `GET places/{id}` con mask `id,displayName,nationalPhoneNumber,internationalPhoneNumber,websiteUri,rating,userRatingCount,reviews,regionalOpeningHours,googleMapsUri`. `reviews` devuelve max 5 resenas (relevantes): se usan para `review_signal_scan`; para mas resenas, el CSV importado del Maps-Scraper aporta texto.
- Queries por niche: mapa en `niche_queries` (dentista -> ["dentista","odontologo","clinica dental"], clinica_estetica -> ["clinica estetica","medicina estetica","spa facial"], taller -> ["taller mecanico","taller automotriz","latoneria y pintura"], restaurante -> ["restaurante"]). Ciudad y barrio opcional (`"dentista en Granada, Cali"`) para superar el tope ~60 resultados por query (3 paginas): `search_all` itera variantes query x barrio y deduplica por `place.id`.
- Resiliencia: timeouts 10s, `tenacity` retry exponencial solo en 429/5xx (max 4), limiter `GOOGLE_PLACES_QPS=5`, tope diario de gasto `PLACES_DAILY_BUDGET_COP` (contador en Redis; al excederse -> 429 interno "presupuesto agotado"). Errores 400/403 no se reintentan y se registran sin la key.
- Cumplimiento ToS: guardar `place_id` (permitido indefinidamente); datos de contenido (nombre, telefono, rating) se **refrescan** cada 30 dias (`refresh_stale_leads` job) en vez de tratarlos como base propia; no se muestran sobre mapa de otro proveedor. Resenas: guardar solo extracto + flags, no replicar.
- Endpoint: `POST /api/leads/search` `{niche, city, neighborhoods?, max_results<=100, min_reviews?, dry_run:bool}` -> encola job arq `search_places_job(source_id)`; respuesta 202 `{source_id}`; UI HTMX hace poll a `/admin/leads/sources/{id}/status`.

## 5. Importar CSV del Maps-Scraper (`csv_import.py`)
```python
@dataclass
class ColumnMap: name: str; phone: str|None; address: str|None; website: str|None; rating: str|None; reviews_count: str|None; reviews_text: str|None; maps_url: str|None; place_id: str|None; category: str|None
def detect_columns(headers: list[str]) -> ColumnMap          # sinonimos es/en: "Titulo|title|name", "Telefono|phone", "Calificacion|rating|stars", "Resenas|reviews"
def parse_csv(stream: BinaryIO, colmap: ColumnMap, *, niche: str, city: str) -> Iterator[LeadCandidate | RowError]
async def import_csv(session, source: LeadSource, candidates, *, dry_run: bool) -> CsvImportReport  # {total, created, merged, skipped_no_phone, invalid, errors[:50]}
```
- `POST /api/leads/import` multipart; controles: max 5 MB / 5.000 filas, solo `text/csv` (+ sniff, UTF-8 con BOM o latin-1 fallback), `csv.DictReader` con limite de campo, neutraliza formula injection al re-exportar (prefijo `'` si empieza con `= + - @`), paso de **previsualizacion** (dry_run + mapeo editable en UI) y luego confirmacion. Guarda sha256 del archivo en `lead_sources.params` para avisar re-subidas.
- Texto de resenas del CSV -> `review_signal_scan`; se descarta el resto.
- Es la ruta principal si la agencia ya tiene Maps-Scraper; Places API es complemento (enriquecer por `place_id`/nombre: `enrich_from_places` opcional).

## 6. Normalizacion y dedupe (`normalize.py`, `dedupe.py`)
- `to_e164_co(raw) -> str|None`: quita espacios/guiones/parentesis, `+57`/`57`/`0057` prefijo, movil = 10 digitos que inician en 3 (`+573XXXXXXXXX`), fijo Cali = 7 digitos -> prefijo `+602` (fijo, marcado `landline`; no se envia WhatsApp, solo llamada/otro canal). Usar `phonenumbers` (region CO) como validador.
- `phone_hash(e164)`: HMAC-SHA256 con clave aparte de la de cifrado; el mismo hash se usa en `suppression_list` y `contacts` (01).
- Prioridad de dedupe: (1) `place_id` exacto; (2) `phone_hash` igual; (3) `website_domain` igual (excluyendo dominios genericos: instagram.com, facebook.com, linktr.ee, wa.me); (4) `name_key` con similitud trigram >= 0.85 **y** misma ciudad. (4) solo marca `possible_duplicate_of` para revision manual, no fusiona.
- `merge_leads(winner, loser)`: conserva el valor mas completo por campo, une `tags`, reasigna `lead_events`/`outreach_messages`/`secret_shop_tests`, `loser` queda `disposition=perdido` con tag `merged`. Nunca fusiona si alguno es `no_contactar` -> el merge hereda `no_contactar`.
- Constraint BD: unique parcial `(phone_hash)` where `phone_hash is not null and tags not @> '{merged}'`; el servicio usa `INSERT ... ON CONFLICT DO UPDATE` solo sobre campos vacios.

## 7. Scoring (`scoring.py`, version 1; determinista, explicable, sin LLM en el camino critico)
```python
def score_lead(lead: Lead, signals: list[ReviewSignal], test: SecretShopTest|None, cfg: ScoringConfig) -> ScoreResult  # score 0-100 + breakdown
def review_signal_scan(reviews: list[str]) -> list[SignalMatch]   # regex es-CO
```
Puntos (suman, tope 100; pesos en `ScoringConfig`, editables en UI):
- Rating: <4.0 = +15; 4.0-4.49 = +25 (zona dolor "bueno pero mejorable"); >=4.5 = 0 (el pitch es prevenir, baja prioridad). Sin rating = 0.
- Volumen de resenas (demanda real): >=200 +20; 50-199 +15; 20-49 +8; <20 +0.
- Resenas "no contestan" (`review_signal_scan` patrones: `no (me )?contest`, `nunca (me )?respond`, `no responden`, `no contestan (el )?(whatsapp|telefono|llamad)`, `dejaron? en visto`, `no atienden`, `no (me )?devolvieron la llamada`, `imposible comunicar`, `tardaron? en responder`): 1 match +15, 2+ +25.
- Contactabilidad: movil valido +15 (fijo +3; sin telefono -> score tope 20); sitio web con boton WhatsApp/Calendly ausente +5; sin web, solo Instagram +5.
- Prueba secreta: `sin_respuesta` o >15 min +25 (la senal mas fuerte); respuesta_ok <5 min -15.
- Fit de nicho: niche en {dentista, clinica_estetica} +5 (ticket alto), `business_status!=OPERATIONAL` -> score 0 y `disposition=perdido`.
- `score_breakdown` guarda cada regla y puntos (UI muestra "por que"). `score_version` permite re-score masivo (`rescore_all_job`). Bandas: >=70 caliente, 40-69 tibio, <40 frio.
- Fase 2 (opcional): resumen de dolor con Claude (`claude-sonnet-5-5`, por lotes de 20 resenas, JSON schema estricto, cache por `review_ref`), nunca decide el score solo. Feedback loop (data-scraper-agent): al marcar `cerrado`/`perdido` se registran features para recalibrar pesos manualmente.

## 8. Pipeline y prueba secreta
Transiciones (`pipeline.py`, tabla `ALLOWED`): nuevo -> prueba_secreta -> contactado -> demo -> cerrado; se permite saltar `prueba_secreta` (nuevo -> contactado) y retroceder un paso; `cerrado` exige `converted_tenant_id`. Cada cambio crea `lead_event` y `audit_log`. UI: kanban HTMX (columnas = etapas, drag via `hx-post /admin/leads/{id}/stage`), filtros niche/ciudad/score/banda, vista tabla con acciones en lote (max 200).
**Prueba secreta** (el operador, desde un numero/cuenta propia de la agencia, escribe como cliente al negocio y mide):
- `POST /api/leads/{id}/secret-shop` `{scenario, channel, sent_at?, message_text}` -> crea test, mueve a `prueba_secreta`, genera guion sugerido por escenario (plantillas internas, ej. "Hola, ¿cuanto vale una limpieza dental y tienen cita para esta semana?"). Nota de etica: es un mensaje de cliente potencial real, sin hacerse pasar por persona concreta, sin datos sensibles de salud, una sola vez por negocio.
- `POST /api/secret-shop/{test_id}/reply` `{first_reply_at, excerpt}` -> calcula `response_seconds`, `after_hours`, `outcome`; dispara `rescore`. Registro manual (MVP). El sistema NO lee el WhatsApp del negocio.
- Job `secret_shop_timeout_job` (arq cron cada 15 min): marca `sin_respuesta` a las 24h y crea `lead_event`; recordatorio al operador para revisar a los 15/60 min.
- Salida para el pitch: `GET /api/leads/{id}/pitch-evidence` -> `{response_minutes, scenario, sent_at}` que alimenta variables de la plantilla ("escribi a las 4:12 pm y no hubo respuesta en 2 horas"). Solo se cita al negocio mismo, nunca con nombres de terceros.

## 9. Outreach (`app/outreach`)
Canal principal WhatsApp via Twilio **plantillas aprobadas** (Content API, mensajes iniciados por el negocio fuera de ventana 24h). Meta: categoria `MARKETING`/`UTILITY` segun contenido; el pitch en frio es marketing y exige opt-in en muchos casos -> **riesgo de politica** (ver s.14); mitigar con: primer contacto breve y relevante, identificacion de la agencia, opt-out claro, y alternativa canal manual (llamada/Instagram DM manual con copia de texto) controlada por el operador.
```python
class OutreachSender:
    async def send_step(self, msg: OutreachMessage) -> SendResult      # Twilio Content API: messages.create(to=whatsapp:+57..., from_=messaging_service_sid, content_sid=..., content_variables=json)
class ComplianceGate:
    async def check(self, lead: Lead, campaign: Campaign, now: datetime) -> GateDecision  # allow | deny(reason) | defer(until)
def render_template(tpl: MessageTemplate, lead: Lead, evidence: PitchEvidence|None) -> RenderedMessage  # valida variables, longitud, sin URLs acortadas
```
- Plantillas (seed en `app/outreach/templates_seed.py`, es_CO, las aprueba Meta via Twilio; `twilio_content_sid` se guarda tras aprobar): 
  1. `pitch_v1` (nombre, niche): "Hola {{1}}, soy {{2}} de {{3}}. Ayudamos a {{4}} de Cali a responder WhatsApp al instante y agendar citas 24/7. ¿Le muestro una demo con los datos de {{1}}? Responda STOP para no recibir mas mensajes." 
  2. `pitch_prueba_v1` (con evidencia de prueba secreta) tono respetuoso, no acusatorio.
  3. `seguimiento_v1` (una sola vez, a los 3 dias si no hay respuesta ni opt-out). Maximo 2 pasos por lead en total; luego `disposition` queda activo pero sin mas envios automaticos.
  4. `optout_confirm`: "Listo, no volveremos a escribirle." (respuesta a STOP, sesion 24h abierta).
- `ComplianceGate` (todo debe cumplirse, orden fail-closed): lead `disposition=activo`; `phone_type=mobile`; `phone_hash` no en `suppression_list`; plantilla `approved`; ventana horaria 08:00-19:00 L-S America/Bogota (excluye domingos y festivos CO via `holidays`); limite diario por campana `daily_limit` (default 20 al inicio, rampa +10/dia hasta tope `OUTREACH_MAX_DAILY=80` para proteger la calidad del numero); limite por lead (max 2 mensajes, min 72h entre pasos); limite global por segundo (Twilio MPS: 1 msg/s) con token bucket Redis; calidad: si `failed` o `bloqueos` > 5% en ultimas 100, la campana se pausa (`paused_reason='quality'`).
- Webhooks `POST /webhooks/twilio/outreach-status` (StatusCallback, firma `X-Twilio-Signature` validada con `RequestValidator`, 403 si falla) actualiza `outreach_messages.status`; `POST /webhooks/twilio/outreach-inbound` procesa respuestas: detecta opt-out (`optout.py`: regex insensible a tildes `^\s*(stop|baja|no mas|no gracias|cancelar|salir|desuscribir|no me escriban)\b` + ES/EN), inserta en `suppression_list`, pone `no_contactar`, cancela mensajes `queued`, envia `optout_confirm`; si no es opt-out -> `replied_at`, evento, sube lead a `contactado` y notifica al operador (handoff humano; el bot NO conversa en frio con leads). Idempotencia por `MessageSid`.
- Aviso Ley 1581: cada lead tiene `source` y base legal "interes legitimo comercial B2B sobre datos publicos de empresa"; primer mensaje incluye identidad del responsable y canal para suprimir; endpoint `GET /api/leads/{id}/dsar` y `DELETE` (erase) borran PII y dejan solo `phone_hash` en suppression. Telefonos personales de personas naturales: tratarlos igual con opt-out; el area legal debe validar (ver s.14).
- Orquestacion: `POST /api/campaigns` (nombre, plantilla, filtro) -> `POST /api/campaigns/{id}/start` congela audiencia en `campaign_targets` (preview con conteo y 5 mensajes renderizados, requiere confirmacion explicita del owner), job arq `dispatch_campaign_job` cada minuto toma lotes respetando el gate; `queued -> sent` con `idempotency_key` (no duplica en reintentos). Botones pausar/reanudar/cancelar. Rol `operator` puede preparar, solo `owner/admin` inician.
- Kill switch: `OUTREACH_ENABLED=false` por defecto; modo `OUTREACH_DRY_RUN=true` renderiza y registra sin llamar a Twilio.

## 10. Planes y ofertas (`app/plans`)
Seed `catalog.py` (COP, sin IVA salvo que se indique; coincide con 01):
| code | Setup | Mensual | Incluye | limits |
|---|---|---|---|---|
| basico | 800.000 | 250.000 | 1 numero WhatsApp, respuesta de precios/FAQ/horarios, agenda 1 calendario Google, recordatorios de cita, panel basico | `max_conversations_month=500, max_calendars=1, followups_enabled=false, voice_enabled=false, max_services=15, max_users=1, support="email"` |
| pro | 1.200.000 | 400.000 | todo Basico + seguimientos automaticos (no-show, reactivacion), handoff a humano con resumen, hasta 3 calendarios/profesionales, reporte mensual, 1 ajuste de bot/mes | `max_conversations_month=1500, max_calendars=3, followups_enabled=true, voice_enabled=false, max_services=40, max_users=3, support="whatsapp"` |
| premium | 2.000.000 | 600.000 | todo Pro + voz (cuando exista) o canal adicional, calendarios ilimitados (tope 10), integraciones a medida, prioridad, revision mensual de conversaciones | `max_conversations_month=4000, max_calendars=10, followups_enabled=true, voice_enabled=true, max_services=100, max_users=10, support="prioritario"` |
- Excedente: mensaje "plan alcanzado" al operador al 80% y 100%; el bot sigue respondiendo hasta 110% (colchon) y luego solo handoff; ofrecer upgrade. Sin cobro automatico por excedente en MVP.
- `entitlements.py`: `def entitlements_for(tenant_id) -> Entitlements`, `async def assert_within_limit(session, tenant_id, metric) -> None` (lanza `PlanLimitExceeded`), `has_feature(tenant_id, "followups")`. Usado por bot (02), booking y tenants.service. Cacheado 60 s en Redis, invalidado al cambiar suscripcion.
- **Oferta Fundador** (`offers.code='fundador'`): primeros 5 clientes; setup 50% off (p. ej. Pro 600.000) y mensualidad congelada de por vida en el precio del plan contratado; condiciones: testimonio + permiso de caso de estudio + feedback quincenal 3 meses. `OfferService.apply(subscription, offer)` valida cupos con `SELECT ... FOR UPDATE` sobre `offers.redeemed < max_redemptions` y `valid_until`. UI muestra cupos restantes ("quedan 3").
- **Garantia**: 30 dias; si en ese periodo el bot no cumple los objetivos acordados (ej. responder <1 min y agendar al menos X citas, definido por escrito por tenant) se reembolsa la mensualidad (no el setup, o setup parcial; texto legal a definir con abogado). `guarantee_until = started_at + 30d`; `POST /api/subscriptions/{id}/guarantee-claim` crea `billing_record(kind=reembolso)` pendiente de aprobacion del owner.
- Endpoints: `GET /api/plans` (publico para landing/cotizador, solo `is_public`), `POST /api/tenants/{id}/subscription` `{plan_code, offer_code?, custom_monthly_fee_cop?}` (owner/admin), `PATCH` para upgrade/downgrade (prorrateo manual, registra `ajuste`), `GET /admin/plans` (editor de precios; cambia precio solo en nuevas suscripciones; versionar con `plans.version`).
- Cotizador `quote(plan, offer, include_iva) -> Quote{setup, monthly, discounts[], iva, total_first_invoice}` puro, con tests.

## 11. Facturacion simple (`app/billing`)
- Sin pasarela en MVP: registros manuales. `BillingService.generate_setup_record(subscription)` al activar; job arq `generate_monthly_records_job` (dia 1, idempotente por unique) crea `mensualidad` pendiente; marca `vencido` al pasar `due_date`; recordatorio al owner. `mark_paid(record_id, method, reference, paid_at)` con `audit_log`.
- Endpoints: `GET /api/billing?tenant_id=&status=&period=`, `POST /api/billing/{id}/mark-paid`, `GET /api/billing/summary` (MRR, setups del mes, vencido, churn) y `GET /admin/billing` (tabla + totales). Export CSV (accion auditada, con neutralizacion de formulas). Si mora > 15 dias, `subscriptions.status=past_due` y alerta (la pausa del bot es decision manual del owner).
- Fase 2: Wompi/PayU/ePayco con webhooks firmados; facturacion electronica DIAN via proveedor (Alegra/Siigo).

## 12. Lead -> Demo bot (un clic) (`leads/demo.py`)
```python
async def lead_to_demo_bot(session, lead_id: UUID, *, actor: User, scrape: bool = True) -> DemoResult  # {tenant_id, bot_config_id, demo_link, expires_at}
```
- `POST /api/leads/{id}/demo-bot` (HTMX boton "Crear demo", idempotente: si `lead.demo_tenant_id` existe, devuelve el existente salvo `?rebuild=true`).
- Pasos: (1) crea `tenant` con `status=draft`, `slug`=slugify(name) unico, `is_demo=true` (columna nueva en tenants), niche mapeado (`places type -> niche enum`; si `otro`, pedir seleccion en modal), city, website, instagram, telefono de contacto del lead; plan `basico` trial; (2) arma `FactoryInput` con lo conocido (name, niche, website_url, instagram_url, address, rating y horario de Places si hay) y llama `bots.factory.build_bot(tenant_id, inputs, scrape=scrape)` de 02 (plantilla de nicho + scraping SSRF-safe); servicios/precios faltantes se marcan `needs_review` en vez de inventarse; (3) NO se crea canal con numero real del negocio; el demo corre en un **numero sandbox de la agencia**: `channel_accounts` con `phone_e164`=numero demo compartido y ruteo por codigo de sesion (`demo-<slug>`) enviado en el link `https://wa.me/<demo>?text=DEMO-<slug>`; sesion demo expira a 7 dias y max 30 mensajes (`DEMO_MAX_MESSAGES`), con aviso "Esto es una demostracion". Alternativa sin WhatsApp: pagina web de chat `/demo/{token}` (token firmado, `itsdangerous`, expira) que usa el mismo `brain`; (4) mueve lead a `demo`, guarda `demo_tenant_id`, `lead_event(demo_created)`, `audit_log`.
- El demo no usa datos de salud reales; guardrails de 02 siguen activos (solo tema del negocio, sin consejo medico). No se contacta al negocio automaticamente: el operador comparte el link.
- Conversion: `POST /api/leads/{id}/convert` `{plan_code, offer_code?}` promueve el tenant demo (`is_demo=false`, `status=draft->building`), crea suscripcion + `billing_record(setup)`, lead -> `cerrado`, `converted_tenant_id`. Limpieza: job `purge_expired_demos_job` borra tenants demo vencidos sin conversion (CASCADE controlado + auditoria).

## 13. Endpoints resumen (prefijo `/api`, JSON; `/admin` HTMX parciales; ver api-design)
Auth: sesion de dashboard + CSRF (01). Roles: owner/admin todo; operator leads, prueba secreta, borradores de campana, sin inicio de envio ni facturacion.
- `GET /api/leads` (paginacion cursor, filtros `stage,niche,city,min_score,q,tag`), `POST /api/leads` (manual), `GET/PATCH /api/leads/{id}`, `POST /api/leads/{id}/stage`, `POST /api/leads/{id}/score`, `POST /api/leads/{id}/notes`, `DELETE /api/leads/{id}` (erase), `POST /api/leads/merge`, `GET /api/leads/export.csv` (owner, auditado)
- `POST /api/leads/search`, `POST /api/leads/import`, `GET /api/lead-sources/{id}`
- `POST /api/leads/{id}/secret-shop`, `POST /api/secret-shop/{id}/reply`, `GET /api/leads/{id}/pitch-evidence`
- `POST /api/leads/{id}/demo-bot`, `POST /api/leads/{id}/convert`
- `GET/POST /api/outreach/templates`, `POST /api/campaigns`, `GET /api/campaigns/{id}`, `POST /api/campaigns/{id}/{preview|start|pause|resume|cancel}`, `GET /api/campaigns/{id}/messages`
- `POST /webhooks/twilio/outreach-status`, `POST /webhooks/twilio/outreach-inbound`
- `GET /api/plans`, `POST/PATCH /api/tenants/{id}/subscription`, `GET /api/offers`, `GET /api/billing...`
Errores RFC 7807 (`application/problem+json`), rate limit en endpoints costosos (`search` 10/h por usuario, `import` 20/h).

## 14. Seguridad y cumplimiento
- Secretos solo en env (`GOOGLE_PLACES_API_KEY`, `TWILIO_*`, `PHONE_HASH_KEY`); `SecretStr`; sin logs de keys ni de cuerpos completos; structlog redacta telefonos (`+57***1234`).
- Validacion estricta pydantic (E.164, URLs http/https). Cualquier fetch de sitio web del lead (enriquecimiento/factory) pasa por `safe_fetch` de 02: bloquea IPs privadas/loopback/link-local/metadata, resuelve DNS y revalida tras redirect, limite 2 MB, 5 s, solo http(s), sin ejecutar JS.
- Salida HTML con autoescape Jinja; resenas y nombres de terceros son **datos no confiables** (stored XSS, prompt injection: si se pasan a Claude, delimitarlos y pedir JSON validado; nunca ejecutar instrucciones de ellos).
- Anti-abuso: `OUTREACH_ENABLED` off por defecto, dry-run, aprobacion explicita, limites duros en codigo (no solo config), auditoria de cada envio y export (`outreach.send`, `lead.export`).
- RLS/aislamiento: tablas de agencia solo accesibles por rol de app con permiso; `billing_records` y `subscriptions` con `tenant_id` y repos tenant-scoped para vistas del tenant.
- Riesgos abiertos: (a) politica de WhatsApp Business sobre mensajes en frio/opt-in: usar canales alternativos (llamada, DM manual, email) y consultar abogado sobre Ley 1581 y Circular SIC de marketing; (b) ToS de Places sobre cache/almacenamiento; (c) riesgo de bloqueo del numero emisor: usar numero dedicado de la agencia con warm-up; (d) garantia y terminos del contrato requieren revision legal; (e) IVA 19% y factura electronica.

## 15. Tests (pytest, SQLite async; Twilio y Google con `respx`/fakes; cobertura >=80%)
- Unit: `to_e164_co` (casos fijo/movil/ruido), `name_key`, `review_signal_scan` (positivos/negativos, tildes), `score_lead` (tabla de casos y bandas, tope 100, rating>=4.5), `quote()`, `optout` regex, `ComplianceGate` (ventana, domingo/festivo, lista supresion, limites, plantilla no aprobada, fail closed ante error), pipeline `ALLOWED`, `entitlements`.
- Integracion: `PlacesClient` con `respx` (paginacion, field mask enviado, retry en 429, no retry 403, tope de presupuesto); import CSV (BOM, latin-1, columnas sinonimas, formula injection, limite filas, dry-run vs commit, dedupe por place_id/telefono/dominio); dispatch de campana (idempotencia ante reintento, pausa por calidad, dry-run no llama Twilio); webhook inbound STOP -> suppression + cancela cola; firma Twilio invalida -> 403; secret shop (calculo `response_seconds`, timeout 24h); `lead_to_demo_bot` con `factory` mockeado (idempotente, mapeo de nicho, no crea canal real); conversion crea suscripcion + billing; oferta Fundador respeta cupos bajo concurrencia (test con 2 sesiones en Postgres marcado `@pytest.mark.pg`).
- Seguridad: operator no puede iniciar campana ni ver billing; CSRF; export auditado; SSRF en website del lead rechazado.
- Fixtures: `tests/fixtures/maps_scraper_sample.csv`, `places_search_page1.json`, `places_details.json`.

## 16. Hitos
1. Migracion `0004`, normalize/dedupe/scoring + tests (sin red). 2. CSV import + lista/kanban de leads. 3. PlacesClient + job de busqueda + presupuesto. 4. Prueba secreta + rescore. 5. Planes/entitlements/ofertas/cotizador + seed. 6. Plantillas, ComplianceGate, optout, webhooks, campanas en dry-run. 7. Envio real Twilio (piloto 10-20 leads). 8. lead_to_demo_bot + convert + billing simple. 9. Hardening (revision security-reviewer), docs de operacion.
