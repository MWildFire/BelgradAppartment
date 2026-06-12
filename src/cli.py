"""Command-line entry point: `otz <command>`."""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone

import click
from sqlalchemy import select

from src.config import get_settings
from src.db.models import InboundPost, Listing, Platform, SourceChannel
from src.scrapers import SCRAPERS
from src.scrapers.orchestrator import ScrapeResult, scrape_platform


def _setup_logging() -> None:
    level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )


@click.group()
def cli() -> None:
    """otz-rent-finder CLI."""
    _setup_logging()


PLATFORM_NAMES = {p.value.lower(): p for p in Platform}


@cli.command("scrape-all")
@click.option("--max-pages", default=None, type=int,
              help="Page cap per platform. Default: 3 (quick pass), or 500 with --full.")
@click.option("--max-new", default=None, type=int, help="Cap newly inserted listings per platform.")
@click.option("--no-photos", is_flag=True, help="Skip downloading photos (faster, no pHash).")
@click.option(
    "--platform",
    "platforms",
    multiple=True,
    type=click.Choice(list(PLATFORM_NAMES.keys()), case_sensitive=False),
    help="Restrict to one or more platforms. Default: all.",
)
@click.option(
    "--full",
    is_flag=True,
    help="Full market walk: no incremental early-stop AND a deep page limit (enables the "
         "price-band segmentation that defeats the sites' ~2000-result caps).",
)
def scrape_all(
    max_pages: int | None,
    max_new: int | None,
    no_photos: bool,
    platforms: tuple[str, ...],
    full: bool,
) -> None:
    """Run one scraping pass across all platforms (or a subset)."""
    selected: list[Platform]
    if platforms:
        selected = [PLATFORM_NAMES[p.lower()] for p in platforms]
    else:
        selected = list(SCRAPERS.keys())

    # `--full` with the old quick-pass default of 3 pages silently skipped segmentation and
    # walked 3 pages "fully" — a footgun. Full now implies deep unless explicitly overridden.
    if max_pages is None:
        max_pages = 500 if full else 3

    results = asyncio.run(
        _run_all(selected, max_pages, max_new, not no_photos, incremental=not full)
    )
    _print_summary(results)


@cli.command("scrape")
@click.argument("platform", type=click.Choice(list(PLATFORM_NAMES.keys()), case_sensitive=False))
@click.option("--max-pages", default=3, show_default=True, type=int)
@click.option("--max-new", default=None, type=int)
@click.option("--no-photos", is_flag=True)
def scrape_one(platform: str, max_pages: int, max_new: int | None, no_photos: bool) -> None:
    """Run one scraping pass for a single platform."""
    plat = PLATFORM_NAMES[platform.lower()]
    results = asyncio.run(_run_all([plat], max_pages, max_new, not no_photos))
    _print_summary(results)


async def _run_all(
    platforms: list[Platform],
    max_pages: int,
    max_new: int | None,
    download_photos: bool,
    *,
    incremental: bool = True,
) -> list[ScrapeResult]:
    results: list[ScrapeResult] = []
    for plat in platforms:
        scraper_cls = SCRAPERS[plat]
        # `async with` stays INSIDE the try: an __aenter__ failure (e.g. halooglasi's Playwright
        # browser refusing to launch) must skip that one platform, not crash the whole run.
        try:
            async with scraper_cls() as scraper:
                result = await scrape_platform(
                    scraper,
                    max_pages=max_pages,
                    max_new=max_new,
                    download_photos=download_photos,
                    incremental=incremental,
                )
        except NotImplementedError as e:
            click.echo(f"[{plat.value}] skipped: {e}", err=True)
            continue
        except Exception:
            logging.getLogger(__name__).exception("[%s] scrape failed", plat.value)
            continue
        results.append(result)
    return results


def _print_summary(results: list[ScrapeResult]) -> None:
    click.echo("")
    click.echo("=== Scrape summary ===")
    for r in results:
        click.echo(
            f"  {r.platform.value:14} previewed={r.previewed:4d}  "
            f"new={r.inserted:4d}  refreshed={r.updated:4d}  "
            f"photos={r.photos_downloaded:4d}  errors={r.errors}"
        )


# --------------------------------------------------------------------------- Phase 3: Telegram


@cli.command("auth")
def auth() -> None:
    """One-off Telethon authentication — creates the .session file (interactive)."""
    asyncio.run(_auth())


async def _auth() -> None:
    from src.telegram.monitor import make_client

    client = make_client()
    settings = get_settings()
    phone = settings.tg_phone or None
    await client.start(phone=phone or click.prompt("Phone (international format)", type=str))
    me = await client.get_me()
    click.echo(
        f"Authenticated as: {me.first_name} (id={me.id}, username=@{me.username})"
    )
    await client.disconnect()


@cli.command("add-source")
@click.argument("identifier")
@click.option(
    "--start-date",
    default=None,
    help="ISO date to backfill from (YYYY-MM-DD). Default: SOURCE_DEFAULT_START_DATE from .env.",
)
def add_source(identifier: str, start_date: str | None) -> None:
    """Resolve a Telegram channel (URL, @username, or username) and add it to the source list."""
    asyncio.run(_add_source(identifier, start_date))


async def _add_source(identifier: str, start_date: str | None) -> None:
    from src.telegram.monitor import add_source, make_client

    sd: datetime | None = None
    if start_date:
        sd = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)

    client = make_client()
    await client.start()
    try:
        row = await add_source(client, identifier, start_date=sd)
        click.echo(
            f"Source channel saved: id={row.id} tg_id={row.tg_id} "
            f"username=@{row.username} title={row.title!r} start_date={row.start_date}"
        )
    finally:
        await client.disconnect()


@cli.command("stats")
def stats() -> None:
    """One-screen overview: listings per platform, inbounds per status, matches per status."""
    asyncio.run(_stats())


async def _stats() -> None:
    from sqlalchemy import func

    from src.db.models import (
        InboundPost,
        InboundStatus,
        Listing,
        Match,
        MatchStatus,
        PublishedPost,
        SourceChannel,
    )
    from src.db.session import get_session

    async with get_session() as s:
        click.echo("=== Listings ===")
        for p in Platform:
            total = (
                await s.execute(select(func.count(Listing.id)).where(Listing.platform == p))
            ).scalar_one()
            with_ph = (
                await s.execute(
                    select(func.count(Listing.id)).where(
                        Listing.platform == p, Listing.photo_phashes != "[]"
                    )
                )
            ).scalar_one()
            active = (
                await s.execute(
                    select(func.count(Listing.id)).where(
                        Listing.platform == p, Listing.is_active.is_(True)
                    )
                )
            ).scalar_one()
            click.echo(
                f"  {p.value:14}  {total:4d} total  "
                f"{active:4d} active  {with_ph:4d} with pHash"
            )

        click.echo("\n=== Inbound posts ===")
        for status in InboundStatus:
            n = (
                await s.execute(
                    select(func.count(InboundPost.id)).where(InboundPost.status == status)
                )
            ).scalar_one()
            click.echo(f"  {status.value:12}  {n}")

        click.echo("\n=== Matches ===")
        for status in MatchStatus:
            n = (
                await s.execute(select(func.count(Match.id)).where(Match.status == status))
            ).scalar_one()
            click.echo(f"  {status.value:12}  {n}")

        click.echo("\n=== Published ===")
        n = (await s.execute(select(func.count(PublishedPost.id)))).scalar_one()
        click.echo(f"  total: {n}")

        click.echo("\n=== Sources ===")
        rows = (
            await s.execute(select(SourceChannel).order_by(SourceChannel.id))
        ).scalars().all()
        for r in rows:
            click.echo(
                f"  #{r.id} @{r.username} active={r.is_active}  "
                f"start={r.start_date.date() if r.start_date else '—'}  "
                f"last_msg_id={r.last_processed_message_id or '—'}"
            )


