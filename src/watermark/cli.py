"""`otz wm-*` commands; registered on the main `otz` group in src/cli.py.

Heavy imports (numpy/cv2/torch) stay inside the commands so other `otz` commands start fast.
"""

from __future__ import annotations

import asyncio
import csv
from pathlib import Path

import click

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".avif"}


def register_watermark_commands(group: click.Group) -> None:
    for command in (
        wm_clean, wm_qa, wm_clean_match, wm_revert, wm_fetch_model, wm_clean_published
    ):
        group.add_command(command)


def _report_line(name: str, report: object) -> str:
    from src.watermark.pipeline import CleanReport

    assert isinstance(report, CleanReport)
    if not report.changed:
        return f"{name}: no marks ({report.seconds:.2f}s)"
    parts = [f"{m.template_id}#{m.instance} score={m.score:.2f} {m.method}" for m in report.marks]
    return f"{name}: " + "; ".join(parts) + f" ({report.seconds:.2f}s)"


def _marks_key(report: object) -> str:
    """Stable text form of the found marks: "halo_corner#0;zida_logo#0" ("" when none)."""
    from src.watermark.pipeline import CleanReport

    assert isinstance(report, CleanReport)
    return ";".join(sorted(f"{m.template_id}#{m.instance}" for m in report.marks))


def _marks_sheet(before: object, after: object, report: object) -> object | None:
    """before|after crops around every found mark, stacked; None when nothing was found."""
    import numpy as np
    from PIL import Image

    from src.watermark.pipeline import CleanReport
    from src.watermark.templates import load_templates

    assert isinstance(report, CleanReport)
    assert isinstance(before, np.ndarray) and isinstance(after, np.ndarray)
    sizes = {t.id: (t.width, t.height) for t in load_templates()}
    h, w = before.shape[:2]
    rows = []
    for m in report.marks:
        tw, th = sizes.get(m.template_id, (0, 0))
        x0, y0 = max(0, int(m.x) - 12), max(0, int(m.y) - 12)
        x1, y1 = min(w, int(m.x + tw * m.scale) + 12), min(h, int(m.y + th * m.scale) + 12)
        if x1 <= x0 or y1 <= y0:
            continue
        gap = np.full((y1 - y0, 6, 3), 255, np.uint8)
        row = Image.fromarray(np.concatenate([before[y0:y1, x0:x1], gap, after[y0:y1, x0:x1]], 1))
        scale = min(300 / row.height, 1600 / row.width)
        rows.append(row.resize((round(row.width * scale), round(row.height * scale))))
    if not rows:
        return None
    sheet = Image.new("RGB", (max(r.width for r in rows), sum(r.height + 6 for r in rows)), "white")
    y = 0
    for row in rows:
        sheet.paste(row, (0, y))
        y += row.height + 6
    return sheet


def _read_expectations(path: Path) -> dict[str, str]:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if rows and not {"file", "marks"} <= set(rows[0]):
        raise click.ClickException(f"{path}: expected columns file,marks")
    return {row["file"]: ";".join(sorted(filter(None, row["marks"].split(";")))) for row in rows}


@click.command("wm-clean")
@click.argument(
    "paths", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--out-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Where to write <name>.clean.jpg (default: next to the source).",
)
def wm_clean(paths: tuple[Path, ...], out_dir: Path | None) -> None:
    """Remove platform marks from arbitrary files (sources are never modified)."""
    from src.watermark.pipeline import NotAnImageError, clean_file

    for src in paths:
        dst = (out_dir or src.parent) / f"{src.stem}.clean.jpg"
        try:
            report = clean_file(src, dst)
        except NotAnImageError as error:
            click.echo(f"{src}: not an image ({error})")
            continue
        suffix = f" -> {dst}" if report.changed else ""
        click.echo(_report_line(str(src), report) + suffix)


