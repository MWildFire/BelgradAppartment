"""W3-G1: LLM decision log (llm_validator) + `otz llm-report` analysis helpers."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import src.db.session as db_session
from src.cli import (
    _llm_borderline,
    _llm_histogram,
    _llm_threshold_counts,
    _load_llm_decisions,
    _resolve_llm_case_refs,
)
from src.db.models import InboundPost, Listing, Platform, SourceChannel
from src.matching import llm_validator
from src.matching.llm_validator import DuplicateCheck, validate_match

TS_FMT = "%Y-%m-%dT%H:%M:%S%z"


def _entry(conf: float, verdict: bool = True, *, days_ago: float = 0, **extra) -> dict:
    e = {
        "ts": (datetime.now(UTC) - timedelta(days=days_ago)).strftime(TS_FMT),
        "inbound_id": None,
        "listing_id": None,
        "inbound_ref": "5/777",
        "listing_ref": "NEKRETNINE/ext-1",
        "verdict": verdict,
        "confidence": conf,
        "latency_ms": 1200,
        "model": "claude-haiku-4-5",
    }
    e.update(extra)
    return e


def _write_jsonl(path: Path, entries: list[dict], *, garbage: bool = False) -> None:
    lines = [json.dumps(e, ensure_ascii=False) for e in entries]
    if garbage:
        lines += ["{not json", "", json.dumps({"confidence": 0.5})]  # no ts → skipped
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ report analysis helpers


def test_load_filters_by_window_and_skips_garbage(tmp_path):
    p = tmp_path / "llm_decisions.jsonl"
    _write_jsonl(
        p,
        [_entry(0.9, days_ago=0), _entry(0.8, days_ago=3), _entry(0.7, days_ago=10)],
        garbage=True,
    )

    entries = _load_llm_decisions(p, days=7)

    assert [e["confidence"] for e in entries] == [0.9, 0.8]
    assert _load_llm_decisions(tmp_path / "missing.jsonl", days=7) == []


def test_threshold_counts_gate_semantics():
    # Gate mirrors the matcher: pass = verdict True AND conf >= threshold (boundary inclusive).
    entries = [
        _entry(0.60),
        _entry(0.64),
        _entry(0.65),
        _entry(0.70),
        _entry(0.90),
        _entry(0.95, verdict=False),  # said different — never passes any threshold
        _entry(0.59),
    ]

    counts = _llm_threshold_counts(entries)

    assert counts == {0.6: 5, 0.65: 3, 0.7: 2}


def test_histogram_buckets_and_total():
    entries = [_entry(c) for c in (0.0, 0.05, 0.65, 0.7, 0.7, 1.0)]

    hist = _llm_histogram(entries)

    by_label = dict(hist)
    assert by_label["0.0–0.1"] == 2
    assert by_label["0.6–0.7"] == 1  # 0.65
    assert by_label["0.7–0.8"] == 2  # 0.70 lands in its own bucket, not the previous one
    assert by_label["0.9–1.0"] == 1  # conf=1.0 folds into the last bucket
    assert sum(n for _, n in hist) == len(entries)


def test_borderline_inclusive_range_any_verdict():
    entries = [
        _entry(0.54),
        _entry(0.55),
        _entry(0.66, verdict=False),
        _entry(0.75),
        _entry(0.76),
    ]

    cases = _llm_borderline(entries)

    assert [e["confidence"] for e in cases] == [0.55, 0.66, 0.75]  # sorted, boundaries in
    assert any(not e["verdict"] for e in cases)  # «different» verdicts also reviewed


async def test_resolve_refs_via_db_ids_and_raw_refs(db):
    async with db_session.get_session() as s:
        chan = SourceChannel(tg_id=-100123, username="srcchan")
        s.add(chan)
        await s.flush()
        ib = InboundPost(source_channel_id=chan.id, tg_message_id=777)
        listing = Listing(
            platform=Platform.NEKRETNINE,
            external_id="ext-1",
            url="https://example.com/NEKRETNINE/ext-1",
        )
        s.add_all([ib, listing])
        await s.flush()
        ib_id, listing_id, chan_id = ib.id, listing.id, chan.id

    by_ids = _entry(0.6, inbound_id=ib_id, listing_id=listing_id)
    by_refs = _entry(0.6, inbound_ref=f"{chan_id}/777", listing_ref="NEKRETNINE/ext-1")
    unknown = _entry(0.6, inbound_ref="999/1", listing_ref="NEKRETNINE/nope")

    refs = await _resolve_llm_case_refs([by_ids, by_refs, unknown])

    assert refs[0] == ("https://example.com/NEKRETNINE/ext-1", "https://t.me/srcchan/777")
    assert refs[1] == ("https://example.com/NEKRETNINE/ext-1", "https://t.me/srcchan/777")
    assert refs[2] == ("listing NEKRETNINE/nope", "inbound 999/1")  # raw fallback


# ------------------------------------------------------------------ decisions-log writing


def test_record_decision_writes_one_json_line(tmp_path, monkeypatch):
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    inbound = [tmp_path / "5" / "777" / "0.jpg"]
    listing = [tmp_path / "NEKRETNINE" / "ext-1" / "000.jpg"]

    llm_validator._record_decision(
        inbound_id=11,
        listing_id=22,
        inbound_photos=inbound,
        listing_photos=listing,
        verdict=True,
        confidence=0.66,
        latency_ms=345,
    )

    lines = (tmp_path / "logs" / "llm_decisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["inbound_id"] == 11
    assert entry["listing_id"] == 22
    assert entry["inbound_ref"] == "5/777"
    assert entry["listing_ref"] == "NEKRETNINE/ext-1"
    assert entry["verdict"] is True
    assert entry["confidence"] == 0.66
    assert entry["latency_ms"] == 345
    assert entry["model"] == "claude-haiku-4-5"  # exact model id, no date suffix
    datetime.strptime(entry["ts"], TS_FMT)  # parseable timestamp


def test_record_decision_failure_never_raises(monkeypatch):
    def boom():
        raise OSError("disk full")

    monkeypatch.setattr(llm_validator, "_decisions_log_path", boom)

    llm_validator._record_decision(  # must not raise
        inbound_id=None,
        listing_id=None,
        inbound_photos=[],
        listing_photos=[],
        verdict=False,
        confidence=0.1,
        latency_ms=1,
    )


# ------------------------------------------------------------------ validator → log wiring


@pytest.fixture
def fake_anthropic(monkeypatch):
    """Replace AsyncAnthropic with a canned-verdict stub; exception classes stay real."""
    import anthropic

    class _Messages:
        async def parse(self, **kwargs):
            class _Response:
                stop_reason = "end_turn"
                parsed_output = DuplicateCheck(
                    is_same_apartment=True, confidence=0.66, reasoning="ok"
                )

            return _Response()

    class _Client:
        def __init__(self, **kwargs):
            self.messages = _Messages()

        async def close(self):
            pass

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Client)


def _make_photos(tmp_path: Path) -> tuple[list[Path], list[Path]]:
    from PIL import Image

    sides = []
    for directory in (tmp_path / "inbound" / "5" / "777",
                      tmp_path / "listings" / "NEKRETNINE" / "ext-1"):
        directory.mkdir(parents=True, exist_ok=True)
        paths = []
        for index, color in enumerate(("red", "blue")):
            path = directory / f"{index}.jpg"
            Image.new("RGB", (32, 32), color).save(path)
            paths.append(path)
        sides.append(paths)
    return sides[0], sides[1]


async def test_validate_match_emits_decision_line(tmp_path, monkeypatch, fake_anthropic):
    from src.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    inbound, listing = _make_photos(tmp_path)

    result = await validate_match(inbound, listing, inbound_id=11, listing_id=22)

    assert result.is_same_apartment is True
    assert result.confidence == 0.66
    lines = (tmp_path / "logs" / "llm_decisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert (entry["inbound_id"], entry["listing_id"]) == (11, 22)
    assert entry["inbound_ref"] == "5/777"
    assert entry["listing_ref"] == "NEKRETNINE/ext-1"
    assert entry["verdict"] is True
    assert entry["confidence"] == 0.66
    assert entry["latency_ms"] >= 0
    assert entry["model"] == "claude-haiku-4-5"


async def test_validate_match_verdict_survives_log_failure(tmp_path, monkeypatch, fake_anthropic):
    from src.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    inbound, listing = _make_photos(tmp_path)

    def boom():
        raise OSError("disk full")

    monkeypatch.setattr(llm_validator, "_decisions_log_path", boom)

    result = await validate_match(inbound, listing)  # ids optional — caller unchanged

    assert result.is_same_apartment is True  # logging failure never breaks the verdict


# ------------------------------------------------------------------ matcher → validator wiring


async def test_matcher_llm_stage_passes_db_ids_to_decision_log(db, tmp_path, monkeypatch, fake_anthropic):
    """Stage-4 call site: _run_llm_validation forwards the REAL DB ids of the inbound and
    candidate listing into the decision log (not the None defaults)."""
    from src.config import get_settings
    from src.matching.matcher import _run_llm_validation

    settings = get_settings()
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "listings_photos_dir", tmp_path / "listings")
    monkeypatch.setattr(settings, "llm_validation_min_confidence", 0.5)  # stub conf=0.66 passes

    async with db_session.get_session() as s:
        chan = SourceChannel(tg_id=-100456, username="srcchan2")
        s.add(chan)
        await s.flush()
        inbound_photo = tmp_path / "inbound" / f"{chan.id}" / "778" / "0.jpg"
        inbound_photo.parent.mkdir(parents=True, exist_ok=True)
        inbound_photo.write_bytes(b"\xff\xd8fake-jpeg")
        inbound = InboundPost(
            source_channel_id=chan.id, tg_message_id=778, photo_paths=[str(inbound_photo)],
        )
        listing = Listing(
            platform=Platform.NEKRETNINE, external_id="ext-2",
            url="https://example.com/NEKRETNINE/ext-2",
        )
        s.add_all([inbound, listing])
        await s.flush()
        listing_photo = (
            tmp_path / "listings" / listing.platform.value / listing.external_id / "000.jpg"
        )
        listing_photo.parent.mkdir(parents=True, exist_ok=True)
        listing_photo.write_bytes(b"\xff\xd8fake-jpeg")

        from src.matching import evidence
        photos = _make_photos(tmp_path / "wiring")
        monkeypatch.setattr(evidence, "select_evidence", lambda *args: photos)
        ok = await _run_llm_validation(inbound, listing, settings)
        expected_ids = (inbound.id, listing.id)

    assert ok.accepted
    assert None not in expected_ids  # DB really assigned ids — guard against vacuous pass
    lines = (tmp_path / "logs" / "llm_decisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert (entry["inbound_id"], entry["listing_id"]) == expected_ids
