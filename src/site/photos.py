"""Photo variants + manifest for the public site (Stage T, contract §P.1/§P.2).

WRITE side: webp-варианты card (640w) / detail (1280w) рядом с оригиналом
(`<orig>.card.webp`) + `photo_manifest.json` в корне photos_root. Всё это —
derived data на VPS: в git/rsync не участвует (`data/` исключена), недостающее
добирается `otz photos-backfill`. Точки генерации: publish-hook админки
(src/admin/publish.py) и CLI backfill (src/cli.py).

READ side (для Jinja-фильтра T4-2): `variant_for()` / `dims_for()` поверх ленивого
кэша манифеста с mtime-инвалидацией — backfill на живом сервере подхватывается без
рестарта; проверка mtime троттлится (`_MANIFEST_CHECK_INTERVAL`), контракт допускает
свежесть «минуты».
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from PIL import Image, UnidentifiedImageError

from src.config import PROJECT_ROOT, get_settings

logger = logging.getLogger(__name__)

# Форма манифеста (§P.1): relpath оригинала → {"w","h","card","detail"} (см. _entry).
ManifestEntry = dict[str, int | str]
Manifest = dict[str, ManifestEntry]

MANIFEST_NAME = "photo_manifest.json"
# card — карточки каталога, detail — галерея /apt (§P.1). Порядок фиксирован: от него
# зависят счётчики generated/skipped в статистике и детерминизм тестов.
VARIANT_WIDTHS: dict[str, int] = {"card": 640, "detail": 1280}
WEBP_QUALITY = 82
# method=6 — самый медленный/плотный энкодер: генерация оффлайн, вес важнее CPU.
WEBP_METHOD = 6

# Кандидаты backfill-обхода по расширению; сгенерённые варианты отсекаются по СУФФИКСУ
# имени (см. _is_generated) — у них расширение тоже .webp.
_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
_GENERATED_SUFFIXES = tuple(f".{kind}.webp" for kind in VARIANT_WIDTHS)

# Битые фото: скип+лог, не падение (§P.1). DecompressionBombError — не OSError,
# перечисляем явно (гигапиксельная бомба в inbound не должна валить backfill).
_BROKEN_IMAGE_ERRORS = (OSError, UnidentifiedImageError, ValueError, Image.DecompressionBombError)


def photos_root() -> Path:
    """Резолвленный SITE_PHOTOS_ROOT — единая точка для write- и read-стороны."""
    return Path(get_settings().site_photos_root).resolve()


def manifest_path(root: Path) -> Path:
    return root / MANIFEST_NAME


# --------------------------------------------------------------------------- generation (§P.1)


@dataclass
class BackfillStats:
    """Счётчики одного прогона: total — оригиналов рассмотрено; generated/skipped —
    файлов-вариантов (по 2 на оригинал); broken — нечитаемых оригиналов (скип+лог)."""

    total: int = 0
    generated: int = 0
    skipped: int = 0
    broken: int = 0


def _is_generated(name: str) -> bool:
    return name.lower().endswith(_GENERATED_SUFFIXES)


def iter_originals(root: Path) -> Iterator[Path]:
    """Все исходники под root: картинки по расширению, минус наши же варианты и служебные
    файлы. Сортировка — детерминированный порядок для --limit и прогресса."""
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in _IMAGE_EXTS:
            continue
        if _is_generated(p.name):
            continue
        yield p


def _entry(rel: str, w: int, h: int) -> ManifestEntry:
    """Манифест-запись (§P.1): w/h — размеры ОРИГИНАЛА (для width/height-атрибутов)."""
    return {"w": w, "h": h, "card": f"{rel}.card.webp", "detail": f"{rel}.detail.webp"}


def process_photo(
    orig: Path, rel: str, root: Path, *, dry_run: bool = False
) -> tuple[ManifestEntry | None, int, int]:
    """Один оригинал → (манифест-запись | None, generated, skipped).

    Идемпотентно: существующий файл-вариант не перезаписывается (skipped). Битый/нечитаемый
    оригинал → (None, …) + warn-лог, НИКОГДА не raise. dry_run считает generated, не пишет.
    """
    generated = skipped = 0
    try:
        with Image.open(orig) as im:
            w, h = im.size
            if w <= 0 or h <= 0:
                raise ValueError(f"degenerate size {im.size}")
            rgb: Image.Image | None = None  # декодируем лениво: full-skip не читает пиксели
            for kind, width in VARIANT_WIDTHS.items():
                target = root / f"{rel}.{kind}.webp"
                if target.exists():
                    skipped += 1
                    continue
                generated += 1
                if dry_run:
                    continue
                if rgb is None:
                    rgb = im.convert("RGB")  # заодно фейлит рано на обрезанном payload'е
                tw = min(width, w)  # апскейла нет (§P.1): узкий оригинал → вариант его размера
                th = max(1, round(h * tw / w))
                if (tw, th) == (w, h):
                    variant = rgb
                else:
                    variant = rgb.resize((tw, th), Image.Resampling.LANCZOS)
                # tmp + rename: Caddy никогда не отдаст полузаписанный webp, а упавший
                # процесс не оставит «существующий» огрызок, который скип бы увековечил.
                tmp = target.with_name(target.name + ".tmp")
                variant.save(tmp, "WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)
                os.replace(tmp, target)
    except _BROKEN_IMAGE_ERRORS as e:
        logger.warning("skip broken photo %s: %s", orig, e)
        return None, generated, skipped
    return _entry(rel, w, h), generated, skipped


def _read_manifest_file(path: Path) -> Manifest:
    """Толерантное чтение: нет файла / битый JSON / не-словарь → {} (+warn), сайт живёт дальше."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning("unreadable %s: %s", path, e)
        return {}
    if not isinstance(data, dict):
        logger.warning("unexpected manifest shape in %s: %s", path, type(data).__name__)
        return {}
    return cast(Manifest, data)


