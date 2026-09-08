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

  // Price → bucket index (1..4) for a sequential ochre→oxblood pin ramp. Thresholds chosen for
  // Belgrade long-let rent: <500 / 500–799 / 800–1199 / ≥1200. Colours live in map.html (.bucket-N),
  // and the legend there documents the same bands. Kept ВНЖ-orthogonal: the .is-vnj tint still wins.
  function priceBucket(eur) {
    var v = Number(eur) || 0;
    if (v < 500) return 1;
    if (v < 800) return 2;
    if (v < 1200) return 3;
    return 4;
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function popupHtml(p) {
    // mini-card: price (mono) + district + link to the listing.
    var vnj = p.for_vnj ? '<span class="mp-vnj">ВНЖ</span>' : "";
    var status = p.availability === "unknown" ? "Актуальность уточняется" :
      (p.availability === "dead" ? "снято" : "");
    return (
      '<div class="map-pop">' +
      '<div class="map-pop-price mono">' + escapeHtml(priceLabel(p.price_eur)) + "</div>" +
      '<div class="map-pop-meta"><span>' + escapeHtml(p.rooms || "") + "</span>" +
      "<span>" + escapeHtml(p.district || "") + "</span>" + vnj + "</div>" +
      (status ? '<div class="map-pop-meta">' + escapeHtml(status) + "</div>" : "") +
      '<a class="map-pop-link" href="' + escapeHtml(p.url) + '">Открыть квартиру →</a>' +
      "</div>"
    );
  }

  function priceMarker(p) {
    // A token-styled divIcon showing the price, not the default teardrop pin. The bucket-N class
    // tints it along the price ramp; data-bucket lets tests assert the band without parsing colour.
    var bucket = priceBucket(p.price_eur);
    var icon = L.divIcon({
      className: "price-pin bucket-" + bucket + (p.for_vnj ? " is-vnj" : ""),
      html:
        '<span class="mono" data-bucket="' + bucket + '">' +
        escapeHtml(priceLabel(p.price_eur)) +
        "</span>",
      iconSize: null, // sized by CSS
    });
    var m = L.marker([p.lat, p.lon], { icon: icon, title: priceLabel(p.price_eur) });
    m.bindPopup(popupHtml(p), { closeButton: true, autoPan: !REDUCE });
    return m;
  }

  function init(points) {
    // Leaflet is present (guarded above), so ALWAYS render a real map — even with zero points —
    // so the page never reads as broken/loading. Clear the no-JS placeholder first.
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

    var plotted = (points || []).filter(function (p) {
      return typeof p.lat === "number" && typeof p.lon === "number";
    });

    // Empty catalog (or no geocoded points yet): keep the Belgrade map on screen and overlay a
    // non-blocking note that points at the list — far better than a dead grey placeholder.
    if (!plotted.length) {
      var note = document.createElement("div");
      note.className = "map-note";
      note.innerHTML =
        'Пока нет квартир на карте. <a href="/catalog">Открыть каталог списком →</a>';
      mount.appendChild(note);
      map.invalidateSize();
      return;
    }

    // Cluster layer (plugin); fall back to a plain layer group if it's unavailable.
    function makeLayer() {
      if (typeof L.markerClusterGroup === "function") {
        return L.markerClusterGroup({
          animate: !REDUCE,
          showCoverageOnHover: false,
          spiderfyOnMaxZoom: true,
          chunkedLoading: true,
        });
      }
      return L.layerGroup();
    }

    var layer = makeLayer();

    // (Re)populate the (re-created) layer from the points matching the current filter, then refit.
    // We rebuild the cluster layer wholesale on toggle — simplest correct path that re-clusters and
    // never leaves orphan markers behind in markerClusterGroup.
    function render(vnjOnly) {
      var shown = plotted.filter(function (p) {
        return !vnjOnly || p.for_vnj;
      });

      map.removeLayer(layer);
      layer = makeLayer();
      var bounds = [];
      shown.forEach(function (p) {
        layer.addLayer(priceMarker(p));
        bounds.push([p.lat, p.lon]);
      });
      map.addLayer(layer);
      if (bounds.length) {
        map.fitBounds(bounds, { padding: [40, 40], maxZoom: 15, animate: !REDUCE });
      }
      map.invalidateSize();
    }

    // ВНЖ filter control: a real <input type=checkbox> in the page (map.html), keyboard-operable and
    // focus-visible via the shared .toggle styles. On → keep only for_vnj points and re-cluster.
    var vnjToggle = document.getElementById("map-vnj-toggle");
    if (vnjToggle) {
      var label = vnjToggle.closest(".toggle");
      var sync = function () {
        if (label) label.classList.toggle("on", vnjToggle.checked);
        render(vnjToggle.checked);
      };
      vnjToggle.addEventListener("change", sync);
      sync(); // honours any pre-checked state (default: unchecked → all points)
    } else {
      render(false);
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
      // Couldn't load points (network/parse): still render the base Belgrade map (L is loaded)
      // so it isn't a dead box; the empty note covers the "no points" wording.
      init([]);
    });
})();