@click.command("wm-qa")
@click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--out-dir", type=click.Path(file_okay=False, path_type=Path), required=True)
@click.option(
    "--expect",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="CSV file,marks (marks like 'zida_logo#0;zida_logo#1'); exit 1 on any mismatch.",
)
def wm_qa(folder: Path, out_dir: Path, expect: Path | None) -> None:
    """Run over a folder: before|after pairs, zoomed mark crops and report.csv to review."""
    import numpy as np
    from PIL import Image

    from src.watermark.pipeline import NotAnImageError, clean_array, decode_image

    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[list[str]] = []
    actual: dict[str, str] = {}
    files = sorted(p for p in folder.rglob("*") if p.suffix.lower() in _IMAGE_SUFFIXES)
    for src in files:
        rel = src.relative_to(folder).as_posix()
        try:
            img = decode_image(src.read_bytes())
        except NotAnImageError:
            click.echo(f"{rel}: not an image, skipped")
            continue
        cleaned, report = clean_array(img)
        actual[rel] = _marks_key(report)
        gap = np.full((img.shape[0], 8, 3), 255, np.uint8)
        pair = np.concatenate([img, gap, cleaned], axis=1)
        flat = rel.replace("/", "__")
        Image.fromarray(pair).save(out_dir / f"{Path(flat).stem}.pair.jpg", quality=88)
        sheet = _marks_sheet(img, cleaned, report)
        if sheet is not None:
            sheet.save(out_dir / f"{Path(flat).stem}.marks.jpg", quality=90)  # type: ignore[attr-defined]
        if not report.marks:
            rows.append([rel, "", "", "", "", f"{report.seconds:.2f}"])
        for m in report.marks:
            rows.append(
                [rel, f"{m.template_id}#{m.instance}", f"{m.score:.3f}", m.method,
                 f"{m.residual:.3f}", f"{report.seconds:.2f}"]
            )
        click.echo(_report_line(rel, report))
    with (out_dir / "report.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["file", "mark", "score", "method", "residual", "seconds"])
        writer.writerows(rows)
    marked = sum(1 for key in actual.values() if key)
    click.echo(f"=== {len(actual)} images, marks in {marked}, report: {out_dir / 'report.csv'} ===")
    if expect is None:
        return
    problems = []
    for rel, wanted in sorted(_read_expectations(expect).items()):
        got = actual.get(rel)
        if got is None:
            problems.append(f"{rel}: missing from {folder}")
        elif got != wanted:
            problems.append(f"{rel}: expected [{wanted}] got [{got}]")
    for line in problems:
        click.echo(f"MISMATCH {line}")
    if problems:
        raise click.ClickException(f"QA failed: {len(problems)} mismatches")
    click.echo("QA expectations: all matched")


async def _match_photo_paths(match_id: int) -> list[str] | None:
    from src.db import get_session
    from src.db.models import InboundPost, Match

    async with get_session() as session:
        match = await session.get(Match, match_id)
        if match is None:
            return None
        inbound = await session.get(InboundPost, match.inbound_post_id)
        return list(inbound.photo_paths or []) if inbound is not None else []


@click.command("wm-clean-match")
@click.argument("match_id", type=int)
@click.option("--force", is_flag=True, help="Recompute even if the clean copy is current.")
def wm_clean_match(match_id: int, force: bool) -> None:
    """Make clean copies (data/clean/...) of one match's photos."""
    from src.watermark.store import ensure_clean

    paths = asyncio.run(_match_photo_paths(match_id))
    if paths is None:
        raise click.ClickException(f"Match {match_id} not found")
    for stored in paths:
        result = ensure_clean(stored, force=force)
        if result is None:
            click.echo(f"{stored}: file not on disk")
        elif result.clean is None:
            click.echo(f"{stored}: no marks")
        else:
            state = "reused" if result.reused else "cleaned"
            click.echo(f"{stored}: {state} -> {result.clean}")


@click.command("wm-revert")
@click.argument("match_id", type=int)
def wm_revert(match_id: int) -> None:
    """Go back to the original photos of one match (site variants are rebuilt from them).

    For a cleaned photo that looks wrong: removes its clean copies, remembers the decision
    (sweeps will not recreate them) and regenerates the site card/detail images.
    """
    from src.site.photos import VARIANT_WIDTHS, generate_for_stored_paths
    from src.watermark.store import discard, resolve_stored

    paths = asyncio.run(_match_photo_paths(match_id))
    if paths is None:
        raise click.ClickException(f"Match {match_id} not found")
    reverted = []
    for stored in paths:
        original = resolve_stored(stored)
        if original is None or not discard(original):
            click.echo(f"{stored}: skipped (not on disk or outside inbound)")
            continue
        for kind in VARIANT_WIDTHS:  # generated files; rebuilt right below from the original
            original.with_name(f"{original.name}.{kind}.webp").unlink(missing_ok=True)
        reverted.append(stored)
        click.echo(f"{stored}: original restored")
    written = generate_for_stored_paths(reverted) if reverted else 0
    click.echo(f"=== reverted {len(reverted)} photos, site variants rebuilt: {written} ===")


@click.command("wm-fetch-model")
@click.option("--dest", type=click.Path(dir_okay=False, path_type=Path), default=None)
def wm_fetch_model(dest: Path | None) -> None:
    """Download the LaMa model (~206 MB) and verify its sha256."""
    from src.config import get_settings
    from src.watermark.lama import MODEL_SHA256, download_model

    target = dest or Path(get_settings().watermark_lama_model_path)
    path = download_model(target)
    size = path.stat().st_size
    click.echo(f"LaMa: {path} ({size} bytes, sha256 {MODEL_SHA256[:12]}...)")


@click.command("wm-clean-published")
@click.option("--limit", type=int, default=None, help="Process at most N photos.")
@click.option("--force", is_flag=True, help="Recompute even current clean copies.")
@click.option("--dry-run", is_flag=True, help="Only count photos, write nothing.")
def wm_clean_published(limit: int | None, force: bool, dry_run: bool) -> None:
    """Clean copies for all public site listings + rebuild their webp variants.

    Variants are rebuilt only with WATERMARK_SITE_USE_CLEAN=1 (otherwise the site ignores
    clean copies). The bot daemon repeats this every 10 minutes for new publications; run it
    by hand once after enabling the flag. Reruns only process new or changed photos.
    """
    from src.config import get_settings
    from src.watermark.published import clean_batch, pending, published_photo_paths

    paths = asyncio.run(published_photo_paths())
    if not force:
        paths = pending(paths)
    if limit is not None:
        paths = paths[:limit]
    click.echo(f"wm-clean-published: {len(paths)} photos{' [DRY-RUN]' if dry_run else ''}")
    if dry_run:
        return
    changed = missing = 0
    for start in range(0, len(paths), 50):
        batch_changed, batch_missing = clean_batch(paths[start : start + 50], force=force)
        changed, missing = changed + batch_changed, missing + batch_missing
        click.echo(f"  ... {min(start + 50, len(paths))}/{len(paths)}")
    if get_settings().watermark_site_use_clean:
        from src.site.photos import generate_for_stored_paths

        written = generate_for_stored_paths(paths)
        click.echo(f"  site variants rebuilt: {written}")
    else:
        click.echo("  WATERMARK_SITE_USE_CLEAN=0: site variants left untouched")
    click.echo(f"=== DONE: with_marks={changed} missing={missing} total={len(paths)} ===")
