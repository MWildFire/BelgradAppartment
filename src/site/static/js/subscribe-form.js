// Subscribe form: channel radio (e-mail ↔ Telegram) → show/hide + required у e-mail поля.
// T4-4: вынесено из inline onchange, чтобы снять 'unsafe-inline' из script-src CSP.
//
// Почему `change`, а не `click`: клавиатурное переключение radio стрелками даёт `change`,
// а не `click` — иначе show/hide + required рассинхронизируются для клавиатуры.
//
// Delegated on document, not bound to the inputs: the form is an htmx outerHTML swap target
// (#subscribe), so a re-rendered form keeps working without re-binding. The handler SETS state
// (never toggles), so it is idempotent even if the script somehow runs twice.
//
// No-JS parity: разметка по умолчанию — e-mail канал checked, поле видимо и required; без JS
// так и остаётся при любом выборе, сервер валидирует в любом случае (см. _subscribe_form.html).
(function () {
  "use strict";
  document.addEventListener("change", function (e) {
    var el = e.target;
    if (!el || el.name !== "channel" || !el.closest || !el.closest("form.subscribe")) return;
    var field = document.getElementById("sub-email-field");
    var email = document.getElementById("sub-email");
    if (!field || !email) return;
    var isEmail = el.value === "email";
    // hidden + style.display вместе: `.field{display:flex}` (site.css) перебивает UA-правило
    // [hidden]{display:none}, так что один атрибут поле НЕ прятал — давний баг ещё inline-версии.
    // CSSOM-присваивание (el.style.display) не попадает под style-src CSP, inline-стили не нужны.
    field.hidden = !isEmail;
    field.style.display = isEmail ? "" : "none";
    email.required = isEmail;
  });
})();
