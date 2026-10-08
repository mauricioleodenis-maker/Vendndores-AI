# 11 - Modelo de amenazas (STRIDE) y checklist de seguridad

Soporte para planeadores y builders. Complementa `docs/plan/01` (secciones 5, 6, 8, 9, 10), `02` (scraping, guardrails) y `03` (webhooks/canales). Donde hay conflicto, gana `01`; los puntos marcados como **ABIERTO** requieren decisión.

## 1. Activos y actores

| Activo | Sensibilidad | Dónde vive |
|---|---|---|
| Teléfonos y nombres de pacientes/clientes | PII | `contacts` (cifrado + `phone_hash`) |
| Mensajes WhatsApp (pueden contener datos de salud) | Sensible (Ley 1581 art. 5) | `messages.body_enc` |
| Tokens Twilio de subcuenta, OAuth Google, API keys | Secreto | `tenant_secrets` (Fernet/AES-GCM) |
| Config del bot (system prompt, guardrails) | Propiedad intelectual / integridad | `bot_configs` |
| Leads (teléfono de negocios, reseñas) | PII B2B | `leads` (mundo agencia) |
| Cuentas admin (owner/admin/operator) | Alto privilegio | `users`, `sessions` |
| Cuota LLM / Twilio | Costo financiero | `usage_counters` |

Actores: visitante anónimo; contacto final por WhatsApp (no confiable); operador de tenant; admin de agencia; owner; scraper contra sitios de terceros (contenido no confiable); proveedores (Twilio, Anthropic, Google).

Límites de confianza: (1) Internet -> webhook Twilio; (2) Internet -> panel admin; (3) contenido externo (WhatsApp, web scrapeada, Places) -> LLM; (4) LLM -> herramientas (agenda, BD); (5) app -> Postgres/Redis; (6) tenant A <-> tenant B.

## 2. STRIDE por componente

| Amenaza | Componente | Escenario | Mitigación obligatoria | Prioridad |
|---|---|---|---|---|
| **S** Spoofing | Webhook Twilio | Un atacante POSTea a `/webhooks/twilio` simulando un mensaje de otro número | `RequestValidator` con `X-Twilio-Signature` y URL = `public_base_url` + path; sin firma válida -> 403 antes de parsear | Crítica |
| **S** | Webhook, ruteo de tenant | Cliente envía `tenant_id` en el form para cambiar de negocio | Tenant solo se resuelve por `To` -> `channel_accounts.phone_e164`; ignorar cualquier `tenant_id` externo | Crítica |
| **S** | Sesión admin | Robo o fijación de sesión | Token aleatorio 256 bit, solo hash sha256 en BD, rotación al login, cookie `__Host-`, `HttpOnly; Secure; SameSite=Lax` | Alta |
| **S** | Contacto por WhatsApp | Suplantar a un paciente que pide cancelar/ver cita | Acciones sobre cita solo por número verificado; confirmación adicional para cancelar/reprogramar si el número no coincide con la cita | Media |
| **T** Tampering | Config del bot | Operador edita prompt/guardrails sin revisión | Versionado `bot_configs` + publicación explícita + `audit_log` (`bot.publish`) | Alta |
| **T** | Audit log | Admin borra rastro | Append-only (`REVOKE UPDATE/DELETE` al rol app), cadena HMAC, job `verify_audit_chain` | Alta |
| **T** | Idempotencia Twilio | Replay de webhook duplica respuestas/citas | Unique en `provider_sid`; dedupe_key en `scheduled_jobs`; EXCLUDE gist en citas | Alta |
| **T** | Export CSV | Inyección de fórmulas en Excel | Prefijar `'` a celdas que empiecen por `= + - @` (y tab/CR) | Media |
| **R** Repudiation | Acciones de admin | "Yo no exporté esos leads" | `audit_log` con actor, ip, request_id, sin PII en `diff` | Alta |
| **R** | Consentimiento | Negar que el titular autorizó | `consents` con `policy_version`, `evidence`, timestamp | Alta |
| **I** Info disclosure | Tenant A ve datos de B | Query sin filtro `tenant_id` | `TenantScopedRepo` + RLS (`FORCE`) + test que escanea `app/` por `select(` fuera de repos | Crítica |
| **I** | Errores | Stack trace o "tenant existe" en 404 | Errores genéricos; tenant desconocido -> 404 idéntico a inexistente; `debug=False` en prod | Alta |
| **I** | Logs | Teléfono, mensaje o token en logs/Sentry | Filtro structlog que enmascara E.164, emails, tokens; `redact_sensitive()` antes de log | Crítica |
| **I** | LLM | Bot revela system prompt, datos de otro contacto o precios inventados | Guardrails de salida (sección 4); contexto del contacto solo con sus datos | Alta |
| **I** | Secretos en BD | Dump de BD expone tokens | Fernet/AES-GCM con `aad = tabla:tenant:columna`; master keys fuera de BD (env/KMS) | Crítica |
| **I** | Enviar datos a LLM | PII innecesaria a Anthropic (transferencia internacional) | Enviar alias, no teléfono ni apellido; solo contexto mínimo | Alta |
| **D** Denial of service | Webhook/LLM | Spam de mensajes dispara costo LLM | Rate limit por contacto y por tenant (Redis token bucket); tope mensual en `usage_counters`; respuesta 200 rápida + procesamiento en arq | Alta |
| **D** | Login | Fuerza bruta | 5 intentos / 15 min por IP+email; bloqueo `locked_until`; hash dummy para timing uniforme | Alta |
| **D** | Scraper | Sitio enorme o lento bloquea worker | Límite 2 MB, timeout 10 s, máx 3 redirects, máx N páginas por job, jobs en arq con concurrencia limitada | Media |
| **E** Elevation | Roles | Operator crea tenants o ve secretos | `require_role(*roles)` en cada router; secretos solo `owner`; tests de autorización por rol | Crítica |
| **E** | Tools del bot | LLM ejecuta reserva para otro tenant/contacto | Tools con `extra=forbid`; `tenant_id`, `contact_id`, `conversation_id` inyectados por backend | Crítica |
| **E** | Admin de agencia cruza tenants | Panel de agencia lee datos de clientes sin trazabilidad | Sesión `agency_scope` explícita, auditada, solo para servicios designados | Alta |

