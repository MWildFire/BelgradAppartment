"""F1 — pure aggregation over the `leads` table: funnel + district/budget demand.

These helpers are deliberately pure (a list of `Lead` rows in, plain dicts/tuples out) so the
analytics logic is unit-tested without HTTP or a DB. The admin route and the CLI both consume
them. Nothing here writes — F1 is read-only aggregation over existing rows.

Funnel semantics: a Lead's `status` is its *current* state (new / contacted / closed), but a
funnel needs cumulative stage-reach. A `closed` lead was once `contacted` and once `new`; a
`contacted` lead was once `new`. So:
  - entered      = every lead
  - reached contact = #contacted + #closed
  - reached closed  = #closed
Conversion rates divide adjacent stages, with zero-division guarded to 0.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable, NamedTuple

# Current-status buckets we recognise on a Lead. Anything else still counts toward `total`
# (so the funnel never silently drops a row) but maps to no funnel stage.
KNOWN_STATUSES = ("new", "contacted", "closed")

# Separators a free-text `districts` value may use to list several districts in one string.
_DISTRICT_SEPARATORS = (",", ";", "/", "·", "|", "\n")


class BudgetBand(NamedTuple):
    label: str
    lo: int | None  # inclusive lower bound (None = open below)
    hi: int | None  # exclusive upper bound (None = open above)


# EUR/month bands. Lower bound inclusive, upper bound exclusive, so e.g. 600 lands in 600–800.
BUDGET_BANDS: tuple[BudgetBand, ...] = (
    BudgetBand("< 400", None, 400),
    BudgetBand("400–600", 400, 600),
    BudgetBand("600–800", 600, 800),
    BudgetBand("800–1000", 800, 1000),
    BudgetBand("1000+", 1000, None),
)

# Bucket for leads that submitted no budget — surfaced, not dropped (it's signal too).
BUDGET_NULL_LABEL = "не указан"


def _zero_div(num: float, den: float) -> float:
    return num / den if den else 0.0


def leads_funnel(leads: Iterable) -> dict[str, float | int]:
    """Funnel counts + conversion rates over current `status` values.

    Returns counts (`new`/`contacted`/`closed`/`total`) and rates:
      - rate_contacted            = reached-contact / total
      - rate_closed_of_contacted  = closed / reached-contact
      - rate_closed               = closed / total
    All rates are 0.0 when their denominator is 0 (no crash on an empty table).
    """
    counts = Counter((getattr(lead, "status", None) or "") for lead in leads)
    n_new = counts.get("new", 0)
    n_contacted = counts.get("contacted", 0)
    n_closed = counts.get("closed", 0)
    total = sum(counts.values())

    reached_contact = n_contacted + n_closed
    return {
        "new": n_new,
        "contacted": n_contacted,
        "closed": n_closed,
        "total": total,
        "rate_contacted": _zero_div(reached_contact, total),
        "rate_closed_of_contacted": _zero_div(n_closed, reached_contact),
        "rate_closed": _zero_div(n_closed, total),
    }


def _split_districts(value: str | None) -> list[str]:
    """Explode a free-text `districts` string into individual, trimmed district names."""
    if not value:
        return []
    parts = [value]
    for sep in _DISTRICT_SEPARATORS:
        parts = [piece for chunk in parts for piece in chunk.split(sep)]
    return [p.strip() for p in parts if p.strip()]


def district_demand(leads: Iterable, *, top: int = 30) -> list[tuple[str, int]]:
    """Top-N requested districts with counts, descending by count then name.

    Multi-valued `districts` strings are exploded so each named district scores once per lead.
    Null/blank values contribute nothing.
    """
    counts: Counter[str] = Counter()
    for lead in leads:
        for district in _split_districts(getattr(lead, "districts", None)):
            counts[district] += 1
    # Sort by count desc, then name asc for a stable, deterministic order.
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return ordered[:top]


def _band_for(budget: int | None) -> str:
    if budget is None:
        return BUDGET_NULL_LABEL
    for band in BUDGET_BANDS:
        if (band.lo is None or budget >= band.lo) and (band.hi is None or budget < band.hi):
            return band.label
    return BUDGET_NULL_LABEL  # unreachable (bands cover the whole line), defensive only


def budget_demand(leads: Iterable) -> list[tuple[str, int]]:
    """Lead counts per budget band, in canonical band order, then the «не указан» bucket.

    Every band is always present (count 0 if empty) so the report has a stable shape.
    """
    counts: Counter[str] = Counter()
    for lead in leads:
        counts[_band_for(getattr(lead, "budget_eur", None))] += 1
    rows = [(band.label, counts.get(band.label, 0)) for band in BUDGET_BANDS]
    rows.append((BUDGET_NULL_LABEL, counts.get(BUDGET_NULL_LABEL, 0)))
    return rows


def build_leads_report(leads: Iterable, *, top_districts: int = 30) -> dict:
    """One-shot bundle of all three aggregates — what the route/CLI render."""
    leads = list(leads)
    return {
        "funnel": leads_funnel(leads),
        "districts": district_demand(leads, top=top_districts),
        "budgets": budget_demand(leads),
    }


def _pct(rate: float) -> str:
    return f"{rate * 100:.1f}%"


def format_leads_report(report: dict) -> str:
    """Render the report bundle as plain readable tables for the CLI."""
    f = report["funnel"]
    lines: list[str] = []
    lines.append("=== Воронка лидов ===")
    lines.append(f"  new (всего лидов):     {f['total']}")
    lines.append(
        f"  дошли до contacted:    {f['contacted'] + f['closed']}"
        f"   ({_pct(f['rate_contacted'])} от всех)"
    )
    lines.append(
        f"  closed:                {f['closed']}"
        f"   ({_pct(f['rate_closed_of_contacted'])} от contacted, "
        f"{_pct(f['rate_closed'])} от всех)"
    )

    lines.append("")
    lines.append("=== Спрос по районам (top) ===")
    if report["districts"]:
        for name, count in report["districts"]:
            lines.append(f"  {count:>4d}  {name}")
    else:
        lines.append("  (районы пока не указывались)")

    lines.append("")
    lines.append("=== Спрос по бюджету (€/мес) ===")
    for label, count in report["budgets"]:
        lines.append(f"  {count:>4d}  {label}")

    return "\n".join(lines)