@cli.command("list-sources")
def list_sources() -> None:
    """Print all source channels in the DB."""
    asyncio.run(_list_sources())


async def _list_sources() -> None:
    from src.db.session import get_session

    async with get_session() as s:
        rows = (await s.execute(select(SourceChannel).order_by(SourceChannel.id))).scalars().all()
    if not rows:
        click.echo("(no sources — run `otz add-source <username>`)")
        return
    for r in rows:
        click.echo(
            f"  [{r.id}] @{r.username} tg_id={r.tg_id} active={r.is_active}  "
            f"start={r.start_date}  last_msg={r.last_processed_message_id}  "
            f"last_at={r.last_processed_at}"
        )


@cli.command("backfill")
@click.option("--source", "source_id", type=int, default=None, help="One source by DB id; default: all active.")
@click.option("--limit", type=int, default=None, help="Cap number of posts processed.")
@click.option("--no-match", is_flag=True, help="Skip running matcher after each insert.")
@click.option(
    "--from-date",
    "from_date",
    default=None,
    help="ISO date — override watermark/start_date and replay from this point (one-shot).",
)
def backfill(
    source_id: int | None, limit: int | None, no_match: bool, from_date: str | None
) -> None:
    """Backfill historical posts from each active source channel."""
    asyncio.run(_backfill(source_id, limit, not no_match, from_date))


async def _backfill(
    source_id: int | None,
    limit: int | None,
    run_matcher: bool,
    from_date: str | None,
) -> None:
    from src.db.session import get_session
    from src.jobs.pipeline import process_new_inbound
    from src.telegram.monitor import backfill as do_backfill
    from src.telegram.monitor import make_client

    override_date: datetime | None = None
    if from_date:
        override_date = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)

    async with get_session() as s:
        stmt = select(SourceChannel).where(SourceChannel.is_active.is_(True))
        if source_id is not None:
            stmt = stmt.where(SourceChannel.id == source_id)
        sources = (await s.execute(stmt)).scalars().all()
        if override_date is not None:
            for src in sources:
                src.start_date = override_date
                src.last_processed_message_id = None

    if not sources:
        click.echo("No active sources to backfill.", err=True)
        return

    import time as _time

    from src.telegram.delivery import send_run_summary

    client = make_client()
    await client.start()

    # Track per-run statistics so we can send Дарья a single summary message at the end.
    run_stats = {"matched": 0, "no_match": 0, "errors": 0}

    async def on_inbound(inbound_id: int) -> None:
        try:
            result = await process_new_inbound(inbound_id, client=client)
            if result.status == "matched":
                run_stats["matched"] += 1
            else:
                run_stats["no_match"] += 1
        except Exception:
            run_stats["errors"] += 1
            logging.getLogger(__name__).exception("inbound=%s pipeline failed", inbound_id)

    if not run_matcher:
        on_inbound = None  # type: ignore[assignment]

    started = _time.monotonic()
    try:
        for src in sources:
            click.echo(
                f"→ Backfilling [{src.id}] @{src.username} from "
                f"{src.last_processed_message_id or src.start_date}"
            )
            stats = await do_backfill(client, src, limit=limit, on_inbound=on_inbound)
            click.echo(
                f"  done: processed={stats.processed} inserted={stats.posts_inserted} "
                f"skipped_no_photo={stats.skipped_no_photo}"
            )

        if run_matcher and any(run_stats.values()):
            elapsed = _time.monotonic() - started
            try:
                await send_run_summary(
                    client,
                    matched=run_stats["matched"],
                    no_match=run_stats["no_match"],
                    errors=run_stats["errors"],
                    duration_seconds=elapsed,
                    label="backfill",
                )
            except Exception:
                logging.getLogger(__name__).exception("run summary delivery failed")
    finally:
        await client.disconnect()


@cli.command("reset-source")
@click.argument("source_id", type=int)
@click.option("--start-date", "start_date", default=None, help="Set new start_date (YYYY-MM-DD).")
def reset_source(source_id: int, start_date: str | None) -> None:
    """Clear watermark on a source so backfill replays history from start_date again."""
    asyncio.run(_reset_source(source_id, start_date))


async def _reset_source(source_id: int, start_date: str | None) -> None:
    from src.db.session import get_session

    async with get_session() as s:
        row = await s.get(SourceChannel, source_id)
        if row is None:
            click.echo(f"Source #{source_id} not found", err=True)
            return
        row.last_processed_message_id = None
        if start_date:
            row.start_date = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
        click.echo(
            f"reset: source #{source_id} @{row.username} "
            f"start_date={row.start_date} watermark cleared"
        )


@cli.command("monitor")
@click.option("--no-backfill", is_flag=True, help="Skip initial backfill — go straight to live.")
def monitor(no_backfill: bool) -> None:
    """Run backfill + live NewMessage handler. Runs forever until interrupted."""
    asyncio.run(_monitor(not no_backfill))


async def _monitor(do_backfill_first: bool) -> None:
    from src.db.models import InboundPost, InboundStatus
    from src.db.session import get_session
    from src.jobs.pipeline import process_new_inbound
    from src.telegram.monitor import (
        backfill as run_backfill,
        make_client,
        register_live_handler,
    )

    async with get_session() as s:
        sources = (
            await s.execute(
                select(SourceChannel).where(SourceChannel.is_active.is_(True))
            )
        ).scalars().all()

    if not sources:
        click.echo("No active sources — run `otz add-source <username>` first.", err=True)
        return

    client = make_client()
    await client.start()

    # Single client shared between live handler, backfill, and Saved-Messages delivery.
    async def on_inbound(inbound_id: int) -> None:
        await process_new_inbound(inbound_id, client=client)

    # Register the live handler upfront so messages arriving during backfill/recovery are
    # buffered into album groups instead of being silently dropped.
    register_live_handler(client, sources, on_inbound=on_inbound)

    try:
        if do_backfill_first:
            for src in sources:
                click.echo(f"→ Catching up [{src.id}] @{src.username}...")
                stats = await run_backfill(client, src, on_inbound=on_inbound)
                click.echo(
                    f"  caught up: inserted={stats.posts_inserted} skipped={stats.skipped_no_photo}"
                )

            # Stranded NEW recovery: previous runs (e.g. killed by OOM mid-pipeline) can leave
            # InboundPost rows in NEW status with the watermark already advanced past them, so
            # backfill won't revisit them. Run the pipeline on whatever's left so they don't sit
            # forever. Idempotent: process_new_inbound is a no-op once status flips off NEW.
            async with get_session() as s:
                stranded = list(
                    (
                        await s.execute(
                            select(InboundPost.id)
                            .where(InboundPost.status == InboundStatus.NEW)
                            .order_by(InboundPost.received_at)
                        )
                    ).scalars().all()
                )
            if stranded:
                import time as _time

                from src.db.models import InboundStatus as _IS
                from src.telegram.delivery import send_run_summary, send_text_to_recipients

                click.echo(f"→ Catching up {len(stranded)} stranded NEW post(s)...")
                # Tell recipients we're catching up — silence here is the symptom Дарья wants
                # to spot before scrolling the daily summary.
                try:
                    await send_text_to_recipients(
                        client,
                        f"🔄 Recovery: догоняем {len(stranded)} зависших NEW пост(ов) "
                        "из предыдущего прогона. Через несколько минут пришлю итог.",
                    )
                except Exception:
                    logging.getLogger(__name__).exception("recovery start notify failed")

                started = _time.monotonic()
                recovery_stats = {"matched": 0, "no_match": 0, "errors": 0}
                for iid in stranded:
                    try:
                        await on_inbound(iid)
                    except Exception:
                        recovery_stats["errors"] += 1
                        logging.getLogger(__name__).exception(
                            "stranded inbound=%s pipeline failed", iid
                        )
                # Re-read final statuses so the summary reflects what actually landed.
                async with get_session() as s:
                    for iid in stranded:
                        post = await s.get(InboundPost, iid)
                        if post is None:
                            continue
                        if post.status == _IS.MATCHED:
                            recovery_stats["matched"] += 1
                        elif post.status == _IS.NO_MATCH:
                            recovery_stats["no_match"] += 1
                try:
                    await send_run_summary(
                        client,
                        matched=recovery_stats["matched"],
                        no_match=recovery_stats["no_match"],
                        errors=recovery_stats["errors"],
                        duration_seconds=_time.monotonic() - started,
                        label=f"Recovery после рестарта ({len(stranded)} зависших)",
                    )
                except Exception:
                    logging.getLogger(__name__).exception("recovery summary delivery failed")

        click.echo("Live monitor running — Ctrl+C to stop.")
        await client.run_until_disconnected()
    finally:
        await client.disconnect()