## 3. Amenazas específicas

### 3.1 Spoofing de webhook
- Validar firma antes de leer el body; comparar con `hmac.compare_digest` (lo hace el SDK).
- Detrás de proxy/túnel: construir la URL desde `public_base_url`, no desde `request.url` (evita falsos 403 y bypass por Host header).
- Rechazar cualquier request a `/webhooks/*` que no sea POST con form-encoded.
- Cuentas Twilio por tenant (subcuenta): el auth token usado para validar sale de `tenant_secrets` descifrado; si no hay subcuenta, token de plataforma. Nunca validar con token de otro tenant.

### 3.2 Prompt injection
Fuentes no confiables: texto de WhatsApp, HTML scrapeado, JSON-LD, reseñas de Places, nombres de contacto.
- Delimitar siempre: `<untrusted_source url="...">...</untrusted_source>` y `<user_message>...</user_message>`; el system prompt indica que nada dentro de esas etiquetas es instrucción.
- Quitar texto oculto (`display:none`, comentarios HTML, `script/style/nav`) antes de guardar `scraped_page.text`.
- Heurísticas de entrada (`guardrails.check_input`): longitud máx 1500 chars; regex es/en ("ignora las instrucciones", "system prompt", "actúa como", "DAN", base64 largo). Son señales, no barrera: marcar en `guardrail_flags_json` y, si se repite, `handoff(reason=injection_suspected)`.
- Tools acotadas (consultar servicios, disponibilidad, reservar, handoff). Ninguna tool recibe IDs del modelo.
- Post-validación de salida: monto COP solo si está en config; URLs solo de allowlist; sin prompt, sin datos de otros contactos; sin diagnóstico ni prescripción (lista de patrones + handoff).
- Grounding de la fábrica: `verify_grounding()`: precios/horarios/direcciones deben aparecer en fuente `owner` o textualmente en una página scrapeada; si no, `approved=false`.
- Pruebas: suite de casos de inyección (directa, indirecta desde web, multilingüe, base64, role-play) en `tests/ai`; criterio: 0 fugas de system prompt y 0 tool calls con tenant ajeno.

