"""Live-smoke probe + structure-fingerprint drift detection (feature A2).

Catches a site's silent markup change within hours — before the whole feed rots — by four layers:
  1. list_recent yields >= MIN_PREVIEWS                       → hard-fail if not.
  2. a live detail carries every REQUIRED_DETAIL_FIELD        → hard-fail if not.
  3. the list page's structure fingerprint matches the last  → WARN (drift) if not.
     known-good baseline.
  4. any exception                                            → hard-fail with the error text.

A positive run is SILENT (the CLI only alerts on not-ok or drift). The drift baseline persists in
`data/state/parser_fingerprints.json` and is updated ONLY after an ok run, so a broken run never
poisons it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config import get_settings
from src.scrapers.base import Scraper

logger = logging.getLogger(__name__)

# How many of the first previews to probe before deciding the parser is broken. price/area can be
# legitimately absent on a single ad, so we accept detail-ok if ANY probed preview yields all
# required fields (photo_urls is almost always present on a live listing). A real markup break
# starves every probed listing of the fields and still trips it.
_DETAIL_PROBE_COUNT = 3


@dataclass
class SmokeResult:
    platform: str
    ok: bool
    n_previews: int
    detail_ok: bool
    missing_fields: list[str]
    drift: list[str]
    error: str | None


def compute_drift(old_fp: dict[str, Any] | None, new_fp: dict[str, Any]) -> list[str]:
    """Human-readable list of structural changes from old_fp → new_fp. Empty = no drift.

    A missing/empty old fingerprint (first run) is a baseline, not drift → []. We compare the
    `source` and the set-valued list keys (e.g. result_keys / item_keys / ad_keys / types); plain
    count fields like `n` are ignored — a changing ad count is not a structure change.
    """
    if not old_fp:
        return []
    drift: list[str] = []

    old_source = old_fp.get("source")
    new_source = new_fp.get("source")
    if old_source != new_source:
        drift.append(f"source changed: {old_source!r} → {new_source!r}")

    # Compare every list-valued key (the structural key-sets); union the keys across both fps so a
    # whole field appearing/disappearing is caught too.
    for key in sorted(set(old_fp) | set(new_fp)):
        if key in ("source", "n"):
            continue
        old_val = old_fp.get(key)
        new_val = new_fp.get(key)
        if not isinstance(old_val, list) and not isinstance(new_val, list):
            continue
        old_set = set(old_val) if isinstance(old_val, list) else set()
        new_set = set(new_val) if isinstance(new_val, list) else set()
        removed = sorted(old_set - new_set)
        added = sorted(new_set - old_set)
        if removed:
            drift.append(f"{key}: removed {removed}")
        if added:
            drift.append(f"{key}: added {added}")
    return drift


# --- baseline state persistence ---


def _state_path() -> Path:
    return get_settings().data_dir / "state" / "parser_fingerprints.json"


def _load_state() -> dict[str, dict[str, Any]]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("[smoke] could not read fingerprint state %s — treating as empty", path)
        return {}
    return data if isinstance(data, dict) else {}


def _save_good_fingerprint(platform_value: str, fingerprint: dict[str, Any]) -> None:
    """Persist the last-known-good fingerprint for one platform (merge into the existing file)."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    state = _load_state()
    state[platform_value] = fingerprint
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


async def _list_html_for_fingerprint(scraper: Scraper) -> str | None:
    """Raw list-page HTML for the structure fingerprint, by the cheapest route per scraper.

    httpx scrapers (nekretnine, 4zida) expose a cheap `_fetch_page`. The Playwright scraper
    (halooglasi) has none, so it stashes page-1 HTML on `_last_list_html` during list_recent — which
    run_smoke calls just before this, so it is freshly populated. Returns None if neither is
    available (fingerprint is then simply skipped — layers 1/2/4 still cover the platform).
    """
    fetch_page = getattr(scraper, "_fetch_page", None)
    if callable(fetch_page):
        return await fetch_page(scraper.list_url)
    return getattr(scraper, "_last_list_html", None)


