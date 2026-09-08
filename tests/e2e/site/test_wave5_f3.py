"""E2E (Wave 5, линия F3): a11y/визуальные фиксы публичного фронта — в живом браузере.

- W5-21: пилюли «Комнат» операбельны с клавиатуры (Tab доходит, Space переключает);
- W5-22: htmx-своп фильтров ЗАМЕЩАЕТ #results (outerHTML) — никаких вложенных дубликатов id;
  счёт объявляет СТАБИЛЬНЫЙ live-регион #results-status вне свопа (обновляется hx-swap-oob,
  узел персистирует между свопами);
- W5-25: «Сбросить фильтры» видна при активном ТОЛЬКО районе и сбрасывает район (select + чипы);
- W5-28: форма «Сохрани поиск» заверстана по токенам (grid, fieldset без UA-groove,
  radio в акценте палитры), тумблер ВНЖ зеркалит .on;
- W5-11/26: бейдж «+N фото» в галерее /apt видим (z-index поверх absolute-якоря + скрим)
  и прозрачен для кликов (весь тайл остаётся ссылкой).

Сервер/сид — tests/e2e/conftest.py (реальный uvicorn + chromium). Гейт RUN_E2E=1.
"""

from __future__ import annotations

from pathlib import Path

from tests.e2e.conftest import REPO_ROOT, requires_e2e

SITE_CSS = REPO_ROOT / "src" / "site" / "static" / "css" / "site.css"
TOKENS_CSS = REPO_ROOT / "src" / "site" / "static" / "css" / "tokens.css"


# ------------------------------------------------------------------ W5-21: keyboard pills
@requires_e2e
def test_rooms_pills_keyboard_tab_and_space(page, site_server: str) -> None:
    """Tab с price_max приземляется на чекбокс «Комнат» (раньше display:none выкидывал их из
    tab-порядка), Space переключает, filters.js зеркалит .on на пилюле."""
    page.goto(f"{site_server}/catalog", wait_until="networkidle")
    page.focus(".filters input[name='price_max']")
    page.keyboard.press("Tab")
    active = page.evaluate(
        "() => ({name: document.activeElement.name, type: document.activeElement.type})"
    )
    assert active == {"name": "rooms", "type": "checkbox"}, (
        f"Tab must land on a rooms checkbox, got {active}"
    )
    page.keyboard.press("Space")
    assert page.evaluate("document.activeElement.checked") is True
    # визуальное зеркало .on на пилюле (filters.js syncLabel)
    page.wait_for_function("document.querySelectorAll('.filters .pill.on').length === 1")


# ---------------------------------------------------- W5-22: outerHTML swap + live region
@requires_e2e
def test_filter_swap_single_results_node_and_stable_counter(page, site_server: str) -> None:
    """Два свопа подряд → в DOM ровно один #results (не вложенные дубликаты), а счётчик
    объявляет персистентный #results-status: его текст меняется, сам узел — никогда."""
    page.goto(f"{site_server}/catalog", wait_until="networkidle")
    assert page.evaluate("document.querySelectorAll('#results').length") == 1
    assert page.locator("#results-status").count() == 1
    # помечаем живой регион, чтобы доказать: свопы его НЕ пересоздают
    page.evaluate("document.getElementById('results-status').__persist = 'orig'")

    # своп 1: ВНЖ → 1 квартира
    page.locator(".filters input[name='vnj'][value='1']").check()
    page.wait_for_function("document.querySelectorAll('#results .card-l').length === 1")
    assert page.evaluate("document.querySelectorAll('#results').length") == 1
    page.wait_for_function(
        "document.getElementById('results-status').textContent.trim() === '1 квартира'"
    )

    # своп 2: снимаем ВНЖ → все 3 public квартиры, включая retained DEAD
    page.locator(".filters input[name='vnj'][value='1']").uncheck()
    page.wait_for_function("document.querySelectorAll('#results .card-l').length === 3")
    assert page.evaluate("document.querySelectorAll('#results').length") == 1
    page.wait_for_function(
        "document.getElementById('results-status').textContent.trim() === '3 квартир'"
    )
    # тот же узел пережил оба свопа → SR-объявление стабильно (не «вставка нового region»)
    assert page.evaluate("document.getElementById('results-status').__persist") == "orig"


# ------------------------------------------------------------- W5-25: clear-all + район
@requires_e2e
def test_clear_all_appears_for_district_only_and_resets_it(page, site_server: str) -> None:
    """Deep-link с одним районом: кнопка сброса ВИДНА (район — активный фильтр) и клик
    сбрасывает и select[name=district], и чипы комбобокса, и re-swap показывает все карточки.
    Фильтр матчится по НОРМАЛИЗОВАННОМУ Cyrillic-имени (human_district: Vracar → Врачар)."""
    page.goto(f"{site_server}/catalog?district=Врачар", wait_until="networkidle")
    assert page.evaluate("document.querySelectorAll('#results .card-l').length") == 2
    assert page.locator(".filters .combobox-chip").count() == 1  # чип применённого района

    btn = page.locator(".filters .clear-all")
    assert btn.count() == 1
    assert btn.is_visible(), "clear-all must be visible when only район is active (W5-25)"

    btn.click()
    page.wait_for_function("document.querySelectorAll('#results .card-l').length === 3")
    assert page.evaluate(
        "document.querySelectorAll(\".filters select[name='district'] option:checked\").length"
    ) == 0
    assert page.locator(".filters .combobox-chip").count() == 0
    assert btn.is_hidden(), "clear-all hides again once nothing is active"


