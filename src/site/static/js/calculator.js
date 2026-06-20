// Rent calculator — pure client math, NO network (DATA_CONTRACT §2: чистый клиентский расчёт).
//
// Sums [data-calc-field] inputs into [data-calc-total] (всё включено, €/мес), and derives the
// one-off старт: депозит (= one month's аренда) + первый месяц (аренда only — режие/инфостан
// are paid in arrears, not upfront). Server already renders sane static defaults so the page is
// useful with JS off; this only makes it live.
//
// Output is МОНО and grouped (e.g. "€1 300") to match the design's price typography.

(function () {
  "use strict";
  var root = document.querySelector("[data-calc]");
  if (!root) return;

  var fields = root.querySelectorAll("[data-calc-field]");
  var totalEl = root.querySelector("[data-calc-total]");
  var depositEl = root.querySelector("[data-calc-deposit]");
  var upfrontEl = root.querySelector("[data-calc-upfront]");

  // non-negative integer euros; blank/garbage → 0 so the total never reads NaN.
  function val(input) {
    var n = parseInt(input.value, 10);
    return isNaN(n) || n < 0 ? 0 : n;
  }

  // thin-space grouping keeps long sums readable in МОНО without locale surprises.
  function eur(n) {
    return "€" + String(n).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  }

  function fieldVal(name) {
    var el = root.querySelector('[data-calc-field="' + name + '"]');
    return el ? val(el) : 0;
  }

  function recompute() {
    var monthly = 0;
    fields.forEach(function (f) { monthly += val(f); });

    var rent = fieldVal("rent");
    var deposit = rent;              // депозит ≈ один месяц аренды (бытовая норма в Београде)
    var upfront = deposit + rent;   // старт: депозит + первый месяц (режие платятся постфактум)

    if (totalEl) totalEl.textContent = eur(monthly) + " / мес";
    if (depositEl) depositEl.textContent = eur(deposit);
    if (upfrontEl) upfrontEl.textContent = eur(upfront);
  }

  fields.forEach(function (f) { f.addEventListener("input", recompute); });
  recompute(); // sync display with whatever defaults the server rendered
})();