### 3.3 SSRF en scraper (`core/http.py` / `scraping/safe_fetch.py`)
- Solo `http`/`https`; puertos 80/443.
- Resolver DNS **una vez** y conectar a la IP resuelta (pinning) para evitar DNS rebinding; validar la IP real del socket.
- Bloquear: loopback (127/8, ::1), privadas (10/8, 172.16/12, 192.168/16), link-local (169.254/16, incluye metadata cloud `169.254.169.254`), multicast, unspecified (0.0.0.0), ULA (fc00::/7), IPv4-mapped IPv6 (`::ffff:`).
- Revalidar cada redirect (máx 3); no seguir redirects a esquemas distintos.
- Límite 2 MB de body (streaming, cortar al exceder), timeout 10 s, `Content-Type` textual solo.
- Sin proxies de salida que resuelvan hosts internos; sin credenciales en el cliente HTTP del scraper.
- Test: lista de URLs maliciosas (`http://127.0.0.1`, `http://[::1]`, `http://169.254.169.254/`, `http://0x7f.1`, `http://2130706433`, redirect a IP privada, DNS que resuelve a privada) -> todas rechazadas.

### 3.4 Aislamiento multi-tenant
- Capas: repositorio (obligatorio `tenant_id` en constructor) + RLS con `FORCE` + `SET LOCAL app.tenant_id` por transacción + rol `app_user` sin `BYPASSRLS`.
- Índices compuestos que empiezan por `tenant_id`.
- Claves de cache, colas (arq) y dedupe incluyen `tenant_id`.
- Workers: el job recibe `tenant_id` explícito y abre sesión con contexto; prohibido job "global" que itere tenants sin `agency_scope`.
- Tests de aislamiento: crear tenants A y B, intentar leer/actualizar/borrar recursos de B con credenciales de A -> 404. Repetir para cada router. Correr contra Postgres real en CI (SQLite no tiene RLS).
- Leads (mundo agencia) no llevan `tenant_id`; al convertir, se crea tenant nuevo y se enlaza `converted_tenant_id`.

### 3.5 Secretos y cifrado (Fernet / AES-GCM)
- Master keys en env como JSON versionado (`{"v2":...,"v1":...}`), primera = activa. Nunca en repo, imagen Docker ni BD.
- Nonce 96 bit aleatorio por operación; nunca reutilizar. `aad = f"{table}:{tenant_id}:{column}"`.
- Rotación: re-cifrar en job con `key_version`; mantener versión anterior hasta terminar.
- Mostrar solo `last4`; nunca devolver secretos en API/HTML/logs; escribir en `audit_log` solo `secret.update` sin valor.
- Búsqueda por teléfono: `phone_hash = HMAC(blind_index_key, e164)` con clave separada de la de cifrado.
- **ABIERTO**: definir si la migración a KMS (GCP/AWS) entra en la fase 1. Recomendación: no, pero la interfaz `EnvelopeCrypto` debe permitirlo.
- Escaneo de secretos en CI (`gitleaks` o `mcp__github__run_secret_scanning` equivalente) y `.env` en `.gitignore`.

### 3.6 Auth, sesión, CSRF
- Login: mensaje genérico; hash argon2 (o bcrypt); hash dummy si el usuario no existe; no revelar existencia de cuenta.
- Sesión server-side (ver `01` §6); TTL deslizante 8 h, absoluto 7 d; logout y cambio de password revocan sesiones.
- CSRF: token por sesión en `<meta>` y en formularios; header `X-CSRF-Token` en HTMX (`htmx:configRequest`); `verify_csrf` obligatorio en POST/PUT/PATCH/DELETE con `compare_digest`; validar `Origin`/`Host`. Webhooks exentos de CSRF pero con firma.
- Cookies: `Secure`, `HttpOnly`, `SameSite=Lax`. HTMX no debe usar GET para acciones con efecto.
- Primer owner por CLI (`python -m app.cli create-owner`); no hay registro público.
- Acciones sensibles (cambiar rol, rotar secreto, exportar, borrar, publicar bot, enviar outreach masivo) requieren rol mínimo + confirmación en UI + audit.
- **ABIERTO**: MFA (TOTP) para owner en fase 1 o posterior. Recomendación: obligatorio para `owner` antes de producción; `users.totp_secret_enc` ya previsto.

