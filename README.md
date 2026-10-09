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
make up                         # (web solo en 127.0.0.1:8000) web, worker, db (Postgres 16), redis y migrate (una vez)
docker compose run --rm web python -m app.cli seed
docker compose run --rm web python -m app.cli create-owner
```

### Publicar en Render (un paso)

1. Cree una cuenta en render.com y conecte su GitHub.
2. *New → Blueprint* → elija este repositorio y la rama. `render.yaml` crea la web, el worker, Postgres y Redis; las migraciones y los planes se cargan solos al arrancar.
3. En el grupo de variables `vendedores-secretos` complete lo marcado como pendiente:
   - `VAI_MASTER_KEYS`: genérela con `python -c "import os,base64,json;print(json.dumps({'v1':base64.b64encode(os.urandom(32)).decode()}))"`
   - `VAI_PUBLIC_BASE_URL=https://<servicio>.onrender.com` y `VAI_ALLOWED_HOSTS=["<servicio>.onrender.com"]`
   - Sus llaves de Anthropic, Twilio, Google y `VAI_DEMO_WHATSAPP_NUMBER`.
4. Cree el usuario dueño desde la *Shell* del servicio web: `python -m app.cli create-owner --email su@correo.com`
5. En Twilio apunte *When a message comes in* a `https://<servicio>.onrender.com/webhooks/twilio/whatsapp` y *Status callback* a `.../webhooks/twilio/status`.

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

## Mensajes de WhatsApp para prospectar (envío manual)

Usted envía estos mensajes a mano desde su WhatsApp; la app solo los prepara y registra.

1. Ponga su nombre en `/admin/ajustes` (si no, los mensajes salen con `[TU_NOMBRE]`).
2. Abra **Mensajes WhatsApp** (`/admin/mensajes`): la cola de hoy, por puntaje, con tres grupos: pruebas secretas, aperturas y seguimientos que tocan hoy.
3. Cada lead tiene su secuencia en `/admin/leads/{id}/mensajes` (botón en la ficha del lead), con los datos del negocio ya rellenos:
   - **Prueba secreta**: pregunta de cliente común para medir cuánto tardan en responder (una sola vez por negocio; regístrela en la sección *Ventas* de la ficha).
   - **Apertura**: se presenta con su nombre real y hace una pregunta sobre cómo manejan el WhatsApp; no menciona la venta e incluye la salida "si prefieren que no les escriba".
   - **Propuesta** (cuando respondan; incluye el tiempo medido si hubo demora de más de 15 min), **demo**, **seguimientos** de los días 1, 3 y 7 y **15 respuestas a objeciones** con precios reales.
4. Edite si quiere y pulse **Copiar** o **Abrir en WhatsApp**; luego **Marcar enviado** (registra el envío y avanza la etapa).
5. Si dicen NO o STOP, pulse **Marcar como no contactar**: el lead sale de la cola y no se puede volver a marcar.
6. Cuando le respondan, pulse **Respondió**: se detienen los seguimientos y el lead pasa al grupo *Respondieron* con la propuesta lista.
7. Tras registrar una prueba secreta, la cola le avisa a los 15 y a los 60 minutos que revise si respondieron.

**Demo por WhatsApp.** Defina `VAI_DEMO_WHATSAPP_NUMBER` (número de la agencia en Twilio) y apunte su webhook de entrada a `https://SU_DOMINIO/webhooks/twilio/whatsapp`. El enlace `wa.me/...?text=DEMO-<slug>` de cada demo conecta al negocio con su bot de prueba; se responde con el mismo tope de mensajes que la demo web.

## Pagos en línea (Wompi)

1. Cree las llaves en el panel de Wompi y configure `VAI_WOMPI_PUBLIC_KEY`, `VAI_WOMPI_PRIVATE_KEY`, `VAI_WOMPI_EVENTS_SECRET` y `VAI_WOMPI_INTEGRITY_SECRET` (ver `.env.example`). Con `VAI_WOMPI_SANDBOX=true` todo corre en pruebas.
2. En Wompi, apunte la URL de eventos a `https://<dominio>/webhooks/wompi/events` (se valida por firma, sin sesión).
3. En `/admin/pagos` genere el link de pago de cada cobro. El worker crea links mensuales y reconcilia pendientes solo si hay llaves.
4. Para enviar el link por WhatsApp defina `VAI_WOMPI_LINK_TEMPLATE_SID` (plantilla aprobada por Meta).

## Voz (llamadas)

1. En Twilio, configure la Voice URL del número de voz a `https://<dominio>/webhooks/twilio/voice/incoming`.
2. Cree un canal `voice` con ese número (distinto del de WhatsApp) y ajuste la voz en `/admin/voz` (`VAI_VOICE_SAY_VOICE`, por defecto `Polly.Mia-Neural`).
3. Cada llamada reproduce el aviso de consentimiento y se registra en `call_sessions`.

## Calidad y seguridad

`make check` ejecuta ruff, `pip-audit`, `bandit` y las pruebas (los mismos controles de CI). El contenedor corre sin root, con `cap_drop: ALL`, `no-new-privileges` y límite de procesos; en producción `web`, `worker` y `caddy` son de solo lectura. `FORWARDED_ALLOW_IPS` es `127.0.0.1` por defecto y solo se abre en el compose de producción (puerto 8000 sin publicar).

## Comandos

`make dev|worker|test|lint|fmt|audit|migrate|seed|create-owner|up|down|logs|backup`. Operación y emergencias: `docs/runbook.md`.
