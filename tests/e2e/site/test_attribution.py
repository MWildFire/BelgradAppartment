"""E2E (T2-3): сабмит формы с ?utm_source=tg → utm-атрибуция в БД заявки + lead_submit.

Гоняет ВЕСЬ атрибуционный конвейер против реального uvicorn + chromium: utm.js снимает
first-touch (query + document.referrer) в sessionStorage, переносит его в hidden-поля при
HTMX-сабмите, роут пишет leads.utm_*/referrer и событие lead_submit. Плюс ключевое свойство
first-touch: метки переживают внутреннюю навигацию (сабмит со «чистой» страницы всё равно
атрибуцирован).

Own server + seed (не shared `site_server`): нужен ПУТЬ к файлу БД, чтобы прочитать
leads/site_events напрямую — общая фикстура отдаёт только base_url (env живёт в child).
Boilerplate повторяет test_map_privacy.py — там это задокументировано как осознанный паттерн.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.e2e.conftest import REPO_ROOT, _free_port, _seed, _wait_healthz, requires_e2e


@pytest.fixture(scope="module")
def attribution_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    """Стандартный e2e-сид, свой uvicorn. Yields (base_url, db_path) — БД читаем напрямую."""
    if not os.getenv("RUN_E2E"):
        pytest.skip("e2e disabled; set RUN_E2E=1")

    db_path = tmp_path_factory.mktemp("e2e_attrib_db") / "site.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    # Worker-thread asyncio.run: после браузерных e2e main-thread loop занят playwright'ом
    # (см. test_map_privacy._run_coro) — так фикстура детерминирована при любом порядке тестов.
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, _seed(db_url)).result()

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = os.environ.copy()
    env["DATABASE_URL"] = db_url
    env["SITE_BIND_HOST"] = "127.0.0.1"
    env["SITE_BIND_PORT"] = str(port)
    env["SITE_CHANNEL_URL"] = "https://t.me/RentBelgrad"
    env["SITE_PUBLIC_BASE_URL"] = base_url
    env["TG_BOT_TOKEN"] = ""
    env["TG_BOT_RECIPIENTS"] = ""
    env["SITE_ANALYTICS_SALT"] = "e2e-attrib-salt"  # секрет — только env, никогда в код
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
        yield base_url, db_path
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _rows(db_path: Path, sql: str) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def _wait_event(db_path: Path, event_type: str, timeout: float = 5.0) -> dict:
    """lead_submit пишется BackgroundTask'ом ПОСЛЕ ответа — коммит может отстать от .lead-ok."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = _rows(db_path, f"SELECT * FROM site_events WHERE type = '{event_type}'")
        if events:
            return events[-1]
        time.sleep(0.2)
    raise AssertionError(f"no '{event_type}' event within {timeout}s")


def _submit_lead(page, contact: str) -> None:
    page.fill("#lead-name", "Атрибуция Тест")
    page.fill("#lead-contact", contact)
    page.locator("form.lead#lead button[type='submit']").click()
    page.wait_for_selector(".lead-ok", timeout=5000)


@requires_e2e
def test_lead_submit_with_utm_lands_in_db(page, attribution_server: tuple[str, Path]) -> None:
    base_url, db_path = attribution_server
    # Заход «по рекламной ссылке»: utm.js должен снять first-touch из query в sessionStorage.
    page.goto(
        f"{base_url}/?utm_source=tg&utm_medium=social&utm_campaign=summer",
        wait_until="networkidle",
    )
    _submit_lead(page, "@utm_e2e")

    leads = _rows(db_path, "SELECT * FROM leads WHERE contact = '@utm_e2e'")
    assert len(leads) == 1
    lead = leads[0]
    assert lead["utm_source"] == "tg"
    assert lead["utm_medium"] == "social"
    assert lead["utm_campaign"] == "summer"
    # переход по прямой ссылке: document.referrer пуст → NULL (не свой хост и не мусор)
    assert lead["referrer"] is None

    # …и то же first-touch касание несёт событие lead_submit (пишется после коммита, фоном).
    event = _wait_event(db_path, "lead_submit")
    assert event["path"] == "/lead"
    assert event["utm_source"] == "tg"
    assert event["utm_medium"] == "social"
    # приватность §A.1: visitor — 16-hex хэш, сырых IP/UA в строке нет
    assert len(event["visitor"]) == 16
    assert int(event["visitor"], 16) >= 0


@requires_e2e
def test_first_touch_survives_internal_navigation(page, attribution_server: tuple[str, Path]) -> None:
    """Ключ first-touch: сабмит со страницы БЕЗ меток всё равно атрибуцирован первым касанием."""
    base_url, db_path = attribution_server
    page.goto(f"{base_url}/?utm_source=first_touch", wait_until="networkidle")
    # внутренняя навигация «чистыми» URL — снимок в sessionStorage не должен перетереться
    page.goto(f"{base_url}/catalog", wait_until="networkidle")
    page.goto(f"{base_url}/", wait_until="networkidle")
    _submit_lead(page, "@first_touch_e2e")

    leads = _rows(db_path, "SELECT * FROM leads WHERE contact = '@first_touch_e2e'")
    assert len(leads) == 1
    assert leads[0]["utm_source"] == "first_touch"
