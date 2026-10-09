# Sistema de diseño UI

Archivos: `app/web/static/app.css` (tokens + componentes), `app/web/static/app.js` (comportamiento), `app/web/templates/partials/macros.html` (macros Jinja). CSP: sin `<script>`/`style=` inline; usar clases y `data-*`. HTMX va vendorizado (`/static/htmx.min.js`). Mobile-first; tema claro/oscuro automático por `prefers-color-scheme` (forzable con `<html data-theme="light|dark">`). Todas las clases previas siguen vigentes.

## Tokens (variables CSS)
`--bg --surface --text --muted --border --primary --primary-text --primary-soft --ok/-bg --warn/-bg --danger/-bg --info/-bg --neutral-bg --radius --gutter --shadow --shadow-lg --focus --band-hot/-warm/-cold`. Úsalos en CSS de módulo en vez de colores fijos.

## Macros (`{% from "partials/macros.html" import ... %}`)
- `badge(value, label)`, `field(...)`, `empty_state(title, text, href, label)`, `pager(url, page, has_more)`, `csrf()` (existentes).
- Nuevos: `kpi(label, value, tone='' | ok | warn | danger, hint)`, `score(value, band='')` (hot >=70, warm >=40, cold), `stepper(['Datos','Canales','Listo'], current)`, `bubble(text, 'in'|'out', meta)`.

## Layout
- `.page-header` (h1 + `.page-actions`), `.card` (+ `.card-header`, `.card-flat`, `.card-link`), `.grid-2`, `.grid-3`, `.row`, `.stack`, `.btn-row`, `.form-grid` (+ `.span-2`), `.divider`, `.detail-list` (dl), `.text-right`, `.nowrap`, `.truncate`, `.sr-only`, `.inline-form` (form inline en filas; reemplaza `style="display:inline"`).
- Sidebar: fija en >=900px; drawer en móvil (botón `[data-menu-toggle]`, backdrop `[data-menu-close]`, Escape cierra).

## KPIs
```html
<div class="kpis">
  <div class="kpi kpi-ok"><div class="value">42</div><div class="label">Leads hoy</div><div class="delta up">+8%</div></div>
</div>
```

## Botones
`.btn`, `.btn-primary`, `.btn-ghost`, `.btn-danger`, `.btn-sm`, `.btn-lg`, `.btn-block`. Con HTMX, `.htmx-request` atenúa el botón.

## Formularios
```html
<div class="field has-error">
  <label for="f-x">Nombre <span class="required" aria-hidden="true">*</span></label>
  <input id="f-x" name="x" required aria-invalid="true" aria-describedby="e-x">
  <small class="error" id="e-x">Obligatorio</small>
</div>
<div class="field-check"><input type="checkbox" id="c"><label for="c">Acepto</label></div>
```
Validación en línea: `app.js` marca `.has-error`/`.is-valid` al salir del campo (usa atributos HTML5). `.help` para ayudas.

## Badges y score
`.badge` + `badge-ok|warn|danger|info|neutral|primary`, bandas `badge-hot|warm|cold` (alias `badge-caliente|tibio|frio`), `.badge-dot`. Score: `{{ score(82) }}`.

## Alertas y toasts
- `.alert alert-ok|error|info|warn|success|danger` (en página).
- Toast JS: `window.showToast("Guardado", "ok")` (kinds: ok, error, warn, info).
- Desde el servidor en respuestas HTMX: cabecera `HX-Trigger: {"toast": {"message": "Guardado", "kind": "ok"}}`. Errores HTTP de HTMX y fallos de red muestran toast automáticamente.
- Un elemento con `data-toast="msg" data-toast-kind="ok"` se muestra como toast al cargar.

## Tablas
```html
<div class="table-wrap"><table class="cards">
  <thead><tr><th>Nombre</th><th class="num">Score</th></tr></thead>
  <tbody><tr><td data-label="Nombre">Ana</td><td class="num" data-label="Score">80</td></tr></tbody>
</table></div>
```
`table.cards` + `data-label` se convierte en tarjetas en móvil (<720px). Sin `cards` hace scroll horizontal. `.row-actions`, `.table-toolbar`.

## Estado vacío y carga
`{{ empty_state('Sin leads','Importa tu primera lista', '/admin/leads/importar', 'Importar') }}`.
Carga: `#htmx-indicator` global (clase `is-loading` en body); local: `<span class="htmx-indicator inline" id="ld">Cargando…</span>` con `hx-indicator="#ld"`. También `.spinner` y `.skeleton`.

## Modal
```html
<button class="btn" data-modal-open="#m1">Abrir</button>
<dialog class="modal" id="m1" aria-labelledby="m1t">
  <div class="modal-header"><h2 id="m1t">Título</h2><button class="btn btn-ghost btn-sm" data-modal-close aria-label="Cerrar">&times;</button></div>
  <div class="modal-body">…</div>
  <div class="modal-footer"><button class="btn" data-modal-close>Cancelar</button><button class="btn btn-primary">OK</button></div>
</dialog>
```

## Kanban
```html
<div class="kanban"><section class="kanban-col"><div class="kanban-col-header"><h2>Nuevo</h2><span class="count-pill">3</span></div>
  <article class="kanban-card hot">…</article></section></div>
```

## Chat
```html
<div class="chat" aria-live="polite">
  <div class="bubble bubble-in">Hola<span class="bubble-meta">10:02</span></div>
  <div class="bubble bubble-out">Buenas</div>
  <div class="bubble bubble-system">Pasó a humano</div>
</div>
<form class="chat-input"><textarea name="m"></textarea><button class="btn btn-primary">Enviar</button></form>
```
Autoscroll al insertar contenido vía HTMX. Alias: `.bubble.user|assistant`.

## Stepper (wizard)
`{{ stepper(['Negocio','Canales','Revisión'], 2) }}` o `<ol class="stepper"><li class="step done">…</li><li class="step current" aria-current="step">…</li><li class="step">…</li></ol>`.

## Pestañas
`<nav class="tabs"><a class="tab active" href="…">Todos</a></nav>` (alias `.lead-tabs`).

## Accesibilidad
Skip-link a `#main`, foco visible 3px, objetivos táctiles 44px, `aria-current` en nav, `role=status` en toasts, `prefers-reduced-motion` respetado. Evitar `style=` inline y handlers `onclick`.
