# Revisión de solo lectura: módulo factory (B4)

Alcance: `app/factory/` (router, service, review, grounding, prompts, schemas), `app/web/templates/factory/`, `tests/factory/`.
Verificado leyendo el código. Ningún archivo de código fue modificado.

## Hallazgos ordenados por severidad

### 1. CRÍTICO: los cambios de borrador y la regeneración llegan al bot en vivo antes de publicar
- Evidencia: el runtime lee el catálogo de las tablas de trabajo, no de `BotConfig.config`:
  - `app/conversation/context.py:155-158` (servicios activos, sin filtrar `needs_review`).
  - `app/conversation/context.py:164-170` (FAQs activas; aquí sí filtra `needs_review`).
  - `app/booking/router.py:188-189` y `app/booking/availability.py:236-239` (servicios para agendar).
  - `app/plans/entitlements.py:145` (conteo de servicios).
- Mutaciones directas de esas tablas durante la revisión, sin pasar por publicación:
  - `app/factory/review.py:352-383` (`update_service`) y `review.py:410-424` (`discard_item`).
  - `app/factory/service.py:370-376` y `review.py:129-197` (`sync_catalog_from_config`, con `replace=False` borra FAQs `template`/`scrape` e inserta servicios nuevos con `is_active=True`).
  - `app/factory/router.py:298-316` (`regenerate`) llama a `build_bot` sobre un tenant que ya tiene bot publicado.
- Escenario: el operador regenera el bot o descarta un servicio mientras la versión publicada sigue activa. El cliente de WhatsApp ve precios sin confirmar (`needs_review=True` no se filtra en servicios) o pierde FAQs que aún no se reemplazaron. Ocurre sin "Aprobar y publicar".
- Corrección: el catálogo de borrador debe vivir ligado a `bot_config_id` (copia por versión), o el runtime debe leer la foto congelada (`BotConfig.config.services/faqs` de la versión publicada). Como mínimo: en `context.py:155-158` añadir `Service.needs_review.is_(False)` y, en `sync_catalog_from_config`, no tocar filas activas de una versión publicada hasta publicar. Añadir un test que confirme que regenerar o descartar no altera lo que ve `load_business_context` antes de publicar.

### 2. ALTO: el rollback se salta la revisión y el chat de prueba
- Evidencia: `app/factory/service.py:511-530` (`rollback_bot`) llama a `sync_catalog_from_config(..., replace=True)` y a `_make_published` sin `publish_blockers` ni `require_sandbox`. `app/factory/router.py:335-347` tampoco exige nada.
- Efecto: se puede volver a una versión con alertas de inyección, horarios sin fuente o preguntas abiertas, y se desactivan en vivo los servicios que no estén en la foto (`service.py:524`, `review.py:178-181`).
- Corrección: al hacer rollback, ejecutar `publish_blockers` sobre la versión destino y exigir confirmación explícita, o pasar la versión a borrador en lugar de publicarla directamente. Añadir test de rollback bloqueado por alerta pendiente.

### 3. MEDIO: precios del dueño sin validación de rango; la configuración puede quedar ilegible
- Evidencia: `app/factory/service.py:215-216` acepta cualquier `int` como `price_cop` (incluye `True`, que es `int` en Python, y valores negativos o mayores a 50.000.000). `service.py:233` valida solo `>= 0`. `model_copy` (`service.py:220`, `248-250`) no revalida.
- Efecto: se persiste un `BotConfig.config` inválido. Después `review.py:106-110` (`parsed_config`) lanza `AppError config_invalid` y la página del bot responde 422 para toda la empresa.
- Corrección: normalizar con `ServiceCfg.model_validate` (o validar el rango 0..50_000_000 y excluir `bool`) antes de `model_copy`. Añadir test con precio `60_000_000` y con `True`.

### 4. MEDIO: la verificación de fundamento acepta cualquier número de la fuente como precio
- Evidencia: `app/factory/grounding.py:35-38` añade cada grupo de dígitos separado por espacios (teléfono, dirección, años) como "monto". Un precio de 150 queda "fundamentado" por "Calle 150" o por una parte de un teléfono. `grounding.py:122` ignora las cifras de FAQ menores a 5000, así que "$4.000" en una FAQ no se marca.
- Efecto: falsos negativos en la alerta "Precio sin fuente", que es la defensa principal contra precios inventados por el LLM.
- Corrección: extraer montos solo con marca de moneda o separador de miles (`$`, `COP`, `mil`, `k`, o patrón `\d{1,3}([.,']\d{3})+`), y quitar la rama de `split` por espacios. Bajar el umbral de FAQ a 1000 o marcar cualquier cifra con símbolo monetario. Tests con dirección y teléfono en el texto fuente.