@cli.command("match")
@click.argument("inbound_id", type=int)
def match_one(inbound_id: int) -> None:
    """Run pHash matching for a single inbound post."""
    asyncio.run(_match_one(inbound_id))


async def _match_one(inbound_id: int) -> None:
    from src.matching.matcher import match_inbound

    result = await match_inbound(inbound_id)
    click.echo(
        f"inbound={result.inbound_id}  listing_id={result.listing_id}  "
        f"matched_photos={result.matched_photos}  confidence={result.confidence:.2f}  "
        f"status={result.status}"
    )


@cli.command("match-all")
def match_all() -> None:
    """Run pHash matching for every InboundPost with status=NEW."""
    asyncio.run(_match_all())


@cli.command("rematch-all")
@click.option(
    "--hamming",
    type=int,
    default=None,
    help="Temporary override of PHASH_MAX_HAMMING for this run (try 10–14 on cold start).",
)
@click.option(
    "--keep-published/--reset-published",
    default=True,
    show_default=True,
    help="Keep PUBLISHED matches (and their inbounds) untouched; --reset-published wipes "
         "ALL matches including published history and re-matches everything.",
)
def rematch_all(hamming: int | None, keep_published: bool) -> None:
    """Reset all PENDING/REJECTED/NO_MATCH state and re-run matcher from scratch.

    Useful when threshold is too tight on cold-start or after adding many new listings.
    """
    asyncio.run(_rematch_all(hamming, keep_published))


async def _rematch_all(hamming: int | None, keep_published: bool) -> None:
    from sqlalchemy import delete, update

    from src.db.models import InboundPost, InboundStatus, Match, MatchStatus
    from src.db.session import get_session

    settings = get_settings()
    if hamming is not None:
        settings.phash_max_hamming = hamming
        click.echo(f"→ Using Hamming threshold = {hamming} for this run only.")

    async with get_session() as s:
        if keep_published:
            # Delete only non-published Match rows; preserve history.
            await s.execute(
                delete(Match).where(Match.status != MatchStatus.PUBLISHED)
            )
            # Reset inbound status — but never touch ones with a PublishedPost.
            published_ib_ids = set(
                (await s.execute(
                    select(InboundPost.id)
                    .join(Match, Match.inbound_post_id == InboundPost.id)
                    .where(Match.status == MatchStatus.PUBLISHED)
                )).scalars().all()
            )
            if published_ib_ids:
                await s.execute(
                    update(InboundPost)
                    .where(~InboundPost.id.in_(published_ib_ids))
                    .values(status=InboundStatus.NEW)
                )
            else:
                await s.execute(update(InboundPost).values(status=InboundStatus.NEW))
        else:
            click.echo("⚠️  --reset-published: wiping ALL matches including published history")
            await s.execute(delete(Match))
            await s.execute(update(InboundPost).values(status=InboundStatus.NEW))
    click.echo("→ State reset. Running matcher...")

    import time as _time

    from src.matching.matcher import match_all_pending
    from src.telegram.delivery import send_run_summary

    started = _time.monotonic()
    results = await match_all_pending()
    elapsed = _time.monotonic() - started
    matched = sum(1 for r in results if r.status == "matched")
    no_match = len(results) - matched
    click.echo(f"\n=== matched {matched} / {len(results)} ===")
    for r in results:
        marker = "✓" if r.status == "matched" else "·"
        click.echo(
            f"  {marker} inbound={r.inbound_id}  listing={r.listing_id}  "
            f"matched_photos={r.matched_photos}  conf={r.confidence:.2f}"
        )

    if results:
        client = await _open_userbot_if_needed()
        try:
            hamming_note = f" (Hamming≤{hamming})" if hamming is not None else ""
            await send_run_summary(
                client,
                matched=matched,
                no_match=no_match,
                duration_seconds=elapsed,
                label=f"rematch-all{hamming_note}",
            )
        except Exception:
            logging.getLogger(__name__).exception("run summary delivery failed")
        finally:
            if client is not None:
                await client.disconnect()


async def _match_all() -> None:
    import time as _time

    from src.matching.matcher import match_all_pending
    from src.telegram.delivery import send_run_summary

    started = _time.monotonic()
    results = await match_all_pending()
    elapsed = _time.monotonic() - started
    matched = sum(1 for r in results if r.status == "matched")
    no_match = len(results) - matched
    click.echo(f"\n=== matched {matched} / {len(results)} ===")
    for r in results:
        marker = "✓" if r.status == "matched" else "·"
        click.echo(
            f"  {marker} inbound={r.inbound_id}  listing={r.listing_id}  "
            f"matched_photos={r.matched_photos}  conf={r.confidence:.2f}"
        )

    if results:
        client = await _open_userbot_if_needed()
        try:
            await send_run_summary(
                client,
                matched=matched,
                no_match=no_match,
                duration_seconds=elapsed,
                label="match-all",
            )
        except Exception:
            logging.getLogger(__name__).exception("run summary delivery failed")
        finally:
            if client is not None:
                await client.disconnect()


@cli.command("embed-all")
@click.option(
    "--target",
    type=click.Choice(["listings", "inbound", "both"], case_sensitive=False),
    default="both",
    show_default=True,
)
@click.option("--force", is_flag=True, help="Recompute even if embeddings already exist.")
def embed_all(target: str, force: bool) -> None:
    """Compute CLIP embeddings for all rows with on-disk photos but no embeddings yet."""
    asyncio.run(_embed_all(target.lower(), force))


