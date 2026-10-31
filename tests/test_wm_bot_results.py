"""Bot scenarios for photos sent to /clean: what the user is told (WM05-W3, B1).

The cleaning engine is scripted with real report shapes (tests/fixtures/wm_n08_reports.json),
so no photo is decoded and no model is loaded. Fake messages, no network.
"""
# ruff: noqa: RUF001  (Russian user-facing texts are asserted verbatim)

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.telegram import wm_bot
from src.watermark import pipeline
from src.watermark.pipeline import CleanReport, MarkResult, NotAnImageError
from tests.test_wm_bot import FakeBot, FakeMessage

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "wm_n08_reports.json").read_text(encoding="utf-8")
)["cases"]
KEPT_SENTENCE = "Знак найден и оставлен без изменений"


def _report(name: str) -> CleanReport:
    raw = FIXTURES[name]
    return CleanReport(raw["width"], raw["height"], [MarkResult(**m) for m in raw["marks"]])


class ScriptedEngine:
    """Replaces pipeline.clean_bytes: one scripted answer per photo, in call order."""

    def __init__(self, *script: str):
        self.script = list(script)
        self.calls = 0

    def __call__(self, data: bytes, **_kw):
        step = self.script[self.calls]
        self.calls += 1
        if step == "not_an_image":
            raise NotAnImageError("cannot identify image file")
        if step == "boom":
            raise RuntimeError("engine exploded")
        report = _report(step)
        return (b"CLEAN-" + data if report.changed else data), report


def _install(monkeypatch, *script: str) -> ScriptedEngine:
    engine = ScriptedEngine(*script)
    monkeypatch.setattr(pipeline, "clean_bytes", engine)
    return engine


def _album(n: int, group: str | None = None):
    files = {f"f{i}": f"RAW{i}".encode() for i in range(n)}
    bot = FakeBot(files)
    return [FakeMessage(100 + i, bot=bot, photo_id=f"f{i}", group=group) for i in range(n)]


async def test_single_photo_only_kept_is_not_called_unmarked(monkeypatch):
    _install(monkeypatch, "only_kept_halo_000")
    (msg,) = _album(1)
    await wm_bot.process_photo_messages([msg])
    assert msg.photos == [] and msg.albums == []  # nothing new to send: pixels untouched
    assert msg.texts[-1].startswith(KEPT_SENTENCE)
    assert wm_bot.NOTHING_FOUND not in msg.texts


async def test_single_photo_mixed_returns_the_photo_with_both_counts(monkeypatch):
    _install(monkeypatch, "mixed_zida_043")
    (msg,) = _album(1)
    await wm_bot.process_photo_messages([msg])
    ((data, caption),) = msg.photos
    assert data == b"CLEAN-RAW0"
    assert "Удалено: 4zida — 2; оставлено: Halo Oglasi — 1" in caption
    assert "все" not in caption.lower()


async def test_single_photo_without_marks(monkeypatch):
    _install(monkeypatch, "no_marks")
    (msg,) = _album(1)
    await wm_bot.process_photo_messages([msg])
    assert msg.photos == [] and msg.texts[-1] == wm_bot.NOTHING_FOUND


async def test_engine_error_returns_the_original_and_says_it_failed(monkeypatch):
    _install(monkeypatch, "boom")
    (msg,) = _album(1)
    await wm_bot.process_photo_messages([msg])
    ((data, caption),) = msg.photos
    assert data == b"RAW0"  # the original, byte for byte
    assert caption.startswith("Не удалось обработать: 1 из 1")
    assert "без найденных знаков" not in caption and wm_bot.NOTHING_FOUND not in msg.texts


async def test_album_of_ten_has_one_caption_and_every_photo_in_order(monkeypatch):
    script = ["mixed_zida_043", "only_kept_halo_000", *["no_marks"] * 7, "boom"]
    engine = _install(monkeypatch, *script)
    messages = _album(10, group="g")
    await wm_bot.process_photo_messages(messages)
    assert engine.calls == 10
    (album,) = messages[0].albums
    assert [m.media.data for m in album] == [
        b"CLEAN-RAW0", b"RAW1", *[f"RAW{i}".encode() for i in range(2, 9)], b"RAW9",
    ]
    caption = album[0].caption
    assert "Удалено: 4zida — 2; оставлено: Halo Oglasi — 2" in caption
    assert "без найденных знаков: 7 из 10 фото" in caption
    assert "Не удалось обработать: 1 из 10" in caption
    assert all(m.caption is None for m in album[1:]) and len(caption) < 1024


async def test_only_kept_album_is_text_only(monkeypatch):
    _install(monkeypatch, "only_kept_halo_000", "only_kept_halo_000", "no_marks")
    messages = _album(3, group="g")
    await wm_bot.process_photo_messages(messages)
    first = messages[0]
    assert first.albums == [] and first.photos == []
    assert first.texts[-1].startswith(KEPT_SENTENCE)
    assert "оставлено: Halo Oglasi — 2" in first.texts[-1]
    assert "без найденных знаков: 1 из 3" in first.texts[-1]