### 5. MEDIO: el chat de prueba falla en silencio
- Evidencia: `app/factory/router.py:393-396` solo captura `NotImplementedError` y `AppError`. Cualquier otro error del LLM o de red sale como 500. `app/web/templates/factory/_chat.html:10` usa `hx-post` con `hx-target` y `outerHTML`, sin manejo de error; HTMX no reemplaza el contenido en 5xx y el operador no ve nada.
- Corrección: capturar `Exception` en `sandbox_chat`, registrar con `log.warning` (sin contenido del mensaje) y devolver el `_chat.html` con `error` y estado 200. En la plantilla, añadir `hx-indicator` y un mensaje de error con `hx-on::response-error`.

### 6. MEDIO: inyección indirecta por descripciones de servicio y FAQs que llegan al prompt de runtime
- Evidencia: `app/factory/service.py:361-363` solo revisa `has_injection` en preguntas y respuestas de FAQ. La descripción de servicio (`ServiceCfg.description`, origen web) no se revisa. `app/factory/prompts.py:142-149` y `render_system_prompt` meten esos textos tal cual en el system prompt de runtime. `_line` solo quita caracteres de control y etiquetas del delimitador, no el texto imperativo.
- Corrección: aplicar `has_injection` también a `svc.description` y `svc.name` en `verify_grounding` o en `build_bot`, y marcar `needs_review`. Tests con descripción de servicio que contenga "ignora las instrucciones".

### 7. MEDIO: rendimiento y transacción abierta durante I/O externo
- `app/factory/router.py:298-316`: `regenerate` mantiene la sesión de BD abierta mientras `build_bot` hace scraping (hasta 10 páginas) y una llamada al LLM de hasta 6000 tokens. Con `get_session` (`app/core/deps.py:28-36`) la conexión queda ocupada decenas de segundos. Corrección: hacer el scraping y la llamada al LLM fuera de la transacción, y abrir la escritura al final (o usar una sesión corta para cada fase).
- `app/factory/router.py:81`: `list_versions` carga todas las versiones con `config`, `system_prompt` y `generation_meta` completos en cada visita a la página. Corrección: cargar solo `version, status, published_at, model` para la lista y el `BotConfig` completo solo para la versión seleccionada.
- `app/factory/router.py:99`: `load_kb` trae hasta 12 documentos con todo su `content` solo para contar y mostrar 1500 caracteres. Corrección: `load_only`/`deferred` o un `SELECT` que traiga `title, source_url, left(content, 1500)`.
- `app/factory/review.py:82-85` (`next_version`) y `review.py:299-335` (`ensure_draft`): `max(version)+1` sin bloqueo. Dos peticiones concurrentes pueden calcular la misma versión. No verificado si hay restricción única `(tenant_id, version)` en `app/db/models/bots.py`; si la hay, la segunda petición termina en `IntegrityError` (500). Corrección: `SELECT ... FOR UPDATE` sobre el tenant o un contador por tenant.

### 8. BAJO: validación de tenant y ruta de datos inconsistente
- `app/factory/service.py:317-319` (`build_bot`) no comprueba `tenant.deleted_at`; solo lo hace el router (`router.py:43`). `build_bot` también se llama desde `app/tenants/service.py:644`. Corrección: replicar la comprobación en el servicio.
- `app/factory/review.py:146-151`: el diccionario `existing` ignora `is_active`. Un servicio descartado con el mismo nombre que el LLM vuelve a proponer bloquea la inserción en silencio (`review.py:157`). El operador no lo ve. Corrección: incluir en el aviso de regeneración los servicios omitidos por estar descartados.
- `app/factory/review.py:372` (`update_service`): renombrar a un nombre ya existente crea duplicados que `fold()` colapsa en `existing`. Corrección: validar unicidad por `fold(name)` dentro del tenant.
- `app/factory/router.py:97`: `from app.factory.service import load_kb` dentro de la función, ya importado en el módulo (`service.py:126`). Es ruido, no bug.
- `app/factory/router.py:162`: `duration_min: int = Form(30)` con valor no numérico devuelve el 422 JSON por defecto de FastAPI a un formulario HTML. Corrección: aceptar `str` y validar con `AppError`.
- `scrape_error` se guarda en `generation_meta` pero no se muestra en `bot.html`. Al operador le aparece "Sin información de la web" sin explicar por qué falló el rastreo.

