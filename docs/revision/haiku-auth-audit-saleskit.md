# Revision de solo lectura: auth + audit + saleskit

Alcance: `app/auth/`, `app/audit/`, `app/saleskit/`, sus plantillas (`auth/`, `audit/`, `saleskit/`, `partials/chain_status.html`) y `tests/auth`, `tests/audit`, `tests/saleskit`. Revisado contra `docs/revision-seguridad.md`. No se editó código.

Lo verificado como correcto: CSRF en `/admin` y `/api` (`app/core/deps.py:101-119`); login con CSRF de doble envío, tiempo uniforme, mensaje genérico y bloqueo; sesiones con hash sha256 y rotación al login; cambio de contraseña revoca las demás sesiones; `safe_next` rechaza `//` y `\`; el redirect de Starlette 1.6 codifica `\t` como `%09`, así que el bypass `/\t/host` no es explotable; `require_role()` sin argumentos limita la auditoría a `owner`; el Markdown escapa antes de formatear y las plantillas usan autoescape; `saleskit` solo sirve slugs de lista blanca (sin traversal, probado).

## Medio

### M1. Alta de usuario con nombre de más de 200 caracteres produce 500
- `app/auth/service.py:60` escribe `full_name.strip()` sin validar longitud. `app/db/models/users.py:23` define `String(200)`. En PostgreSQL el INSERT falla con `DataError` y el operador ve un 500 en lugar de un 422.
- Escenario: owner envía `full_name` de 250 caracteres en `POST /admin/ajustes/usuarios` → 500.
- Corrección: en `create_user` lanzar `AppError("invalid_name", "Nombre demasiado largo", 422)` si `len(full_name.strip()) > 200`; añadir `maxlength="200"` en `app/web/templates/auth/ajustes.html:44`; prueba en `tests/auth/test_service.py`.

### M2. `verify_chain` carga toda la bitácora en memoria y bloquea la solicitud
- `app/audit/service.py:140-153` ejecuta `select(AuditLog).order_by(AuditLog.id)` y recorre todos los registros con `scalars()` sin `yield_per`/paginación, calculando HMAC en el event loop por cada fila. `app/audit/router.py:236-243` lo expone a través de `POST /admin/auditoria/verificar`.
- Escenario: con millones de filas, el owner pulsa "Verificar cadena", el proceso consume memoria proporcional al total y el event loop se bloquea durante minutos.
- Corrección: paginar por keyset (`WHERE id > :last ORDER BY id LIMIT 1000`) o `stream_scalars`; mover el cómputo a `asyncio.to_thread` por lote; devolver también el número de registros verificados; y, si se quiere, verificación incremental desde un checkpoint.

### M3. Rotar `SECRET_KEY` invalida toda la cadena de auditoría
- `app/audit/service.py:31-33` deriva la clave HMAC de `get_settings().secret_key`. Tras una rotación, `verify_chain` devuelve `(False, 1)` aunque nadie haya alterado nada, y `docs/revision-seguridad.md` (pendiente 3) indica que `rotate-keys` no cubre la bitácora.
- Corrección: usar una clave dedicada `AUDIT_CHAIN_KEY` con versión (`key_id` guardado en cada fila) y mantener las claves anteriores para verificar; o al menos documentar el procedimiento y bloquear la rotación si la cadena no está íntegra.

### M4. `sanitize_diff` no redacta valores compuestos bajo claves sensibles
- `app/audit/service.py:38-44`: si la clave coincide con `_SENSITIVE` pero el valor es `dict`, se recurre sin redactar. `{"secret": {"value": "abc123"}}` se guarda tal cual. Para listas bajo claves sensibles solo se aplican los filtros de correo y teléfono (`_EMAIL`, `_PHONE`), no la redacción por clave.
- Estado actual: revisé los `diff=` de `app/` y ninguno pasa hoy un dict bajo clave sensible, así que es un riesgo latente, no una fuga activa.
- Corrección: si la clave es sensible, redactar el valor completo independientemente de su tipo (`"[redacted]"`); añadir prueba `sanitize_diff({"token": {"x": 1}}) == {"token": "[redacted]"}`.

### M5. Bloqueo de cuenta por correo: cualquiera puede bloquear a un usuario
- `app/auth/service.py:105-109`: cinco contraseñas incorrectas sobre un correo fijan `locked_until` 15 minutos (`app/core/config.py:37`). El bloqueo es por cuenta, no por IP+correo, así que un atacante puede mantener bloqueado a un owner indefinidamente con una petición cada 15 minutos.
- Corrección: bloquear por par (IP, correo) o aplicar retardo progresivo; mantener el bloqueo global solo tras un umbral mayor; registrar `auth.lockout` con IP para detectar el patrón. Es una decisión de diseño; documentarla si se mantiene.

## Bajo

### B1. Mensajes flash arbitrarios en la UI (superficie de phishing)
- `app/auth/router.py:146-153`: `_flash` muestra en `/admin/ajustes` cualquier texto de `?ok=` o `?error=` (hasta 200 caracteres). Un enlace como `/admin/ajustes?error=Su cuenta fue suspendida, llame al 300...` se ve como mensaje oficial. El texto se escapa, por lo que no hay XSS.
- Corrección: pasar un código (`?flash=password_ok`) y mapearlo a un texto fijo en el servidor.

### B2. Carreras en desactivación de owners y activación sin limpiar el bloqueo
- `app/auth/service.py:241-259`: dos owners que se desactivan mutuamente en paralelo pueden dejar cero owners activos (cada petición cuenta al otro como activo). Además, al reactivar no se reinician `failed_logins` ni `locked_until` (líneas 249-250), por lo que una cuenta reactivada puede seguir bloqueada.
- Corrección: `SELECT ... FOR UPDATE` sobre los owners activos antes de desactivar, y verificar que quede al menos uno; al activar, `failed_logins = 0` y `locked_until = None`.

### B3. Bloqueo global de auditoría durante toda la transacción del llamador
- `app/audit/service.py:109-110`: `pg_advisory_xact_lock` se mantiene hasta el commit del llamador. Toda escritura de auditoría de la app se serializa, y si esa transacción contiene IO externo (WhatsApp, LLM, calendario) el resto de solicitudes espera.
- Corrección: confirmar que ninguna transacción que audita hace IO externo, o registrar el evento en una transacción separada tras el commit de negocio. Documentar el compromiso.

### B4. Filtro de tenant inválido se ignora en silencio
- `app/audit/router.py:210-214`: un `tenant_id` mal formado se convierte en `None` y la lista muestra todos los tenants, pero el campo conserva el valor inválido (`app/web/templates/audit/list.html:8`). El operador cree que el filtro está aplicado.
- Corrección: devolver 422 o mostrar un aviso "ID de empresa no válido" con estado vacío.

### B5. Comodines `%` y `_` sin escapar en el filtro de acción
- `app/audit/service.py:170`: `AuditLog.action.like(f"{action}%")` trata `_` como comodín; `auth_login` coincide con `auth.login`. No es inyección, pero el filtro devuelve resultados incorrectos.
- Corrección: `like(..., escape="\\")` tras escapar `%`, `_` y `\`.

### B6. Mensajes de error en la query sin codificar
- `app/auth/router.py:174`, `:193`, `:210` construyen `?error={exc.message}`. Starlette codifica el `Location`, pero `&` o `#` dentro de un mensaje cortarían el parámetro. Hoy los mensajes son fijos, es un arreglo preventivo (ya listado como pendiente 8 en `docs/revision-seguridad.md`).
- Corrección: `urllib.parse.urlencode({"error": exc.message})`; mejor aún, ver B1.