async def _embed_all(target: str, force: bool) -> None:
    from pathlib import Path

    from src.db.models import InboundPost, Listing
    from src.db.session import get_session
    from src.matching.clip_embed import compute_embeddings_batch, pack_embeddings

    if target in ("listings", "both"):
        async with get_session() as s:
            rows = (await s.execute(select(Listing).where(Listing.is_active.is_(True)))).scalars().all()
            ids_to_do = [r.id for r in rows if force or (not r.photo_embeddings and r.photo_phashes)]
        click.echo(f"→ {len(ids_to_do)} listings need CLIP embeddings")
        settings = get_settings()
        for i, lid in enumerate(ids_to_do, 1):
            async with get_session() as s:
                listing = await s.get(Listing, lid)
                if listing is None:
                    continue
                target_dir = (
                    settings.listings_photos_dir / listing.platform.value / listing.external_id
                )
                files = sorted(target_dir.glob("*.*")) if target_dir.exists() else []
                if not files:
                    click.echo(f"  [{i}/{len(ids_to_do)}] listing #{lid}: no files on disk — skip")
                    continue
                try:
                    vecs = await asyncio.to_thread(compute_embeddings_batch, list(files), batch_size=4)
                except Exception as e:
                    click.echo(f"  [{i}/{len(ids_to_do)}] listing #{lid}: FAILED {e}", err=True)
                    continue
                listing.photo_embeddings = pack_embeddings(vecs)
                click.echo(f"  [{i}/{len(ids_to_do)}] listing #{lid} ({listing.platform.value}/{listing.external_id}): {len(vecs)} embeddings")

    if target in ("inbound", "both"):
        async with get_session() as s:
            rows = (await s.execute(select(InboundPost))).scalars().all()
            ids_to_do = [r.id for r in rows if force or (not r.photo_embeddings and r.photo_paths)]
        click.echo(f"→ {len(ids_to_do)} inbound need CLIP embeddings")
        for i, iid in enumerate(ids_to_do, 1):
            async with get_session() as s:
                ib = await s.get(InboundPost, iid)
                if ib is None:
                    continue
                files = [Path(p) for p in ib.photo_paths if Path(p).exists()]
                if not files:
                    click.echo(f"  [{i}/{len(ids_to_do)}] inbound #{iid}: no files on disk — skip")
                    continue
                try:
                    vecs = await asyncio.to_thread(compute_embeddings_batch, files, batch_size=4)
                except Exception as e:
                    click.echo(f"  [{i}/{len(ids_to_do)}] inbound #{iid}: FAILED {e}", err=True)
                    continue
                ib.photo_embeddings = pack_embeddings(vecs)
                click.echo(f"  [{i}/{len(ids_to_do)}] inbound #{iid}: {len(vecs)} embeddings")


@cli.command("hash-listings")
@click.option(
    "--platform",
    "platforms",
    multiple=True,
    type=click.Choice(list(PLATFORM_NAMES.keys()), case_sensitive=False),
)
@click.option("--limit", type=int, default=None)
def hash_listings(platforms: tuple[str, ...], limit: int | None) -> None:
    """Download photos and compute pHash for listings whose photo_phashes is empty."""
    asyncio.run(_hash_listings(platforms, limit))


async def _hash_listings(platform_names: tuple[str, ...], limit: int | None) -> None:
    import httpx

    from src.db.session import get_session
    from src.matching.phash import download_and_hash
    from src.scrapers.orchestrator import _photo_request_headers

    settings = get_settings()
    settings.ensure_dirs()

    async with get_session() as s:
        stmt = select(Listing).where(Listing.is_active.is_(True))
        if platform_names:
            chosen = [PLATFORM_NAMES[p.lower()] for p in platform_names]
            stmt = stmt.where(Listing.platform.in_(chosen))
        rows = (await s.execute(stmt)).scalars().all()

    to_hash = [r for r in rows if not r.photo_phashes and r.photo_urls]
    if limit is not None:
        to_hash = to_hash[:limit]
    click.echo(f"→ {len(to_hash)} listings need photos hashed")

    for i, listing in enumerate(to_hash):
        target_dir = (
            settings.listings_photos_dir / listing.platform.value / listing.external_id
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        hashes: list[str] = []
        # Per-listing Referer — CDNs (nekretnine especially) reject hot-linking.
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers=_photo_request_headers(listing.url),
        ) as client:
            for idx, url in enumerate(listing.photo_urls):
                ext = ".webp" if ".webp" in url.lower() else ".jpg"
                target = target_dir / f"{idx:03d}{ext}"
                res = await download_and_hash(
                    url, target, client,
                    platform=listing.platform.value,
                    listing_external_id=listing.external_id,
                )
                if res is None:
                    continue
                hashes.append(res[1])
        async with get_session() as s:
            row = await s.get(Listing, listing.id)
            if row is not None:
                row.photo_phashes = hashes
        click.echo(
            f"  [{i + 1}/{len(to_hash)}] {listing.platform.value}/{listing.external_id}: "
            f"{len(hashes)}/{len(listing.photo_urls)} photos hashed"
        )


# --------------------------------------------------------------------------- Phase 4: Admin


@cli.command("test-batch")
@click.argument("n", type=int)
@click.option(
    "--from-id",
    type=int,
    default=None,
    help="Use inbound IDs >= this. Default: last N by tg_message_id.",
)
def test_batch(n: int, from_id: int | None) -> None:
    """Re-run the full pipeline (CLIP+text+LLM → delivery) on the last N inbound posts.

    Resets their Match/PublishedPost rows and InboundPost.status to NEW, then calls
    process_new_inbound for each in chronological order. Use through `otz-bg test-batch N`
    to detach from the SSH session.
    """
    asyncio.run(_test_batch(n, from_id))


async def _test_batch(n: int, from_id: int | None) -> None:
    import time as _time

    from sqlalchemy import delete, update

    from src.db.models import InboundPost, InboundStatus, Match, PublishedPost
    from src.db.session import get_session
    from src.jobs.pipeline import process_new_inbound
    from src.telegram.delivery import send_run_summary

    async with get_session() as s:
        stmt = select(InboundPost.id).order_by(InboundPost.tg_message_id.desc())
        if from_id is not None:
            stmt = stmt.where(InboundPost.id >= from_id)
        rows = (await s.execute(stmt.limit(n))).scalars().all()
    inbound_ids = sorted(rows)
    click.echo(f"→ test-batch on {len(inbound_ids)} inbound(s): {inbound_ids}")
    if not inbound_ids:
        click.echo("  (no inbound posts found)")
        return

    # Reset state: drop non-published matches + flip status to NEW.
    async with get_session() as s:
        match_ids = list(
            (
                await s.execute(select(Match.id).where(Match.inbound_post_id.in_(inbound_ids)))
            ).scalars().all()
        )
        if match_ids:
            await s.execute(delete(PublishedPost).where(PublishedPost.match_id.in_(match_ids)))
            await s.execute(delete(Match).where(Match.id.in_(match_ids)))
        await s.execute(
            update(InboundPost)
            .where(InboundPost.id.in_(inbound_ids))
            .values(status=InboundStatus.NEW)
        )
    click.echo(f"  reset: dropped {len(match_ids)} prior matches")

    # Open a Telethon client only if we need it (legacy userbot mode).
    # In bot mode we never touch Telethon here — that avoids SQLite session-file lock conflicts
    # with the long-running otz-monitor.service.
    client = await _open_userbot_if_needed()

    stats = {"matched": 0, "no_match": 0, "errors": 0}
    started = _time.monotonic()
    try:
        for iid in inbound_ids:
            try:
                result = await process_new_inbound(iid, client=client, refresh_listings=False)
                if result.status == "matched":
                    stats["matched"] += 1
                    click.echo(f"  ✓ inbound={iid} → match (conf={result.confidence:.2f}, {result.method})")
                else:
                    stats["no_match"] += 1
                    click.echo(f"  · inbound={iid} → no_match")
            except Exception as e:
                stats["errors"] += 1
                click.echo(f"  ⚠ inbound={iid} FAILED: {e}", err=True)
        elapsed = _time.monotonic() - started
        try:
            await send_run_summary(
                client,
                matched=stats["matched"],
                no_match=stats["no_match"],
                errors=stats["errors"],
                duration_seconds=elapsed,
                label=f"test-batch (последних {len(inbound_ids)})",
            )
        except Exception:
            logging.getLogger(__name__).exception("run summary delivery failed")
    finally:
        if client is not None:
            await client.disconnect()

    click.echo(
        f"\n=== DONE: matched={stats['matched']} no_match={stats['no_match']} errors={stats['errors']} ==="
    )


