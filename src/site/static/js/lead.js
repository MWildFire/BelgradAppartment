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

  // Pre-fill the hidden site_listing_id from the apartment context, if any. The server-rendered
  // value (apt.html sets it from item.id) wins; this only fills a still-empty field, e.g. when
  // the form is reused without that context. Empty stays empty → a general (homepage) lead.
  function applyApartmentContext(form) {
    var hidden = form.querySelector('[name="site_listing_id"]');
    if (!hidden || hidden.value) return; // already carries an id (server-rendered) — leave it
    // nearest ancestor that declares the apartment id (the <section data-site-listing-id>)
    var holder = form.closest("[data-site-listing-id]");
    var id = holder ? holder.getAttribute("data-site-listing-id") : "";
    if (id) hidden.value = id;
  }

  function wire(form) {
    form.setAttribute("novalidate", "novalidate"); // own the messaging; no native bubbles
    applyApartmentContext(form);

    // htmx fires this before the request; cancel it if client validation fails.
    form.addEventListener("htmx:configRequest", function (e) {
      if (!validate(form)) e.preventDefault();
    });

    // no-htmx path (script loaded but htmx didn't): block native submit on invalid.
    form.addEventListener("submit", function (e) {
      if (!validate(form)) e.preventDefault();
    });

    // server-side error → keep the form, surface a form-level message inline AND move focus to
    // it (the slot is role=alert, so it's also announced). The message differs by status so an
    // expired session / rate-limit isn't mistaken for a field-validation problem.
    form.addEventListener("htmx:responseError", function (e) {
      var status = e.detail && e.detail.xhr ? e.detail.xhr.status : 0;
      var msg = "";
      if (status === 400) {
        msg = "Проверь поля и попробуй ещё раз.";
      } else if (status === 403) {
        // CSRF token expired (e.g. the page sat open across a deploy/restart).
        msg = "Сессия устарела — обнови страницу и отправь заявку ещё раз.";
      } else if (status === 429) {
        msg = "Слишком много заявок подряд — попробуй ещё раз через несколько минут.";
      } else if (status) {
        msg = "Что-то пошло не так. Попробуй ещё раз или напиши нам в Telegram.";
      }
      if (!msg) return;
      showError(form, "_form", msg);
      var slot = errorSlot(form, "_form");
      if (slot) {
        slot.setAttribute("tabindex", "-1");
        slot.focus();
        if (slot.scrollIntoView) slot.scrollIntoView({ block: "center" });
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

  // «Оставить заявку на эту квартиру» on the detail page: native #lead jump scrolls to the
  // form; we additionally focus the first field and (re)assert the apartment id on the form.
  document.querySelectorAll("[data-lead-cta]").forEach(function (cta) {
    cta.addEventListener("click", function () {
      var form = document.querySelector("form.lead");
      if (!form) return;
      var id = cta.getAttribute("data-site-listing-id");
      var hidden = form.querySelector('[name="site_listing_id"]');
      if (hidden && id) hidden.value = id;
      var name = form.querySelector('[name="name"]');
      if (name) name.focus();
    });
  });
})();
