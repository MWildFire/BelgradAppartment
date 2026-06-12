"""Vision-based duplicate detection via Claude API (Haiku 4.5).

After CLIP + text cross-check narrows the candidates, we ask Claude to look at 2-3 photos from
each side and confirm whether they show the same apartment. Catches the remaining false positives
where two unrelated apartments happen to have similar interiors (similar furniture, similar
viewpoints) — CLIP scores them high; Claude sees that the windows, layout, balcony, or fixtures
are clearly different.

Cost: ~$0.005-0.010 per validation (3 inbound photos + 3 listing photos at Haiku 4.5 rates).
"""

from __future__ import annotations

import base64
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from src.config import get_settings

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5"
MAX_PHOTOS_PER_SIDE = 3  # cap to keep cost predictable
# Single source for the decisions-log `ts` format — cli's `otz llm-report` parses with this.
LLM_DECISIONS_TS_FMT = "%Y-%m-%dT%H:%M:%S%z"
SYSTEM_PROMPT = """Ты — помощник, который проверяет, изображены ли на двух наборах фотографий одна и та же квартира в Белграде.

Контекст: пользователь отбирает квартиры из Telegram-каналов в свой канал. Алгоритм по pHash/CLIP нашёл потенциальное совпадение между Telegram-постом и объявлением на площадке. Тебе нужно подтвердить или опровергнуть.

ВАЖНО:
- На фото площадки может быть watermark платформы (nekretnine.rs / 4zida.rs / halooglasi.com) — игнорируй его.
- Один ракурс/похожая мебель/похожий стиль ≠ одна квартира. Смотри на конкретные детали: вид из окна, планировка комнат, расположение балкона, цвет стен, расположение розеток/выключателей, мебель.
- Если совпадает хотя бы 2 ярко выраженных детали (одинаковая мебель в одинаковых местах, одинаковый вид из окна, одинаковая планировка) — это одна квартира.
- Если только общее настроение похоже (бежевые стены, диван, белая кухня) — это РАЗНЫЕ квартиры. Будь строгим.

Возвращай JSON со структурой:
{"is_same_apartment": true|false, "confidence": 0.0-1.0, "reasoning": "1-2 короткие фразы на русском, что именно совпало или не совпало"}"""


class DuplicateCheck(BaseModel):
    """Schema enforced via output_config — Claude returns valid JSON matching this exact shape."""

    is_same_apartment: bool = Field(description="True only if you're confident the photos show the same apartment")
    confidence: float = Field(ge=0.0, le=1.0, description="Calibrated confidence 0..1")
    reasoning: str = Field(description="Brief explanation in Russian, 1-2 sentences")


class LLMValidatorError(RuntimeError):
    """Raised when the validator can't run (no API key, network failure, etc.)."""


def _decisions_log_path() -> Path:
    """data/logs/llm_decisions.jsonl — settings-relative, like photo_downloads.jsonl."""
    p = get_settings().data_dir / "logs" / "llm_decisions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _photo_parent_ref(photos: list[Path]) -> str | None:
    """'<grandparent>/<parent>' dirs of the first photo — 'NEKRETNINE/<external_id>' for listing
    photos, '<source_channel_id>/<tg_message_id>' for inbound ones. Best-effort review aid for
    runs where the caller didn't pass DB ids."""
    if not photos:
        return None
    p = photos[0]
    return f"{p.parent.parent.name}/{p.parent.name}"


def _record_decision(
    *,
    inbound_id: int | None,
    listing_id: int | None,
    inbound_photos: list[Path],
    listing_photos: list[Path],
    verdict: bool,
    confidence: float,
    latency_ms: int,
) -> None:
    """Append one JSON line per verdict to llm_decisions.jsonl (read by `otz llm-report`).

    MUST never raise: a full disk or broken log file must not kill the validator verdict.
    """
    try:
        entry = {
            "ts": datetime.now(UTC).strftime(LLM_DECISIONS_TS_FMT),
            "inbound_id": inbound_id,
            "listing_id": listing_id,
            "inbound_ref": _photo_parent_ref(inbound_photos),
            "listing_ref": _photo_parent_ref(listing_photos),
            "verdict": verdict,
            "confidence": round(confidence, 4),
            "latency_ms": latency_ms,
            "model": MODEL,
        }
        with _decisions_log_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        logger.warning("[llm_validator] decision log write failed", exc_info=True)


def _b64_image(path: Path, max_bytes: int = 4 * 1024 * 1024) -> tuple[str, str]:
    """Read an image, return (base64_data, media_type). Skips anything that's not a JPEG/PNG/WEBP."""
    suffix = path.suffix.lower()
    media_type = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix)
    if media_type is None:
        raise LLMValidatorError(f"Unsupported image extension: {path.name}")

    raw = path.read_bytes()
    if len(raw) > max_bytes:
        raise LLMValidatorError(f"Image too large ({len(raw)} bytes): {path.name}")
    return base64.standard_b64encode(raw).decode("ascii"), media_type