async def run_smoke(scraper: Scraper) -> SmokeResult:
    """Probe one scraper's list+detail path and compare its structure to the last-known-good.

    Returns a SmokeResult. ok = (n_previews >= MIN_PREVIEWS) and detail_ok. Drift is WARN-only and
    does NOT set ok=False. Any exception → ok=False with the error text. The good fingerprint is
    persisted only when ok is True.
    """
    platform_value = scraper.platform.value
    # Hoisted so the outer except can report the TRUE preview count: a crash AFTER list_recent
    # succeeded (T1-3) must not masquerade as "previews=0" — the 13.07 nekretnine alert did exactly
    # that (list had 20 previews, a detail 403 blew up the probe, the alert claimed previews=0).
    n_previews = 0
    try:
        previews = await scraper.list_recent(max_pages=1)
        n_previews = len(previews)

        # --- detail probe: accept if any of the first few previews yields all required fields ---
        # T1-2: one preview's fetch_detail raising (e.g. a targeted 403 on a single ad) is NOT a
        # dead parser — catch it, remember the error, and try the next preview. Only if EVERY probed
        # preview fails do we report detail_ok=False with that last error (a real signal).
        detail_ok = False
        missing_fields: list[str] = []
        detail_error: str | None = None
        for prev in previews[:_DETAIL_PROBE_COUNT]:
            try:
                detail = await scraper.fetch_detail(prev.url)
            except Exception as e:  # noqa: BLE001 — one bad detail ≠ dead parser; probe the next
                detail_error = str(e) or type(e).__name__
                logger.warning(
                    "[smoke] %s detail probe %s failed: %s", platform_value, prev.url, e
                )
                continue
            this_missing = [
                f for f in scraper.REQUIRED_DETAIL_FIELDS if not getattr(detail, f, None)
            ]
            if not this_missing:
                detail_ok = True
                missing_fields = []
                detail_error = None  # a healthy detail clears the earlier probe failures
                break
            # Keep the LAST probe's misses for the alert (all probes failed → real signal).
            missing_fields = this_missing
        if not previews:
            detail_ok = False

        # --- fingerprint drift vs last-known-good (best-effort: NEVER fails the probe) ---
        # Layer 3 is WARN-only, so a hiccup acquiring the list HTML (e.g. a scraper without a cheap
        # `_fetch_page`) must not manufacture a not-ok alert from an otherwise-healthy list+detail.
        drift: list[str] = []
        new_fp: dict[str, Any] = {}
        try:
            list_html = await _list_html_for_fingerprint(scraper)
            if list_html:
                new_fp = scraper.fingerprint(list_html)
                if new_fp:
                    old_fp = _load_state().get(platform_value)
                    drift = compute_drift(old_fp, new_fp)
        except Exception:  # noqa: BLE001 — fingerprint is best-effort; layers 1/2/4 decide ok
            logger.warning(
                "[smoke] %s fingerprint step failed (non-fatal)", platform_value, exc_info=True
            )

        ok = (n_previews >= scraper.MIN_PREVIEWS) and detail_ok

        # Persist the good fingerprint ONLY on an ok run — never poison the baseline from a break.
        if ok and new_fp:
            _save_good_fingerprint(platform_value, new_fp)

        return SmokeResult(
            platform=platform_value,
            ok=ok,
            n_previews=n_previews,
            detail_ok=detail_ok,
            missing_fields=missing_fields,
            drift=drift,
            # T1-2: surface the last detail failure even when the probe didn't crash — a soft
            # all-details-403 must still tell the alert WHY detail_ok is False (was silently None).
            error=detail_error,
        )
    except Exception as e:  # noqa: BLE001 — any failure is a smoke failure worth alerting on
        logger.exception("[smoke] %s probe raised", platform_value)
        return SmokeResult(
            platform=platform_value,
            ok=False,
            n_previews=n_previews,  # T1-3: honest count — 0 only if list_recent itself never returned
            detail_ok=False,
            missing_fields=[],
            drift=[],
            error=str(e) or type(e).__name__,
        )
