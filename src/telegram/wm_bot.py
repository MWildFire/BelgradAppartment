"""RentFinderBot: the /clean command and incoming photos - remove listing-platform marks.

Only private chats whose chat_id is in TG_BOT_RECIPIENTS or WATERMARK_ALLOWED_CHAT_IDS:
- send/forward 1-10 photos (album, or image files) -> the bot replies with them cleaned;
- reply /clean to a candidate album the bot delivered -> clean copies of that match's
  originals (full resolution from disk; also stored in data/clean for the site);
- `/clean <match_id>` -> the same by match number;
- `/clean` with neither -> help text.

CPU work runs in a worker thread, one job at a time (the server CPU is shared with the
monitor and nightly jobs). Handlers are plain async functions so tests can call them with
fake messages and no live bot.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from sqlalchemy import select

from src.config import get_settings
from src.db import get_session
from src.db.models import InboundPost, Match, MatchDelivery
from src.watermark.pipeline import (
    PLATFORM_TITLES,
    CleanReport,
    MarkResult,
    NotAnImageError,
    clean_bytes,
)
from src.watermark.store import StoredResult, ensure_clean

logger = logging.getLogger(__name__)

ALBUM_WAIT_SECONDS = 1.5
MAX_ALBUM = 10
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024  # Bot API downloads files up to 20 MB
_WORK_LOCK = asyncio.Semaphore(1)

# User-facing texts (Russian, for Darya).
HELP_TEXT = (
    "🧽 Убираю водяные знаки Halo Oglasi, 4zida и CityExpert.\n\n"
    "• Пришлите или перешлите мне фото (можно альбомом до 10 штук) — верну без знаков.\n"
    "• Ответьте /clean на альбом кандидата — пришлю его фото без знаков в полном качестве.\n"  # noqa: RUF001
    "• /clean 123 — то же для кандидата №123.\n\n"
    "Знаки агентств (BEOSTAN и т. п.) пока не убираю."
)
NOTHING_FOUND = "Знаков площадок не нашёл — фото без изменений."
NOT_FOUND_MATCH = "Не нашёл кандидата для этого сообщения. Ответьте /clean на альбом кандидата."  # noqa: RUF001
ALREADY_CLEAN = "Фото этого кандидата уже были отправлены без знаков."
WORKING_PHOTOS = "🧽 Обрабатываю {n} фото…"
WORKING_MATCH = "🧽 Чищу фото кандидата №{match_id}…"
MATCH_NOT_FOUND = "Кандидат №{match_id} не найден."
MATCH_NO_PHOTOS = "У кандидата №{match_id} нет фото на диске."  # noqa: RUF001
NOT_A_PHOTO = "Файл {name} не похож на фото — пропускаю."
FAILED_ONE = "Не получилось обработать {name} — попробуйте ещё раз позже."  # noqa: RUF001
FAILED_MATCH = "Не получилось обработать фото — попробуйте ещё раз позже."  # noqa: RUF001
DONE_PREFIX = "🧽 Готово. Убрал знаки: "
UNMARKED_SUFFIX = " Без знаков: {clean} из {total}."


def parse_chat_ids(*raw_values: str) -> frozenset[int]:
    ids: set[int] = set()
    for raw in raw_values:
        for piece in (raw or "").split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                ids.add(int(piece))
            except ValueError:
                logger.warning("[wm-bot] ignoring non-numeric chat id %r", piece)
    return frozenset(ids)


def allowed_chat_ids() -> frozenset[int]:
    settings = get_settings()
    return parse_chat_ids(settings.tg_bot_recipients, settings.watermark_allowed_chat_ids)


def summarize(reports: Iterable[CleanReport]) -> str:
    """Caption for the cleaned album, e.g. 'Halo Oglasi - 1, 4zida - 2' in Russian wording."""
    reports = list(reports)
    counts: dict[str, int] = {}
    for report in reports:
        for platform, n in report.platform_counts().items():
            counts[platform] = counts.get(platform, 0) + n
    clean = sum(1 for r in reports if not r.changed)
    if not counts:
        return NOTHING_FOUND
    order = [PLATFORM_TITLES[k] for k in ("halooglasi", "4zida", "cityexpert")]
    parts = [f"{name} — {counts[name]}" for name in order if name in counts]
    parts += [f"{name} — {n}" for name, n in counts.items() if name not in order]
    text = DONE_PREFIX + ", ".join(parts) + "."
    if clean:
        text += UNMARKED_SUFFIX.format(clean=clean, total=len(reports))
    return text


def is_image_document(document: Any) -> bool:
    mime = (getattr(document, "mime_type", "") or "").lower()
    size = getattr(document, "file_size", 0) or 0
    return mime.startswith("image/") and size <= MAX_DOCUMENT_BYTES


async def _download(bot: Any, file_obj: Any) -> bytes:
    buf = await bot.download(file_obj)
    if buf is None:
        raise RuntimeError("download returned nothing")
    data = buf.getvalue() if isinstance(buf, io.BytesIO) else buf.read()
    return bytes(data)


async def run_exclusive(func: Callable[..., Any], *args: Any) -> Any:
    """CPU work in a thread, one job at a time."""
    async with _WORK_LOCK:
        return await asyncio.to_thread(func, *args)


async def _send_files(
    message: Any, files: list[tuple[bytes, str]], caption: str, as_documents: bool
) -> None:
    from aiogram.types import BufferedInputFile, InputMediaDocument, InputMediaPhoto

    for start in range(0, len(files), MAX_ALBUM):
        chunk = files[start : start + MAX_ALBUM]
        cap = caption if start == 0 else None
        if len(chunk) == 1:
            data, name = chunk[0]
            upload = BufferedInputFile(data, filename=name)
            if as_documents:
                await message.answer_document(upload, caption=cap)
            else:
                await message.answer_photo(upload, caption=cap)
            continue
        media: list[Any] = []
        for i, (data, name) in enumerate(chunk):
            upload = BufferedInputFile(data, filename=name)
            kind = InputMediaDocument if as_documents else InputMediaPhoto
            media.append(kind(media=upload, caption=cap if i == 0 else None))
        await message.answer_media_group(media=media)


async def process_photo_messages(messages: list[Any]) -> None:
    """Photos sent to the bot: download -> clean -> send back (as photos or as files)."""
    first = messages[0]
    bot = first.bot
    items: list[tuple[Any, str]] = []
    as_documents = False
    for m in sorted(messages, key=lambda x: x.message_id)[:MAX_ALBUM]:
        if getattr(m, "photo", None):
            items.append((m.photo[-1], f"photo_{m.message_id}.jpg"))
        elif getattr(m, "document", None) is not None and is_image_document(m.document):
            as_documents = True
            stem = Path(m.document.file_name or f"photo_{m.message_id}").stem
            items.append((m.document, f"{stem}_clean.jpg"))
    if not items:
        await first.answer(HELP_TEXT)
        return
    await first.answer(WORKING_PHOTOS.format(n=len(items)))
    outputs: list[tuple[bytes, str]] = []
    reports: list[CleanReport] = []
    for file_obj, name in items:
        try:
            data = await _download(bot, file_obj)
            cleaned, report = await run_exclusive(clean_bytes, data)
        except NotAnImageError:
            await first.answer(NOT_A_PHOTO.format(name=name))
            continue
        except Exception:
            logger.exception("[wm-bot] cleaning failed chat=%s", first.chat.id)
            await first.answer(FAILED_ONE.format(name=name))
            continue
        outputs.append((cleaned, name))
        reports.append(report)
    if not outputs:
        return
    if not any(r.changed for r in reports):
        await first.answer(NOTHING_FOUND)
        return
    await _send_files(first, outputs, summarize(reports), as_documents)


async def find_match_for_message(chat_id: int, message_id: int) -> int | None:
    """The match whose album the bot delivered to this chat and that contains message_id."""
    async with get_session() as session:
        rows = (
            await session.execute(
                select(MatchDelivery.match_id, MatchDelivery.message_ids)
                .where(MatchDelivery.recipient_id == chat_id, MatchDelivery.status == "sent")
                .order_by(MatchDelivery.id.desc())
                .limit(5000)
            )
        ).all()
    for match_id, message_ids in rows:
        if message_id in (message_ids or []):
            return int(match_id)
    return None


async def match_photo_paths(match_id: int, chat_id: int) -> list[str] | None:
    """Match photos as delivered to this chat (frozen payload), else the inbound post photos.

    None: no such match.
    """
    async with get_session() as session:
        match = await session.get(Match, match_id)
        if match is None:
            return None
        payload = (
            await session.execute(
                select(MatchDelivery.payload).where(
                    MatchDelivery.match_id == match_id, MatchDelivery.recipient_id == chat_id
                )
            )
        ).scalar_one_or_none()
        if isinstance(payload, dict) and payload.get("photo_paths"):
            return [str(p) for p in payload["photo_paths"] if p]
        inbound = await session.get(InboundPost, match.inbound_post_id)
        return [str(p) for p in (inbound.photo_paths or [])] if inbound is not None else []


def clean_stored_paths(paths: list[str]) -> list[StoredResult]:
    results: list[StoredResult] = []
    for stored in paths:
        result = ensure_clean(stored)
        if result is not None:
            results.append(result)
    return results


async def _refresh_site_variants(paths: list[str]) -> None:
    if not get_settings().watermark_site_use_clean:
        return
    try:
        from src.watermark.published import rebuild_site_variants

        await asyncio.to_thread(rebuild_site_variants, paths)
    except Exception:
        logger.warning("[wm-bot] site variant refresh failed (cleaning kept)", exc_info=True)


def _report_from_dict(result: StoredResult) -> CleanReport:
    """StoredResult keeps the report as a dict (from the sidecar); rebuild it for summarize()."""
    report = result.report if isinstance(result.report, dict) else {}
    raw_marks = report.get("marks")
    marks = [MarkResult(**m) for m in raw_marks or [] if isinstance(m, dict)]  # type: ignore[union-attr]
    width = int(report.get("width", 0) or 0)  # type: ignore[call-overload]
    height = int(report.get("height", 0) or 0)  # type: ignore[call-overload]
    return CleanReport(width=width, height=height, marks=marks if result.clean else [])


def _read_files(results: list[StoredResult], match_id: int) -> list[tuple[bytes, str]]:
    return [
        (r.best.read_bytes(), f"match{match_id}_{i + 1:02d}.jpg") for i, r in enumerate(results)
    ]


async def clean_match_and_reply(message: Any, match_id: int) -> None:
    paths = await match_photo_paths(match_id, message.chat.id)
    if paths is None:
        await message.answer(MATCH_NOT_FOUND.format(match_id=match_id))
        return
    if not paths:
        await message.answer(MATCH_NO_PHOTOS.format(match_id=match_id))
        return
    await message.answer(WORKING_MATCH.format(match_id=match_id))
    try:
        results: list[StoredResult] = await run_exclusive(clean_stored_paths, paths[:MAX_ALBUM])
    except Exception:
        logger.exception("[wm-bot] match %s cleaning failed", match_id)
        await message.answer(FAILED_MATCH)
        return
    if not results:
        await message.answer(MATCH_NO_PHOTOS.format(match_id=match_id))
        return
    if all(r.report.get("outside_inbound") for r in results):
        await message.answer(ALREADY_CLEAN)
        return
    reports = [_report_from_dict(r) for r in results]
    if not any(r.changed for r in reports):
        await message.answer(NOTHING_FOUND)
        return
    files = await asyncio.to_thread(_read_files, results, match_id)
    await _send_files(message, files, summarize(reports), as_documents=False)
    await _refresh_site_variants([str(r.original) for r in results])


async def handle_clean_command(message: Any, args: str | None) -> None:
    arg = (args or "").strip()
    if arg:
        if not arg.isdigit():
            await message.answer(HELP_TEXT)
            return
        await clean_match_and_reply(message, int(arg))
        return
    reply = getattr(message, "reply_to_message", None)
    if reply is None:
        await message.answer(HELP_TEXT)
        return
    match_id = await find_match_for_message(message.chat.id, reply.message_id)
    if match_id is not None:
        await clean_match_and_reply(message, match_id)
        return
    if getattr(reply, "photo", None) or (
        getattr(reply, "document", None) is not None and is_image_document(reply.document)
    ):
        await process_photo_messages([reply])
        return
    await message.answer(NOT_FOUND_MATCH)


class AlbumCollector:
    """Telegram delivers an album as separate messages sharing media_group_id: collect them
    for ALBUM_WAIT_SECONDS and handle them together."""

    def __init__(
        self,
        handler: Callable[[list[Any]], Awaitable[None]],
        wait: float = ALBUM_WAIT_SECONDS,
    ) -> None:
        self._handler = handler
        self._wait = wait
        self._groups: dict[tuple[int, str], list[Any]] = {}
        self._tasks: dict[tuple[int, str], asyncio.Task[None]] = {}

    async def add(self, message: Any) -> None:
        group_id = getattr(message, "media_group_id", None)
        if not group_id:
            await self._handler([message])
            return
        key = (int(message.chat.id), str(group_id))
        self._groups.setdefault(key, []).append(message)
        if key not in self._tasks:
            self._tasks[key] = asyncio.create_task(self._flush(key))

    async def _flush(self, key: tuple[int, str]) -> None:
        await asyncio.sleep(self._wait)
        messages = self._groups.pop(key, [])
        self._tasks.pop(key, None)
        if not messages:
            return
        try:
            await self._handler(messages)
        except Exception:  # a background task has nobody to report to: log it
            logger.exception("[wm-bot] album handler failed chat=%s", key[0])


def build_watermark_router(allowed: frozenset[int]) -> Any:
    """aiogram Router: private chats from `allowed` only; other updates fall through."""
    from aiogram import F, Router
    from aiogram.filters import Command, CommandObject
    from aiogram.types import Message

    router = Router(name="watermark")
    router.message.filter(F.chat.type == "private", F.chat.id.in_(allowed))
    collector = AlbumCollector(process_photo_messages)

    @router.message(Command("clean", "watermark"), F.text)
    async def _on_clean(message: Message, command: CommandObject) -> None:  # pragma: no cover
        await handle_clean_command(message, command.args)

    @router.message(F.photo | F.document)
    async def _on_media(message: Message) -> None:  # pragma: no cover
        await collector.add(message)

    return router
