// T2-3 attribution (DATA_CONTRACT-T §A.4) — first-touch utm_* + внешний referrer.
//
// Зачем: Дарье важно знать, ОТКУДА пришла заявка. Метки живут в URL первой страницы визита,
// а форму посетитель отправляет позже и с другой страницы — поэтому снимок «первого касания»
// (utm_source/utm_medium/utm_campaign + document.referrer) кладём в sessionStorage и
// переносим в hidden-поля форм lead/subscribe при сабмите. Строго БЕЗ cookies (§A.4);
// sessionStorage умирает вместе с вкладкой — ничего не «догоняет» посетителя между визитами.
//
// First-touch: снимок пишется ОДИН раз — на первой странице, где есть хоть один сигнал
// (utm в query или внешний referrer); дальнейшая навигация его не перетирает. Внутренний
// referrer (свой хост) — навигация по сайту, не источник: отбрасывается ещё здесь.
//
// Сервер всё равно валидирует по §A.1 (charset/длины/свой хост, мусор → NULL) — клиентские
// обрезки ниже лишь держат payload в рамках. No-JS фоллбэк живёт в роуте (query Referer'а).
//
// Скрипт подключают ШАБЛОНЫ ФОРМ (_lead_form/_subscribe_form), не base.html — на странице
// с обеими формами он исполнится дважды, поэтому и снимок, и обвязка форм идемпотентны.

(function () {
  "use strict";

  var KEY = "rb_attrib"; // sessionStorage: {"s","m","c","r"} — снимок первого касания
  var UTM_MAX = 100; // §A.1: utm_* ≤100
  var REF_MAX = 200; // §A.1: referrer ≤200

  function firstTouch() {
    try {
      if (sessionStorage.getItem(KEY)) return; // снимок уже снят — first-touch не перетираем
      var qs = new URLSearchParams(location.search);
      var ref = document.referrer || "";
      try {
        // только ВНЕШНИЙ referrer: переход со своей страницы — навигация, не источник
        if (ref && new URL(ref).host === location.host) ref = "";
      } catch (e) {
        ref = ""; // непарсибельный referrer — сигнала нет
      }
      var snap = {
        s: (qs.get("utm_source") || "").slice(0, UTM_MAX),
        m: (qs.get("utm_medium") || "").slice(0, UTM_MAX),
        c: (qs.get("utm_campaign") || "").slice(0, UTM_MAX),
        r: ref.slice(0, REF_MAX),
      };
      if (!snap.s && !snap.m && !snap.c && !snap.r) return; // прямой заход — снимать нечего
      sessionStorage.setItem(KEY, JSON.stringify(snap));
    } catch (e) {
      // sessionStorage может быть недоступен (приватный режим) — атрибуция best-effort
    }
  }

  function readSnapshot() {
    try {
      return JSON.parse(sessionStorage.getItem(KEY) || "null");
    } catch (e) {
      return null;
    }
  }

  function fill(form) {
    var snap = readSnapshot();
    if (!snap) return;
    var fields = { utm_source: snap.s, utm_medium: snap.m, utm_campaign: snap.c, referrer: snap.r };
    Object.keys(fields).forEach(function (name) {
      var input = form.querySelector('input[type="hidden"][name="' + name + '"]');
      if (input && fields[name]) input.value = fields[name];
    });
  }

  function wire(form) {
    if (form.hasAttribute("data-utm-wired")) return; // двойное включение скрипта — не дублируем
    form.setAttribute("data-utm-wired", "1");
    fill(form); // снимок статичен в рамках сессии: заполняем сразу (htmx сериализует поля)…
    form.addEventListener("submit", function () {
      fill(form); // …и переутверждаем при сабмите — на случай позднего firstTouch
    });
  }

  firstTouch();
  // обвязываем любую форму с hidden-полем атрибуции — сегодня это lead и subscribe
  document.querySelectorAll("form").forEach(function (form) {
    if (form.querySelector('input[type="hidden"][name="utm_source"]')) wire(form);
  });
})();
