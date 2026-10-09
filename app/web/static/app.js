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

/* ===== Sistema UI v2: toasts, drawer, modal, validacion (CSP-safe, sin eval) ===== */
(function () {
  "use strict";

  function region() {
    var r = document.getElementById("toast-region");
    if (!r) {
      r = document.createElement("div");
      r.id = "toast-region";
      r.className = "toast-region";
      r.setAttribute("role", "status");
      r.setAttribute("aria-live", "polite");
      document.body.appendChild(r);
    }
    return r;
  }

  // API publica: window.showToast("mensaje", "ok"|"error"|"warn"|"info")
  function showToast(message, kind) {
    var t = document.createElement("div");
    t.className = "toast toast-" + (kind || "info");
    if (kind === "error") { t.setAttribute("role", "alert"); }
    var m = document.createElement("div");
    m.className = "toast-msg";
    m.textContent = String(message);
    var b = document.createElement("button");
    b.type = "button";
    b.className = "toast-close";
    b.setAttribute("aria-label", "Cerrar aviso");
    b.textContent = "×";
    b.addEventListener("click", function () { t.remove(); });
    t.appendChild(m);
    t.appendChild(b);
    region().appendChild(t);
    setTimeout(function () { if (t.parentNode) { t.remove(); } }, kind === "error" ? 9000 : 5000);
    return t;
  }
  window.showToast = showToast;

  // Servidor: cabecera HX-Trigger: {"toast": {"message": "...", "kind": "ok"}} o "toast".
  document.addEventListener("toast", function (evt) {
    var d = evt.detail || {};
    if (typeof d === "string") { d = { message: d }; }
    if (d.message || d.value) { showToast(d.message || d.value, d.kind || "ok"); }
  });
  // Errores HTMX -> toast.
  document.addEventListener("htmx:responseError", function (evt) {
    var s = evt.detail && evt.detail.xhr ? evt.detail.xhr.status : "";
    showToast(s === 403 ? "Sin permiso o sesión vencida." : "No se pudo completar la acción (" + s + ").", "error");
  });
  document.addEventListener("htmx:sendError", function () {
    showToast("Sin conexión con el servidor.", "error");
  });
  // Alertas del servidor con data-toast se muestran como toast tambien.
  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.forEach.call(document.querySelectorAll("[data-toast]"), function (el) {
      showToast(el.getAttribute("data-toast"), el.getAttribute("data-toast-kind") || "info");
    });
  });

  // Drawer movil: cerrar con Escape, backdrop o al navegar.
  function closeMenu() {
    document.body.classList.remove("menu-open");
    var b = document.querySelector("[data-menu-toggle]");
    if (b) { b.setAttribute("aria-expanded", "false"); }
  }
  document.addEventListener("click", function (evt) {
    var t = evt.target;
    if (t.closest && (t.closest("[data-menu-close]") || (t.closest(".sidebar a") && document.body.classList.contains("menu-open")))) {
      closeMenu();
    }
  });
  document.addEventListener("keydown", function (evt) {
    if (evt.key === "Escape") { closeMenu(); }
  });

  // Modal: data-modal-open="#id" / data-modal-close sobre <dialog class="modal">.
  document.addEventListener("click", function (evt) {
    var o = evt.target.closest && evt.target.closest("[data-modal-open]");
    if (o) {
      var d = document.querySelector(o.getAttribute("data-modal-open"));
      if (d && d.showModal) { d.showModal(); }
      return;
    }
    var c = evt.target.closest && evt.target.closest("[data-modal-close]");
    if (c) {
      var dlg = c.closest("dialog");
      if (dlg && dlg.close) { dlg.close(); }
    } else if (evt.target.tagName === "DIALOG" && evt.target.classList.contains("modal")) {
      evt.target.close();
    }
  });

  // Validacion en linea: marca .has-error al salir de un campo invalido.
  function mark(el) {
    var f = el.closest && el.closest(".field");
    if (!f || !el.willValidate) { return; }
    var bad = !el.checkValidity();
    f.classList.toggle("has-error", bad);
    f.classList.toggle("is-valid", !bad && el.value !== "");
    if (bad) { el.setAttribute("aria-invalid", "true"); } else { el.removeAttribute("aria-invalid"); }
  }
  document.addEventListener("focusout", function (evt) {
    if (evt.target.matches && evt.target.matches("input, select, textarea")) { mark(evt.target); }
  });
  document.addEventListener("input", function (evt) {
    var el = evt.target;
    if (el.matches && el.matches("input, select, textarea") && el.getAttribute("aria-invalid") === "true") { mark(el); }
  });

  // Chat: autoscroll al final tras insertar mensajes.
  document.addEventListener("htmx:afterSwap", function (evt) {
    var chat = (evt.target.closest && evt.target.closest(".chat")) || (evt.target.querySelector && evt.target.querySelector(".chat"));
    if (chat) { chat.scrollTop = chat.scrollHeight; }
  });
})();
