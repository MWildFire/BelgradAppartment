"""E2E (T4-4): публичные страницы без inline-JS — готовность снять 'unsafe-inline' из script-src.

CSP блокирует inline-обработчики (on*=) так же, как <script>-блоки, поэтому гейт здесь — парсинг
СЕРВЕРНОГО HTML каждой публичной страницы: НОЛЬ on*-атрибутов. Плюс два регрессионных теста на
поведение, которое раньше жило inline и переехало во внешние файлы:

  * сортировка каталога: <select id="sort"> → htmx-swap #results (filters.js, delegated change);
  * subscribe-radio: переключение канала двигает видимость и required-статус поля e-mail
    (subscribe-form.js, delegated change — работает и для клавиатурных стрелок).

Telegram-radio рендерится только при заданном SITE_TELEGRAM_BOT_USERNAME, который общий сервер
сессии не выставляет — поэтому модуль поднимает отдельный uvicorn (тот же seed плюс переменная).
"""

from __future__ import annotations

import os
import subprocess
import sys
import urllib.request
from collections.abc import Iterator
from html.parser import HTMLParser

import pytest

from tests.e2e.conftest import (
    REPO_ROOT,
    _free_port,
    _seed,
    _wait_healthz,
    requires_e2e,
    run_coro,
)

# Every public HTML page (routes.py GET + response_class=HTMLResponse, without auth/params-магии).
PUBLIC_PAGES = (
    "/",
    "/catalog",
    "/catalog?show_rented=1",  # рендерит и «снято»-вариант карточки
    "/apt/1",
    "/map",
    "/calculator",
    "/vnj",
    "/how",
    "/offline",
)


# --------------------------------------------------------------------- inline-handler detector
# HTMLParser вместо regex: имена атрибутов достаёт сам парсер, так что «on…» внутри текста или
# значений атрибутов не даёт ложных срабатываний.


class _InlineHandlerCollector(HTMLParser):
    """Collects (tag, attr) for every attribute whose name starts with `on`."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, _value in attrs:
            if name.startswith("on"):
                self.found.append((tag, name))

    # self-closing tags (<img …/>) go through handle_startendtag
    handle_startendtag = handle_starttag


def _inline_handlers(html: str) -> list[tuple[str, str]]:
    parser = _InlineHandlerCollector()
    parser.feed(html)
    return parser.found


def _get_html(base: str, path: str) -> str:
    with urllib.request.urlopen(f"{base}{path}", timeout=5.0) as resp:
        return resp.read().decode("utf-8")


# --------------------------------------------------------------------- tg-enabled server
# Копия conftest.site_server + SITE_TELEGRAM_BOT_USERNAME: без него Telegram-radio не рендерится
# и переключение канала нечего тестировать. module-scope — один лишний uvicorn на модуль.


@pytest.fixture(scope="module")
def tg_site_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    if not os.getenv("RUN_E2E"):
        pytest.skip("e2e disabled; set RUN_E2E=1")

    db_path = tmp_path_factory.mktemp("e2e_db_tg") / "site.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    # run_coro, не голый asyncio.run: в полном прогоне playwright уже оставил running-loop
    # в главном треде (см. conftest.run_coro) — module-scoped фикстура строится ПОСЛЕ браузера.
    run_coro(_seed(db_url))

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["DATABASE_URL"] = db_url
    env["SITE_BIND_HOST"] = "127.0.0.1"
    env["SITE_BIND_PORT"] = str(port)
    env["SITE_CHANNEL_URL"] = "https://t.me/RentBelgrad"
    env["SITE_PUBLIC_BASE_URL"] = base_url
    env["SITE_TELEGRAM_BOT_USERNAME"] = "RentBelgradBot"  # ← единственное отличие от conftest
    env["TG_BOT_TOKEN"] = ""
    env["TG_BOT_RECIPIENTS"] = ""
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.site.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_healthz(base_url, proc)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


# --------------------------------------------------------------------------------- the CSP gate


@requires_e2e
def test_zero_inline_handlers_on_public_pages(site_server: str) -> None:
    """Гейт: ни одного on*-атрибута в серверном HTML всех публичных страниц."""
    leaks: dict[str, list[tuple[str, str]]] = {}
    for path in PUBLIC_PAGES:
        found = _inline_handlers(_get_html(site_server, path))
        if found:
            leaks[path] = found
    assert not leaks, f"inline on*-handlers block dropping 'unsafe-inline': {leaks}"


@requires_e2e
def test_zero_inline_handlers_with_telegram_channel(tg_site_server: str) -> None:
    """Тот же гейт для разметки, где отрендерен Telegram-radio (нужен bot username)."""
    found = _inline_handlers(_get_html(tg_site_server, "/catalog"))
    assert not found, f"inline on*-handlers in telegram-enabled subscribe form: {found}"


# ------------------------------------------------------------------------- behavior regressions


@requires_e2e
def test_sort_select_swaps_results_without_inline_handler(page, site_server: str) -> None:
    """Сортировка живёт в filters.js (delegated change) вместо inline-атрибута onchange."""
    page.goto(f"{site_server}/catalog", wait_until="networkidle")

    sort = page.locator("#sort")
    assert sort.get_attribute("onchange") is None, "sort <select> still carries inline onchange"

    # price_asc → дешёвая (€600, id=1) первой; hx-push-url кладёт sort в URL.
    page.select_option("#sort", "price_asc")
    page.wait_for_function(
        "() => window.location.search.includes('sort=price_asc')", timeout=5000
    )
    first_price = page.locator("#results .card-l .price .mono").first
    assert "600" in first_price.inner_text()

    # Делегирование переживает htmx-swap: #results уже заменён, НОВЫЙ select тоже работает.
    page.select_option("#sort", "price_desc")
    page.wait_for_function(
        "() => window.location.search.includes('sort=price_desc')", timeout=5000
    )
    first_price = page.locator("#results .card-l .price .mono").first
    assert "800" in first_price.inner_text()


@requires_e2e
def test_subscribe_radio_toggles_email_field(page, tg_site_server: str) -> None:
    """Переключение канала (e-mail ↔ Telegram) двигает hidden/required без inline-обработчика."""
    page.goto(f"{tg_site_server}/catalog", wait_until="networkidle")

    radios = page.locator("#subscribe input[name='channel']")
    assert radios.count() == 2, "telegram radio not rendered — fixture env not applied?"
    for i in range(2):
        assert radios.nth(i).get_attribute("onchange") is None, (
            "channel radio still carries inline onchange"
        )

    email_field = page.locator("#sub-email-field")
    # Дефолт (no-JS parity): e-mail канал выбран, поле видимо и required.
    assert email_field.is_visible()
    assert page.evaluate("document.getElementById('sub-email').required") is True

    # Клик по Telegram → поле прячется, required снимается (сервер валидирует в любом случае).
    page.locator("#subscribe input[name='channel'][value='telegram']").check()
    page.wait_for_selector("#sub-email-field", state="hidden", timeout=3000)
    assert page.evaluate("document.getElementById('sub-email').required") is False

    # Клавиатура: стрелка на radio-группе даёт `change` (не `click`) — поле должно вернуться.
    # Именно ради этого обработчик слушает change, см. subscribe-form.js.
    page.locator("#subscribe input[name='channel'][value='telegram']").press("ArrowUp")
    page.wait_for_selector("#sub-email-field", state="visible", timeout=3000)
    assert page.evaluate("document.getElementById('sub-email').required") is True
    assert page.evaluate(
        "document.querySelector(\"#subscribe input[name='channel'][value='email']\").checked"
    ) is True
