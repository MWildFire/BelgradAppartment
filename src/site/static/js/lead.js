// Lead form UX — inline validation, never a browser dialog (DATA_CONTRACT §6).
//
// Required per §6: name + contact. We validate client-side and write the message into the
// field's [data-error-for] slot (htmx hx-post otherwise fires regardless). The honeypot
// (name="company") is left untouched — humans never fill it, so we don't validate it; bots
// that do are dropped server-side with a silent 200.
//
// No-JS fallback is intact: the <form> is a real method=post action=/lead with `required`
// attrs, so it works (and the browser shows native validation) if this script never loads.
// On the JS path we suppress native bubbles (novalidate) and own the messaging inline.
//
// Server-side 400s (e.g. CSRF/length) are surfaced inline too: htmx swaps the 400 body in,
// or — if the response isn't a form — we show a single inline form-level error.

(function () {
  "use strict";

  var REQUIRED = ["name", "contact"]; // §6 required fields

  function errorSlot(form, name) {
    return form.querySelector('[data-error-for="' + name + '"]');
  }

  function clearErrors(form) {
    form.querySelectorAll("[data-error-for]").forEach(function (el) {
      el.textContent = "";
    });
    form.querySelectorAll("[aria-invalid]").forEach(function (el) {
      el.removeAttribute("aria-invalid");
    });
  }

  function showError(form, name, msg) {
    var slot = errorSlot(form, name);
    if (slot) slot.textContent = msg;
    var input = form.querySelector('[name="' + name + '"]');
    if (input) input.setAttribute("aria-invalid", "true");
  }

  // returns true when valid; paints inline errors + focuses the first offender otherwise.
  function validate(form) {
    clearErrors(form);
    var firstBad = null;
    REQUIRED.forEach(function (name) {
      var input = form.querySelector('[name="' + name + '"]');
      var val = input ? input.value.trim() : "";
      if (!val) {
        showError(form, name, "Заполни это поле");
        if (!firstBad) firstBad = input;
      }
    });
    if (firstBad) firstBad.focus();
    return firstBad === null;
  }

  function wire(form) {
    form.setAttribute("novalidate", "novalidate"); // own the messaging; no native bubbles

    // htmx fires this before the request; cancel it if client validation fails.
    form.addEventListener("htmx:configRequest", function (e) {
      if (!validate(form)) e.preventDefault();
    });

    // no-htmx path (script loaded but htmx didn't): block native submit on invalid.
    form.addEventListener("submit", function (e) {
      if (!validate(form)) e.preventDefault();
    });

    // server-side validation error → keep the form, surface a form-level message inline.
    form.addEventListener("htmx:responseError", function (e) {
      var status = e.detail && e.detail.xhr ? e.detail.xhr.status : 0;
      if (status === 400) {
        showError(form, "_form", "Проверь поля и попробуй ещё раз.");
      }
    });

    // clear a field's error as soon as the user starts fixing it.
    REQUIRED.forEach(function (name) {
      var input = form.querySelector('[name="' + name + '"]');
      if (input) {
        input.addEventListener("input", function () {
          var slot = errorSlot(form, name);
          if (slot) slot.textContent = "";
          input.removeAttribute("aria-invalid");
        });
      }
    });
  }

  document.querySelectorAll("form.lead").forEach(wire);
})();
