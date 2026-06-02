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
import logging
from pathlib import Path

from pydantic import BaseModel, Field

from src.config import get_settings

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5"
MAX_PHOTOS_PER_SIDE = 3  # cap to keep cost predictable
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
) -> DuplicateCheck:
    """Ask Claude whether two photo sets show the same apartment. Async.

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

    logger.info(
        "[llm_validator] verdict: same=%s conf=%.2f reasoning=%r",
        result.is_same_apartment, result.confidence, result.reasoning[:80],
    )
    return result
