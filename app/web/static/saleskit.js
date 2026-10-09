/* Boton Copiar del kit de ventas (sin inline scripts: CSP script-src 'self'). */
(function () {
  "use strict";
  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.className = "visually-hidden";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) { /* sin permisos */ }
    document.body.removeChild(ta);
  }
  document.addEventListener("click", function (evt) {
    var btn = evt.target.closest && evt.target.closest("[data-kit-copy]");
    if (!btn) { return; }
    var box = btn.closest(".kit-msg");
    var src = box && box.querySelector(".kit-msg-text");
    if (!src) { return; }
    var text = Array.prototype.map.call(src.querySelectorAll("p"), function (p) {
      return p.innerText.trim();
    }).join("\n\n");
    var done = function () {
      btn.textContent = "Copiado";
      setTimeout(function () { btn.textContent = "Copiar"; }, 1800);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text); done(); });
    } else {
      fallbackCopy(text);
      done();
    }
  });
})();
