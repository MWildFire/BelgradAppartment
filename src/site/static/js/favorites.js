// Favorites — localStorage only, NO accounts, NO backend (DATA_CONTRACT §7: только клиент).
//
// A [data-fav][data-fav-id] control on each card/apt toggles membership in a set persisted to
// localStorage; a [data-fav-count] badge in the nav reflects the count. State survives reloads
// and re-applies after htmx swaps (the catalog grid is replaced wholesale on filter changes).
//
// The control is a real <button> rendered by the template (keyboard-operable); we only manage
// its pressed state + glyph fill via aria-pressed / .is-fav, not its markup.

(function () {
  "use strict";
  var KEY = "rb:favs"; // versioned-ish namespace; bump if shape ever changes

  function load() {
    try {
      var raw = localStorage.getItem(KEY);
      var arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr.map(String) : [];
    } catch (e) {
      return []; // private mode / quota / corrupt → degrade to empty, never throw
    }
  }

  function save(ids) {
    try {
      localStorage.setItem(KEY, JSON.stringify(ids));
    } catch (e) {
      /* storage unavailable — favorites simply won't persist this session */
    }
  }

  function has(ids, id) { return ids.indexOf(String(id)) !== -1; }

  function syncCount(ids) {
    var n = ids.length;
    document.querySelectorAll("[data-fav-count]").forEach(function (badge) {
      badge.textContent = String(n);
      badge.hidden = n === 0; // empty badge is visual noise; hide at zero
    });
  }

  function syncControl(btn, ids) {
    var on = has(ids, btn.getAttribute("data-fav-id"));
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    btn.classList.toggle("is-fav", on);
    var label = on ? "Убрать из избранного" : "В избранное";
    btn.setAttribute("aria-label", label);
    btn.setAttribute("title", label);
  }

  // apply current state to every control + the counter (idempotent; safe after htmx swaps)
  function refresh() {
    var ids = load();
    document.querySelectorAll("[data-fav][data-fav-id]").forEach(function (btn) {
      syncControl(btn, ids);
    });
    syncCount(ids);
  }

  function toggle(btn) {
    var id = String(btn.getAttribute("data-fav-id"));
    if (!id) return;
    var ids = load();
    var i = ids.indexOf(id);
    if (i === -1) ids.push(id);
    else ids.splice(i, 1);
    save(ids);
    syncControl(btn, ids);
    syncCount(ids);
  }

  // event delegation → works for cards present now AND those swapped in later by htmx.
  document.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("[data-fav][data-fav-id]") : null;
    if (!btn) return;
    e.preventDefault();  // the control sits inside the card <a>; don't navigate
    e.stopPropagation();
    toggle(btn);
  });

  // htmx replaces the results grid → re-apply pressed state to the new controls.
  document.body.addEventListener("htmx:afterSwap", refresh);

  // cross-tab consistency: another tab toggled a favorite → reflect it here.
  window.addEventListener("storage", function (e) {
    if (e.key === KEY) refresh();
  });

  refresh();
})();