async def test_not_an_image_is_skipped_but_counted_as_not_processed(monkeypatch):
    _install(monkeypatch, "removed_zida_001", "not_an_image")
    messages = _album(2, group="g")
    await wm_bot.process_photo_messages(messages)
    first = messages[0]
    assert any("не похож на фото" in t for t in first.texts)
    ((data, caption),) = first.photos  # only the real photo is sent
    assert data == b"CLEAN-RAW0" and caption.endswith("Не удалось обработать: 1 из 2.")


async def test_download_failure_is_reported_without_inventing_an_original(monkeypatch):
    _install(monkeypatch, "removed_zida_001")
    bot = FakeBot({"f0": b"RAW0"})  # f1 is missing -> download raises
    first = FakeMessage(100, bot=bot, photo_id="f0", group="g")
    second = FakeMessage(101, bot=bot, photo_id="f1", group="g")
    await wm_bot.process_photo_messages([first, second])
    assert any("Не получилось обработать" in t for t in first.texts)
    ((_, caption),) = first.photos
    assert "Не удалось обработать: 1 из 2" in caption


@pytest.mark.parametrize("script", [["no_marks"] * 2, ["only_kept_halo_000", "no_marks"]])
async def test_no_removal_never_sends_photos_back(monkeypatch, script):
    _install(monkeypatch, *script)
    messages = _album(2, group="g")
    await wm_bot.process_photo_messages(messages)
    assert messages[0].photos == [] and messages[0].albums == []


# --- /clean on a delivered candidate: results restored from stored reports -----------


def _stored(tmp_path: Path, names: list[str | None]) -> tuple[list[str], dict[str, object]]:
    """Fake store: a name is a fixture report, None means the photo fails to clean."""
    paths, table = [], {}
    for i, name in enumerate(names):
        original = tmp_path / f"orig{i}.jpg"
        original.write_bytes(f"ORIG{i}".encode())
        paths.append(str(original))
        if name is None:
            table[str(original)] = None
            continue
        raw = FIXTURES[name]
        clean = None
        if _report(name).changed:
            clean = tmp_path / f"clean{i}.jpg"
            clean.write_bytes(f"CLEAN{i}".encode())
        table[str(original)] = wm_bot.StoredResult(original, clean, raw, reused=True)
    return paths, table


def _patch_store(monkeypatch, paths, table):
    async def fake_paths(_match_id, _chat_id):
        return paths

    def fake_ensure(stored, **_kw):
        result = table[stored]
        if result is None:
            raise RuntimeError("engine exploded")
        return result

    monkeypatch.setattr(wm_bot, "match_photo_paths", fake_paths)
    monkeypatch.setattr(wm_bot, "ensure_clean", fake_ensure)


async def test_stored_only_kept_match_says_kept_not_no_marks(monkeypatch, tmp_path):
    paths, table = _stored(tmp_path, ["only_kept_halo_000", "no_marks"])
    _patch_store(monkeypatch, paths, table)
    msg = FakeMessage(300)
    await wm_bot.clean_match_and_reply(msg, 7)
    assert msg.albums == [] and msg.photos == []
    assert msg.texts[-1].startswith(KEPT_SENTENCE)
    assert "оставлено: Halo Oglasi — 1" in msg.texts[-1]
    assert "без найденных знаков: 1 из 2" in msg.texts[-1]


async def test_stored_mixed_match_counts_removed_and_kept_apart(monkeypatch, tmp_path):
    paths, table = _stored(tmp_path, ["mixed_zida_043", "removed_zida_001", "no_marks"])
    _patch_store(monkeypatch, paths, table)
    msg = FakeMessage(301)
    await wm_bot.clean_match_and_reply(msg, 7)
    (album,) = msg.albums
    assert [m.media.data for m in album] == [b"CLEAN0", b"CLEAN1", b"ORIG2"]
    assert "Удалено: 4zida — 4; оставлено: Halo Oglasi — 1" in album[0].caption
    assert "без найденных знаков: 1 из 3 фото" in album[0].caption


async def test_one_failing_photo_does_not_lose_the_rest(monkeypatch, tmp_path):
    paths, table = _stored(tmp_path, ["removed_zida_001", None])
    _patch_store(monkeypatch, paths, table)
    msg = FakeMessage(302)
    await wm_bot.clean_match_and_reply(msg, 7)
    (album,) = msg.albums
    assert [m.media.data for m in album] == [b"CLEAN0", b"ORIG1"]  # the original, untouched
    assert "Не удалось обработать: 1 из 2" in album[0].caption


async def test_every_photo_failing_gets_the_failure_text(monkeypatch, tmp_path):
    paths, table = _stored(tmp_path, [None, None])
    _patch_store(monkeypatch, paths, table)
    msg = FakeMessage(303)
    await wm_bot.clean_match_and_reply(msg, 7)
    assert msg.albums == [] and msg.texts[-1] == wm_bot.FAILED_MATCH
