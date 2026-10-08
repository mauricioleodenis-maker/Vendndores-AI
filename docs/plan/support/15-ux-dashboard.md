# 15 - UX del dashboard de administración (soporte)

Estado: propuesta de soporte para los planes 01-04 y 00-MAESTRO. Si contradice a 00, gana 00.
Idioma de la UI: español (Colombia, tuteo). Stack: Jinja2 + HTMX vendorizado en `app/web/static/`, sin SPA.
Rutas HTML bajo `/admin/*`; los parciales HTMX devuelven fragmentos (`hx-target`, `hx-swap`).

## 1. Principios
- Un operador no técnico debe crear un bot en menos de 10 minutos desde el wizard.
- Cada pantalla tiene una acción principal visible y un estado vacío con llamado a la acción.
- Acciones destructivas o de envío (campañas, cancelar suscripción, borrar demo) piden confirmación explícita.
- Estados siempre visibles con texto y color: `draft`, `generating`, `review`, `active`, `paused`, `needs_review`.
- Mobile first: gutter de 16 px, tablas que se vuelven tarjetas bajo 720 px, sin scroll horizontal de página.
  Tablas anchas con `overflow-x: auto` dentro de su contenedor.
- Errores en español, junto al campo. Nunca mostrar trazas ni datos sensibles de salud (Ley 1581).
- Roles: `owner`/`admin` ven todo; `operator` ve leads, prueba secreta y borradores de campaña, sin iniciar envíos
  ni facturación. Los botones que no le aplican se ocultan (y el servidor responde 403 igual).

## 2. Mapa de navegación
Barra lateral (escritorio) / barra inferior con menú "Más" (móvil):
1. Inicio
2. Empresas (negocios/tenants)
3. Conversaciones
4. Citas
5. Leads
6. Campañas
7. Planes (y facturación como subpestaña)
8. Ajustes

Ruta base por sección: `/admin/inicio`, `/admin/negocios`, `/admin/negocios/nuevo`, `/admin/negocios/{id}`,
`/admin/negocios/{id}/bot`, `/admin/negocios/{id}/bot/probar`, `/admin/conversaciones`, `/admin/citas`,
`/admin/leads`, `/admin/leads/{id}`, `/admin/campanas`, `/admin/campanas/{id}`, `/admin/planes`,
`/admin/facturacion`, `/admin/ajustes`.

## 3. Pantallas

### 3.1 Inicio
- Fila de KPIs (tarjetas de 2 por fila en móvil, 4 en escritorio):
  negocios activos, bots en revisión, conversaciones hoy, citas próximas 7 días, MRR (COP), setups del mes,
  leads nuevos, mensajes de campaña enviados hoy / tope diario.
- Bloque "Pendiente de tu atención": bots en `review`, `needs_review` (servicios/precios sin dato),
  handoffs sin responder > 15 min, campañas pausadas por calidad, facturas vencidas.
- Gráfico simple de conversaciones últimos 14 días (SVG sin librería; ver skill dataviz para paleta).
- HTMX: cada KPI se recarga con `hx-get="/admin/inicio/kpis" hx-trigger="every 60s"`.

### 3.2 Empresas (lista)
- Buscador (`hx-get` con `keyup changed delay:400ms`), filtros: nicho, estado, plan.
- Tabla: nombre, nicho, ciudad, plan, estado del bot, último mensaje. Móvil: tarjeta con nombre + badges.
- Paginación por cursor "Cargar más" (`hx-trigger="revealed"`) o páginas de 25.
- Botón primario: "Nueva empresa".

### 3.3 Nueva empresa (wizard de 3 pasos)
Barra de progreso arriba con los 3 pasos; cada paso es un form parcial `hx-post` que guarda borrador y devuelve el siguiente paso.
- Paso 1 "Datos del negocio": nombre, nicho (select con 4 opciones, obligatorio), ciudad (default Cali), dirección,
  teléfono, web, Instagram. Validación en vivo (`hx-post /admin/negocios/validar-campo`).
- Paso 2 "Servicios y horarios": lista editable de servicios (nombre, precio COP, duración min) con
  "Agregar servicio" (`hx-get` de una fila vacía, `hx-swap="beforeend"`); horarios por día con rangos;
  notas libres. Precargado desde la plantilla de nicho.
- Paso 3 "Revisar y generar": resumen en solo lectura, checkbox "Autorizo el uso de datos del negocio", botón "Generar bot".
  Al enviar, estado `generating` y polling `hx-get="/admin/negocios/{id}/estado" hx-trigger="every 3s"`
  hasta pasar a `review`, luego redirección (`HX-Redirect`) a la pantalla de revisión del bot.
- Si el scraping falla: aviso no bloqueante "No pudimos leer la web; completa los servicios a mano".

### 3.4 Ficha de empresa
- Encabezado: nombre, nicho, estado (badge), plan, botones "Revisar bot", "Probar bot", "Pausar/Reanudar".
- Pestañas (HTMX, `hx-get` por pestaña, URL con `hx-push-url`): Datos, Bot, Conversaciones, Citas, Suscripción.