### 3.7 Rate limiting
| Punto | Límite inicial (ajustable) | Clave |
|---|---|---|
| Login | 5 intentos / 15 min, luego bloqueo progresivo | IP + email |
| Webhook Twilio | 60 req/min por número de canal | `channel_accounts.id` |
| Mensajes entrantes | 20 msg / 10 min | contacto |
| Mensajes entrantes por tenant | según plan (tope mensual + burst) | tenant |
| Llamadas LLM | tokens/día por tenant | tenant |
| Scraper | 1 job activo por negocio; 3 jobs/hora por tenant | tenant |
| Outreach (envío) | horario, tope diario por número, opt-out inmediato | agencia |

Respuesta 429 con `Retry-After`. Redis (token bucket). En webhook, rate limit se aplica después de validar firma (si no, cualquiera puede llenar el bucket de otro).

### 3.8 PII en logs y analítica
- Nunca loguear: body de mensajes, teléfono completo, nombre, email, tokens, URLs con query string sensible, HTML scrapeado completo.
- Logs: `request_id`, `tenant_id` (UUID), `contact_hash` (no teléfono), código de evento.
- Errores de Twilio/Anthropic: registrar código y status, no el payload.
- Sentry/analítica: `send_default_pii=False`, `before_send` que aplica el filtro.
- Test: sembrar un teléfono y un mensaje con dato de salud, ejecutar flujo, grep de logs -> 0 coincidencias.

### 3.9 Datos de salud (Ley 1581, art. 5 sensibles)
- El bot no pide diagnósticos ni síntomas detallados; si el paciente los escribe, `redact_sensitive()` antes de persistir logs/analítica y handoff a humano.
- Urgencia médica (dolor fuerte, sangrado, dificultad para respirar) -> mensaje fijo de "llame a emergencias" + handoff inmediato; no lo resuelve el LLM.
- Solo se guarda lo necesario para la cita (nombre, teléfono, servicio, fecha).
- Mensajes cifrados en reposo; retención corta (ver §4).

## 4. Ley 1581 de 2012 y Decreto 1377: obligaciones

Roles: la agencia es **Encargada** de los datos de pacientes/clientes de cada negocio, que es **Responsable**. Para leads B2B la agencia es **Responsable**. Confirmar con abogado antes de lanzar.

| Obligación | Implementación | Dónde |
|---|---|---|
| Autorización previa, expresa e informada | Primer mensaje del bot con aviso corto + link a política; `consents(purpose=atencion)` al continuar | `privacy/consent.py`, bot |
| Finalidades separadas | `recordatorios` y `marketing` se piden aparte; sin marketing consentido no hay promociones | `consents` |
| Prueba de autorización | `consents.evidence`, `policy_version`, timestamp, canal | BD |
| Aviso de privacidad | Página `/privacidad` editable por tenant con datos del responsable, finalidades, derechos, canal de consultas | Web |
| Contrato de transmisión (encargo) | Checkbox + versión en onboarding: `tenants.dpa_version`, `dpa_accepted_at` | Onboarding |
| Derechos: conocer, actualizar, rectificar, suprimir, revocar | Export y borrado (`dsar.py`); respuesta <= 15 días hábiles | `privacy/dsar.py` |
| Revocación por WhatsApp | "BAJA"/"STOP" -> `opted_out` + `suppression_list` inmediato; confirmación al contacto | Canal |
| Supresión efectiva | Anonimizar: borrar `phone_enc`, nombre, mensajes; conservar cita agregada sin PII; mantener `phone_hash` solo para lista de supresión | `erase_contact()` |
| Retención | `messages.purge_after = created_at + 90 días`; job diario; leads sin interés 12 meses | `retention.purge()` |
| Seguridad de la información | Cifrado, control de acceso, audit, backups cifrados | Transversal |
| Transferencia internacional | Anthropic, Twilio, Google procesan fuera de Colombia: declararlo en política; minimizar datos al LLM (alias) | Política + prompt |
| Incidentes | Procedimiento para notificar a la SIC y a titulares; registro de incidentes | Runbook (falta) |
| Registro Nacional de Bases de Datos (RNBD) | Evaluar inscripción según tamaño de la agencia y de sus clientes | Legal |
| Comunicaciones comerciales | Canal de opt-out en cada mensaje outreach; horario; no enviar a `do_not_contact` | Outreach |