def _build_content(
    inbound_photos: list[Path],
    listing_photos: list[Path],
    *,
    inbound_text: str | None,
    listing_meta: dict[str, object],
) -> list[dict]:
    """Compose the multimodal user message — labelled photo grids on both sides plus metadata."""
    content: list[dict] = [
        {"type": "text", "text": "=== ФОТО ИЗ TELEGRAM-ПОСТА ===\n"},
    ]
    for i, p in enumerate(inbound_photos[:MAX_PHOTOS_PER_SIDE]):
        try:
            data, media_type = _b64_image(p)
        except LLMValidatorError:
            continue
        content.append({"type": "text", "text": f"Inbound фото #{i + 1}:"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        })

    if inbound_text:
        snippet = (inbound_text[:500] + "…") if len(inbound_text) > 500 else inbound_text
        content.append({"type": "text", "text": f"\nТекст inbound поста:\n{snippet}\n"})

    content.append({"type": "text", "text": "\n=== ФОТО С ПЛОЩАДКИ ===\n"})
    for i, p in enumerate(listing_photos[:MAX_PHOTOS_PER_SIDE]):
        try:
            data, media_type = _b64_image(p)
        except LLMValidatorError:
            continue
        content.append({"type": "text", "text": f"Listing фото #{i + 1}:"})
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        })

    meta_lines = []
    for key in ("platform", "price_eur", "area_m2", "rooms", "district", "address_raw", "title"):
        value = listing_meta.get(key)
        if value is None or value == "":
            continue
        meta_lines.append(f"  {key}: {value}")
    if meta_lines:
        content.append({
            "type": "text",
            "text": "\nДанные listing с площадки:\n" + "\n".join(meta_lines),
        })

    content.append({
        "type": "text",
        "text": "\n\nЭто одна и та же квартира? Верни JSON по схеме.",
    })
    return content


async def validate_match(
    inbound_photos: list[Path],
    listing_photos: list[Path],
    *,
    inbound_text: str | None = None,
    listing_meta: dict[str, object] | None = None,
    inbound_id: int | None = None,
    listing_id: int | None = None,
) -> DuplicateCheck:
    """Ask Claude whether two photo sets show the same apartment. Async.

    Each verdict is appended to data/logs/llm_decisions.jsonl (threshold-tuning evidence for
    `otz llm-report`); `inbound_id`/`listing_id` are optional log context — when absent, the
    photo-path-derived refs still identify the case.

    Raises LLMValidatorError on missing API key or transport failure.
    """
    # Read from pydantic-settings (which loads .env) rather than os.environ directly.
    api_key = getattr(get_settings(), "anthropic_api_key", None)
    if not api_key:
        raise LLMValidatorError("ANTHROPIC_API_KEY not set — LLM validator disabled")

    # Skip work if either side has zero usable photos.
    inbound_photos = [p for p in inbound_photos if p.exists()]
    listing_photos = [p for p in listing_photos if p.exists()]
    if not inbound_photos or not listing_photos:
        raise LLMValidatorError("No photos available on disk for one of the sides")

    # Lazy import — keeps anthropic out of the import graph when the feature is off.
    import asyncio as _asyncio

    import anthropic

    # max_retries=5 → SDK's built-in exponential backoff handles transient 429/5xx/529.
    # Default is 2 which gives up too fast under Anthropic-side overload.
    client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=5, timeout=60.0)
    content = _build_content(
        inbound_photos,
        listing_photos,
        inbound_text=inbound_text,
        listing_meta=listing_meta or {},
    )

    # On top of the SDK retries, our own loop specifically for 529 ("Overloaded") — these are
    # not rate limits, they're capacity events that often clear within 30-90 seconds.
    # The OverloadedError class isn't exported to the top-level `anthropic` namespace in all SDK
    # versions, so we discriminate by APIStatusError.status_code rather than `except OverloadedError`.
    response = None
    last_exc: Exception | None = None
    started = time.monotonic()
    for attempt in range(4):
        try:
            response = await client.messages.parse(
                model=MODEL,
                max_tokens=500,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
                output_format=DuplicateCheck,
            )
            break
        except anthropic.APIStatusError as e:
            if e.status_code == 529:
                last_exc = e
                wait = 10 * (attempt + 1) + (attempt * 5)  # 10s, 25s, 40s, 55s
                logger.info(
                    "[llm_validator] Anthropic overloaded (attempt %d/4) — waiting %ds",
                    attempt + 1, wait,
                )
                await _asyncio.sleep(wait)
                continue
            raise LLMValidatorError(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMValidatorError(f"Network error talking to Claude: {e}") from e
    if response is None:
        raise LLMValidatorError(
            f"Anthropic API still overloaded after 4 retries: {last_exc}"
        )

    result = response.parsed_output
    if result is None:
        raise LLMValidatorError("Claude returned no parsed_output — schema mismatch")

    # Verdict log for `otz llm-report` — never raises (see _record_decision).
    _record_decision(
        inbound_id=inbound_id,
        listing_id=listing_id,
        inbound_photos=inbound_photos,
        listing_photos=listing_photos,
        verdict=bool(result.is_same_apartment),
        confidence=float(result.confidence),
        latency_ms=int((time.monotonic() - started) * 1000),
    )

    logger.info(
        "[llm_validator] verdict: same=%s conf=%.2f reasoning=%r",
        result.is_same_apartment, result.confidence, result.reasoning[:80],
    )
    return result
