# Revisión de seguridad y calidad (B19)

Alcance: toda la app (`app/`), revisada con los criterios de los agentes ECC `security-reviewer`,
`fastapi-reviewer`, `python-reviewer`, `healthcare-reviewer` (datos personales / Ley 1581),
`silent-failure-hunter` y `performance-optimizer`. Estado final: suite verde, `ruff` limpio.

## Resultado general
No se encontraron hallazgos críticos. Los controles base están bien implementados: CSRF global
(+ verificación de Origin), cookies `__Host-` HttpOnly/Secure/SameSite, sesiones con hash en DB,
login con rate limit + bloqueo y tiempo uniforme, firma Twilio con `compare_digest`, idempotencia
por `MessageSid`, `safe_fetch` anti-SSRF (DNS pinning, IPs no públicas, redirecciones revalidadas),
CSP estricta sin inline, `autoescape` activo (el único `Markup` está escapado), neutralización de
inyección de fórmulas en CSV, filtros de PII en logs (auditado por AST: ningún log recibe
teléfono/correo/cuerpo/token), `ComplianceGate` fail-closed, herramientas del bot que validan
`contact_id`/`tenant_id` en servidor (el LLM nunca decide el tenant), estado OAuth firmado con PKCE.

## Hallazgos corregidos
| Sev. | Hallazgo | Corrección | Prueba |
|---|---|---|---|
| Alta | Webhooks `/webhooks/*`: el tope de 64 KB solo miraba `Content-Length`; un cuerpo `chunked` sin firma se leía completo antes de validar la firma (DoS de memoria). | `WebhookBodyLimitMiddleware` cuenta bytes reales y responde 413 (`app/core/middleware.py`). | `tests/security/test_hardening.py` |
| Alta | Hash/verificación Argon2 se ejecutaba en el event loop (bloquea todas las solicitudes durante cada login; amplificación de DoS). | `asyncio.to_thread` en `app/auth/service.py` (login, alta de usuario, cambio de contraseña, rehash). | `tests/auth` |
| Media | Chat de prueba del bot y “regenerar bot” llaman al LLM sin límite por usuario (costo / abuso). | `enforce()` 30 msgs/min y 10 regeneraciones/h por usuario (`app/factory/router.py`). | `test_sandbox_chat_is_rate_limited` |
| Media | Configuración prod aceptaba `PUBLIC_BASE_URL` http y `ALLOWED_HOSTS=["*"]` (rompe firma Twilio/OAuth y habilita Host spoofing). | Validación de arranque en `Settings._validate_prod`. | `tests/core/test_config.py` |
| Media | Fallo del calendario externo se tragaba en silencio y se agendaba solo contra la DB (riesgo de doble reserva). | Se registra `booking.external_busy_failed` (sin PII). Se mantiene la degradación a DB. | cubierto por `tests/booking` |
| Baja | N+1 en `mark_overdue` (un `get` por suscripción atrasada). | UPDATE masivo (`app/billing/service.py`). | `tests/billing` |

## Pendientes (medio/bajo) — no bloquean el MVP
1. **Rate limit en memoria con varios workers**: en prod con `VAI_REDIS_URL` se usa Redis; sin Redis cada proceso cuenta aparte. Exigir Redis en prod.
2. **`--forwarded-allow-ips "*"`** en el `Dockerfile`: correcto solo porque en `docker-compose.prod.yml` el puerto 8000 no se publica (solo Caddy). En `docker-compose.yml` (dev) sí está publicado: no usar en internet. Restringir a la IP de Caddy si se despliega distinto.
3. **Rotación de claves**: `rotate-keys` solo re-cifra `tenant_secrets`; mensajes/contactos/leads cifrados requieren su propia rotación.
4. **RLS de Postgres** sigue pendiente (hardening posterior, MAESTRO §2). El aislamiento hoy depende de filtros `tenant_id` en código (revisados: servicios de booking, factory, privacy/DSAR y tools del bot filtran por tenant). Los operadores de la agencia ven todos los tenants por diseño.
5. **Roles**: `operator` puede crear citas, mover leads, tomar conversaciones y subir CSV; facturación, planes, tenants, aprobación de plantillas e inicio de campañas son `admin`. Revisar si el operador debe poder importar CSV masivos.
6. **Dedupe difuso de leads** (`app/leads/dedupe.py`) compara contra todos los leads de la ciudad: O(N²) en importaciones muy grandes. Pre-filtrar por prefijo/`pg_trgm` en Postgres.
7. **`/api/plans`** es público a propósito (catálogo para landing); no expone datos de clientes.
8. **Redirecciones con mensaje en query** (`/admin/ajustes?error=...`): se escapan al renderizar (sin XSS) pero no se URL-encodean; usar `urllib.parse.quote`.
9. **Dependencias**: ejecutar `pip-audit` y `bandit -r app` en CI (añadir al workflow; no se modificó `pyproject.toml`).
10. **Descifrado fallido** (clave rotada) en varios módulos devuelve un texto genérico con log; considerar alerta/métrica.