### 3.5 Revisión del bot
- Panel izquierdo (escritorio) / sección superior (móvil): secciones colapsables del config generado:
  persona/tono, servicios y precios, FAQs, reglas de agenda, plantillas de mensaje, temas prohibidos, disparadores de handoff.
- Campos marcados `needs_review` con borde ámbar y texto "Falta confirmar".
- Cada sección se edita en línea: `hx-get` del formulario, `hx-put` al guardar, feedback "Guardado" con `hx-swap-oob`.
- Barra inferior fija (móvil) con "Aprobar y activar" (deshabilitado hasta que no queden `needs_review`) y "Regenerar sección" (con confirmación).
- Historial de cambios (`audit_log`) colapsado al final.

### 3.6 Probar bot (chat de prueba)
- Pantalla de chat con burbujas (cliente a la derecha, bot a la izquierda), input fijo abajo.
- Envío `hx-post /admin/negocios/{id}/bot/probar/mensaje` con `hx-swap="beforeend"` y `hx-target="#chat"`;
  indicador "escribiendo…" mientras responde (`htmx-indicator`).
- Panel lateral (escritorio) / botón "Ver contexto" (móvil): intención detectada, si hubo handoff, si se
  agendó cita simulada, tokens usados.
- Botón "Reiniciar conversación". Banner fijo: "Modo prueba: no se envían mensajes reales".
- Se marca cada respuesta con 👍/👎 (`hx-post`) para alimentar la revisión del bot.

### 3.7 Conversaciones
- Dos columnas en escritorio (lista + hilo); en móvil, lista y luego hilo a pantalla completa con "Volver".
- Lista: teléfono enmascarado (`+57 ••• ••67`), último mensaje, badge `bot` / `humano` / `handoff pendiente`.
- Filtro rápido por estado; búsqueda por texto (debounced).
- Hilo: mensajes, sello de tiempo en America/Bogota, botón "Tomar control" (pasa a humano) y "Devolver al bot".
- Respuesta manual del operador: textarea + enviar (`hx-post`); deshabilitado fuera de la ventana de 24 h de WhatsApp.
- Actualización: `hx-get` del hilo con `hx-trigger="every 10s"` solo cuando la conversación está abierta.

### 3.8 Citas
- Vista lista agrupada por día (hoy, mañana, esta semana) con toggle "Lista / Semana" (semana en escritorio; en móvil solo lista).
- Cada cita: hora, servicio, nombre, teléfono enmascarado, estado (`confirmada`, `pendiente`, `cancelada`, `no asistió`).
- Acciones: confirmar, reprogramar, cancelar (confirmación), marcar no asistió.
- Filtro por negocio (si el usuario ve varios) y estado.
- Aviso si Google Calendar no está conectado: "Calendario no conectado. Las citas solo quedan en el sistema."

### 3.9 Leads
Tres vistas con la misma URL base y `hx-push-url`: Tabla (por defecto), Pipeline, Mapa de calor opcional (no MVP).
- Buscador y filtros: nicho, ciudad, banda de score (alta/media/baja), etapa, "con prueba secreta" sí/no.
  Filtros como `hx-get` con `hx-include` del form; contador "23 leads" con `hx-swap-oob`.
- Tabla: nombre, nicho, barrio, score (badge por banda), etapa, última acción. Casillas para acciones en lote (máx. 200)
  con barra inferior "Cambiar etapa / Agregar a campaña".
- Móvil: tarjetas; el pipeline pasa a columnas con scroll horizontal dentro de su contenedor, o a lista por etapa colapsable.
- Pipeline (kanban): columnas `nuevo`, `prueba_secreta`, `contactado`, `demo`, `cerrado`. Arrastrar en escritorio
  (`hx-post /admin/leads/{id}/stage`); en móvil, botón "Mover a…" en cada tarjeta (sin drag).
- Buscar nuevos leads: formulario (nicho, ciudad, barrios, máx. resultados, dry-run) → 202 y barra de progreso con
  polling a `/admin/leads/sources/{id}/status`. Muestra presupuesto estimado antes de confirmar.
- Importar CSV: arrastrar archivo o seleccionar; previa de 10 filas con errores marcados; confirmar.
- Detalle de lead (`/admin/leads/{id}`, panel lateral o pantalla completa): datos, score con motivos, prueba secreta
  (registrar intento), botones "Crear demo" (idempotente) y "Ver pitch" con evidencia de tiempo de respuesta.

### 3.10 Campañas de outreach
- Lista: nombre, plantilla, estado (`borrador`, `lista`, `en curso`, `pausada`, `terminada`), enviados / tope diario.
- Editor de borrador: nombre, plantilla aprobada (select, solo `approved`), filtro de audiencia, límite diario.
- Paso "Vista previa": conteo de audiencia y 5 mensajes renderizados. Botón "Iniciar" solo para `owner/admin`,
  abre modal de confirmación con texto explícito "Se enviarán N mensajes por WhatsApp".
- Controles en curso: pausar, reanudar, cancelar; contador en vivo (`hx-trigger="every 15s"`).
- Banner si está pausada por calidad: "Pausada: más de 5% de fallos en los últimos 100 envíos".
- Lista de supresión y opt-out visible en Ajustes (solo lectura para operador).

