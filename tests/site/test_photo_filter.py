"""T4-2 — Jinja-фильтры photo_v/photo_dims + <picture>/srcset-разметка (contract §P.2).

Слои (зеркалят test_photos.py + test_render.py):
- Регистрация: import src.site.app вешает фильтры на ОБЩИЙ templates.env (как telegram_url).
- Семантика фильтра: вариант есть → путь варианта; нет манифеста/записи → ОРИГИНАЛ (фоллбэк,
  прод без backfill не ломается). Логика существования — в photos.py (T4-1), тут только обёртка.
- Разметка: _card.html — card-вариант + srcset/sizes + width/height из манифеста; apt.html —
  detail в галерее + клик на ПОЛНЫЙ оригинал. Везде loading/decoding (CLS→0, LCP-цель волны).
- Guard: шаблоны обязаны рендериться и в ЧИСТОМ Environment без наших фильтров — так собирают
  env render-smoke suites (test_render/test_card_cta/test_a11y…), ломать их нельзя.

Фикстурные картинки генерятся Pillow в tmp_path (без бинарей в репо) — как в test_photos.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image

import src.site.photos as photos
from src.config import get_settings

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "src" / "site" / "templates"
CSS_FILE = Path(__file__).resolve().parents[2] / "src" / "site" / "static" / "css" / "site.css"

_CSRF = "test-csrf-token"


# --------------------------------------------------------------------------- helpers / fixtures


@dataclass(frozen=True)
class FakeView:
    """Mirror of SiteListingView (DATA_CONTRACT §1) — only the fields templates may read."""

    id: int = 128
    price_eur: int = 650
    area_m2: float | None = 48.0
    rooms: str = "1+1"
    floor: str | None = "3/5"
    district: str = "Врачар"
    address: str | None = None
    lat: float | None = 44.8
    lon: float | None = 20.47
    for_vnj: bool = True
    for_white_card: bool = False
    is_rented: bool = False
    photos: list[str] = field(default_factory=lambda: ["/photos/a.jpg", "/photos/b.jpg"])
    note: str | None = None
    channel_url: str = "https://t.me/RentBelgrad/128"
    posted_at: str = "2026-06-18T10:00:00"


def _make_img(path: Path, w: int = 800, h: int = 600) -> Path:
    """Плоский JPEG — веса тут не проверяем (это T4-1), нужны только валидные пиксели/размер."""
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (w, h), (180, 140, 90)).save(path, "JPEG")
    return path


@pytest.fixture
def root(tmp_path: Path, monkeypatch) -> Path:
    """Фейковый photos_root в настройках + сброс read-кэша манифеста (изоляция тестов)."""
    root = tmp_path / "photos"
    root.mkdir()
    monkeypatch.setattr(get_settings(), "site_photos_root", root)
    photos.invalidate_manifest_cache()
    return root


def _plain_env() -> Environment:
    """Чистый env — ровно тот seam, которым рендерят render-smoke suites (без фильтров)."""
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )


def _site_env() -> Environment:
    """Чистый env + БОЕВЫЕ фильтры, снятые с общего templates.env (регистрирует src.site.app) —
    проверяем реальную проводку, но рендерим изолированно от роутов (паттерн test_render)."""
    import src.site.app  # noqa: F401 — side-effect: регистрация фильтров
    from src.site.routes import templates as site_templates

    env = _plain_env()
    env.filters["photo_v"] = site_templates.env.filters["photo_v"]
    env.filters["photo_dims"] = site_templates.env.filters["photo_dims"]
    return env


def _render(env: Environment, name: str, **ctx: object) -> str:
    return env.get_template(name).render(**ctx)


# --------------------------------------------------------------------------- registration + semantics


def test_app_registers_photo_filters(root: Path) -> None:
    """T4-2: import src.site.app регистрирует фильтры photo_v/photo_dims на общем env;
    дефолтный kind — 'card' (контракт §P.2)."""
    _make_img(root / "a.jpg", 800, 600)
    photos.backfill_variants(root=root)
    photos.invalidate_manifest_cache()

    import src.site.app  # noqa: F401
    from src.site.routes import templates

    pv = templates.env.filters.get("photo_v")
    pd = templates.env.filters.get("photo_dims")
    assert callable(pv) and callable(pd)
    assert pv("/photos/a.jpg") == "/photos/a.jpg.card.webp"  # kind по умолчанию = card
    assert pv("/photos/a.jpg", "detail") == "/photos/a.jpg.detail.webp"
    assert pd("/photos/a.jpg") == (800, 600)


def test_photo_v_falls_back_without_manifest(root: Path) -> None:
    """Нет манифеста вообще → photo_v отдаёт ОРИГИНАЛ, photo_dims → None (прод не ломается)."""
    import src.site.app  # noqa: F401
    from src.site.routes import templates

    assert templates.env.filters["photo_v"]("/photos/a.jpg") == "/photos/a.jpg"
    assert templates.env.filters["photo_dims"]("/photos/a.jpg") is None


def test_photo_v_falls_back_for_missing_entry(root: Path) -> None:
    """Манифест есть, но записи о фото нет → оригинал (per-photo фоллбэк)."""
    _make_img(root / "a.jpg")
    photos.backfill_variants(root=root)
    photos.invalidate_manifest_cache()

    import src.site.app  # noqa: F401
    from src.site.routes import templates

    assert templates.env.filters["photo_v"]("/photos/other.jpg", "card") == "/photos/other.jpg"


# --------------------------------------------------------------------------- _card.html markup


def test_card_img_uses_card_variant_with_srcset(root: Path) -> None:
    """Вариант есть → src=card-вариант, srcset 640w(+detail 1280w), sizes по контракту,
    lazy+async, width/height = размеры ОРИГИНАЛА из манифеста (CLS→0)."""
    _make_img(root / "a.jpg", 800, 600)
    photos.backfill_variants(root=root)
    photos.invalidate_manifest_cache()

    html = _render(_site_env(), "partials/_card.html", item=FakeView())

    assert 'src="/photos/a.jpg.card.webp"' in html
    assert 'srcset="/photos/a.jpg.card.webp 640w, /photos/a.jpg.detail.webp 1280w"' in html
    assert 'sizes="(max-width:700px) 100vw, 33vw"' in html
    assert 'loading="lazy"' in html
    assert 'decoding="async"' in html
    assert 'width="800"' in html and 'height="600"' in html
    # C1-фоллбэк битого фото не потерян
    assert "/static/img/placeholder.svg" in html


def test_card_img_falls_back_to_original_without_manifest(root: Path) -> None:
    """Нет манифеста → оригинальный src, БЕЗ srcset (врать браузеру про 640w нельзя),
    прежние 600×400 как w/h-фоллбэк; lazy/async остаются."""
    html = _render(_site_env(), "partials/_card.html", item=FakeView())

    assert 'src="/photos/a.jpg"' in html
    assert ".card.webp" not in html
    assert "srcset" not in html
    assert 'width="600"' in html and 'height="400"' in html
    assert 'loading="lazy"' in html and 'decoding="async"' in html


def test_card_template_renders_in_plain_env(root: Path) -> None:
    """GUARD: _card.html обязан рендериться ЧИСТЫМ Environment (без photo_v) — так собирают
    env test_render/test_card_cta/test_a11y; статический `|photo_v` валил бы им компиляцию."""
    _make_img(root / "a.jpg")
    photos.backfill_variants(root=root)  # манифест есть, но фильтров в env нет
    photos.invalidate_manifest_cache()

    html = _render(_plain_env(), "partials/_card.html", item=FakeView())

    assert 'src="/photos/a.jpg"' in html  # деградация в оригинал
    assert ".card.webp" not in html


# --------------------------------------------------------------------------- apt.html gallery


_GALLERY_PHOTOS = [f"/photos/p{i}.jpg" for i in range(6)]  # 6 → показываются [:5] + «+1 фото»


def test_apt_gallery_detail_variant_and_original_click(root: Path) -> None:
    """Галерея: detail-вариант в <img>, клик по тайлу → ПОЛНЫЙ оригинал (новая вкладка);
    LCP-элемент (первое фото) остаётся eager + fetchpriority=high, остальные lazy+async."""
    for i in range(6):
        _make_img(root / f"p{i}.jpg", 800, 600)
    photos.backfill_variants(root=root)
    photos.invalidate_manifest_cache()

    html = _render(
        _site_env(), "apt.html",
        item=FakeView(photos=list(_GALLERY_PHOTOS)), similar=[], csrf_token=_CSRF,
    )

    assert 'src="/photos/p0.jpg.detail.webp"' in html
    assert 'src="/photos/p1.jpg.detail.webp"' in html
    # клик → полный оригинал, безопасно в новой вкладке
    assert '<a href="/photos/p0.jpg"' in html
    assert '<a href="/photos/p4.jpg"' in html
    assert 'rel="noopener"' in html
    # LCP: первое фото НЕ lazy + приоритет; остальные lazy; async у всех
    assert 'fetchpriority="high"' in html
    assert 'loading="eager"' in html and 'loading="lazy"' in html
    assert 'decoding="async"' in html
    # width/height оригинала из манифеста
    assert 'width="800"' in html and 'height="600"' in html
    # оверлей «+N фото» не потерян (6 фото → +1)
    assert "+1 фото" in html


def test_apt_gallery_falls_back_per_photo(root: Path) -> None:
    """Фоллбэк пофотный: у p0 вариант есть, у p1 нет записи → p0 идёт detail'ом,
    p1 — оригиналом; клик-ссылки в обоих случаях ведут на оригинал."""
    _make_img(root / "p0.jpg", 800, 600)
    photos.backfill_variants(root=root)  # p1.jpg на диске нет → в манифест не попадает
    photos.invalidate_manifest_cache()

    html = _render(
        _site_env(), "apt.html",
        item=FakeView(photos=["/photos/p0.jpg", "/photos/p1.jpg"]), similar=[], csrf_token=_CSRF,
    )

    assert 'src="/photos/p0.jpg.detail.webp"' in html
    assert 'src="/photos/p1.jpg"' in html  # per-photo деградация
    assert '<a href="/photos/p0.jpg"' in html
    assert '<a href="/photos/p1.jpg"' in html


def test_apt_template_renders_in_plain_env(root: Path) -> None:
    """GUARD: apt.html рендерится чистым Environment (см. test_card_template_renders_in_plain_env)."""
    html = _render(
        _plain_env(), "apt.html",
        item=FakeView(photos=["/photos/p0.jpg"]), similar=[], csrf_token=_CSRF,
    )

    assert 'src="/photos/p0.jpg"' in html
    assert ".detail.webp" not in html
    assert '<a href="/photos/p0.jpg"' in html  # клик на оригинал не зависит от фильтров


# --------------------------------------------------------------------------- CSS (load-bearing)


def test_css_gallery_anchor_covers_tile() -> None:
    """Якорь-обёртка тайла позиционируется на весь тайл: кликается вся плитка, а keyboard-фокус
    (:focus-visible ring) обводит её, не схлопываясь в нулевой inline-бокс (a11y)."""
    css = CSS_FILE.read_text(encoding="utf-8")
    import re

    rule = re.search(r"\.gallery \.g\s*>\s*a\{[^}]*\}", css)
    assert rule, "expected a .gallery .g>a rule (tile-covering link)"
    body = rule.group(0).replace(" ", "")
    assert "position:absolute" in body and "inset:0" in body
