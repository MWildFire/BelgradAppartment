"""Result texts of the /clean bot: what was removed, what was found but kept, what failed.

A photo ends in exactly one of five groups, so the groups always add up to the album size:
removed_only / kept_only / mixed / not_found / failed. Marks are counted separately from
photos. A mark is "kept" when the engine found it but left the pixels alone (method "skip");
the reason (`MarkResult.kept_reason`) is deliberately not shown to the user - every reason
reads the same. Pure functions: no bot, no model, no network.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from src.watermark.pipeline import CleanReport

PhotoStatus = Literal["removed_only", "kept_only", "mixed", "not_found", "failed"]
PHOTO_STATUSES: tuple[PhotoStatus, ...] = (
    "removed_only", "kept_only", "mixed", "not_found", "failed",
)

NOTHING_FOUND = "Знаков площадок не нашёл — фото без изменений."
DONE_HEAD = "🧽 Готово."
KEPT_HEAD = "Знак найден и оставлен без изменений."
FAILED_SENTENCE = "Не удалось обработать: {failed} из {total}"  # noqa: RUF001
ORIGINALS_NOTE = " (оригиналы без изменений)"
_PLATFORM_ORDER = ("Halo Oglasi", "4zida", "CityExpert")


@dataclass(frozen=True)
class PhotoOutcome:
    status: PhotoStatus
    removed: Mapping[str, int] = field(default_factory=dict)  # platform title -> marks removed
    kept: Mapping[str, int] = field(default_factory=dict)  # platform title -> marks kept
    original_kept: bool = False  # failed photo: its original goes back to the user


@dataclass(frozen=True)
class AlbumSummary:
    total: int
    photos: Mapping[PhotoStatus, int]
    marks_removed: int
    marks_kept: int
    text: str
    send_files: bool  # False: a text answer is enough, nothing new to send

    @property
    def marks_found(self) -> int:
        return self.marks_removed + self.marks_kept


def failed_outcome(*, original_kept: bool = True) -> PhotoOutcome:
    return PhotoOutcome("failed", original_kept=original_kept)


def outcome_from_report(report: CleanReport | None) -> PhotoOutcome:
    """None means the photo could not be processed."""
    if report is None:
        return failed_outcome(original_kept=False)
    from src.watermark.pipeline import PLATFORM_TITLES

    removed: Counter[str] = Counter()
    kept: Counter[str] = Counter()
    for mark in report.marks:
        title = PLATFORM_TITLES.get(mark.platform, mark.platform)
        (kept if mark.method == "skip" else removed)[title] += 1
    if removed and kept:
        status: PhotoStatus = "mixed"
    elif removed:
        status = "removed_only"
    elif kept:
        status = "kept_only"
    else:
        status = "not_found"
    return PhotoOutcome(status, dict(removed), dict(kept))


def _format_counts(counts: Mapping[str, int]) -> str:
    if not counts:
        return "0"
    ordered = [name for name in _PLATFORM_ORDER if name in counts]
    ordered += [name for name in counts if name not in _PLATFORM_ORDER]
    return ", ".join(f"{name} — {counts[name]}" for name in ordered)


def _sum_counts(parts: Sequence[Mapping[str, int]]) -> Counter[str]:
    total: Counter[str] = Counter()
    for part in parts:
        total.update(part)
    return total


def summarize_outcomes(outcomes: Sequence[PhotoOutcome]) -> AlbumSummary:
    total = len(outcomes)
    photos: dict[PhotoStatus, int] = dict.fromkeys(PHOTO_STATUSES, 0)
    for outcome in outcomes:
        photos[outcome.status] += 1
    removed = _sum_counts([o.removed for o in outcomes])
    kept = _sum_counts([o.kept for o in outcomes])
    marks_removed, marks_kept = sum(removed.values()), sum(kept.values())
    failed = photos["failed"]
    originals_back = sum(1 for o in outcomes if o.status == "failed" and o.original_kept)

    clauses: list[str] = []
    if marks_removed or marks_kept:
        clauses.append(f"удалено: {_format_counts(removed)}")
    if marks_kept:
        clauses.append(f"оставлено: {_format_counts(kept)}")
    if photos["not_found"] and (marks_removed or marks_kept or failed):
        clauses.append(f"без найденных знаков: {photos['not_found']} из {total} фото")

    if marks_removed:
        head = DONE_HEAD
    elif marks_kept:
        head = KEPT_HEAD
    else:
        head = ""
    sentences: list[str] = []
    if head:
        sentences.append(head)
    if clauses:
        body = "; ".join(clauses)
        sentences.append(body[:1].upper() + body[1:] + ".")
    if failed:
        note = ORIGINALS_NOTE if originals_back == failed else ""
        sentences.append(FAILED_SENTENCE.format(failed=failed, total=total) + note + ".")
    text = " ".join(sentences) if sentences else NOTHING_FOUND
    send_files = bool(photos["removed_only"] or photos["mixed"] or originals_back)
    return AlbumSummary(total, photos, marks_removed, marks_kept, text, send_files)
