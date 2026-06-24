// /apt/{id} fuzzy mini-map island (Stage S, Wave 2 — S2-2).
//
// Reads #apt-map[data-lat][data-lon] and draws a FUZZY ~150 m circle (NO point marker) centred
// on the listing coords — DATA_CONTRACT-S §M / DECISIONS-S D1. The precise building is never
// revealed: there is no L.marker, only an L.circle, and the map auto-fits the circle's bounds.
//
// Leaflet (core only — no markercluster) is loaded from the same jsdelivr CDN (SRI-pinned) as
// map.html, before this script. If it failed to load, or the coords are missing/invalid, we
// no-op: the mount stays empty and the Yandex/Google deep-link BUTTONS below still work (they're
// plain anchors, JS-independent — graceful degradation).
//
// Mobile gotcha (§M, REQUIRED): scrollWheelZoom is OFF so the map never hijacks page scroll, and
// one-finger dragging is disabled on touch (dragging: !L.Browser.mobile) so a vertical swipe
// scrolls the page instead of panning the map. A small overlay gate invites a tap to interact;
// tapping enables dragging. prefers-reduced-motion disables zoom/fade animations (matches map.js).

(function () {
  "use strict";

  var mount = document.getElementById("apt-map");
  if (!mount) return;

  // Leaflet must have loaded from the CDN; if not, keep the empty mount (buttons still work).
  if (typeof window.L === "undefined") return;

  var L = window.L;

  var lat = parseFloat(mount.getAttribute("data-lat"));
  var lon = parseFloat(mount.getAttribute("data-lon"));
  if (!isFinite(lat) || !isFinite(lon)) return; // bad/missing coords → no-op

  var REDUCE = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var TOUCH = !!L.Browser.mobile;

  var map = L.map(mount, {
    zoomAnimation: !REDUCE,
    fadeAnimation: !REDUCE,
    scrollWheelZoom: false, // never hijack page scroll (mobile gotcha)
    dragging: !TOUCH, // on touch: one-finger swipe scrolls the page, not the map
    tap: !TOUCH, // match map.js: disable Leaflet's tap handler on touch devices
    keyboard: false,
  });

  // Establish the map's pixel origin/panes BEFORE projecting any vector geometry — otherwise
  // fitBounds(circle.getBounds()) can throw "layerPointToLatLng of undefined" on a fresh map.
  map.setView([lat, lon], 15);

  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "© OpenStreetMap",
  }).addTo(map);

  // Fuzzy area — a ~150 m radius circle, NO point marker (privacy: building not revealed).
  // Colours come from tokens via CSS custom props read off the mount (apt.css sets them);
  // fall back to literal oxblood if a prop is somehow unset.
  var css = window.getComputedStyle(mount);
  var stroke = (css.getPropertyValue("--apt-map-stroke") || "").trim() || "#A8362A";
  var fill = (css.getPropertyValue("--apt-map-fill") || "").trim() || "#A8362A";

  var circle = L.circle([lat, lon], {
    radius: 150,
    color: stroke,
    weight: 1.5,
    opacity: 0.85,
    fillColor: fill,
    fillOpacity: 0.14,
    interactive: false,
  }).addTo(map);

  map.fitBounds(circle.getBounds(), { padding: [20, 20] });
  map.invalidateSize();

  // Touch interaction gate: a one-tap overlay that enables dragging on demand, so the user can
  // still pan the fuzzy area without the map stealing the first scroll gesture.
  if (TOUCH) {
    var gate = document.createElement("button");
    gate.type = "button";
    gate.className = "apt-map-gate";
    gate.textContent = "нажмите, чтобы взаимодействовать";
    gate.addEventListener("click", function () {
      map.dragging.enable();
      gate.parentNode && gate.parentNode.removeChild(gate);
    });
    mount.appendChild(gate);
  }
})();
