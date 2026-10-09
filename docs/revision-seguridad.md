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

## Ronda 2 (verificación final)

Revisión final con los criterios `security-reviewer`, `healthcare-reviewer` y `performance-optimizer`
sobre toda la app tras la ronda de mejoras. Suite completa verde y `ruff` limpio.

### Rutas críticas re-verificadas (sin hallazgos críticos/altos nuevos)
- **Twilio**: firma validada con `hmac.compare_digest` por tenant (token propio) antes de persistir; destino desconocido o firma inválida responde 403; `/status` usa el token de la plataforma para outreach. Idempotencia por `MessageSid` (duplicado devuelve TwiML vacío; si falla la cola se libera el SID y se responde 503 para reintento).
- **Aislamiento de tenant**: servicios y herramientas del bot filtran por `tenant_id` en servidor; el LLM nunca elige el tenant.
- **CSRF / SSRF / inyección**: CSRF global con verificación de Origin; `safe_fetch` con DNS pinning; guardarraíles de prompt sin cambios.
- **Baja y supresión antes de cada envío**: todo envío pasa por `send_whatsapp_text`/`send_whatsapp_template` (comprueban `is_suppressed`; solo la confirmación de STOP usa `bypass_suppression`); campañas además pasan por `ComplianceGate`. Recordatorios, respuesta manual del operador, bot y outreach usan esas funciones (sin llamadas directas a Twilio fuera de `_post_message`).
- **PII cifrada, cadena de auditoría y rate limits**: sin regresiones; cubiertos por las suites `tests/security`, `tests/auth`, `tests/core`.

### Endurecido / optimizado / UI en la ronda
Los cambios de los módulos (índices FK y únicos parciales `0002`/`0003`, pool de DB fijo, límites de tasa del chat de prueba, UI del sistema de diseño, menú de Recordatorios) fueron integrados y verificados con la suite completa.

### Pendientes (no bloquean)
1. Exigir Redis en producción para el rate limit con varios workers.
2. `key_id` y clave dedicada para la cadena de auditoría (rotar `SECRET_KEY` hoy invalida `verify_chain`).
3. Rotación de claves para mensajes/contactos/leads cifrados.
4. RLS de Postgres; índice `pg_trgm` en búsqueda de conversaciones; dedupe difuso O(N²).
5. Submit-guard genérico en formularios (doble clic); URL-encode de mensajes en redirecciones.
6. `pip-audit` y `bandit` en CI; barrido periódico `expire_stale_sources`.