# ----------------------------------------------------------- W5-28: subscribe form верстка
@requires_e2e
def test_subscribe_form_styled_and_vnj_toggle_syncs(page, site_server: str) -> None:
    """Форма «Сохрани поиск»: критерии в grid, fieldset канала без UA-groove, radio в акценте
    палитры (не UA-синий), тумблер ВНЖ получает .on при чеке (subscribe-form.js)."""
    page.goto(f"{site_server}/catalog", wait_until="networkidle")
    assert page.eval_on_selector(
        ".subscribe-grid", "el => getComputedStyle(el).display"
    ) == "grid"
    assert page.eval_on_selector(
        "fieldset.subscribe-channel", "el => getComputedStyle(el).borderTopStyle"
    ) != "groove"
    assert page.eval_on_selector(
        ".channel-opt input", "el => getComputedStyle(el).accentColor"
    ) != "auto"
    # тумблер ВНЖ вне form.filters тоже зеркалит .on
    toggle = page.locator("form.subscribe label.toggle").first
    toggle.locator("input").check()
    assert "on" in (toggle.get_attribute("class") or "")
    toggle.locator("input").uncheck()
    assert "on" not in (toggle.get_attribute("class") or "")


# ------------------------------------------------- W5-11/26: «+N фото» бейдж в галерее
@requires_e2e
def test_gallery_more_badge_painted_above_tile_and_click_through(page, site_server: str) -> None:
    """Бейдж «+N фото» рисуется ПОВЕРХ absolute-якоря/фото (positioned + z-index≥1 + скрим на
    якоре), а клики проходят сквозь него в тайл-ссылку (pointer-events:none). Сид даёт 1 фото
    на квартиру, поэтому рендерим РЕАЛЬНЫЙ apt.html с 7 фото через Jinja и боевые CSS."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    templates_dir = Path(REPO_ROOT) / "src" / "site" / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html"]),
    )
    env.globals["site_base_url"] = site_server

    class _Item:  # минимальный SiteListingView (§1) с 7 фото → тайл .more с «+2 фото»
        id = 1
        price_eur = 650
        area_m2 = 48.0
        rooms = "1+1"
        floor = "3/5"
        district = "Врачар"
        address = None
        lat = lon = None
        for_vnj = True
        for_white_card = False
        is_rented = False
        photos = [f"/photos/{i}.jpg" for i in range(7)]
        note = None
        channel_url = "https://t.me/RentBelgrad/1"
        posted_at = "2026-06-18T10:00:00"

    html = env.get_template("apt.html").render(item=_Item(), similar=[], csrf_token="x")
    page.set_content(html)
    # боевые стили: <link>-и в set_content не резолвятся — инжектим содержимое файлов
    page.add_style_tag(content=TOKENS_CSS.read_text(encoding="utf-8"))
    page.add_style_tag(content=SITE_CSS.read_text(encoding="utf-8"))

    state = page.evaluate(
        """() => {
          const span = document.querySelector('.gallery .g.more > a ~ span');
          const a = document.querySelector('.gallery .g.more > a');
          span.scrollIntoView({block: 'center'});
          const cs = getComputedStyle(span);
          const after = getComputedStyle(a, '::after');
          const r = span.getBoundingClientRect();
          const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
          return {
            text: span.textContent.trim(),
            position: cs.position,
            zIndex: cs.zIndex,
            pointerEvents: cs.pointerEvents,
            scrim: after.content !== 'none' && after.backgroundColor !== 'rgba(0, 0, 0, 0)',
            hitHref: hit && hit.closest('a') ? hit.closest('a').getAttribute('href') : null,
          };
        }"""
    )
    assert state["text"] == "+2 фото"
    # paint-order: positioned span с z-index ≥ 1 рисуется поверх positioned-auto якоря (CSS App. E)
    assert state["position"] != "static", "badge must be positioned to join the paint order fix"
    assert state["zIndex"].isdigit() and int(state["zIndex"]) >= 1, (
        f"badge z-index must lift it above the tile anchor, got {state['zIndex']!r}"
    )
    # скрим на якоре — иначе paper-текст нечитаем на светлом фото
    assert state["scrim"], "tile anchor must carry a darkening ::after scrim under the badge"
    # клики проходят сквозь бейдж в тайл-ссылку (5-е фото) — бейдж не «съедает» клик
    assert state["pointerEvents"] == "none"
    assert state["hitHref"] == "/photos/4.jpg", (
        f"click at badge center must land on the tile link, got {state['hitHref']!r}"
    )
