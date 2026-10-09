# Vendedores AI

Plataforma de agencia para vender **Recepcionista IA por WhatsApp** a negocios pequeños (dentistas, clínicas estéticas, talleres, restaurantes). Tres pilares:

1. **Fábrica de bots**: con los datos del negocio (y su web) la IA arma servicios, precios, FAQs, reglas de agenda y prompt.
2. **Planes listos**: Básico / Pro / Premium en COP, oferta Fundador y garantía de 30 días.
3. **Buscador de leads**: Google Places + CSV, puntuación, prueba secreta, pipeline, demo en un clic y campañas con cumplimiento.

Stack: Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, Postgres, Redis + arq, Jinja2 + HTMX. Plan completo en `docs/plan/00-MAESTRO.md`.

## Inicio rápido (desarrollo)

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env            # edite VAI_ANTHROPIC_API_KEY; en dev el resto puede quedar por defecto
make migrate                    # crea el esquema
make seed                       # planes y ofertas
make create-owner               # primer usuario owner (pide correo y clave)
make dev                        # http://localhost:8000/admin
```

Sin `VAI_REDIS_URL` los jobs corren en proceso. Pruebas: `make test`; calidad: `make lint`; seguridad: `make audit`.

## Con Docker

```bash
cp .env.example .env            # complete secretos
make up                         # web, worker, db (Postgres 16), redis y migrate (una vez)
docker compose run --rm web python -m app.cli seed
docker compose run --rm web python -m app.cli create-owner
```

### Producción (TLS automático con Caddy)

1. Apunte el DNS de `DOMAIN` al servidor; abra puertos 80/443.
2. En `.env`: `VAI_ENV=prod`, `DOMAIN`, `ACME_EMAIL`, `POSTGRES_PASSWORD`, `VAI_PUBLIC_BASE_URL=https://su-dominio`, `VAI_SECRET_KEY`, `VAI_MASTER_KEYS`, `VAI_PHONE_HASH_KEY`, `VAI_ALLOWED_HOSTS`. En prod la app **no arranca** si los secretos son débiles.
3. `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`

## Crear el bot de un negocio en 3 pasos

1. **Datos**: `/admin` → *Nueva empresa*: nombre, nicho, ciudad, teléfono y sitio web.
2. **Generar**: la IA crea el bot (servicios, precios, FAQs, agenda) desde la plantilla del nicho y la web del negocio. Revise el borrador y pruébelo en el chat de prueba.
3. **Publicar**: confirme horarios y canal de WhatsApp, y publique. Se puede volver a una versión anterior.

## Conectar Twilio (WhatsApp)

1. Cree una cuenta Twilio; copie `Account SID` y `Auth Token` a `VAI_TWILIO_ACCOUNT_SID` y `VAI_TWILIO_AUTH_TOKEN`.
2. **Sandbox (pruebas)**: Twilio Console → Messaging → Try it out → WhatsApp sandbox. Use `VAI_TWILIO_WHATSAPP_FROM=whatsapp:+14155238886`. En *When a message comes in* ponga `https://SU_DOMINIO/webhooks/twilio/whatsapp` (POST) y en *Status callback* `https://SU_DOMINIO/webhooks/twilio/status`. Cada tester envía el código `join ...` al sandbox. En local use un túnel HTTPS y ajuste `VAI_PUBLIC_BASE_URL` (la firma se valida contra esa URL).
3. **Sender real**: registre el número en WhatsApp Business (verificación de Meta), cree el sender en Twilio, apunte los mismos webhooks y registre el número en el canal del negocio en la app. Cambie `VAI_TWILIO_DRY_RUN=false` solo cuando todo funcione.

## Conectar Google Calendar

1. Google Cloud Console → habilite *Google Calendar API* → credenciales OAuth (aplicación web).
2. URI de redirección autorizada: `https://SU_DOMINIO/admin/calendario/google/callback`.
3. Ponga `VAI_GOOGLE_OAUTH_CLIENT_ID` y `VAI_GOOGLE_OAUTH_CLIENT_SECRET`; en la app, cada negocio pulsa *Conectar Google Calendar*. Los tokens se guardan cifrados.

## Google Places API (buscador de leads)

1. Habilite *Places API (New)* y cree una API key restringida a esa API y a la IP del servidor.
2. Defina `VAI_GOOGLE_PLACES_API_KEY` y un tope `VAI_GOOGLE_PLACES_BUDGET_USD_MONTH`; configure también alertas de presupuesto en Google Cloud.
3. También puede importar el CSV del Maps-Scraper (con vista previa).

## Buscar leads y lanzar campañas con seguridad

1. Busque por nicho + ciudad (+ barrios); revise puntuación y descarte duplicados.
2. Use la **prueba secreta** y el **demo en un clic** antes de contactar.
3. Las campañas pasan por el *ComplianceGate* (opt-out, supresión, horarios, consentimiento). Empiece así:
   - `VAI_TWILIO_DRY_RUN=true` y `VAI_OUTREACH_ENABLED=false`: previsualice objetivos y mensajes.
   - Envíe a su propio número, luego a un lote pequeño con plantillas aprobadas por Meta.
   - Suba volumen gradualmente; ante quejas o bloqueos, pause la campaña o apague `VAI_OUTREACH_ENABLED` (kill switch).
   - Respete siempre los STOP/BAJA: se registran en la lista de supresión (Ley 1581).

## Comandos

`make dev|worker|test|lint|fmt|audit|migrate|seed|create-owner|up|down|logs|backup`. Operación y emergencias: `docs/runbook.md`.