### 3.11 Planes
- Tarjetas de planes (Básico, Pro, Premium) en COP con setup y mensual, lista de funciones, badge "Más elegido" en Pro.
- Oferta Fundador: bloque con "Quedan N cupos" (valor real desde `offers`).
- Cotizador: select de plan + oferta + "Incluir IVA", resultado en vivo (`hx-get` con `hx-trigger="change"`):
  setup, mensual, descuentos, total primera factura.
- Editor de precios (`owner/admin`): formulario en modal; aviso "Aplica solo a nuevas suscripciones".
- Facturación (subpestaña): tabla de cobros con filtro por estado, totales (MRR, setups del mes, vencido),
  botón "Marcar pagado" con confirmación, exportar CSV (acción auditada).

### 3.12 Ajustes
- Secciones: Cuenta (cambiar contraseña, cerrar sesiones), Usuarios y roles (invitar, cambiar rol, desactivar),
  Canales (estado de Twilio y número demo; sin mostrar secretos), Google (Places y Calendar: estado de conexión),
  Cumplimiento (lista de supresión, opt-outs, retención de datos, exportar/borrar datos de un negocio),
  Auditoría (lista filtrable de `audit_log`).
- Secretos y tokens: solo se muestran enmascarados; "Rotar" pide confirmación y muestra el valor una sola vez.

## 4. Interacciones HTMX clave
| Interacción | Patrón |
|---|---|
| Buscador con filtros | `hx-get` + `hx-trigger="input changed delay:400ms, change"` + `hx-include` del form |
| Validación de campo | `hx-post` en `blur` devuelve el mensaje debajo del campo |
| Wizard | `hx-post` por paso, `hx-target="#wizard"`, `hx-push-url` del número de paso |
| Agregar fila (servicios) | `hx-get` a fila vacía, `hx-swap="beforeend"` |
| Eliminar fila | `hx-delete` con `hx-confirm` o confirmación inline |
| Estado de generación | `hx-get` polling cada 3 s hasta `review` (luego `HX-Redirect`) |
| Chat de prueba | `hx-post` + `beforeend` + indicador `htmx-indicator` |
| Hilo de conversación | polling cada 10 s solo con hilo abierto; se detiene al cambiar de conversación |
| Pipeline | `hx-post /admin/leads/{id}/stage` con drag (escritorio) o select (móvil) |
| Acciones en lote | `hx-post` con `hx-vals` de ids; respuesta OOB refresca contador |
| Modales | `hx-get` del fragmento a `#modal`; cerrar con `hx-on` tras éxito |
| Mensajes flash | `hx-swap-oob` a `#toasts`, se oculta a los 4 s |

Reglas técnicas:
- Todo `POST/PUT/DELETE` incluye token CSRF en header `HX-Request` + cookie/`X-CSRF-Token` (ver 01 seguridad).
- Errores de validación HTTP 422 se renderizan como parcial del formulario (no como página de error).
- Cada parcial tiene su ruta sin layout (`?partial=1` o header `HX-Request`) para no duplicar plantillas.
- Toda pantalla con datos de salud o teléfonos usa la versión enmascarada; el valor completo solo en detalle con permiso.

## 5. Mobile y accesibilidad
- Puntos de quiebre: 720 px (tarjetas) y 1024 px (dos columnas).
- Objetivos táctiles de 44 px mínimo; botón principal de la pantalla siempre visible (barra inferior fija cuando aplica).
- Contraste AA en ambos temas (`prefers-color-scheme`); estados nunca solo por color.
- Etiquetas `label` en todos los campos; `aria-live="polite"` en toasts y en el chat.
- Teclado: el chat envía con Enter y Shift+Enter hace salto de línea.

## 6. Estados vacíos y de error (texto sugerido)
- Sin negocios: "Aún no tienes empresas. Crea la primera en 3 pasos." [Nueva empresa]
- Sin leads: "Busca negocios en Cali o importa un CSV del Maps-Scraper." [Buscar] [Importar]
- Sin conversaciones: "Cuando un cliente escriba al bot, la conversación aparecerá aquí."
- Error de servidor en fragmento: "No pudimos cargar esto. [Reintentar]" (el resto de la página sigue funcionando).

## 7. Criterios de aceptación UX (para tests E2E)
1. Crear un negocio con el wizard, generar bot y llegar a revisión sin recargar la página completa.
2. Chat de prueba responde y no envía mensajes reales (banner visible).
3. "Aprobar y activar" deshabilitado mientras haya `needs_review`.
4. Lead cambia de etapa por drag (escritorio) y por select (móvil), con entrada en `lead_event`.
5. Campaña no se puede iniciar sin confirmación; operador no ve el botón "Iniciar".
6. Todas las pantallas sin scroll horizontal de página a 360 px de ancho.
7. Teléfonos siempre enmascarados en listas.

## 8. Fuera de alcance del MVP
Notificaciones push, app nativa, reportes exportables en PDF, editor visual de flujos del bot, multi-idioma (solo español),
mapa de calor de leads, drag en móvil.