def _save_manifest(manifest: Manifest, root: Path) -> None:
    """Atomic write (tmp + rename): читатели видят либо старый, либо новый манифест целиком."""
    path = manifest_path(root)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=1), encoding="utf-8"
    )
    os.replace(tmp, path)
    invalidate_manifest_cache()  # same-process читатели (админка) видят обновление сразу


def _merge_manifest(entries: Manifest, root: Path) -> None:
    """Read-modify-write мердж. Записей о пропавших оригиналах не чистим: они безвредны
    (variant_for проверяет файл на диске), а полный обход их всё равно перезапишет.
    Кросс-процессный писатель ровно один в каждый момент (hook или ручной backfill) —
    файловый лок не заводим (YAGNI)."""
    current = _read_manifest_file(manifest_path(root))
    merged = {**current, **entries}
    if merged != current:  # no-op мердж не трогает mtime → mtime-кэш читателей не сбрасывается
        _save_manifest(merged, root)


def backfill_variants(
    *,
    root: Path | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> BackfillStats:
    """Идемпотентный обход photos_root: варианты для каждого оригинала + мердж манифеста.

    `progress(done, total)` дёргается после каждого оригинала (кадансом рулит вызывающий).
    dry_run: только счётчики, ноль записей (ни вариантов, ни манифеста).
    """
    root = (root or photos_root()).resolve()
    stats = BackfillStats()
    if not root.is_dir():
        logger.warning("photos root %s does not exist — nothing to backfill", root)
        return stats

    originals = list(iter_originals(root))
    if limit is not None:
        originals = originals[:limit]
    stats.total = len(originals)

    entries: Manifest = {}
    for done, orig in enumerate(originals, 1):
        rel = orig.relative_to(root).as_posix()
        entry, generated, skipped = process_photo(orig, rel, root, dry_run=dry_run)
        stats.generated += generated
        stats.skipped += skipped
        if entry is None:
            stats.broken += 1
        else:
            entries[rel] = entry
        if progress is not None:
            progress(done, stats.total)

    if entries and not dry_run:
        _merge_manifest(entries, root)
    return stats


def _resolve_stored(stored: str, root: Path) -> tuple[Path, str] | None:
    """Путь из InboundPost.photo_paths → (абсолютный файл, relpath под root) или None.

    Зеркалит резолв public_photo_url (src/site/format.py): relative анкорится на
    PROJECT_ROOT. Remote-URL / вне root / отсутствующий файл → None: такой файл сайт
    всё равно не отдаст, вариант для него бессмыслен.
    """
    if not stored or "://" in stored:
        return None
    p = Path(stored)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    try:
        abs_p = p.resolve()
        rel = abs_p.relative_to(root)
    except (OSError, ValueError):
        return None
    if not abs_p.is_file():
        return None
    return abs_p, rel.as_posix()


def generate_for_stored_paths(photo_paths: Iterable[str]) -> int:
    """Publish-hook (§P.1 а): варианты для фото ОДНОГО листинга + мердж манифеста.

    Синхронная (Pillow) — вызывающий async-код заворачивает в asyncio.to_thread.
    Непригодные пути скипаются молча, битые фото — warn внутри process_photo.
    Возвращает число записанных файлов-вариантов.
    """
    root = photos_root()
    entries: Manifest = {}
    written = 0
    for stored in photo_paths:
        resolved = _resolve_stored(stored, root)
        if resolved is None:
            continue
        abs_p, rel = resolved
        entry, generated, _skipped = process_photo(abs_p, rel, root)
        written += generated
        if entry is not None:
            entries[rel] = entry
    if entries:
        _merge_manifest(entries, root)
    return written


# --------------------------------------------------------------------------- read side (§P.2)

# Троттлинг stat()-проверки манифеста. Не 0: variant_for зовётся на каждый <img> каждой
# карточки; свежесть «минуты» контракт допускает явно. Тесты подменяют на 0 (всегда re-stat).
_MANIFEST_CHECK_INTERVAL = 60.0


@dataclass
class _ManifestCache:
    root: Path | None = None
    mtime_ns: int | None = None  # None == файла не было при последней проверке
    data: Manifest = field(default_factory=dict)
    checked_at: float = float("-inf")  # time.monotonic последней stat()-проверки


_cache = _ManifestCache()
_cache_lock = threading.Lock()


def invalidate_manifest_cache() -> None:
    """Сброс read-кэша: после same-process записи и между тестами."""
    global _cache
    with _cache_lock:
        _cache = _ManifestCache()


def load_manifest(root: Path | None = None) -> Manifest:
    """Содержимое photo_manifest.json c ленивым кэшем и mtime-инвалидацией.

    Горячий путь внутри окна троттлинга — возврат кэша без stat(). По истечении окна —
    stat(); mtime не сменился → только продлеваем окно, сменился/появился/пропал →
    перечитываем. НЕ мутировать возвращённый словарь.
    """
    root = (root or photos_root()).resolve()
    now = time.monotonic()
    with _cache_lock:
        if _cache.root == root and now - _cache.checked_at < _MANIFEST_CHECK_INTERVAL:
            return _cache.data
        path = manifest_path(root)
        try:
            mtime_ns: int | None = path.stat().st_mtime_ns
        except OSError:
            mtime_ns = None
        if _cache.root == root and mtime_ns == _cache.mtime_ns:
            _cache.checked_at = now
            return _cache.data
        data = _read_manifest_file(path) if mtime_ns is not None else {}
        _cache.root, _cache.mtime_ns, _cache.data, _cache.checked_at = root, mtime_ns, data, now
        return data


def _public_to_rel(src: str) -> str | None:
    """'/photos/<rel>' → rel, иначе None. Гард от traversal — зеркалит og_image._photo_file."""
    if not src or not src.startswith("/photos/"):
        return None
    rel = src[len("/photos/") :]
    if not rel or ".." in rel.split("/"):
        return None
    return rel


def variant_for(src: str, kind: str) -> str | None:
    """Публичный путь варианта ('/photos/<rel>.card.webp') или None → фоллбэк на оригинал.

    None при: не-/photos src, нет манифеста/записи/kind, вариант-файла нет на диске
    (устаревший манифест не должен отдать 404-<img>). Чистый API для Jinja-фильтра T4-2.
    """
    rel = _public_to_rel(src)
    if rel is None:
        return None
    entry = load_manifest().get(rel)
    if not isinstance(entry, dict):
        return None
    variant_rel = entry.get(kind)
    if not isinstance(variant_rel, str) or not variant_rel:
        return None
    # Defense-in-depth (как public_photo_url): манифест пишем мы сами, но подпорченная
    # запись всё равно не должна дать traversal ни в is_file(), ни в публичный URL.
    if variant_rel.startswith("/") or ".." in variant_rel.split("/"):
        return None
    try:
        if not (photos_root() / variant_rel).is_file():
            return None
    except OSError:
        return None
    return "/photos/" + variant_rel


def dims_for(src: str) -> tuple[int, int] | None:
    """(w, h) ОРИГИНАЛА из манифеста — для width/height-атрибутов (CLS→0), или None."""
    rel = _public_to_rel(src)
    if rel is None:
        return None
    entry = load_manifest().get(rel)
    if not isinstance(entry, dict):
        return None
    w, h = entry.get("w"), entry.get("h")
    if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
        return (w, h)
    return None
