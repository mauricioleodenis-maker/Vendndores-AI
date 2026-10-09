# Revisión Haiku: tenants

The design system is in place and the full suite passes (`pytest -q`, all green). ruff reports no issues. I didn't open the pages in a browser, so none of it has been checked visually.

**What changed**
- **`app/web/static/app.css`** (extended, existing classes kept):
  - New variables for light and dark mode.
  - Sidebar fixed on desktop and a drawer on mobile.
  - Cards, KPI tiles, buttons, forms with inline error and valid styles.
  - Badges, including hot/warm/cold score bands, and responsive tables that turn into cards on mobile.
  - Empty states, tabs, toasts, loading indicators, modal, kanban, chat bubbles and wizard stepper.
- **`app/web/static/app.js`** (existing behaviour kept, no inline scripts or `eval`):
  - `window.showToast(msg, kind)`, plus toasts from an `HX-Trigger: {"toast": {...}}` response header, from HTMX errors and network failures, and from any element with `data-toast`.
  - Drawer closes on Escape or when you tap outside it.
  - Modal, inline field validation, and chat autoscroll.
- **`app/web/templates/base.html`**: skip link, drawer backdrop, a toast region, and `aria-controls` on the menu button. HTMX stays vendored with no CDN.
- **`app/web/templates/partials/macros.html`**: four new macros, `kpi`, `score`, `stepper` and `bubble`.
- **`docs/ui-sistema.md`**: class names, macros and HTML snippets for the other modules to follow.

I left the login template and the per-module CSS files as they were. They work with the new styles.

**For other module owners:** four templates still use `style="display:inline"`, which the CSP doesn't allow:
- `booking/horarios.html` (line 43)
- `tenants/detail.html` (lines 11 and 125)

These files aren't mine, so I didn't edit them. The new `.inline-form` class replaces that attribute. I did not append this to `integracion-pendientes.md`, so the owners will need to be told.
