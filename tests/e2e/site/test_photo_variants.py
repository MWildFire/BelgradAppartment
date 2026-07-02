"""E2E (T4-2): фото-варианты в ОТДАННОЙ сервером разметке (contract §P.2).

Два реальных uvicorn-процесса поверх сидированной БД (сид из tests/e2e/conftest):
- variants_server: SITE_PHOTOS_ROOT → tmp-корень с настоящим JPEG, прогнанным через
  photos.backfill_variants (варианты + манифест на диске) → карточки каталога ссылаются на
  .card.webp (srcset/sizes), галерея /apt — на .detail.webp + клик на полный оригинал.
- bare_server: пустой корень (ни манифеста, ни вариантов) → везде ОРИГИНАЛЫ и валидные
  страницы (фоллбэк: прод без backfill не ломается).

Ассерты — по сырому HTML (urllib), НЕ по живому DOM: /photos/* в e2e не обслуживается
(в проде это Caddy), и onerror-фоллбэк подменил бы src на placeholder прежде, чем браузерный
локатор его прочитал. Нас интересует именно «какие ссылки в разметке» — сырой HTML честнее.

Сид-нюанс: photo_paths сида лежат ВНЕ photos_root → public_photo_url схлопывает их в
/photos/00.jpg (basename), поэтому и оригинал в корне, и ключ манифеста — просто 00.jpg.
Гейт RUN_E2E=1 (requires_e2e) — как у остальных e2e.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import urllib.request
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.e2e.conftest import REPO_ROOT, _free_port, _seed, _wait_healthz, requires_e2e


def _fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as r:
        assert r.status == 200, f"{url} -> {r.status}"
        return r.read().decode("utf-8")


def _run_site(tmp_path_factory: pytest.TempPathFactory, photos_root: Path) -> Iterator[str]:
    """Сид темп-БД + uvicorn с SITE_PHOTOS_ROOT=photos_root (зеркало conftest.site_server —
    свой запуск нужен ровно из-за env-переменной: get_settings() кэшируется в чайлде)."""
    db_path = tmp_path_factory.mktemp("photo_e2e_db") / "site.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    # Сид — в отдельном потоке: module-фикстура поднимается ПОСЛЕ первых playwright-тестов
    # сессии, а sync_playwright держит в main-треде «running» event loop (греенлеты) —
    # прямой asyncio.run() здесь падает RuntimeError'ом при прогоне всего e2e-suite.
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, _seed(db_url)).result()

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["DATABASE_URL"] = db_url
    env["SITE_BIND_HOST"] = "127.0.0.1"
    env["SITE_BIND_PORT"] = str(port)
    env["SITE_PHOTOS_ROOT"] = str(photos_root)
    env["SITE_CHANNEL_URL"] = "https://t.me/RentBelgrad"
    env["SITE_PUBLIC_BASE_URL"] = base_url
    env["TG_BOT_TOKEN"] = ""
    env["TG_BOT_RECIPIENTS"] = ""
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.site.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(REPO_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
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


@pytest.fixture(scope="module")
def variants_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Сервер с настоящими вариантами: 00.jpg (800×600) + backfill → манифест + .webp на диске."""
    if not os.getenv("RUN_E2E"):
        pytest.skip("e2e disabled; set RUN_E2E=1")
    from PIL import Image

    import src.site.photos as photos

    root = tmp_path_factory.mktemp("photos_with_variants")
    Image.new("RGB", (800, 600), (180, 140, 90)).save(root / "00.jpg", "JPEG")
    stats = photos.backfill_variants(root=root)
    assert stats.generated == 2 and (root / "photo_manifest.json").is_file()
    yield from _run_site(tmp_path_factory, root)


@pytest.fixture(scope="module")
def bare_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Сервер БЕЗ манифеста/вариантов (пустой photos_root) — фоллбэк-путь прода до backfill."""
    if not os.getenv("RUN_E2E"):
        pytest.skip("e2e disabled; set RUN_E2E=1")
    yield from _run_site(tmp_path_factory, tmp_path_factory.mktemp("photos_bare"))


# --------------------------------------------------------------------- варианты есть


@requires_e2e
def test_catalog_cards_reference_card_webp(variants_server: str) -> None:
    html = _fetch(f"{variants_server}/catalog")
    # card-вариант в src + honest srcset (640w + retina 1280w) + sizes по контракту
    assert 'src="/photos/00.jpg.card.webp"' in html
    assert 'srcset="/photos/00.jpg.card.webp 640w, /photos/00.jpg.detail.webp 1280w"' in html
    assert 'sizes="(max-width:700px) 100vw, 33vw"' in html
    # CLS→0: width/height ОРИГИНАЛА из манифеста; ленивая загрузка + async-декод
    assert 'width="800"' in html and 'height="600"' in html
    assert 'loading="lazy"' in html and 'decoding="async"' in html


@requires_e2e
def test_apt_gallery_detail_webp_and_click_to_original(variants_server: str) -> None:
    html = _fetch(f"{variants_server}/apt/1")
    assert 'src="/photos/00.jpg.detail.webp"' in html  # галерея — detail-вариант
    assert '<a href="/photos/00.jpg"' in html  # клик по тайлу → полный оригинал
    # LCP-элемент: первое фото eager + fetchpriority (файл теперь лёгкий 1280w-webp)
    assert 'fetchpriority="high"' in html
    assert 'loading="eager"' in html
    assert 'decoding="async"' in html


# --------------------------------------------------------------------- манифеста нет


@requires_e2e
def test_catalog_falls_back_to_originals_without_manifest(bare_server: str) -> None:
    html = _fetch(f"{bare_server}/catalog")
    assert 'src="/photos/00.jpg"' in html  # оригинал как был
    assert ".card.webp" not in html and ".detail.webp" not in html
    assert "srcset" not in html  # без варианта srcset не объявляется (не врём про 640w)
    assert 'class="card-l' in html  # страница валидна: грид карточек отрендерился


@requires_e2e
def test_apt_falls_back_to_originals_without_manifest(bare_server: str) -> None:
    html = _fetch(f"{bare_server}/apt/1")
    assert 'src="/photos/00.jpg"' in html
    assert ".detail.webp" not in html
    assert '<a href="/photos/00.jpg"' in html  # клик на оригинал не зависит от манифеста
    assert 'class="gallery' in html  # страница валидна: галерея отрендерилась
