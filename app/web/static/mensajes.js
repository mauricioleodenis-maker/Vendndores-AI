/* Copiar mensaje y abrir WhatsApp con el texto (editado) del cuadro. Sin inline scripts (CSP). */
(function () {
  "use strict";
  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.className = "sr-only";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) { /* sin permisos */ }
    document.body.removeChild(ta);
  }
  function textOf(el) {
    var box = document.getElementById(el.getAttribute("data-msg-target"));
    return box ? box.value : "";
  }
  document.addEventListener("click", function (evt) {
    var copy = evt.target.closest && evt.target.closest("[data-msg-copy]");
    if (copy) {
      var text = textOf(copy);
      var label = copy.textContent;
      var done = function () {
        var live = document.querySelector("[data-msg-live]");
        if (live) { live.textContent = "Mensaje copiado"; }
        copy.textContent = "¡Copiado!";
        setTimeout(function () { copy.textContent = label; }, 1600);
      };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text); done(); });
      } else { fallbackCopy(text); done(); }
      return;
    }
    var wa = evt.target.closest && evt.target.closest("[data-msg-wa]");
    if (wa) {
      var phone = wa.getAttribute("data-phone");
      wa.href = "https://wa.me/" + phone + "?text=" + encodeURIComponent(textOf(wa));
    }
    var alt = evt.target.closest && evt.target.closest("[data-msg-alt]");
    if (alt) {
      var box = document.getElementById(alt.getAttribute("data-msg-target"));
      if (box) { box.value = alt.getAttribute("data-text"); box.focus(); }
    }
  });
})();
