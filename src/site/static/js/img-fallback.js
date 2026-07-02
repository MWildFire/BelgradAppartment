// Broken-photo fallback (C1) — external replacement for the old inline
// onerror="this.onerror=null;this.src='/static/img/placeholder.svg'" (T4-4: CSP blocks
// inline on*-handlers exactly like <script> blocks, so script-src can drop 'unsafe-inline').
//
// Почему делегирование на document в CAPTURE-фазе: `error` НЕ всплывает (bubbles:false),
// но capture-фаза на document видит его по пути вниз к target — один listener покрывает
// карточки, галерею и всё, что htmx подменит в #results (никаких re-bind после swap).
//
// Почему стартовый sweep: скрипт подключён с defer и может опоздать к ранним ошибкам —
// у уже-битой картинки error отгремел до нас. Такие узнаём по img.complete &&
// naturalWidth === 0 и чиним той же подменой.
//
// Scope: listing photos only ('.ph img' on cards, '.gallery img' on the detail page) —
// OG images, icons and any other <img> are none of our business.
//
// No-JS parity: без JS битое фото остаётся битым (ровно как было бы с CSP-блокированным
// inline-onerror) — осознанно, alt-текст на месте.
(function () {
  "use strict";

  var PLACEHOLDER = "/static/img/placeholder.svg";
  var SCOPE = ".ph img, .gallery img";

  function swap(img) {
    // Гард от цикла: подменяем РОВНО один раз (dataset-флаг). Если сам placeholder
    // вдруг 404 — его error приходит на уже помеченный img и игнорируется, цикла нет.
    if (!img.matches || !img.matches(SCOPE) || img.dataset.imgFallback) return;
    img.dataset.imgFallback = "1";
    // srcset outranks src in the browser's source selection — drop it (and sizes),
    // otherwise the placeholder assigned to src would never actually be displayed.
    img.removeAttribute("srcset");
    img.removeAttribute("sizes");
    img.src = PLACEHOLDER;
  }

  document.addEventListener(
    "error",
    function (e) {
      var el = e.target;
      if (el && el.tagName === "IMG") swap(el);
    },
    true // capture — см. шапку: error не всплывает, bubble-фаза его не увидит
  );

  // sweep photos that already failed before this (defer) script attached its listener
  function sweep() {
    document.querySelectorAll(SCOPE).forEach(function (img) {
      if (img.complete && img.naturalWidth === 0) swap(img);
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", sweep);
  } else {
    sweep();
  }
})();
