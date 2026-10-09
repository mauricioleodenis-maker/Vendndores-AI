# Revision read-only: modulo leads-search-ui

Alcance: `app/leads/places.py`, `app/leads/search.py`, `app/leads/router.py` (rutas de busqueda), `app/web/templates/leads/search.html`, `_search_result.html`, `_status.html`, tests `tests/leads/test_search.py`, `test_places.py`, `test_router.py`. Sin cambios de codigo.

Ya cubierto y correcto: CSRF global (`app/main.py:135`), API key no logueada (`places.py:323`, `:393`), reintentos solo 429/5xx, `maps_url` de detalle filtrado a https (`detail.html:18`), `website` normalizado a https (`normalize.py:98`), autoescape en plantillas, mascara de campos explicita, tope de presupuesto y rate limit por usuario (10/h).

## Hallazgos (por severidad)

### 1. Media - costo reportado no coincide con solicitudes facturables
- `app/leads/search.py:166` calcula `cost_usd = stats.requests * COST_USD_PER_SEARCH_REQUEST`, pero `stats.requests` cuenta llamadas logicas (`places.py:489`), no HTTP. Cada reintento (hasta 4 intentos, `places.py:354-375`) puede ser facturado y no se cuenta. `client.requests_made` (`places.py:369`) tiene el dato real y no se usa.
- Falla: una busqueda con 2 reintentos por 429/503 reporta un costo 3x menor al real y el presupuesto diario/mensual (`places.py:249-261`) tambien subestima el gasto.
- Fix: contar cada intento HTTP en el presupuesto (`consume` dentro del bucle de `_request`, o `cost_units` por intento) y guardar `cost_usd` desde `requests_made`. Agregar test que simule 429 y verifique presupuesto y costo.

### 2. Media - estados atascados sin salida (silent failure)
- `app/leads/router.py:199-201`: `enqueue` ocurre despues del `commit`. Si el encolado falla, la fuente queda en `queued` para siempre. En la ruta HTML solo se captura `AppError` (`router.py:567-570`), asi que el error llega como 500 y la tarjeta puede quedar sin feedback.
- `app/leads/search.py:181`: si el worker muere a mitad de la busqueda, la fuente queda en `running` sin timeout. `_status.html:2` hace polling cada 2 s sin limite, y el job ignora fuentes que no esten en `queued` (`search.py:179`), asi que no hay reintento.
- Fix: en `launch_search`, capturar la excepcion de `enqueue` y marcar la fuente `failed` con mensaje claro; agregar un barrido (o cron) que marque `queued`/`running` con mas de N minutos como `failed`; limitar el polling en `_status.html` (p. ej. maximo de intentos o mensaje "tarda mas de lo esperado" tras 2-3 min).
- Tests faltantes: enqueue que lanza excepcion; fuente `running` vieja.

### 3. Media - autorizacion de busquedas pagadas y lectura de fuentes
- `app/leads/router.py:323` (`/api/leads/search`), `:530` (`/admin/leads/buscar` POST) y `:349`/`:578` (status) usan solo `current_user`, sin `require_role`. El `docs/revision-seguridad.md` (punto 5) reserva facturacion y campanas a `admin`; las busquedas consumen presupuesto de Google y crean leads.
- `app/leads/search.py:210-214` `get_source` no verifica dueno ni tenant: cualquier usuario autenticado puede leer `params`, `queries` y `error` de cualquier fuente por UUID.
- Fix: `Depends(require_role("admin"))` (o al menos `"operator"` si es intencional) en los endpoints de busqueda; en `get_source` filtrar por `created_by == user.id` salvo owner. Decidir el rol con el dueno del producto.

### 4. Baja - silencio al truncar barrios
- `app/leads/search.py:76` (`parse_neighborhoods`) corta en 20 sin avisar; `search.html:25` dice "maximo 20" pero el usuario no ve que se descartaron los demas.
- Fix: devolver los descartados y mostrar aviso en `_search_result.html` (p. ej. "Se usaron 20 de 27 barrios").
- Test faltante: 21 barrios -> aviso y lista truncada.

### 5. Baja - `get_details` sin validacion completa de id y codigo no usado
- `app/leads/places.py:434` valida solo `/` y longitud. Caracteres `?`, `#`, `..` se concatenan en `places/{place_id}` (`places.py:438`) y pueden alterar la consulta.
- `get_details` no tiene llamadores en `app/` (solo definicion). Fix: validar con regex `^[A-Za-z0-9_-]{1,200}$` o eliminar la funcion hasta que se use.

### 6. Baja - accesibilidad y UX de formulario
- `search.html:24`, `:33`, `:29`: el texto de ayuda no esta asociado con `aria-describedby`; el campo de barrios no declara limite de caracteres visible.
- `search.html:36`: `data-confirm` solo funciona si la app tiene un manejador para ese atributo; verificar que exista para HTMX (si no, el confirm no se muestra).
- `search.html:37`: "Presupuesto mensual configurado: US$ {{ monthly_budget }}" no muestra gasto acumulado del mes; el usuario no sabe cuanto queda.
- `_status.html:8`: un error de `partial` (presupuesto agotado) y de `failed` se ven igual (`alert-warn`) aunque `failed` deberia ser `alert-error`.
- Mobile: los botones de `search.html:35-36` estan en linea sin ajuste; verificar en 360 px.

### 7. Brecha de tests
- Faltan: encolado fallido; fuente `running` atascada; costo con reintentos; truncado de barrios; acceso cruzado a `GET /api/leads/sources/{id}/status`; estado `failed` renderizado en `_status.html`.
- Existentes y utiles: `test_search.py` (job, parcial por presupuesto, fallo de Google), `test_places.py` (reintentos, presupuesto, QPS), `test_router.py:184-290` (estimacion sin llamadas, rate limit, validacion).