### B7. Copy en español sin acentos y con términos inconsistentes
- `app/auth/service.py:27` ("Correo o contrasena incorrectos") y `:226` ("La contrasena actual no es correcta") no llevan tilde, mientras la plantilla y los flash usan "Contraseña". La ayuda de `ajustes.html:12` dice "Mínimo 12 caracteres" pero el error de `validate_password_strength` no menciona la regla de letras y números al mismo tiempo.
- Corrección: "contraseña" en todas las cadenas; alinear el texto de ayuda con las dos reglas.

### B8. Markdown: formato dentro de `código en línea`
- `app/saleskit/markdown.py:111-113`: `_CODE` se aplica antes que `_BOLD` e `_ITALIC`, así que `` `**x**` `` se convierte en `<code><strong>x</strong></code>`, y la cursiva puede cruzar límites de etiquetas. No es XSS (el texto ya está escapado), pero el guion de ventas puede mostrar formato no deseado.
- Corrección: extraer los tramos de código a marcadores antes de aplicar negrita y cursiva, y restaurarlos al final. Añadir prueba.

### B9. `list_docs` relee todos los documentos en cada solicitud
- `app/saleskit/service.py:32-40` y `:48-53`: `get_doc` llama a `list_docs`, que lee la primera línea de cada `.md` con `read_text` en cada petición. `doc.path.stat()` (línea 52) lanza `FileNotFoundError` si un archivo se borra entre el listado y el `stat`, provocando 500. `_title` (línea 26) no maneja `UnicodeDecodeError`.
- Corrección: cachear el listado con `lru_cache` indexado por `mtime_ns` del directorio, validar el slug contra el listado cacheado, y capturar `OSError`/`UnicodeDecodeError` con registro sin PII.

### B10. Registro de límite de intentos sin valor diagnóstico
- `app/auth/service.py:93`: `diff={"key": key[:8]}` registra `login:ip` o `login:em`, que no identifica ni el origen ni el correo. Es inofensivo pero no sirve para investigar.
- Corrección: registrar el tipo de clave (`"ip"` o `"email"`) y un hash truncado del valor si se necesita correlación.

### B11. UX de confirmaciones y del formulario de usuarios
- `app/web/templates/auth/ajustes.html:29`: `data-confirm="¿Confirmas el cambio?"` no indica a quién se activa o desactiva ni la consecuencia (se cierran sus sesiones). Corrección: "¿Desactivar a {{ u.email }}? Se cerrarán sus sesiones activas."
- `ajustes.html:44`: el campo "Nombre" no tiene `maxlength`; ver M1.
- `ajustes.html:47`: el selector ofrece "Dueño" a un owner sin explicar el alcance; añadir texto de ayuda.

## Pruebas faltantes
- `tests/auth/test_service.py`: nombre de más de 200 caracteres (M1); desactivación concurrente del último owner (B2); reactivación sin limpiar bloqueo (B2).
- `tests/audit/test_audit.py`: `sanitize_diff` con dict bajo clave sensible (M4); `verify_chain` sobre un lote grande o paginado (M2).
- `tests/audit/test_router.py`: `tenant_id` inválido (B4); comodines en `action` (B5).
- `tests/saleskit/test_markdown.py`: código en línea con negrita (B8). `tests/saleskit/test_router.py`: documento borrado entre listado y `stat` (B9).
- `tests/auth/test_router.py`: `?error=` con caracteres especiales (B6).

## Carry-over de la revisión previa
- Pendientes 1, 2, 4 y 9 de `docs/revision-seguridad.md` siguen vigentes para este módulo (rate limit en memoria, `forwarded-allow-ips`, RLS, `pip-audit`/`bandit`). No se reabren aquí.
