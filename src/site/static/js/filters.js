// Catalog filter rail — progressive enhancement ONLY (Wave 4-E1).
//
// The form works fully without JS: it's a GET <form action="/catalog"> and htmx already
// swaps #results on change/submit (hx-get + hx-push-url → deep-link + Back work natively).
// This file adds nicety on top; if it never loads, nothing breaks.
//
// What it does:
//   0. submit the sort <select>'s form on change (T4-4: was inline onchange — moved here so
//      CSP script-src can drop 'unsafe-inline'; sorting always needed JS, no-JS is unchanged);
//   1. debounce the price <input> change → htmx swap (avoid a request per keystroke);
//   2. mirror the .on visual state on toggles/pills when their checkbox flips (no-JS uses
//      :has() in CSS, but older engines fall back to this);
//   3. a "сбросить" (clear-all) control that resets every field and re-swaps once.
//
// Respects prefers-reduced-motion (only affects our own transient hint, htmx anims are CSS).

(function () {
  "use strict";

  // --- 0. sort <select> submit (T4-4: CSP) --------------------------------------------
  // Раньше жило inline (onchange="this.form.requestSubmit()") — вынесено сюда, чтобы снять
  // 'unsafe-inline' из script-src. Delegated on document, NOT bound to the node: #results is
  // an htmx swap target, a fresh <select id="sort"> arrives with every filter change, and a
  // document-level listener survives every swap. requestSubmit (not submit) so htmx's submit
  // handler fires — same path as the no-JS GET form.
  document.addEventListener("change", function (e) {
    var el = e.target;
    if (!el || el.id !== "sort" || !el.form) return;
    if (el.form.requestSubmit) el.form.requestSubmit();
    else el.form.submit();
  });

  var form = document.querySelector("form.filters");
  if (!form) return;

  var REDUCE = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // --- 1. debounce price inputs ------------------------------------------------------
  // htmx fires on every "change"; numeric typing should settle before we hit the server.
  var DEBOUNCE_MS = REDUCE ? 0 : 350;
  form.querySelectorAll('.price-range input[type="number"]').forEach(function (inp) {
    var t = null;
    inp.addEventListener("input", function () {
      if (t) clearTimeout(t);
      t = setTimeout(function () {
        // requestSubmit triggers htmx's submit handler (and the no-JS path identically).
        if (form.requestSubmit) form.requestSubmit();
        else form.submit();
      }, DEBOUNCE_MS);
    });
    // a committed change (blur / Enter) should not wait on the debounce.
    inp.addEventListener("change", function () {
      if (t) { clearTimeout(t); t = null; }
    });
  });

  // --- 2. keep .on label state in sync with the checkbox -----------------------------
  // The toggles (.toggle) and room pills (.pill) carry .on for the active style. CSS :has()
  // covers modern browsers; this mirror keeps it correct everywhere and after htmx swaps.
  function syncLabel(input) {
    var label = input.closest("label.toggle, label.pill, label.opt");
    if (label) label.classList.toggle("on", input.checked);
  }
  form.addEventListener("change", function (e) {
    var el = e.target;
    if (el && el.matches && el.matches('input[type="checkbox"]')) syncLabel(el);
  });

  // --- 3. clear-all ------------------------------------------------------------------
  // Injected (not in the no-JS template) so it only appears when it can actually work.
  function anyActive() {
    var cbs = form.querySelectorAll('input[type="checkbox"]:checked').length;
    var nums = Array.prototype.some.call(
      form.querySelectorAll('.price-range input[type="number"]'),
      function (i) { return i.value !== ""; }
    );
    return cbs > 0 || nums;
  }

  var clearBtn = document.createElement("button");
  clearBtn.type = "button";
  clearBtn.className = "btn ghost clear-all";
  clearBtn.textContent = "Сбросить фильтры";
  clearBtn.hidden = !anyActive();
  clearBtn.addEventListener("click", function () {
    form.querySelectorAll('input[type="checkbox"]').forEach(function (cb) {
      cb.checked = false;
      syncLabel(cb);
    });
    form.querySelectorAll('.price-range input[type="number"]').forEach(function (i) {
      i.value = "";
    });
    var sort = form.querySelector('select[name="sort"]');
    if (sort) sort.value = "new";
    if (form.requestSubmit) form.requestSubmit();
    else form.submit();
  });
  form.appendChild(clearBtn);

  form.addEventListener("change", function () {
    clearBtn.hidden = !anyActive();
  });
})();
