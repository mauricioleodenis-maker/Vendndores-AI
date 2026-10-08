/* Comportamiento global (sin inline scripts: CSP script-src 'self'). */
(function () {
  "use strict";

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") : "";
  }

  // HTMX: adjunta el token CSRF a toda solicitud.
  document.addEventListener("htmx:configRequest", function (evt) {
    var token = csrfToken();
    if (token) {
      evt.detail.headers["X-CSRF-Token"] = token;
    }
  });

  // Indicador de carga (estilos propios en app.css).
  document.addEventListener("htmx:beforeRequest", function () {
    document.body.classList.add("is-loading");
  });
  document.addEventListener("htmx:afterRequest", function () {
    document.body.classList.remove("is-loading");
  });

  // Confirmaciones: <form data-confirm="¿Seguro?"> y <button data-confirm="...">.
  document.addEventListener("submit", function (evt) {
    var form = evt.target;
    var msg = form.getAttribute && form.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) {
      evt.preventDefault();
    }
  });
  document.addEventListener("htmx:confirm", function (evt) {
    var el = evt.detail.elt;
    var msg = el && el.getAttribute && el.getAttribute("data-confirm");
    if (msg) {
      evt.preventDefault();
      if (window.confirm(msg)) {
        evt.detail.issueRequest(true);
      }
    }
  });

  // Menu movil.
  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest && evt.target.closest("[data-menu-toggle]");
    if (!btn) {
      return;
    }
    var open = document.body.classList.toggle("menu-open");
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  });
})();