**ABIERTO**: plazo de retención de `audit_log` (plan dice 2 años) vs. borrado de datos personales: el audit debe contener solo IDs y acciones, no PII, para que no choque con el derecho de supresión.

## 5. Checklist de revisión (para cada PR)

Secretos y configuración
- [ ] Sin secretos hardcodeados; `.env` ignorado; `master_keys` y `secret_key` desde env (secret_key >= 32 bytes).
- [ ] `debug=False`, `cookie_secure=True`, `allowed_hosts` configurado en prod.

Entrada
- [ ] Pydantic v2 `extra=forbid` en schemas de API y tools del LLM.
- [ ] E.164 validado con `phonenumbers`; límites de longitud en todos los campos de texto.
- [ ] SQL solo parametrizado (SQLAlchemy `text()` con bind params); sin f-strings en queries.
- [ ] Upload CSV: tamaño máx, encoding, sanitización anti CSV-injection.

Autenticación y autorización
- [ ] Ruta protegida con `current_user` + `require_role`.
- [ ] POST/PUT/PATCH/DELETE con `verify_csrf`.
- [ ] Recursos cargados con `TenantScopedRepo`; test de acceso cruzado incluido.
- [ ] Acción sensible escribe en `audit_log` sin PII.

Webhooks y canales
- [ ] Firma Twilio validada antes de parsear; URL desde `public_base_url`.
- [ ] Idempotencia por `provider_sid`; respuesta 200 rápida; procesamiento en arq.

LLM
- [ ] Contenido externo delimitado; tools con IDs inyectados por backend.
- [ ] Guardrails de entrada y salida ejecutados; flags persistidos.
- [ ] Ningún log contiene system prompt ni body completo.

Scraping
- [ ] Todo fetch pasa por `safe_fetch()`; sin `httpx` directo.
- [ ] Límites de tamaño, tiempo, redirects y páginas.

Datos
- [ ] Campos sensibles cifrados (`*_enc`); búsqueda por `*_hash`.
- [ ] Nuevos campos con PII: decidir retención y entrada en DSAR.
- [ ] Logs revisados con filtro; sin PII en métricas.

Dependencias
- [ ] `uv.lock` actualizado; `pip-audit`, `bandit`, `ruff` en CI sin hallazgos altos.

## 6. Tests de seguridad mínimos (para `tests/security/`)
1. Webhook sin firma o con firma de otra URL -> 403, sin efectos.
2. Tenant A no lee/escribe recursos de tenant B (por cada router).
3. Rol `operator` no accede a secretos, publicación de bot ni exportación.
4. SSRF: lista de hosts/IPs privadas y redirects -> bloqueados.
5. Inyección: casos de §3.2 -> sin fuga de prompt ni tool call ajena.
6. Logs: sembrar teléfono y dato de salud -> ausentes en salida de logs.
7. CSRF: POST sin token -> 403.
8. Rate limit: login y webhook -> 429 con `Retry-After`.
9. CSV export: celda `=HYPERLINK(...)` -> prefijada.
10. DSAR: `erase_contact` deja cero PII en tablas y audit.

## 7. Inconsistencias detectadas entre planes (corregir antes de implementar)
- `messages`: en `01` tiene `tenant_id`, `purge_after`, `body_enc`; en `02` la tabla `message` no tiene `tenant_id` y usa `role`/`twilio_sid`. Unificar nombre (`messages`) y columnas; `tenant_id` obligatorio.
- `contacts`: `01` usa `phone_hash` + `phone_enc`; el texto de `01` §4 dice "cifrado determinístico no", lo que contradice el blind index. Aclarar: teléfono nunca cifrado determinístico, solo `phone_hash` HMAC.
- Clave de `consents`: `01` usa `policy_version` y `evidence`; confirmar que `02` no define otra tabla de consentimiento.
- Retención de mensajes: `01` dice 90 días; `02` no define purga. Unificar en `purge_after`.
- Modelo LLM: `01` fija `claude-sonnet-5-5`; el clasificador de entrada (`02`) es "Haiku-class". Definir cuál usa cada etapa y su tope de costo.