async def _open_userbot_if_needed():
    """Open a Telethon userbot only when delivery actually needs it.

    In bot mode (`TG_DELIVERY_MODE=bot`) the Bot API does all delivery, so a Telethon client is
    redundant — and opening one would deadlock on the SQLite session file held by otz-monitor.
    Returns None when no client is needed.
    """
    settings = get_settings()
    if (settings.tg_delivery_mode or "userbot").lower() == "bot":
        return None
    from src.telegram.monitor import make_client
    c = make_client()
    await c.start()
    return c


@cli.command("photo-stats")
@click.option(
    "--since",
    "since_str",
    default="24h",
    show_default=True,
    help="Window: 1h, 24h, 7d.",
)
@click.option(
    "--platform",
    type=click.Choice(list(PLATFORM_NAMES.keys()), case_sensitive=False),
    default=None,
    help="Only this platform.",
)
def photo_stats(since_str: str, platform: str | None) -> None:
    """Aggregate the per-photo download log — how many ok / failed per platform."""
    asyncio.run(_photo_stats(since_str, platform))


async def _photo_stats(since_str: str, platform: str | None) -> None:
    from src.jobs.photo_stats import aggregate

    # Parse "24h" / "7d" / "30m"
    unit = since_str[-1].lower()
    try:
        amount = int(since_str[:-1])
    except ValueError:
        click.echo(f"invalid --since: {since_str!r}; use 30m / 24h / 7d", err=True)
        raise SystemExit(2) from None
    multipliers = {"m": 60, "h": 3600, "d": 86400}
    if unit not in multipliers:
        click.echo(f"invalid --since unit: {unit!r}; use m/h/d", err=True)
        raise SystemExit(2)
    seconds = amount * multipliers[unit]

    platform_filter = PLATFORM_NAMES[platform.lower()].value if platform else None
    counts = aggregate(since_seconds=seconds, platform=platform_filter)

    click.echo(f"=== Photo downloads — last {since_str} ===")
    if not counts:
        click.echo("(no entries)")
        return
    click.echo(f"{'platform':14s}  {'ok':>8s}  {'failed':>8s}  {'total':>8s}  {'fail %':>7s}")
    grand = {"ok": 0, "failed": 0, "total": 0}
    for plat in sorted(counts.keys()):
        c = counts[plat]
        grand["ok"] += c["ok"]
        grand["failed"] += c["failed"]
        grand["total"] += c["total"]
        pct = (100 * c["failed"] / c["total"]) if c["total"] else 0
        click.echo(f"{plat:14s}  {c['ok']:>8d}  {c['failed']:>8d}  {c['total']:>8d}  {pct:>6.1f}%")
    if len(counts) > 1:
        pct = (100 * grand["failed"] / grand["total"]) if grand["total"] else 0
        click.echo(f"{'TOTAL':14s}  {grand['ok']:>8d}  {grand['failed']:>8d}  {grand['total']:>8d}  {pct:>6.1f}%")