### 9. UX y copy (plantillas)
- `bot.html:116` y `bot.html:141`: "Descartar" es inmediato, sin confirmación ni deshacer, y además afecta al catálogo en vivo (ver hallazgo 1). Añadir `onsubmit="return confirm('...')"` o un paso de confirmación, y mensaje de deshacer.
- `bot.html:125`: el precio en la tabla de la versión publicada se muestra como `$80000 COP`, sin separador de miles. El prompt (`prompts.py:126`) usa `$80.000 COP`. Corrección: usar el mismo formateador en ambos.
- `bot.html:110`: el campo de precio usa `inputmode="numeric"` pero acepta `80.000` y `80,000`; el servidor lo acepta (`router.py:65-71`). Añadir un texto de ayuda: "Solo números. Ej: 80000".
- `bot.html:112`: el campo de minutos no tiene texto de ayuda sobre el rango 5-480. No verificado en móvil: `app/web/static/factory.css` no se leyó en esta pasada, así que el ajuste de `fb-row` a una columna en pantallas pequeñas queda sin confirmar.
- `_chat.html:4` y `bot.html:197`: el chat no tiene estado de carga para respuestas que pueden tardar varios segundos. Añadir `aria-busy` o un indicador `htmx-indicator`.
- Copy en español: correcto en general. "Aún no hay un bot" y los mensajes de error son claros. Revisar la alerta de inyección: "posibles órdenes dirigidas a la IA" puede ser confuso para un operador no técnico.

### 10. Seguridad: verificado correcto (sin hallazgo)
- Autorización: `router.py` usa `require_role("admin")` en todas las rutas. `review.get_tenant_bot` (`review.py:45-51`) valida que la versión pertenezca al tenant de la URL. `review._service` y `_faq` (`review.py:338-349`) validan tenant. Pruebas en `test_router.py:202` (otro tenant no accesible).
- CSRF: todos los formularios incluyen `csrf()` y el chat envía `csrf_token` (`_chat.html:11`). Validación en `app/core/deps.py:102-109`.
- Escapado: `bot.html` y `_chat.html` usan autoescape. `tojson` en `bot.html:69` escapa `<`, `>` y `&`. El prompt generado (`bot.html:191`) también está escapado.
- Límite de uso: `enforce` en regeneración (10/h) y chat de prueba (30/min), probado en `tests/security/test_hardening.py:45`.
- Prompt: los datos externos van dentro de `<datos_no_confiables>` con sanitización (`prompts.py:48-57`). Las reglas fijas las arma el código, no el LLM (`prompts.py:87-108`).
- Pendiente de verificar: que `scrape_and_store` use `safe_fetch` de `app/core/http.py` en todas sus rutas (`crawl_business_site`, `fetch_instagram_profile`). `core/http.py` bloquea IPs no globales, pero no se confirmó el uso en `app/scraping/`.

### 11. Cobertura de pruebas: huecos
- No hay prueba de que el borrador no afecte al runtime antes de publicar (hallazgo 1).
- No hay prueba de rollback bloqueado por alertas pendientes (hallazgo 2).
- No hay prueba de precios fuera de rango en la rama del dueño (hallazgo 3).
- No hay prueba de falsos positivos de `extract_amounts` con dirección o teléfono (hallazgo 4).
- No hay prueba de excepción genérica en `sandbox_chat` (hallazgo 5).
- No hay prueba de inyección en descripción de servicio (hallazgo 6).
- Sí existen pruebas de rate limit (`tests/security/test_hardening.py:45`), de otro tenant (`test_router.py:202`) y de rollback que requiere versión publicada previa (`test_service.py:306`).

## Prioridad sugerida
1. Hallazgo 1 (aislar catálogo de borrador del runtime).
2. Hallazgo 2 (gate de revisión en rollback).
3. Hallazgos 3 y 4 (validación de precios y grounding).
4. Hallazgo 5 (manejo de errores en chat).
5. Resto.
