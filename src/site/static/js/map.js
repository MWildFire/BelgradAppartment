// /map island — Leaflet + marker clustering (Wave 4-E2).
//
// Reads #map[data-map-src] (default /api/map.json), drops one PRICE marker per public point
// ({id,lat,lon,price_eur,rooms,district,for_vnj,url}); popup = mini-card (price MONO, district,
// link to point.url = /apt/{id}). Backend already returns only public+coords points.
//
// Leaflet (core + markercluster) is loaded from CDN in map.html (plan-sanctioned). If it failed
// to load, or there are no points, we leave the .map-empty placeholder visible — graceful.
// prefers-reduced-motion → disable cluster/zoom animations.

(function () {
  "use strict";

  var mount = document.getElementById("map");
  if (!mount) return;

  // Leaflet must have loaded from the CDN; if not, keep the no-JS empty state.
  if (typeof window.L === "undefined") {
    return;
  }

  var L = window.L;
  var REDUCE = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var src = mount.getAttribute("data-map-src") || "/api/map.json";

  // Belgrade fallback view (used until points fit the map).
  var BELGRADE = [44.8125, 20.4612];

  function priceLabel(eur) {
    // MONO + grouped thousands, no decimals (design: price always monospace).
    return new Intl.NumberFormat("ru-RU").format(eur) + " €";
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function popupHtml(p) {
    // mini-card: price (mono) + district + link to the listing.
    var vnj = p.for_vnj ? '<span class="mp-vnj">ВНЖ</span>' : "";
    return (
      '<div class="map-pop">' +
      '<div class="map-pop-price mono">' + escapeHtml(priceLabel(p.price_eur)) + "</div>" +
      '<div class="map-pop-meta"><span>' + escapeHtml(p.rooms || "") + "</span>" +
      "<span>" + escapeHtml(p.district || "") + "</span>" + vnj + "</div>" +
      '<a class="map-pop-link" href="' + escapeHtml(p.url) + '">Открыть квартиру →</a>' +
      "</div>"
    );
  }

  function priceMarker(p) {
    // A token-styled divIcon showing the price, not the default teardrop pin.
    var icon = L.divIcon({
      className: "price-pin" + (p.for_vnj ? " is-vnj" : ""),
      html: '<span class="mono">' + escapeHtml(priceLabel(p.price_eur)) + "</span>",
      iconSize: null, // sized by CSS
    });
    var m = L.marker([p.lat, p.lon], { icon: icon, title: priceLabel(p.price_eur) });
    m.bindPopup(popupHtml(p), { closeButton: true, autoPan: !REDUCE });
    return m;
  }

  function init(points) {
    if (!points || !points.length) {
      // leave .map-empty visible — nothing to plot.
      return;
    }
    // clear the placeholder before mounting the canvas.
    mount.innerHTML = "";

    var map = L.map(mount, {
      zoomAnimation: !REDUCE,
      fadeAnimation: !REDUCE,
      markerZoomAnimation: !REDUCE,
      scrollWheelZoom: false, // avoid hijacking page scroll; user clicks to zoom
    }).setView(BELGRADE, 12);

    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: "© OpenStreetMap",
    }).addTo(map);

    // Cluster layer (plugin); fall back to a plain layer group if it's unavailable.
    var layer;
    if (typeof L.markerClusterGroup === "function") {
      layer = L.markerClusterGroup({
        animate: !REDUCE,
        showCoverageOnHover: false,
        spiderfyOnMaxZoom: true,
        chunkedLoading: true,
      });
    } else {
      layer = L.layerGroup();
    }

    var bounds = [];
    points.forEach(function (p) {
      if (typeof p.lat !== "number" || typeof p.lon !== "number") return;
      layer.addLayer(priceMarker(p));
      bounds.push([p.lat, p.lon]);
    });
    map.addLayer(layer);

    if (bounds.length) {
      map.fitBounds(bounds, { padding: [40, 40], maxZoom: 15, animate: !REDUCE });
    }
  }

  fetch(src, { headers: { Accept: "application/json" } })
    .then(function (r) {
      if (!r.ok) throw new Error("map.json " + r.status);
      return r.json();
    })
    .then(function (data) {
      init((data && data.points) || []);
    })
    .catch(function () {
      // network/parse failure → keep the graceful empty state, no alert.
    });
})();