@cli.command("admin")
@click.option("--host", default=None, help="Override ADMIN_HOST.")
@click.option("--port", default=None, type=int, help="Override ADMIN_PORT.")
@click.option("--reload", is_flag=True, help="Auto-reload on code changes (dev only).")
def admin(host: str | None, port: int | None, reload: bool) -> None:
    """Launch the web admin (read-only viewer). Basic auth: ADMIN_USERNAME / ADMIN_PASSWORD."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "src.admin.app:app",
        host=host or settings.admin_host,
        port=port or settings.admin_port,
        reload=reload,
        log_level=settings.log_level.lower(),
    )


@cli.command("nightly-refresh")
@click.option(
    "--max-pages",
    default=500,
    show_default=True,
    help="Page cap per platform — should be high enough to walk the entire site.",
)
@click.option(
    "--inactive-after-days",
    default=3,
    show_default=True,
    help="Listings not seen in N days get marked is_active=False. "
         "With nightly cadence, 3 days = 1-day cadence + 2-day grace.",
)
@click.option("--skip-sweep", is_flag=True, help="Run the full scrape but skip the stale-sweep.")
@click.option("--skip-scrape", is_flag=True, help="Skip the scrape, only do the stale-sweep.")
def nightly_refresh(
    max_pages: int,
    inactive_after_days: int,
    skip_sweep: bool,
    skip_scrape: bool,
) -> None:
    """Nightly maintenance: full (non-incremental) scrape + mark stale listings inactive.

    Runs once a night via systemd timer (otz-nightly-refresh.timer at 03:00 UTC = 05:00 Belgrade).
    Daily runs from each new inbound post auto-scrape in incremental mode (1-5 min); this full
    walk takes hours but guarantees the active-listings set reflects what's actually live.
    """
    asyncio.run(_nightly_refresh(max_pages, inactive_after_days, skip_sweep, skip_scrape))


# Backwards-compat alias — old systemd units / scripts may still call `weekly-refresh`.
@cli.command("weekly-refresh", hidden=True)
@click.option("--max-pages", default=500, show_default=True)
@click.option("--inactive-after-days", default=10, show_default=True)
@click.option("--skip-sweep", is_flag=True)
@click.option("--skip-scrape", is_flag=True)
def weekly_refresh_alias(
    max_pages: int, inactive_after_days: int, skip_sweep: bool, skip_scrape: bool
) -> None:
    """Deprecated — use `nightly-refresh`. Kept as alias for legacy systemd units."""
    asyncio.run(_nightly_refresh(max_pages, inactive_after_days, skip_sweep, skip_scrape))


async def _nightly_refresh(
    max_pages: int, inactive_after_days: int, skip_sweep: bool, skip_scrape: bool
) -> None:
    import time as _time

    from src.scrapers.orchestrator import mark_stale_inactive
    from src.telegram.delivery import send_text_to_recipients

    started = _time.monotonic()
    results = []
    sweep_result = None

    if not skip_scrape:
        click.echo(f"→ Full scrape (incremental=False, max_pages={max_pages})…")
        results = await _run_all(
            list(SCRAPERS.keys()),
            max_pages=max_pages,
            max_new=None,
            download_photos=True,
            incremental=False,
        )
        _print_summary(results)

    # Sweep ONLY platforms whose scrape actually worked. Sweeping after a failed/segment-starved
    # walk is how 3 bad nights wipe a platform's whole active set: nothing refreshed last_seen_at,
    # so everything "ages out" even though the apartments are still live on the site.
    sweep_skipped_platforms: list[str] = []
    if not skip_sweep:
        click.echo(f"\n→ Marking listings inactive (not seen in last {inactive_after_days} days)…")
        healthy: list[Platform] = []
        if skip_scrape:
            # No scrape this run — operator explicitly asked for sweep-only; sweep everything.
            healthy = list(SCRAPERS.keys())
        else:
            by_platform = {r.platform: r for r in results}
            for plat in SCRAPERS:
                r = by_platform.get(plat)
                # site_total is REQUIRED: a full walk that couldn't even read the site's own
                # result count can't prove coverage, and sweeping after an unproven walk is how
                # a silently-truncated scrape mass-deactivates a platform.
                ok = (
                    r is not None
                    and r.previewed > 0
                    and bool(r.site_total)
                    and r.previewed >= 0.5 * r.site_total
                )
                if ok:
                    healthy.append(plat)
                else:
                    sweep_skipped_platforms.append(plat.value)
        total_marked = 0
        for plat in healthy:
            res = await mark_stale_inactive(
                inactive_after_days=inactive_after_days, platform=plat
            )
            total_marked += res.marked_inactive
        from src.scrapers.orchestrator import StaleSweepResult
        sweep_result = StaleSweepResult(platform=None, marked_inactive=total_marked)
        click.echo(f"  marked {total_marked} listings inactive")
        if sweep_skipped_platforms:
            click.echo(
                f"  ⚠️ sweep skipped for unhealthy platforms: {', '.join(sweep_skipped_platforms)}"
            )

    if skip_scrape:
        return

    elapsed = _time.monotonic() - started
    total_new = sum(r.inserted for r in results)
    total_refreshed = sum(r.updated for r in results)
    total_errors = sum(r.errors for r in results)

    if elapsed < 3600:
        dur = f"{int(elapsed // 60)} мин"
    else:
        h, rem = divmod(int(elapsed), 3600)
        dur = f"{h} ч {rem // 60} мин"

    lines = [
        "🌙 Ночной refresh — завершён",
        "",
        f"Новых listings: {total_new}",
        f"Обновлено (active): {total_refreshed}",
    ]
    if sweep_result is not None:
        lines.append(f"Помечено inactive: {sweep_result.marked_inactive}")
    if total_errors:
        lines.append(f"⚠️ Ошибки: {total_errors}")
    lines.append(f"⏱ Время: {dur}")

    # Per-platform breakdown + dead-source guard. A platform that returns zero previews almost
    # always means its scraper broke (site migration, IP block) — surface it loudly instead of
    # letting the active set silently bleed out over the inactive-after-days window.
    if results:
        lines.append("")
        lines.append("По площадкам (previewed/new):")
        for r in results:
            extra = ""
            if r.site_total:
                pct = 100 * r.previewed // max(r.site_total, 1)
                extra += f"  покрытие {pct}% из {r.site_total}"
            if r.skipped_pages:
                extra += f"  ⚠️ пропущено стр.: {r.skipped_pages}"
            lines.append(f"  • {r.platform.value}: {r.previewed} / {r.inserted}{extra}")
    # The guard runs UNCONDITIONALLY: if every scraper raised, `results` is empty and `_run_all`
    # records no error counter, so without this an all-platforms-down night would post a cheerful
    # "0 new" with no alarm. A platform absent from `results` (its scraper raised) is also dead.
    seen_platforms = {r.platform for r in results}
    dead = [r.platform.value for r in results if r.previewed == 0]
    dead += [p.value for p in SCRAPERS if p not in seen_platforms]
    degraded = [f"{r.platform.value} ({r.skipped_pages} стр.)" for r in results if r.skipped_pages]
    # Coverage guard: a walk that collects far less than the site holds means pagination caps /
    # dead segments are eating the active set — the exact silent failure behind the "9000 instead
    # of 20000" incident. 70% leaves slack for ads appearing/expiring mid-walk.
    low_coverage = [
        f"{r.platform.value} ({r.previewed}/{r.site_total})"
        for r in results
        if r.site_total and r.previewed < 0.7 * r.site_total
    ]
    if dead:
        lines.append("")
        lines.append(f"🚨 ВНИМАНИЕ: площадки без результатов: {', '.join(dead)} — проверь скрейпер!")
    if low_coverage:
        lines.append(f"🚨 Низкое покрытие: {', '.join(low_coverage)} — кап пагинации/сегменты?")
    if degraded:
        lines.append(f"⚠️ Частичный обход (пропущены страницы): {', '.join(degraded)}")
    if sweep_skipped_platforms:
        lines.append(f"⚠️ Sweep пропущен (нездоровый скрейп): {', '.join(sweep_skipped_platforms)}")
    client = await _open_userbot_if_needed()
    try:
        # bot mode → fans out to all chat_ids; userbot mode → Saved Messages.
        await send_text_to_recipients(client, "\n".join(lines))
    except Exception:
        logging.getLogger(__name__).exception("nightly refresh summary delivery failed")
    finally:
        if client is not None:
            await client.disconnect()

    # Exit non-zero on dead platforms so systemd marks the unit failed (it exits 0 today even
    # when every scraper is down, which keeps monitoring blind). Summary is already sent above.
    if dead:
        raise SystemExit(1)


@cli.command("mark-stale")
@click.option("--days", default=10, show_default=True, help="Inactive threshold in days.")
@click.option(
    "--platform",
    type=click.Choice(list(PLATFORM_NAMES.keys()), case_sensitive=False),
    default=None,
    help="Limit to one platform.",
)
def mark_stale(days: int, platform: str | None) -> None:
    """Standalone: mark listings as inactive if last_seen_at is older than --days.

    Usually you don't call this directly — weekly-refresh runs it after a full scrape.
    """
    asyncio.run(_mark_stale(days, platform))


async def _mark_stale(days: int, platform: str | None) -> None:
    from src.scrapers.orchestrator import mark_stale_inactive

    plat = PLATFORM_NAMES[platform.lower()] if platform else None
    result = await mark_stale_inactive(inactive_after_days=days, platform=plat)
    click.echo(f"marked {result.marked_inactive} listings inactive")


@cli.command("refresh")
@click.option("--force", is_flag=True, help="Ignore the 15-min throttle.")
def refresh(force: bool) -> None:
    """Manually refresh listings on all platforms (auto-triggered by each new inbound too)."""
    asyncio.run(_refresh(force))


async def _refresh(force: bool) -> None:
    from src.scrapers.orchestrator import _last_scrape_marker_path, scrape_all_with_throttle

    if force:
        marker = _last_scrape_marker_path()
        if marker.exists():
            marker.unlink()
            click.echo("→ cleared throttle marker")

    ran, results = await scrape_all_with_throttle()
    if not ran:
        click.echo("throttled (last scrape <15 min ago — use --force to override)")
        return
    for r in results:
        click.echo(
            f"  {r.platform.value:14} previewed={r.previewed} "
            f"new={r.inserted} refreshed={r.updated} errors={r.errors}"
        )


@cli.command("deliver-all")
@click.option(
    "--force",
    is_flag=True,
    help="Re-send EVERY match (incl. already-PUBLISHED) with fresh post text — "
         "useful after a template/URL/hashtag change.",
)
def deliver_all(force: bool) -> None:
    """Send PENDING matches to the configured recipients (Saved Messages / bot).

    With --force, also re-deliver matches that were previously PUBLISHED — the post text
    gets re-rendered against the current code (so e.g. a Yandex-URL fix or new district
    hashtag will appear in the re-send).
    """
    asyncio.run(_deliver_all(force))


async def _deliver_all(force: bool = False) -> None:
    import time as _time

    from src.db.models import Match, MatchStatus
    from src.db.session import get_session
    from src.telegram.delivery import send_match_to_saved, send_run_summary
    from src.telegram.monitor import make_client

    async with get_session() as s:
        stmt = select(Match.id).order_by(Match.created_at)
        if not force:
            stmt = stmt.where(Match.status == MatchStatus.PENDING)
        else:
            stmt = stmt.where(Match.status != MatchStatus.REJECTED)
        ids = list((await s.execute(stmt)).scalars().all())
    if not ids:
        click.echo("No matches to deliver." if force else "No PENDING matches to deliver.")
        return
    click.echo(f"→ deliver-all{' --force' if force else ''}: {len(ids)} match(es)")

    delivered = 0
    failed = 0
    client = await _open_userbot_if_needed()
    started = _time.monotonic()
    try:
        for mid in ids:
            try:
                p = await send_match_to_saved(mid, client, force_resend=force)
                click.echo(f"  match {mid} → msg {p.tg_message_id}")
                delivered += 1
            except Exception as e:
                click.echo(f"  match {mid} FAILED: {e}", err=True)
                failed += 1
        elapsed = _time.monotonic() - started
        try:
            await send_run_summary(
                client,
                matched=delivered,
                no_match=0,
                errors=failed,
                duration_seconds=elapsed,
                label="deliver-all" + (" --force" if force else ""),
            )
        except Exception:
            logging.getLogger(__name__).exception("run summary delivery failed")
    finally:
        if client is not None:
            await client.disconnect()


@cli.command("refetch-details")
@click.option(
    "--platform",
    type=click.Choice(list(PLATFORM_NAMES.keys()), case_sensitive=False),
    default=None,
    help="Limit to one platform.",
)
@click.option(
    "--all", "refetch_all", is_flag=True,
    help="Re-fetch EVERY active listing (default: only broken rows — no photos, no phashes, "
         "or no price). Use after a parser fix to backfill, e.g. the 4zida cover photo.",
)
@click.option("--limit", type=int, default=None, help="Cap number of listings.")
@click.option("--no-photos", is_flag=True, help="Update text fields only, skip photo downloads.")
def refetch_details(platform: str | None, refetch_all: bool, limit: int | None, no_photos: bool) -> None:
    """Re-fetch detail pages for existing listings and repair their stored data.

    Listings are normally detail-fetched exactly once at insert; a transient failure or a
    parser bug at that moment freezes the row forever (no price/photos → invisible to the
    matcher). This command heals such rows, and backfills after parser improvements.
    """
    asyncio.run(_refetch_details(platform, refetch_all, limit, no_photos))


async def _refetch_details(
    platform: str | None, refetch_all: bool, limit: int | None, no_photos: bool
) -> None:
    from src.db.models import Listing
    from src.db.session import get_session
    from src.scrapers.orchestrator import _download_photos

    stmt = select(Listing.id, Listing.platform, Listing.url).where(Listing.is_active.is_(True))
    if platform:
        stmt = stmt.where(Listing.platform == PLATFORM_NAMES[platform.lower()])
    if not refetch_all:
        stmt = stmt.where(
            (Listing.photo_urls == [])
            | (Listing.photo_phashes == [])
            | (Listing.price_eur.is_(None))
        )
    stmt = stmt.order_by(Listing.id)
    if limit:
        stmt = stmt.limit(limit)
    async with get_session() as s:
        rows = (await s.execute(stmt)).all()

    by_platform: dict[Platform, list] = {}
    for row in rows:
        by_platform.setdefault(row.platform, []).append(row)
    total = len(rows)
    click.echo(f"→ refetch-details: {total} listings ({'ALL' if refetch_all else 'broken only'})")
    stats = {"updated": 0, "photos": 0, "errors": 0}

    for plat, plat_rows in by_platform.items():
        scraper_cls = SCRAPERS[plat]
        try:
            async with scraper_cls() as scraper:
                for row in plat_rows:
                    try:
                        detail = await scraper.fetch_detail(row.url)
                        if not detail.photo_urls and detail.price_eur is None and detail.title is None:
                            raise RuntimeError("still an empty payload")
                        # Decide and DOWNLOAD photos before touching the row: committing the new
                        # photo_urls first would make a failed download undetectable forever
                        # (urls match, phashes non-empty → no filter ever selects the row again).
                        async with get_session() as s:
                            current = await s.get(Listing, row.id)
                            if current is None:
                                continue
                            old_urls = list(current.photo_urls or [])
                            old_phash_count = len(current.photo_phashes or [])
                        need_photos = (
                            not no_photos
                            and detail.photo_urls
                            and (
                                list(detail.photo_urls) != old_urls
                                or old_phash_count < len(detail.photo_urls)
                            )
                        )
                        hashes: list[str] = []
                        emb = None
                        if need_photos:
                            paths, hashes, emb = await _download_photos(detail)
                            if not hashes:
                                raise RuntimeError("photo download produced no hashes — row left untouched")
                        async with get_session() as s:
                            listing = await s.get(Listing, row.id)
                            if listing is None:
                                continue
                            listing.price_eur = detail.price_eur
                            listing.area_m2 = detail.area_m2
                            listing.rooms = detail.rooms
                            listing.district = detail.district
                            listing.address_raw = detail.address_raw
                            listing.lat = detail.lat
                            listing.lon = detail.lon
                            listing.title = detail.title
                            listing.description = detail.description
                            listing.raw_payload = detail.raw_payload
                            if need_photos:
                                listing.photo_urls = detail.photo_urls
                                listing.photo_phashes = hashes
                                listing.photo_embeddings = emb
                                stats["photos"] += len(hashes)
                        stats["updated"] += 1
                        if stats["updated"] % 100 == 0:
                            click.echo(
                                f"  … {stats['updated']}/{total} photos={stats['photos']} errors={stats['errors']}"
                            )
                        await asyncio.sleep(0.3)
                    except Exception as e:
                        stats["errors"] += 1
                        if stats["errors"] <= 5:
                            click.echo(f"  ⚠ {plat.value} #{row.id}: {type(e).__name__}: {e}", err=True)
        except Exception:
            logging.getLogger(__name__).exception("[refetch %s] platform failed", plat.value)

    click.echo(
        f"=== DONE: updated={stats['updated']} photos={stats['photos']} errors={stats['errors']} ==="
    )


@cli.command("backfill-nekretnine-address")
@click.option("--limit", type=int, default=None, help="Cap number of listings.")
@click.option("--concurrency", type=int, default=4, help="Parallel HTTP requests.")
def backfill_nekretnine_address(limit: int | None, concurrency: int) -> None:
    """Re-fetch each nekretnine listing detail and fill address_raw.

    Skips listings that already have a non-null address_raw — safe to re-run.
    """
    asyncio.run(_backfill_nekretnine_address(limit, concurrency))


async def _backfill_nekretnine_address(limit: int | None, concurrency: int) -> None:
    import asyncio as _asyncio

    import httpx

    from src.db.models import Listing, Platform
    from src.db.session import get_session
    from src.scrapers.nekretnine import NekretnineScraper

    async with get_session() as s:
        stmt = (
            select(Listing.id, Listing.url)
            .where(Listing.platform == Platform.NEKRETNINE)
            .where(Listing.is_active.is_(True))
            .where(Listing.address_raw.is_(None))
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = (await s.execute(stmt)).all()

    total = len(rows)
    click.echo(f"→ backfill-nekretnine-address: {total} listings to update")
    if total == 0:
        return

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Accept-Language": "sr-RS,sr;q=0.9,en;q=0.7",
    }
    sem = _asyncio.Semaphore(concurrency)
    stats = {"ok": 0, "no_addr": 0, "errors": 0}
    progress_lock = _asyncio.Lock()
    progress = {"done": 0}
    updates: list[tuple[int, str]] = []

    async with httpx.AsyncClient(timeout=20, headers=headers, follow_redirects=True) as client:
        async def fetch(listing_id: int, url: str) -> None:
            async with sem:
                try:
                    resp = await client.get(url)
                    if resp.status_code == 404:
                        stats["no_addr"] += 1
                        return
                    resp.raise_for_status()
                    # Address now lives in the detail page's __NEXT_DATA__ JSON (post-migration),
                    # so reuse the canonical parser instead of the removed DOM helper.
                    addr = NekretnineScraper.parse_detail(str(resp.url), resp.text).address_raw
                    if addr:
                        updates.append((listing_id, addr))
                        stats["ok"] += 1
                    else:
                        stats["no_addr"] += 1
                except Exception as e:
                    stats["errors"] += 1
                    if stats["errors"] <= 5:
                        click.echo(f"  ⚠ {listing_id}: {type(e).__name__}: {e}", err=True)
                finally:
                    async with progress_lock:
                        progress["done"] += 1
                        if progress["done"] % 200 == 0 or progress["done"] == total:
                            click.echo(
                                f"  … {progress['done']}/{total} "
                                f"ok={stats['ok']} no_addr={stats['no_addr']} errors={stats['errors']}"
                            )

        await _asyncio.gather(*(fetch(lid, url) for lid, url in rows))

    # Commit in one transaction.
    if updates:
        async with get_session() as s:
            from sqlalchemy import update
            for lid, addr in updates:
                await s.execute(update(Listing).where(Listing.id == lid).values(address_raw=addr))
        click.echo(f"✓ committed {len(updates)} address rows")

    click.echo(
        f"=== DONE: ok={stats['ok']} no_addr={stats['no_addr']} errors={stats['errors']} ==="
    )


@cli.command("ship-log")
@click.argument("log_file", type=click.Path(exists=False))
@click.option("--label", default=None, help="Header shown in the Saved Messages post.")
@click.option("--lines", type=int, default=100, help="How many tail lines to ship (default 100).")
@click.option(
    "--session",
    default="otz_logger",
    help="Telethon session name (relative to data_dir). Must be a separate file from the live "
         "monitor's session — running both processes against the same .session file deadlocks.",
)
def ship_log(log_file: str, label: str | None, lines: int, session: str) -> None:
    """Tail a log file to Saved Messages via userbot.

    Used as `ExecStartPost=` on systemd oneshot services so each run posts its own log tail.
    Requires the `otz_logger.session` to be authorised once — run `scripts/auth_logger.py`.
    """
    asyncio.run(_ship_log(log_file, label, lines, session))


async def _ship_log(log_file: str, label: str | None, lines: int, session_name: str) -> None:
    from collections import deque
    from pathlib import Path as _Path

    from telethon import TelegramClient as _TelegramClient

    from src.config import get_settings

    p = _Path(log_file)
    if not p.exists():
        click.echo(f"  ⚠ ship-log: {log_file} not found — skip", err=True)
        return

    with p.open("r", encoding="utf-8", errors="replace") as fh:
        tail = list(deque(fh, maxlen=lines))
    body = "".join(tail).rstrip()
    if not body:
        click.echo(f"  · ship-log: {log_file} empty — skip")
        return

    header = label or p.name
    text = f"📜 {header} (tail {len(tail)} lines)\n\n<pre>{_escape_html(body)}</pre>"
    # Telegram message hard cap: 4096 chars. Trim from the front (keep latest).
    MAX = 3900
    if len(text) > MAX:
        text = text[-MAX:]
        text = "📜 " + header + " (trimmed)\n\n<pre>" + _escape_html(body[-(MAX - 200):]) + "</pre>"

    settings = get_settings()
    if not settings.tg_api_id or not settings.tg_api_hash:
        click.echo("  ⚠ ship-log: TG_API_ID / TG_API_HASH not set", err=True)
        return
    session_path = str(settings.data_dir / session_name)
    async with _TelegramClient(session_path, settings.tg_api_id, settings.tg_api_hash) as client:
        await client.send_message("me", text, parse_mode="html")
    click.echo(f"  ✓ ship-log: tail of {p.name} sent to Saved Messages ({len(tail)} lines)")


def _escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@cli.command("daily-summary")
@click.option(
    "--hours",
    type=int,
    default=24,
    help="Window size in hours (default 24). Counts activity from now-window to now.",
)
def daily_summary(hours: int) -> None:
    """Push a daily activity summary to bot recipients — inbound / matched / delivered counts.

    Wired up as a systemd timer (otz-daily-summary.timer @ 07:00 UTC = 09:00 Belgrade).
    Live monitor delivers each match in real-time but never emits a wrap-up, so this fills the gap.
    """
    asyncio.run(_daily_summary(hours))


async def _daily_summary(hours: int) -> None:
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func

    from src.db.models import InboundPost, InboundStatus, Match, PublishedPost
    from src.db.session import get_session
    from src.telegram.delivery import send_run_summary

    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    # Count by InboundPost.post_date (when Дарья originally posted), and use InboundStatus as
    # the source of truth for matched/no_match — this avoids inflating numbers when backfill
    # re-creates Match rows with a fresh `created_at` for old inbounds.
    async with get_session() as s:
        inbound_total = (
            await s.execute(
                select(func.count())
                .select_from(InboundPost)
                .where(InboundPost.post_date >= since)
            )
        ).scalar_one()
        matched = (
            await s.execute(
                select(func.count())
                .select_from(InboundPost)
                .where(InboundPost.post_date >= since)
                .where(InboundPost.status == InboundStatus.MATCHED)
            )
        ).scalar_one()
        no_match = (
            await s.execute(
                select(func.count())
                .select_from(InboundPost)
                .where(InboundPost.post_date >= since)
                .where(InboundPost.status == InboundStatus.NO_MATCH)
            )
        ).scalar_one()
        delivered = (
            await s.execute(
                select(func.count())
                .select_from(PublishedPost)
                .join(Match, Match.id == PublishedPost.match_id)
                .join(InboundPost, InboundPost.id == Match.inbound_post_id)
                .where(InboundPost.post_date >= since)
            )
        ).scalar_one()

    label = f"Сводка за последние {hours} ч"
    click.echo(
        f"→ {label}: inbound={inbound_total} matched={matched} no_match={no_match} delivered={delivered}"
    )

    client = await _open_userbot_if_needed()
    try:
        await send_run_summary(
            client,
            matched=matched,
            no_match=no_match,
            errors=max(matched - delivered, 0),
            duration_seconds=None,
            label=label,
        )
    finally:
        if client is not None:
            await client.disconnect()


@cli.command("notify-failure")
@click.option("--service", default="otz-monitor", help="Failing systemd unit name.")
@click.option("--reason", default=None, help="Optional human-readable cause for the alert.")
def notify_failure(service: str, reason: str | None) -> None:
    """Push an alert that a systemd service crashed.

    Wired up as `OnFailure=otz-monitor-failure.service` on long-running units so Дарья sees
    immediately when monitor (or another service) dies — instead of waiting until tomorrow's
    daily summary to notice it stopped delivering.
    """
    asyncio.run(_notify_failure(service, reason))


async def _notify_failure(service: str, reason: str | None) -> None:
    from datetime import datetime, timezone

    from src.telegram.delivery import send_text_to_recipients

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"🚨 {service} упал — systemd сейчас рестартует.",
        f"⏱ {ts}",
    ]
    if reason:
        lines.append(f"причина: {reason}")
    lines.append("")
    lines.append("После рестарта пришлю Recovery-сводку, если были зависшие посты.")

    client = await _open_userbot_if_needed()
    try:
        await send_text_to_recipients(client, "\n".join(lines))
        click.echo(f"✓ alert sent for {service}")
    except Exception as e:
        click.echo(f"⚠ alert failed: {e}", err=True)
    finally:
        if client is not None:
            await client.disconnect()


def main() -> None:
    try:
        cli()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
