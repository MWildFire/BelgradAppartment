/* RentBelgrad PWA registration (S5-1).
 *
 * Registers the root-scoped service worker (/sw.js) after the page loads, so SW install never
 * competes with first paint. Guarded by feature-detection — no-op on browsers without SW
 * support (and on insecure origins, where navigator.serviceWorker is undefined). The SW itself
 * is served at root scope by a FastAPI route so it controls the whole origin.
 */
(function () {
  "use strict";
  if (!("serviceWorker" in navigator)) return;
  window.addEventListener("load", function () {
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function (err) {
      // Registration failure is non-fatal — the site works fine without the SW.
      if (window.console && console.warn) console.warn("SW registration failed:", err);
    });
  });
})();
