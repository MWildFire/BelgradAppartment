/* RentBelgrad service worker (S5-1) — installable PWA + offline shell.
 *
 * Served at ROOT scope via GET /sw.js (a FastAPI route, see routes.py) so it controls the
 * whole origin — NOT from /static/js/sw.js, which would scope it to /static only. The route
 * substitutes the live asset version into __CACHE_VERSION__ below, so every deploy (which
 * bumps asset_v) yields a new cache name → old caches are dropped on activate.
 *
 * Strategy:
 *   • cache-first for fingerprinted /static/* (they carry ?v=<hash>, immutable → safe to keep).
 *   • network-first for HTML navigations (featured/leads stay fresh); on network failure →
 *     the precached /offline shell.
 *   • everything dynamic (POST, /lead, /subscribe, /healthz, /admin, /apt/*/og.png, /api/*)
 *     bypasses the SW entirely → straight to network.
 */

const CACHE_VERSION = "__CACHE_VERSION__";
const CACHE_NAME = "rentbelgrad-" + CACHE_VERSION;

// Precached on install: the offline shell + its core token/style assets so the fallback
// renders branded even with zero network. Versioned URLs bust naturally on deploy.
const PRECACHE_URLS = [
  "/offline",
  "/static/css/tokens.css?v=" + CACHE_VERSION,
  "/static/css/site.css?v=" + CACHE_VERSION,
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      // addAll is atomic: a single failed precache URL aborts install (and we keep the old SW),
      // so we never activate a half-populated cache. Core assets are same-origin + must exist.
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) =>
        Promise.all(
          names
            .filter((n) => n.startsWith("rentbelgrad-") && n !== CACHE_NAME)
            .map((n) => caches.delete(n))
        )
      )
      .then(() => self.clients.claim())
  );
});

function isStaticAsset(url) {
  return url.origin === self.location.origin && url.pathname.startsWith("/static/");
}

// Dynamic endpoints we never want the SW to cache or shadow — always hit the network.
function isBypass(url) {
  const p = url.pathname;
  return (
    p === "/healthz" ||
    p === "/lead" ||
    p === "/subscribe" ||
    p === "/sw.js" ||
    p.startsWith("/api/") ||
    p.startsWith("/admin") ||
    /^\/apt\/\d+\/og\.png$/.test(p)
  );
}

self.addEventListener("fetch", (event) => {
  const req = event.request;

  // Only GET is ever cacheable; let everything else (POST forms, etc.) pass through.
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // Cross-origin (e.g. OSM tiles) + explicit bypass list → don't intercept.
  if (url.origin !== self.location.origin || isBypass(url)) return;

  // cache-first: fingerprinted static assets are immutable; serve cached, fall back to net,
  // and populate the cache on first network fetch.
  if (isStaticAsset(url)) {
    event.respondWith(
      caches.match(req).then(
        (cached) =>
          cached ||
          fetch(req).then((res) => {
            if (res && res.ok) {
              const copy = res.clone();
              caches.open(CACHE_NAME).then((c) => c.put(req, copy));
            }
            return res;
          })
      )
    );
    return;
  }

  // network-first for HTML navigations: keep dynamic pages fresh; on failure serve the
  // precached offline shell. (Navigations have mode === "navigate".)
  if (req.mode === "navigate") {
    event.respondWith(
      fetch(req).catch(() =>
        caches.match("/offline", { ignoreSearch: true }).then(
          (res) => res || new Response("Offline", { status: 503, statusText: "Offline" })
        )
      )
    );
    return;
  }
  // Anything else: default network behaviour (no respondWith → browser handles it).
});
