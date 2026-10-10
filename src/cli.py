"""Command-line entry point: `otz <command>`."""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import click
import httpx
from sqlalchemy import select

from src.config import get_settings
from src.db.models import InboundPost, Listing, Platform, SourceChannel
from src.jobs.lifecycle import remaining_budget, run_budget, run_cooperatively

# Shared with the decisions-log writer (`_record_decision`) — one ts format, defined at the source.
from src.matching.llm_validator import LLM_DECISIONS_TS_FMT
from src.scrapers import SCRAPERS
from src.scrapers.orchestrator import PriceChange, ScrapeResult, scrape_platform
from src.scrapers.shadow_audit import run_shadow_audit, write_report

if TYPE_CHECKING:  # runtime-импорт dedup остаётся ленивым для несвязанных CLI-команд
    from src.matching.dedup import DedupStats


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

    results = run_cooperatively(
        _run_all(selected, max_pages, max_new, not no_photos, incremental=not full)
    )
    _print_summary(results)
    if any(r.outcome in {"failed", "degraded"} for r in results):
        raise SystemExit(1)


@cli.command("scrape")
@click.argument("platform", type=click.Choice(list(PLATFORM_NAMES.keys()), case_sensitive=False))
@click.option("--max-pages", default=3, show_default=True, type=int)
@click.option("--max-new", default=None, type=int)
@click.option("--no-photos", is_flag=True)
def scrape_one(platform: str, max_pages: int, max_new: int | None, no_photos: bool) -> None:
    """Run one scraping pass for a single platform."""
    plat = PLATFORM_NAMES[platform.lower()]
    results = run_cooperatively(_run_all([plat], max_pages, max_new, not no_photos))
    _print_summary(results)
    if any(r.outcome in {"failed", "degraded"} for r in results):
        raise SystemExit(1)


async def _run_all(
    platforms: list[Platform],
    max_pages: int,
    max_new: int | None,
    download_photos: bool,
    *,
    incremental: bool = True,
    dry_run: bool = False,
) -> list[ScrapeResult]:
    from src.jobs.run_history import progress

    results: list[ScrapeResult] = []
    for plat in platforms:
        if not dry_run:
            progress(f"scrape:{plat.value}")
        scraper_cls = SCRAPERS[plat]
        scraper = None
        try:
            settings = get_settings()
            source_budget = (settings.scrape_incremental_source_budget_sec if incremental
                             else settings.scrape_source_budget_sec)
            async with asyncio.timeout(source_budget):
                async with scraper_cls() as scraper:
                    options = {"dry_run": True} if dry_run else {}
                    result = await scrape_platform(
                        scraper, max_pages=max_pages, max_new=max_new,
                        download_photos=download_photos, incremental=incremental,
                        source_budget_sec=source_budget,
                        item_budget_sec=settings.scrape_item_budget_sec,
                        **options,
                    )
        except asyncio.CancelledError:
            partial = getattr(scraper, "last_scrape_result", None)
            if not dry_run and isinstance(partial, ScrapeResult):
                progress(f"cancelled:{plat.value}", partial)
            raise
        except Exception as exc:
            logging.getLogger(__name__).error(
                "[%s] scrape failed (%s)", plat.value, type(exc).__name__
            )
            partial = getattr(scraper, "last_scrape_result", None)
            if isinstance(partial, ScrapeResult):
                result = partial
                result.errors += 1
                result.crawl_complete = False
                result.quality_reasons.append(type(exc).__name__)
                result.stop_reason = (
                    "source_timeout" if isinstance(exc, TimeoutError) else "source_exception"
                )
            else:
                result = ScrapeResult.failed(
                    plat, reason=type(exc).__name__, full_crawl=not incremental
                )
        results.append(result)
        if not dry_run:
            progress(f"scraped:{plat.value}", result)
    return results


def _print_summary(results: list[ScrapeResult]) -> None:
    click.echo("")
    click.echo("=== Scrape summary ===")
    for r in results:
        extra = ""
        if r.snapshot_mode != "legacy" or r.snapshots_seen:
            extra += (
                f"  snapshots={r.snapshots_complete}/{r.snapshots_seen}"
                f"  fallback={r.snapshot_fallbacks}"
            )
        if r.proxy_metrics:
            challenges = r.proxy_metrics.get("proxy_challenges", 0)
            failovers = r.proxy_metrics.get("proxy_failovers", 0)
            proxy_errors = r.proxy_metrics.get("proxy_errors", 0)
            extra += (
                f"  proxy(challenge={challenges},failover={failovers},"
                f"errors={proxy_errors})"
            )
        click.echo(
            f"  {r.platform.value:14} previewed={r.previewed:4d}  "
            f"new={r.inserted:4d}  refreshed={r.updated:4d}  "
            f"photos={r.photos_downloaded:4d}  errors={r.errors}{extra}"
        )


# --------------------------------------------------------------------------- Phase 3: Telegram


@cli.command("auth")
def auth() -> None:
    """One-off Telethon authentication — creates the .session file (interactive)."""
    run_cooperatively(_auth())


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
    run_cooperatively(_add_source(identifier, start_date))


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
    run_cooperatively(_stats())


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
            # T1-3: active/inactive вместо одного раздутого total — с десятками тысяч
            # корпусов «total» ничего не говорил о живом рынке; сумма и так очевидна.
            inactive = (
                await s.execute(
                    select(func.count(Listing.id)).where(
                        Listing.platform == p, Listing.is_active.is_(False)
                    )
                )
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
                f"  {p.value:14}  {active:5d} active  "
                f"{inactive:5d} inactive  {with_ph:5d} with pHash"
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
    run_cooperatively(_list_sources())


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
    run_cooperatively(_backfill(source_id, limit, not no_match, from_date))


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
    from src.matching.validation_queue import unresolved_counts

    run_stats = {"matched": 0, "no_match": 0, "errors": 0, "needs_review": 0, "pending": 0}

    async def on_inbound(inbound_id: int) -> None:
        try:
            result = await process_new_inbound(inbound_id, client=client)
            if result.status == "matched":
                run_stats["matched"] += 1
            elif result.status == "no_match":
                run_stats["no_match"] += 1
            else:
                counts = await unresolved_counts([inbound_id])
                run_stats["needs_review"] += counts["needs_review"]
                run_stats["pending"] += counts["pending"] or int(not counts["needs_review"])
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
                    needs_review=run_stats["needs_review"], pending=run_stats["pending"],
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
    run_cooperatively(_reset_source(source_id, start_date))


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


_AUTH_LOST_ALERT = (
    "🔐 otz-monitor: сессия Telethon отозвана — userbot не может работать. "
    "Реавторизуйтесь: python scripts/auth_qr.py, затем systemctl restart otz-monitor"
)


async def _alert_auth_lost() -> None:
    """Send the auth-loss alert through the bot DIRECTLY (one message).

    Must NOT go via send_text_to_recipients(None, ...): under the default bot mode that path is
    fine, but under userbot mode it raises DeliveryError and the alert would be swallowed — so we
    call the bot sender directly. No-op (logged) if no bot token is configured.
    """
    settings = get_settings()
    if not settings.tg_bot_token:
        logging.getLogger(__name__).error(
            "Telethon session revoked but TG_BOT_TOKEN unset — cannot alert; re-auth via scripts/auth_qr.py"
        )
        return
    try:
        from src.telegram.bot_delivery import send_text_via_bot

        await send_text_via_bot(_AUTH_LOST_ALERT)
    except Exception:
        logging.getLogger(__name__).exception("auth-loss bot alert failed to send")


@cli.command("monitor")
@click.option("--no-backfill", is_flag=True, help="Skip initial backfill — go straight to live.")
def monitor(no_backfill: bool) -> None:
    """Run backfill + live NewMessage handler. Runs forever until interrupted."""
    run_cooperatively(_monitor(not no_backfill))


@cli.command("subscription-bot")
def subscription_bot() -> None:
    """C2 — run the Telegram linking daemon for saved-search subscriptions (long-polling).

    Listens for /start sub_<token> and links the sender's chat to the subscription. Config-gated:
    when SUBSCRIPTION_BOT_ENABLED=0 (default) the command no-ops. Runs forever until interrupted.
    """
    from src.telegram.subscription_bot import run_subscription_bot

    run_cooperatively(run_subscription_bot())


@cli.command("notify-subscriptions")
@click.option("--since-hours", type=int, default=24, show_default=True,
              help="Only listings exposed within this window are candidates.")
@click.option("--dry-run", is_flag=True, help="Compute + report, but neither send nor record.")
def notify_subscriptions_cmd(since_hours: int, dry_run: bool) -> None:
    """C2 — run the saved-search matcher once (also wired into nightly-refresh)."""
    from src.jobs import notify_subscriptions as notify_mod

    stats = run_cooperatively(
        notify_mod.notify_subscriptions(since_hours=since_hours, dry_run=dry_run)
    )
    click.echo(
        f"подписки: рассмотрено {stats.subscriptions_considered}, "
        f"уведомлено {stats.subscriptions_notified}, "
        f"квартир отправлено {stats.listings_sent}, ошибок {stats.errors}"
        + (" (dry-run)" if dry_run else "")
    )


async def _monitor(do_backfill_first: bool) -> None:
    from telethon.errors import AuthKeyDuplicatedError, UnauthorizedError

    from src.db.session import get_session
    from src.jobs.pipeline import process_new_inbound
    from src.telegram.monitor import (
        SessionNotAuthorized,
        backfill as run_backfill,
        connect_authorized,
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
    # Startup auth gate: a revoked/missing session must alert ONCE and exit 3 (non-restarting
    # via RestartPreventExitStatus=3) — NEVER fall through to start()'s phone prompt on systemd.
    try:
        await connect_authorized(client)
    except SessionNotAuthorized:
        logging.getLogger(__name__).error(
            "Telethon session not authorized at startup — re-auth via scripts/auth_qr.py"
        )
        await _alert_auth_lost()
        raise SystemExit(3)

    # Single client shared between live handler, backfill, and Saved-Messages delivery.
    async def on_inbound(inbound_id: int) -> None:
        await process_new_inbound(inbound_id, client=client)

    # Register the live handler upfront so messages arriving during backfill/recovery are
    # buffered into album groups instead of being silently dropped.
    register_live_handler(client, sources, on_inbound=on_inbound)

    retry_task = None
    try:
        if do_backfill_first:
            for src in sources:
                click.echo(f"→ Catching up [{src.id}] @{src.username}...")
                stats = await run_backfill(client, src, on_inbound=on_inbound)
                click.echo(
                    f"  caught up: inserted={stats.posts_inserted} skipped={stats.skipped_no_photo}"
                )

            from src.jobs.recovery import recover_new_posts
            from src.telegram.delivery import send_run_summary, send_text_to_recipients

            async def notify_start(text: str) -> None:
                await send_text_to_recipients(client, text)

            async def notify_summary(**stats) -> None:
                await send_run_summary(client, **stats)

            await recover_new_posts(
                on_inbound, notify_start=notify_start, notify_summary=notify_summary,
            )

        click.echo("Live monitor running — Ctrl+C to stop.")
        from src.jobs.validation_retry import retry_loop

        retry_task = asyncio.create_task(retry_loop(on_inbound))
        await client.run_until_disconnected()
    except (UnauthorizedError, AuthKeyDuplicatedError):
        # Session revoked at RUNTIME (e.g. SessionRevokedError from GetDifferenceRequest).
        # SessionRevokedError / AuthKeyUnregisteredError subclass UnauthorizedError; the
        # duplicated-key case is separate. Alert ONCE and exit 3 → unit goes to failed without
        # crash-looping (RestartPreventExitStatus=3). reset-failed/start can't fix a revoked
        # session — only re-auth (scripts/auth_qr.py) can.
        logging.getLogger(__name__).error(
            "Telethon session revoked at runtime — re-auth via scripts/auth_qr.py"
        )
        await _alert_auth_lost()
        raise SystemExit(3)
    finally:
        if retry_task is not None:
            retry_task.cancel()
            await asyncio.gather(retry_task, return_exceptions=True)
        await client.disconnect()


@cli.command("validation-jobs")
@click.option("--limit", default=50, type=click.IntRange(1, 500))
def validation_jobs_report(limit: int) -> None:
    """Read pending/review work without captions, photos, providers or delivery."""
    import json

    from src.matching.review_queue import pending_report

    report = run_cooperatively(pending_report(limit))
    click.echo(json.dumps(report, indent=2))


@cli.command("retry-validation")
@click.argument("inbound_id", type=int)
def retry_validation(inbound_id: int) -> None:
    """Requeue an unresolved inbound; refuse active leases and historical Match rows."""
    from src.matching.review_queue import requeue_validation

    changed = run_cooperatively(requeue_validation(inbound_id))
    click.echo(
        "Requeued for the next retry tick" if changed else "Not changed: leased, matched or absent"
    )


@cli.group("review")
def review() -> None:
    """Operator decisions for unresolved matching cases; local DB only, no provider or delivery."""


@review.command("close")
@click.argument("inbound_id", type=int)
@click.option("--as", "decision", type=click.Choice(["NO_MATCH", "DUPLICATE"]), required=True)
@click.option("--reason", required=True, help="Причина решения; сохраняется в журнале кейса.")
def review_close(inbound_id: int, decision: str, reason: str) -> None:
    """Close a needs_review inbound as NO_MATCH or DUPLICATE without sending anything."""
    from src.matching.review_queue import ReviewError, close_review

    try:
        run_cooperatively(close_review(inbound_id, decision, reason))
    except ReviewError as error:
        raise click.ClickException(str(error)) from error
    click.echo(f"inbound={inbound_id} closed as {decision}")


@review.command("reject-match")
@click.argument("match_id", type=int)
@click.option("--reason", required=True, help="Причина отклонения; сохраняется в матче.")
def review_reject_match(match_id: int, reason: str) -> None:
    """Reject a PENDING match that was never sent; PUBLISHED matches stay with the admin tool."""
    from src.matching.review_queue import ReviewError, reject_match

    try:
        outcome = run_cooperatively(reject_match(match_id, reason))
    except ReviewError as error:
        raise click.ClickException(str(error)) from error
    click.echo(f"match={match_id} {outcome}")


@cli.command("match")
@click.argument("inbound_id", type=int)
def match_one(inbound_id: int) -> None:
    """Run pHash matching for a single inbound post."""
    run_cooperatively(_match_one(inbound_id))


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
    run_cooperatively(_match_all())


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
    run_cooperatively(_rematch_all(hamming, keep_published))


async def _rematch_all(hamming: int | None, keep_published: bool) -> None:
    from src.matching.review_queue import reset_for_rematch

    settings = get_settings()
    if hamming is not None:
        settings.phash_max_hamming = hamming
        click.echo(f"→ Using Hamming threshold = {hamming} for this run only.")

    if not keep_published:
        click.echo("⚠️  --reset-published: wiping ALL matches including published history")
    await reset_for_rematch(keep_published=keep_published)
    click.echo("→ State reset. Running matcher...")

    import time as _time

    from src.matching.matcher import match_all_pending
    from src.matching.validation_queue import unresolved_counts
    from src.telegram.delivery import send_run_summary

    started = _time.monotonic()
    results = await match_all_pending()
    elapsed = _time.monotonic() - started
    matched = sum(1 for r in results if r.status == "matched")
    no_match = sum(r.status == "no_match" for r in results)
    pending = sum(r.status == "validation_pending" for r in results)
    click.echo(f"Validation pending: {pending}")
    click.echo(f"\n=== matched {matched} / {len(results)} ===")
    for r in results:
        marker = "✓" if r.status == "matched" else "·"
        click.echo(
            f"  {marker} inbound={r.inbound_id}  listing={r.listing_id}  "
            f"matched_photos={r.matched_photos}  conf={r.confidence:.2f}"
        )

    remaining = await unresolved_counts()
    click.echo(
        f"NEW queue: needs_review={remaining['needs_review']} pending={remaining['pending']}"
    )
    if results or any(remaining.values()):
        client = await _open_userbot_if_needed()
        try:
            hamming_note = f" (Hamming≤{hamming})" if hamming is not None else ""
            await send_run_summary(
                client,
                matched=matched,
                no_match=no_match,
                **remaining,
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
    from src.matching.validation_queue import unresolved_counts
    from src.telegram.delivery import send_run_summary

    started = _time.monotonic()
    # match_all_pending builds one candidates snapshot per batch lazily — a run with zero
    # pending inbounds does no candidate SELECT at all.
    results = await match_all_pending()
    elapsed = _time.monotonic() - started
    matched = sum(1 for r in results if r.status == "matched")
    no_match = sum(r.status == "no_match" for r in results)
    pending = sum(r.status == "validation_pending" for r in results)
    click.echo(f"Validation pending: {pending}")
    click.echo(f"\n=== matched {matched} / {len(results)} ===")
    for r in results:
        marker = "✓" if r.status == "matched" else "·"
        click.echo(
            f"  {marker} inbound={r.inbound_id}  listing={r.listing_id}  "
            f"matched_photos={r.matched_photos}  conf={r.confidence:.2f}"
        )

    remaining = await unresolved_counts()
    click.echo(
        f"NEW queue: needs_review={remaining['needs_review']} pending={remaining['pending']}"
    )
    if results or any(remaining.values()):
        client = await _open_userbot_if_needed()
        try:
            await send_run_summary(
                client,
                matched=matched,
                no_match=no_match,
                **remaining,
                duration_seconds=elapsed,
                label="match-all",
            )
        except Exception:
            logging.getLogger(__name__).exception("run summary delivery failed")
        finally:
            if client is not None:
                await client.disconnect()


@cli.command("dupes-report")
@click.option(
    "--write/--no-write",
    default=False,
    show_default=True,
    help="--write persists dup_group_id + is_primary to the DB; default --no-write only "
         "reports (safe to run any time).",
)
@click.option(
    "--hamming",
    type=int,
    default=None,
    help="Override DEDUP_MAX_HAMMING for this run.",
)
@click.option(
    "--min-shared",
    type=int,
    default=None,
    help="Override DEDUP_MIN_SHARED_PHOTOS for this run.",
)
def dupes_report(write: bool, hamming: int | None, min_shared: int | None) -> None:
    """A4: group active listings that are the same apartment (≥2 near-dup photos) and report.

    Default is read-only; pass --write to persist the dup_group_id + is_primary tags.
    """
    run_cooperatively(_dupes_report(write, hamming, min_shared))


async def _dupes_report(write: bool, hamming: int | None, min_shared: int | None) -> None:
    from src.matching.dedup import find_cross_platform_dupes

    stats = await find_cross_platform_dupes(
        max_hamming=hamming, min_shared_photos=min_shared, write=write
    )

    mode = "WROTE tags" if write else "read-only (no DB changes)"
    click.echo(
        f"=== Cross-platform dupes — {mode} ===\n"
        f"active scanned: {stats.active_scanned}   groups: {stats.groups_found}   "
        f"demoted (non-primary): {stats.listings_demoted}"
    )
    if not stats.groups:
        click.echo("(no duplicate groups found)")
        return
    for line in await _render_dupes_groups(stats):
        click.echo(line)


async def _render_dupes_groups(stats: DedupStats) -> list[str]:
    """Render one text block per dup group with the primary starred — shared by the
    `dupes-report` CLI command and the T1-4 nightly artifact, so Дарья видит одну и ту же
    курационную картинку (платформа / цена / #фото / URL) в консоли и в файле."""
    from sqlalchemy import select

    from src.db.models import Listing
    from src.db.session import get_session

    # Hydrate member display fields (platform / price / #photos) for every group member.
    member_ids = [mid for g in stats.groups for mid in g.member_ids]
    async with get_session() as s:
        rows = (
            await s.execute(
                select(
                    Listing.id, Listing.platform, Listing.price_eur,
                    Listing.photo_urls, Listing.url,
                ).where(Listing.id.in_(member_ids))
            )
        ).all()
    by_id = {r.id: r for r in rows}

    lines: list[str] = []
    for g in sorted(stats.groups, key=lambda gg: gg.dup_group_id):
        lines.append("")
        lines.append(f"— group {g.dup_group_id} ({len(g.member_ids)} listings) —")
        for mid in g.member_ids:
            r = by_id.get(mid)
            if r is None:
                continue
            marker = "★ primary" if mid == g.primary_id else "  dup    "
            price = f"{r.price_eur}€" if r.price_eur else "?€"
            n_photos = len(r.photo_urls) if r.photo_urls else 0
            lines.append(
                f"  {marker}  id={mid:<7d} {r.platform.value:12s} {price:>7s} "
                f"photos={n_photos:<3d} {r.url}"
            )
    return lines


async def _write_dupes_report() -> tuple[Path, DedupStats]:
    """T1-4/T3-2: compute cross-platform dupes and persist data/reports/dupes-YYYY-MM-DD.txt.

    Mode is D2's NIGHTLY_DUPES_WRITE: default `write=True` idempotently tags dup_group_id/is_primary
    (reset + re-tag active) so the liveness sweep can see a card is still alive on another platform;
    `false` keeps the pre-liveness report-only behaviour (no DB changes). Either way there's no
    auto-merge — the artifact is for Дарья's manual curation. Идемпотентно: повторный прогон за тот
    же день перезаписывает тот же файл. Returns (report path, DedupStats).
    """
    from src.matching.dedup import find_cross_platform_dupes

    write = get_settings().nightly_dupes_write
    stats = await find_cross_platform_dupes(write=write)

    # data/ не в гите — на свежем деплое каталога reports/ ещё нет.
    report_dir = get_settings().data_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"dupes-{datetime.now(timezone.utc).date().isoformat()}.txt"

    mode = "wrote dup_group tags" if write else "report-only (no DB changes)"
    lines = [
        f"Cross-platform dupes — {mode}",
        f"active scanned: {stats.active_scanned}   groups: {stats.groups_found}   "
        f"demoted (non-primary): {stats.listings_demoted}",
    ]
    if stats.groups:
        lines.extend(await _render_dupes_groups(stats))
    else:
        lines.append("(no duplicate groups found)")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path, stats


def _utc_today() -> date:
    """Единственное чтение wall-clock analytics-шага — тесты инжектят фикс-дату, чтобы
    прогон через UTC-полночь не давал ложных красных (двойное чтение часов SUT/ассертом)."""
    return datetime.now(UTC).date()


async def _analytics_nightly_line() -> str:
    """T2-4 nightly step (§N): догон unrolled_days() + rollup(сегодня) → retention().

    План дней — все дни с raw-событиями без маркера _rolled ПЛЮС «сегодня»: пропущенные
    ночи (VPS лежал, шаг падал) самоизлечиваются следующим прогоном, а «сегодня» получает
    партиал-агрегаты БЕЗ маркера (rollup ставит _rolled только дням < today) — финализирует
    его следующий nightly. Kill-switch SITE_ANALYTICS_ENABLED=false глушит шаг ЦЕЛИКОМ
    (rollback-раздел плана обещает полный откат одной настройкой): ни маркеров пустых дней,
    ни ротации raw — честная строка «off» в summary. Под-шаги изолированы ПО ОТДЕЛЬНОСТИ:
    retention гоняем и после упавшего rollup — он безопасен по построению (трогает только
    дни с маркером _rolled). Сбой любого под-шага → 🚨-фрагмент строки, исключение наружу
    не уходит — fault-isolation по контракту price-drop/site-retention/dupes-report."""
    from src.site import analytics

    if not get_settings().site_analytics_enabled:
        return "аналитика: off (SITE_ANALYTICS_ENABLED=false)"

    log = logging.getLogger(__name__)
    today = _utc_today()
    parts: list[str] = []
    try:
        days = [d for d in await analytics.unrolled_days() if d != today]
        days.append(today)  # «сегодня» пересчитываем всегда (партиал без маркера)
        for day in days:
            await analytics.rollup(day, today=today)
        parts.append("rollup " + "+".join(d.isoformat() for d in days) + " ок")
    except Exception:
        log.exception("nightly step failed: analytics-rollup")
        parts.append("🚨 rollup упал — см. логи")
    try:
        deleted = await analytics.retention()
        parts.append(f"raw удалено: {deleted}")
    except Exception:
        log.exception("nightly step failed: analytics-retention")
        parts.append("🚨 retention упал — см. логи")
    return "аналитика: " + ", ".join(parts)


async def _dupes_report_nightly_line() -> str:
    """T1-4 nightly step, fault-isolated по контракту price-drop/site-retention: сбой шага
    становится 🚨-строкой summary, исключение наружу не уходит — nightly живёт дальше."""
    try:
        path, stats = await _write_dupes_report()
        return (
            f"дубли (report-only): групп {stats.groups_found}, "
            f"объявлений {stats.listings_grouped} — {path.name}"
        )
    except Exception as error:
        if isinstance(error, RuntimeError) and str(error) == "identity_edge_budget":
            # The bounded edge guard stopped the report: a known limit, not a failed step.
            return "⚠️ дубли: отчёт пропущен, лимит рёбер (identity_edge_budget)"
        logging.getLogger(__name__).exception("nightly step failed: dupes-report")
        return "🚨 dupes-report шаг упал — см. логи"


async def _liveness_nightly_line(*, notify: bool = True, max_hides: int | None = None) -> str:
    """T3-4 nightly step — liveness sweep + notice, fault-isolated (сбой → 🚨-строка, nightly жив).
    Kill switch SITE_LIVENESS_ENABLED=0 глушит шаг целиком (сводка получает строку «выкл»)."""
    if not get_settings().site_liveness_enabled:
        return "liveness: выкл (SITE_LIVENESS_ENABLED=0)"
    try:
        from src.site.liveness import send_liveness_notice, site_liveness_sweep

        report = await site_liveness_sweep(max_hides=max_hides)
        sent = await send_liveness_notice(report) if notify else False
        note = " — нотис отправлен" if sent else ""
        errors = getattr(report, "errors", 0)
        prefix = "⚠️ " if errors else ""
        note += (f", проверок {getattr(report, 'checked', 0)}, "
                 f"отложено {getattr(report, 'skipped', 0)}, ошибок {errors}")
        return (
            f"{prefix}liveness: снято {len(report.hidden)}, "
            f"подтв. {len(report.candidates_confirmed)}, "
            f"неподтв. {len(report.candidates_unconfirmed)}, "
            f"держим {report.kept_active + report.kept_pinned}{note}"
        )
    except Exception:
        logging.getLogger(__name__).exception("nightly step failed: site-liveness")
        return "🚨 liveness шаг упал — см. логи"


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
    run_cooperatively(_embed_all(target.lower(), force))


async def _listing_ids_needing_embeddings(force: bool) -> list[int]:
    """Active-listing ids for embed-all. Column-only select: full ORM rows would pull every
    photo_embeddings BLOB (hundreds of MB on 20k+ rows) just to pick ids."""
    from src.db.session import get_session

    stmt = select(Listing.id).where(Listing.is_active.is_(True)).order_by(Listing.id)
    if not force:
        stmt = stmt.where(
            (Listing.photo_embeddings.is_(None)) | (Listing.photo_embeddings == b""),
            Listing.photo_phashes != [],
        )
    async with get_session() as s:
        return list((await s.execute(stmt)).scalars().all())


async def _inbound_ids_needing_embeddings(force: bool) -> list[int]:
    """InboundPost ids for embed-all — same column-only rationale as the listings variant."""
    from src.db.models import InboundPost
    from src.db.session import get_session

    stmt = select(InboundPost.id).order_by(InboundPost.id)
    if not force:
        stmt = stmt.where(
            (InboundPost.photo_embeddings.is_(None)) | (InboundPost.photo_embeddings == b""),
            InboundPost.photo_paths != [],
        )
    async with get_session() as s:
        return list((await s.execute(stmt)).scalars().all())


async def _embed_all(target: str, force: bool) -> None:
    from pathlib import Path

    from src.db.models import InboundPost, Listing
    from src.db.session import get_session
    from src.matching.clip_embed import compute_embeddings_batch, pack_embeddings

    if target in ("listings", "both"):
        ids_to_do = await _listing_ids_needing_embeddings(force)
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
        ids_to_do = await _inbound_ids_needing_embeddings(force)
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
    run_cooperatively(_hash_listings(platforms, limit))


async def _listing_rows_needing_phash(
    platform_names: tuple[str, ...], limit: int | None
) -> list[Any]:
    """(id, platform, external_id, url, photo_urls) rows for hash-listings — column-only
    select keeps the photo_embeddings BLOBs out of memory (same rationale as embed-all)."""
    from src.db.session import get_session

    stmt = (
        select(
            Listing.id, Listing.platform, Listing.external_id, Listing.url, Listing.photo_urls
        )
        .where(Listing.is_active.is_(True))
        .where(Listing.photo_phashes == [], Listing.photo_urls != [])
        .order_by(Listing.id)
    )
    if platform_names:
        chosen = [PLATFORM_NAMES[p.lower()] for p in platform_names]
        stmt = stmt.where(Listing.platform.in_(chosen))
    if limit is not None:
        stmt = stmt.limit(limit)
    async with get_session() as s:
        return list((await s.execute(stmt)).all())


async def _hash_listings(platform_names: tuple[str, ...], limit: int | None) -> None:
    import httpx

    from src.db.session import get_session
    from src.matching.phash import download_and_hash
    from src.scrapers.orchestrator import _photo_request_headers

    settings = get_settings()
    settings.ensure_dirs()

    to_hash = await _listing_rows_needing_phash(platform_names, limit)
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
    run_cooperatively(_test_batch(n, from_id))


async def _test_batch(n: int, from_id: int | None) -> None:
    import time as _time

    from sqlalchemy import delete, update

    from src.db.models import (
        InboundPost,
        InboundStatus,
        Match,
        MatchStatus,
        PublishedPost,
        ValidationJob,
    )
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

    # Operator decisions are never wiped: skip posts with a manual decision or a rejected match.
    async with get_session() as s:
        decided = set(
            (
                await s.execute(
                    select(ValidationJob.inbound_id).where(
                        ValidationJob.inbound_id.in_(inbound_ids),
                        ValidationJob.reason_code.like("manual_%"),
                    )
                )
            ).scalars().all()
        )
        decided |= set(
            (
                await s.execute(
                    select(Match.inbound_post_id).where(
                        Match.inbound_post_id.in_(inbound_ids),
                        Match.status == MatchStatus.REJECTED,
                    )
                )
            ).scalars().all()
        )
    if decided:
        click.echo(f"  skipped posts with operator decisions: {sorted(decided)}")
        inbound_ids = [i for i in inbound_ids if i not in decided]
        if not inbound_ids:
            click.echo("  (nothing left to test)")
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
        await s.execute(delete(ValidationJob).where(ValidationJob.inbound_id.in_(inbound_ids)))
    click.echo(f"  reset: dropped {len(match_ids)} prior matches")

    # Open a Telethon client only if we need it (legacy userbot mode).
    # In bot mode we never touch Telethon here — that avoids SQLite session-file lock conflicts
    # with the long-running otz-monitor.service.
    client = await _open_userbot_if_needed()

    from src.matching.validation_queue import unresolved_counts

    stats = {"matched": 0, "no_match": 0, "errors": 0, "pending": 0}
    started = _time.monotonic()
    try:
        for iid in inbound_ids:
            try:
                result = await process_new_inbound(iid, client=client, refresh_listings=False)
                if result.status == "matched":
                    stats["matched"] += 1
                    click.echo(f"  ✓ inbound={iid} → match (conf={result.confidence:.2f}, {result.method})")
                elif result.status == "no_match":
                    stats["no_match"] += 1
                    click.echo(f"  · inbound={iid} → no_match")
                else:
                    stats["pending"] += 1
                    click.echo(f"  ⏳ inbound={iid} → validation_pending")
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
                **(await unresolved_counts(inbound_ids)),
                duration_seconds=elapsed,
                label=f"test-batch (последних {len(inbound_ids)})",
            )
        except Exception:
            logging.getLogger(__name__).exception("run summary delivery failed")
    finally:
        if client is not None:
            await client.disconnect()

    state = "REQUIRES_ATTENTION" if stats["pending"] or stats["errors"] else "DONE"
    click.echo(
        f"\n=== {state}: matched={stats['matched']} no_match={stats['no_match']} "
        f"pending={stats['pending']} errors={stats['errors']} ==="
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
    from src.telegram.monitor import connect_authorized, make_client
    c = make_client()
    # Service context: never prompt for a code. A dead session raises SessionNotAuthorized,
    # which surfaces loudly to the caller instead of hanging on start()'s phone prompt.
    await connect_authorized(c)
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
    run_cooperatively(_photo_stats(since_str, platform))


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


@cli.command("photos-backfill")
@click.option("--dry-run", is_flag=True, help="Count the work; write no variants, no manifest.")
@click.option("--limit", type=int, default=None, help="Cap number of originals processed.")
def photos_backfill(dry_run: bool, limit: int | None) -> None:
    """Generate card/detail webp variants + photo_manifest.json under SITE_PHOTOS_ROOT (§P.1).

    Идемпотентен: существующие варианты скипаются, битые оригиналы — скип+warn (не падение).
    Варианты и манифест — derived data: в git/rsync не участвуют, команда гоняется НА VPS
    (раннбук Wave 6; publish-hook покрывает только новые публикации).
    """
    from src.site.photos import backfill_variants, photos_root

    root = photos_root()
    click.echo(f"→ photos-backfill: root={root}{' [DRY-RUN]' if dry_run else ''}")

    def _progress(done: int, total: int) -> None:
        # Каданс: не чаще раза на 50 файлов — на VPS тысячи фото, лог не должен тонуть.
        if done % 50 == 0 or done == total:
            click.echo(f"  … {done}/{total}")

    stats = backfill_variants(root=root, limit=limit, dry_run=dry_run, progress=_progress)
    verb = "would generate" if dry_run else "generated"
    click.echo(
        f"=== DONE: originals={stats.total} {verb}={stats.generated} "
        f"skipped={stats.skipped} broken={stats.broken} ==="
    )


@cli.command("admin")
@click.option("--host", default=None, help="Override ADMIN_HOST.")
@click.option("--port", default=None, type=int, help="Override ADMIN_PORT.")
@click.option("--reload", is_flag=True, help="Auto-reload on code changes (dev only).")
def admin(host: str | None, port: int | None, reload: bool) -> None:
    """Launch the web admin (read-only viewer). Basic auth: ADMIN_USERNAME / ADMIN_PASSWORD."""
    import uvicorn

    # CSRF token is per-process (src/admin/csrf.py) ⇒ single-worker uvicorn only:
    # --workers N would render worker A's token and verify against worker B's → random 403s.
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
@click.option("--dry-run", is_flag=True, help="Evaluate without database/media writes or messages.")
@click.option("--no-notify", is_flag=True, help="Run maintenance without external messages.")
@click.option(
    "--incremental",
    is_flag=True,
    help="Use the cheap recent-listings walk; defer full coverage/stale-sweep to the weekly job.",
)
def nightly_refresh(
    max_pages: int,
    inactive_after_days: int,
    skip_sweep: bool,
    skip_scrape: bool,
    incremental: bool,
    dry_run: bool,
    no_notify: bool,
) -> None:
    """Nightly maintenance: full scrape by default, optional incremental scrape.

    The canonical daily and weekly systemd units both use full mode. ``--incremental``
    remains an explicit operator option and skips the coverage/stale sweep.
    """
    run_cooperatively(
        _nightly_refresh(max_pages, inactive_after_days, skip_sweep, skip_scrape, incremental,
                         dry_run=dry_run, no_notify=no_notify)
    )


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
    run_cooperatively(_nightly_refresh(max_pages, inactive_after_days, skip_sweep, skip_scrape))


@cli.command("site-liveness")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Compute the sweep and print the report + notice preview WITHOUT hiding anything.",
)
def site_liveness(dry_run: bool) -> None:
    """Liveness sweep (replaces the age-based «пора убрать» retention notice, D5).

    A public card stays up while its apartment is listed on >=1 platform (its A4 dup-group) and only
    comes down when confirmed gone from all. Prints the report + a notice preview; --dry-run mutates
    nothing (Wave-6 triage). The nightly job runs the same sweep and actually sends the notice."""
    run_cooperatively(_site_liveness(dry_run))


async def _site_liveness(dry_run: bool) -> None:
    from src.site.liveness import format_liveness_notice, site_liveness_sweep

    report = await site_liveness_sweep(dry_run=dry_run)
    click.echo(
        f"=== Liveness sweep{' (dry-run)' if dry_run else ''} ===\n"
        f"держим: активные {report.kept_active}, pinned {report.kept_pinned}   "
        f"снято: {len(report.hidden)}   подтв. мертвы: {len(report.candidates_confirmed)}   "
        f"неподтв.: {len(report.candidates_unconfirmed)}   errors: {report.errors}"
    )
    notice = format_liveness_notice(report)
    if notice:
        click.echo("\n--- нотис (превью) ---")
        click.echo(notice)
    else:
        click.echo("(нечего слать — всё живо)")


def _parse_rate_degraded(
    results: list[ScrapeResult],
    *,
    parse_rate_min: float,
    parse_rate_min_sample: int,
) -> list[str]:
    """B1: one 🚨 line per platform whose detail parse-rate fell below `parse_rate_min` with a
    big-enough sample (details_attempted >= parse_rate_min_sample). A low rate on a tiny sample —
    or zero attempts (parse_rate==1.0) — does NOT alert: that's noise, not a markup change.

    Sits alongside the dead-source / low-coverage guards (all computed inline from the per-run
    ScrapeResult list) — the parser-degradation signal is the same kind of per-run quality gate."""
    alerts: list[str] = []
    for r in results:
        if r.details_attempted < parse_rate_min_sample:
            continue
        if r.parse_rate < parse_rate_min:
            pct = round(100 * r.parse_rate)
            alerts.append(
                f"🚨 {r.platform.value}: parse-rate {pct}% "
                f"({r.details_parsed_ok}/{r.details_attempted}) — вероятно сменилась разметка"
            )
    return alerts


async def _nightly_refresh(
    max_pages: int, inactive_after_days: int, skip_sweep: bool, skip_scrape: bool,
    incremental: bool = False, *, dry_run: bool = False, no_notify: bool = False,
) -> None:
    from src.jobs.run_history import finish_refresh, start_refresh

    settings = get_settings()
    budget = (settings.scrape_incremental_run_budget_sec if incremental
              else settings.scrape_run_budget_sec)
    token = None if dry_run else start_refresh(incremental=incremental)
    error = None
    try:
        with run_budget(budget):
            async with asyncio.timeout(budget):
                await _nightly_refresh_impl(
                    max_pages, inactive_after_days, skip_sweep, skip_scrape, incremental,
                    dry_run=dry_run, no_notify=no_notify,
                )
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        if token is not None:
            finish_refresh(token, error=error)


def _nightly_unhealthy(*, dead, low_coverage, degraded, parse_degraded, sweep_reasons,
                       maintenance_failed, results) -> bool:
    """Exit 1 only for real failures: dead platform, incomplete coverage, failed steps (🚨).

    Item errors (⚠️ Ошибки: N) stay warnings in the summary. Partial crawls (skipped pages),
    failed steps and dead or low-coverage platforms still fail the night.
    """
    return bool(dead or low_coverage or degraded or parse_degraded or sweep_reasons
                or maintenance_failed or any(r.outcome in {"failed", "degraded"} for r in results))


async def _nightly_refresh_impl(
    max_pages: int,
    inactive_after_days: int,
    skip_sweep: bool,
    skip_scrape: bool,
    incremental: bool = False,
    *,
    dry_run: bool = False,
    no_notify: bool = False,
) -> None:
    import time as _time

    from src.scrapers.orchestrator import mark_stale_inactive
    from src.telegram.delivery import send_text_to_recipients

    started = _time.monotonic()
    results = []
    sweep_result = None

    if not skip_scrape:
        click.echo(
            f"→ {'Incremental' if incremental else 'Full'} scrape "
            f"(incremental={incremental}, max_pages={max_pages})…"
        )
        results = await _run_all(
            list(SCRAPERS.keys()),
            max_pages=max_pages,
            max_new=None,
            download_photos=True,
            incremental=incremental,
            **({"dry_run": True} if dry_run else {}),
        )
        _print_summary(results)

    sweep_skipped_platforms: list[str] = []
    sweep_reasons: list[str] = []
    if not skip_sweep and not incremental:
        from src.scrapers.orchestrator import StaleSweepResult

        total_marked = 0
        by_platform = {r.platform: r for r in results}
        for plat in SCRAPERS:
            evidence = by_platform.get(plat)
            if evidence is None or not evidence.sweep_allowed:
                sweep_skipped_platforms.append(plat.value)
                continue
            res = await mark_stale_inactive(
                inactive_after_days=inactive_after_days, platform=plat,
                source_result=evidence, dry_run=dry_run,
            )
            if res.blocked_reasons:
                sweep_reasons.append(f"{plat.value}: {', '.join(res.blocked_reasons)}")
            total_marked += res.would_mark_inactive if dry_run else res.marked_inactive
        sweep_result = StaleSweepResult(platform=None, marked_inactive=total_marked)
        click.echo(f"  {'would mark' if dry_run else 'marked'} {total_marked} listings inactive")
        if sweep_skipped_platforms:
            skipped = ", ".join(sweep_skipped_platforms)
            click.echo(f"  sweep blocked without verified full crawl: {skipped}")
        for reason in sweep_reasons:
            click.echo(f"  sweep blocked: {reason}")
    elif incremental:
        click.echo("  stale sweep deferred to verified weekly full refresh")

    if dry_run:
        from src.site.liveness import site_liveness_sweep

        report = await site_liveness_sweep(dry_run=True)
        count = len(report.candidates_confirmed)
        click.echo(f"DRY RUN: no writes/messages; confirmed public candidates={count}")
        if any(r.errors or r.outcome in {"failed", "degraded"} for r in results):
            raise SystemExit(1)
        return
    if skip_scrape:
        return

    # Post-sweep maintenance steps (heal / rolling refresh / metrics / prune). Each one is
    # env-disableable (=0) and fault-isolated — see _run_nightly_steps.
    steps = await _run_nightly_steps(**({"notify": False} if no_notify else {}))

    # W3-G3: price-drop alerts off the W2-C2 `steps.price_changes` contract. Fault-isolated
    # like the other nightly extras — a failure becomes a 🚨 summary line, never kills nightly.
    price_alerts: list[str] = []
    price_line: str | None = None
    try:
        price_alerts, price_line = await _build_price_drop_alerts(steps.price_changes)
    except Exception:
        logging.getLogger(__name__).exception("nightly step failed: price-drop")
        price_line = "🚨 price-drop шаг упал — см. логи"

    # Liveness sweep (D5, replaces the age-based 21-day retention notice): a card comes down only
    # when its apartment is confirmed gone from EVERY platform (its dup-group), never merely by age.
    # Fault-isolated inside the helper; kill switch SITE_LIVENESS_ENABLED=0.
    click.echo("→ Site-listing liveness sweep…")
    liveness_line = await _liveness_nightly_line(
        notify=not no_notify,
        max_hides=max(0, get_settings().site_liveness_max_hides - steps.site_listings_hidden),
    )
    click.echo(f"  {liveness_line}")

    # T2-4: site-аналитика — догон несагрегированных дней + сегодня → retention raw
    # (§N, ПЕРЕД dupes-шагом); SITE_ANALYTICS_ENABLED=false глушит шаг целиком.
    # Fault isolation внутри хелпера: сбой под-шага → 🚨-фрагмент строки summary.
    click.echo("→ Site analytics rollup + retention…")
    analytics_line = await _analytics_nightly_line()
    click.echo(f"  {analytics_line}")

    # T1-4: report-only дедуп-артефакт (data/reports/dupes-YYYY-MM-DD.txt) для курации Дарьи —
    # авто-мержа НЕТ (решение D6). Fault isolation внутри хелпера: сбой → 🚨-строка summary.
    click.echo("→ Cross-platform dupes report (report-only)…")
    dupes_line = await _dupes_report_nightly_line()
    click.echo(f"  {dupes_line}")

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
    lines.extend(steps.summary_lines)
    if price_line:
        lines.append(price_line)
    lines.append(analytics_line)  # T2-4: rollup/retention — всегда в summary (в т.ч. «удалено 0»)
    lines.append(dupes_line)  # T1-4: счётчик дублей — всегда в summary (0 тоже информативен)
    lines.append(liveness_line)  # T3-4: liveness — всегда в summary (снято/кандидаты/держим)
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
            if not incremental and r.platform.value in {"NEKRETNINE", "CETIRI_ZIDA", "HALOOGLASI"}:
                if (r.denominator_trusted and r.site_total is not None
                        and r.site_total > 0 and r.fetched <= r.site_total):
                    pct = 100 * r.fetched / r.site_total
                    extra += (f"  в базе {r.fetched}/{r.site_total} ({pct:.1f}%), "
                              f"не хватает {r.site_total - r.fetched}")
                    if not r.crawl_complete:
                        extra += " [неполный обход]"
                else:
                    extra += "  полнота базы не подтверждена"
            if r.skipped_pages:
                extra += f"  ⚠️ пропущено стр.: {r.skipped_pages}"
            if r.details_attempted:
                extra += (
                    f"  parse-rate {round(100 * r.parse_rate)}% "
                    f"({r.details_parsed_ok}/{r.details_attempted})"
                )
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
    low_coverage = [] if incremental else [
        f"{r.platform.value} ({r.previewed}/{r.site_total})"
        for r in results
        if r.site_total and r.previewed < 0.7 * r.site_total
    ]
    # B1: parser-degradation guard — same per-run quality gate as dead/low-coverage above.
    _settings = get_settings()
    parse_degraded = _parse_rate_degraded(
        results,
        parse_rate_min=_settings.parse_rate_min,
        parse_rate_min_sample=_settings.parse_rate_min_sample,
    )
    if dead:
        lines.append("")
        lines.append(f"🚨 ВНИМАНИЕ: площадки без результатов: {', '.join(dead)} — проверь скрейпер!")
    if low_coverage:
        lines.append(f"🚨 Низкое покрытие: {', '.join(low_coverage)} — кап пагинации/сегменты?")
    for pd_line in parse_degraded:
        lines.append(pd_line)
    if degraded:
        lines.append(f"⚠️ Частичный обход (пропущены страницы): {', '.join(degraded)}")
    if sweep_skipped_platforms:
        lines.append(f"⚠️ Sweep пропущен (нездоровый скрейп): {', '.join(sweep_skipped_platforms)}")
    for reason in sweep_reasons:
        lines.append(f"Sweep blocked: {reason}")
    if incremental:
        lines.append("ℹ️ Daily incremental run: stale sweep deferred to weekly full refresh")
    maintenance_failed = any("🚨" in line for line in [
        *steps.summary_lines, liveness_line, analytics_line, dupes_line,
    ])
    unhealthy = _nightly_unhealthy(
        dead=dead, low_coverage=low_coverage, degraded=degraded, parse_degraded=parse_degraded,
        sweep_reasons=sweep_reasons, maintenance_failed=maintenance_failed, results=results,
    )
    from src.jobs.run_history import record_refresh_result

    record_refresh_result(results, incremental=incremental,
                          status="degraded" if unhealthy else "success", summary=lines)
    click.echo("\n".join(lines))
    if not no_notify:
        client = await _open_userbot_if_needed()
        try:
            # bot mode → fans out to all chat_ids; userbot mode → Saved Messages.
            try:
                await send_text_to_recipients(client, "\n".join(lines))
            except Exception:
                logging.getLogger(__name__).exception("nightly refresh summary delivery failed")
            # Active-drop + price-drop alerts go out as standalone messages too — they must not
            # drown in the summary wall of text.
            for alert in [*steps.alerts, *price_alerts, *parse_degraded]:
                try:
                    await send_text_to_recipients(client, alert)
                except Exception:
                    logging.getLogger(__name__).exception("nightly drop alert delivery failed")
        finally:
            if client is not None:
                await client.disconnect()

    if unhealthy:
        raise SystemExit(1)


class NightlyStepsResult(NamedTuple):
    """_run_nightly_steps outcome — named because two same-typed str-list members
    positionally would be swap-prone."""

    summary_lines: list[str]  # ready-made lines for the nightly telegram summary
    price_changes: list[PriceChange]  # W2-C2 contract for the Wave-3 price-drop alert
    alerts: list[str]  # standalone 🚨 messages (must not drown in the summary)
    site_listings_hidden: int = 0


async def _run_nightly_steps(*, notify: bool = True) -> NightlyStepsResult:
    """Run the post-sweep nightly maintenance steps (W2-C1/C2/C5/C3).

    Every step is optional (its env knob =0 → skipped entirely) and fault-isolated: a step
    failure adds a 🚨 line to the returned summary lines instead of killing the nightly.
    """
    # In-function import is the test seam: names resolve at call time, so tests can
    # monkeypatch them on the orchestrator module — a top-level from-import would freeze
    # the originals (and the import stays lazy for unrelated CLI commands).
    from src.scrapers.orchestrator import (
        auto_publish_new_listings,
        heal_broken_listings,
        prune_inactive_embeddings,
        rolling_detail_refresh,
        snapshot_daily_metrics,
        verify_active_links,
    )

    import time as _time

    settings = get_settings()
    log = logging.getLogger(__name__)
    lines: list[str] = []
    alerts: list[str] = []
    price_changes: list[PriceChange] = []
    site_listings_hidden = 0

    # Reserve time for later queues and final publication/analytics work. Unused time
    # flows forward; a hanging heal cannot consume the rolling refresh or link slice.
    refetch_start = _time.monotonic()
    refetch_budget = remaining_budget(settings.nightly_refetch_budget_sec, reserve=900)
    # All-inventory refresh already prioritizes broken rows. Do not spend a separate
    # browser/timeout slice attempting the same broken inventory before that pass.
    heal_enabled = settings.nightly_heal_limit > 0 and not settings.nightly_detail_refresh_all
    weights = [
        ("heal", 0.2, heal_enabled),
        ("rolling", 0.7,
         settings.nightly_detail_refresh_all or settings.nightly_detail_refresh > 0),
        ("links", 0.1, settings.nightly_verify_links),
    ]
    enabled_weight = sum(weight for _, weight, enabled in weights if enabled) or 1
    deadlines = {}
    cumulative = 0.0
    for name, weight, enabled in weights:
        if enabled:
            cumulative += weight / enabled_weight
        deadlines[name] = refetch_start + refetch_budget * cumulative

    if heal_enabled:
        click.echo(f"\n→ Heal broken listings (limit={settings.nightly_heal_limit})…")
        try:
            heal = await heal_broken_listings(
                limit=settings.nightly_heal_limit, deadline=deadlines["heal"]
            )
            line = f"heal: исправлено {heal.fixed} / осталось {heal.remaining}"
            if heal.skipped:
                line += f", пропущено по бюджету: {heal.skipped}"
            if heal.errors or heal.skipped or getattr(heal, "source_errors", {}):
                # Source failures stay alarms; item errors and budget skips are warnings.
                alarm = "🚨 " if getattr(heal, "source_errors", {}) else "⚠️ "
                line = (f"{alarm}{line}, ошибок: {heal.errors}; "
                        f"ошибок источников: {len(getattr(heal, 'source_errors', {}))}")
            lines.append(line)
        except Exception:
            log.exception("nightly step failed: heal")
            lines.append("🚨 heal-шаг упал — см. логи")
    elif settings.nightly_detail_refresh_all:
        click.echo("→ Broken listings included in the all-inventory refresh")
    else:
        click.echo("→ Heal step disabled (NIGHTLY_HEAL_LIMIT=0)")

    if settings.nightly_detail_refresh_all or settings.nightly_detail_refresh > 0:
        scope = ("all inventory" if settings.nightly_detail_refresh_all
                 else str(settings.nightly_detail_refresh))
        click.echo(f"→ Rolling detail refresh (limit={scope})…")
        try:
            selection = {"all_listings": True} if settings.nightly_detail_refresh_all else {}
            rolling = await rolling_detail_refresh(
                limit=settings.nightly_detail_refresh, deadline=deadlines["rolling"], **selection,
            )
            price_changes = rolling.price_changes
            line = (
                f"деталей обновлено: {rolling.refreshed}, "
                f"цен изменилось: {len(rolling.price_changes)}"
            )
            if rolling.skipped:
                line += f", пропущено по бюджету: {rolling.skipped}"
            if settings.nightly_detail_refresh_all:
                line += (f", выбрано: {rolling.selected} / база: {rolling.inventory_count}, "
                         f"ожидают повторной попытки: {rolling.retry_deferred}")
            if (rolling.errors or rolling.skipped or getattr(rolling, "source_errors", {})
                    or (settings.nightly_detail_refresh_all and rolling.retry_deferred)):
                alarm = "🚨 " if getattr(rolling, "source_errors", {}) else "⚠️ "
                line = (f"{alarm}{line}, ошибок: {rolling.errors}; "
                        f"ошибок источников: {len(getattr(rolling, 'source_errors', {}))}")
            lines.append(line)
        except Exception:
            log.exception("nightly step failed: detail-refresh")
            lines.append("🚨 detail-refresh шаг упал — см. логи")
    else:
        click.echo("→ Rolling detail refresh disabled (NIGHTLY_DETAIL_REFRESH=0)")

    if settings.nightly_verify_links:
        click.echo(
            f"→ Verify active links (limit={settings.nightly_verify_links_limit})…"
        )
        try:
            # The final reserved slice includes unused time from earlier queues.
            vl = await verify_active_links(
                limit=settings.nightly_verify_links_limit, deadline=deadlines["links"]
            )
            line = (
                f"ссылки: проверено {vl.checked}, "
                f"деактивировано {vl.deactivated}, ошибок {vl.errors}"
            )
            site_listings_hidden = vl.site_listings_hidden
            if vl.site_listings_hidden:
                line += f", скрыто карточек с сайта: {vl.site_listings_hidden}"  # X2 auto-remove
            if vl.skipped:
                line += f", пропущено по бюджету: {vl.skipped}"
            if vl.errors or vl.skipped:
                # Skipped link checks stay an alarm: unverified links can leave dead listings up.
                line = ("🚨 " if vl.skipped else "⚠️ ") + line
            lines.append(line)
        except Exception:
            log.exception("nightly step failed: verify-links")
            lines.append("🚨 verify-links шаг упал — см. логи")
    else:
        click.echo("→ Verify links disabled (NIGHTLY_VERIFY_LINKS=0)")

    # X1 auto-publish backfill: expose any delivered-but-not-yet-published matches (hot-path
    # hook missed: kill switch was on, a helper exception, or an older deploy). Fault-isolated.
    if settings.nightly_auto_publish:
        click.echo("→ Auto-publish backfill (delivered matches → site)…")
        try:
            exposed = await auto_publish_new_listings(since_hours=24)
            if exposed:
                lines.append(f"🆕 авто-публикация на сайт: {exposed}")
        except Exception:
            log.exception("nightly step failed: auto-publish-backfill")
            lines.append("🚨 auto-publish backfill упал — см. логи")
    else:
        click.echo("→ Auto-publish backfill disabled (NIGHTLY_AUTO_PUBLISH=0)")

    if notify and settings.tg_delivery_mode == "bot":
        from src.telegram.bot_delivery import retry_pending_match_deliveries

        try:
            deliveries = await retry_pending_match_deliveries(limit=30)
            prefix = "🚨 " if deliveries["errors"] or deliveries["unknown"] else ""
            if any(deliveries.values()):
                lines.append(
                    f"{prefix}Finder queue: completed {deliveries['completed']}; "
                    f"errors {deliveries['errors']}; unknown {deliveries['unknown']}"
                )
        except Exception:
            log.exception("nightly step failed: finder-delivery-retry")
            lines.append("🚨 Finder queue step failed")

    # C2 saved-search subscriptions: new public listings × active subscriptions → digests
    # (idempotent via subscription_matches). In-function import is the test seam (tests
    # monkeypatch notify_subscriptions on its module). Fault-isolated like every other step.
    if notify and settings.nightly_notify_subscriptions:
        click.echo("→ Notify saved-search subscriptions…")
        try:
            from src.jobs import notify_subscriptions as _notify_subs_mod

            sub_stats = await _notify_subs_mod.notify_subscriptions(
                since_hours=settings.subscription_notify_since_hours
            )
            line = f"подписки: {sub_stats.subscriptions_notified} уведомлений отправлено"
            if sub_stats.errors:
                line = "🚨 " + line + f", ошибок доставки: {sub_stats.errors}"
            lines.append(line)
        except Exception:
            log.exception("nightly step failed: notify-subscriptions")
            lines.append("🚨 подписки-шаг упал — см. логи")
    else:
        click.echo("→ Subscription notifications disabled (NIGHTLY_NOTIFY_SUBSCRIPTIONS=0)")

    if settings.nightly_metrics:
        click.echo("→ Daily metrics snapshot…")
        try:
            snap = await snapshot_daily_metrics()
            drop_pct = round(snap.drop_threshold * 100)
            for plat_value, prev_active, cur_active, prev_date in snap.drops:
                alert = (
                    f"🚨 {plat_value}: active {prev_active} → {cur_active} "
                    f"(падение >{drop_pct}% против прошлого снапшота {prev_date}) — "
                    "проверь скрейпер, sweep мог деактивировать живые объявления!"
                )
                lines.append(alert)
                alerts.append(alert)
        except Exception:
            log.exception("nightly step failed: metrics")
            lines.append("🚨 metrics-шаг упал — см. логи")
    else:
        click.echo("→ Metrics snapshot disabled (NIGHTLY_METRICS=0)")

    if settings.nightly_prune_embeddings:
        click.echo("→ Prune embeddings of long-inactive listings…")
        try:
            pruned = await prune_inactive_embeddings()
            lines.append(f"embeddings очищено (неактивные >30 дн): {pruned}")
        except Exception:
            log.exception("nightly step failed: prune")
            lines.append("🚨 prune-шаг упал — см. логи")
    else:
        click.echo("→ Embeddings prune disabled (NIGHTLY_PRUNE_EMBEDDINGS=0)")

    return NightlyStepsResult(summary_lines=lines, price_changes=price_changes, alerts=alerts,
                             site_listings_hidden=site_listings_hidden)


# --------------------------------------------------------------------------- W3-G3 price drops

# Bot send path has no flood-wait retry; a mass repricing must not burst-spam recipients.
PRICE_DROP_MAX_ALERTS = 10


def _select_price_drops(
    price_changes: list[PriceChange], *, min_pct: float
) -> list[tuple[PriceChange, float]]:
    """Keep only real drops of ≥ min_pct percent: old and new both known, 0 < new < old.

    Returns (change, drop_pct) pairs; pct is computed against the old price.
    """
    out: list[tuple[PriceChange, float]] = []
    for change in price_changes:
        old, new = change.old_price_eur, change.new_price_eur
        # new <= 0 is a scrape glitch (failed price parse), not a «→ 0€ (−100%)» bargain.
        if old is None or new is None or old <= 0 or new <= 0 or new >= old:
            continue
        pct = (old - new) / old * 100
        if pct >= min_pct:
            out.append((change, pct))
    return out


async def _build_price_drop_alerts(
    price_changes: list[PriceChange],
) -> tuple[list[str], str | None]:
    """Compose «📉 Цена снизилась» messages for dropped listings with a live match.

    Only listings that have a PENDING or PUBLISHED match qualify — nobody cares about price
    moves on listings we never delivered. Gated by PRICE_DROP_ALERTS (returns ([], None) when
    off); threshold = PRICE_DROP_MIN_PCT. Returns (standalone alert texts, summary line).

    Standalone sends are capped at PRICE_DROP_MAX_ALERTS (top-N by drop pct); the summary
    line always counts ALL eligible drops.
    """
    from sqlalchemy.orm import selectinload

    from src.db.models import InboundPost, Match, MatchStatus
    from src.db.session import get_session

    settings = get_settings()
    if not settings.price_drop_alerts:
        return [], None

    drops = _select_price_drops(price_changes, min_pct=settings.price_drop_min_pct)
    eligible: list[tuple[float, str]] = []
    if drops:
        async with get_session() as s:
            for change, pct in drops:
                match_row = (
                    await s.execute(
                        select(Match)
                        .where(
                            Match.listing_id == change.listing_id,
                            Match.status.in_([MatchStatus.PENDING, MatchStatus.PUBLISHED]),
                        )
                        .order_by(Match.id.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if match_row is None:
                    continue
                listing = await s.get(Listing, change.listing_id)
                if listing is None:
                    continue
                inbound = (
                    await s.execute(
                        select(InboundPost)
                        .options(selectinload(InboundPost.source_channel))
                        .where(InboundPost.id == match_row.inbound_post_id)
                    )
                ).scalar_one_or_none()
                # Source reference mirrors delivery.py: t.me link for public channels,
                # otherwise the inbound DB id.
                source = None
                if inbound is not None:
                    chan = inbound.source_channel
                    if chan is not None and chan.username:
                        source = f"https://t.me/{chan.username}/{inbound.tg_message_id}"
                    else:
                        source = f"inbound #{inbound.id}"
                text_lines = [
                    f"📉 Цена снизилась: {change.old_price_eur}€ → {change.new_price_eur}€ "
                    f"(−{pct:.0f}%)",
                    f"🔗 площадка: {listing.url}",
                ]
                if source:
                    text_lines.append(f"📥 источник: {source}")
                eligible.append((pct, "\n".join(text_lines)))
    # Cap standalone sends to the biggest drops — see PRICE_DROP_MAX_ALERTS.
    eligible.sort(key=lambda pair: pair[0], reverse=True)
    alerts = [text for _, text in eligible[:PRICE_DROP_MAX_ALERTS]]
    overflow = len(eligible) - PRICE_DROP_MAX_ALERTS
    if overflow > 0:
        alerts.append(f"…и ещё {overflow} снижений (см. ночную сводку)")
    return alerts, f"📉 Алертов о снижении цены: {len(eligible)}"


# --------------------------------------------------------------------------- W3-G1 llm-report

LLM_REPORT_THRESHOLDS = (0.6, 0.65, 0.7)
LLM_BORDERLINE_RANGE = (0.55, 0.75)


def _load_llm_decisions(
    path: Any, *, days: int, include_failures: bool = False,
) -> list[dict[str, Any]]:
    """Parse llm_decisions.jsonl entries newer than `days`; garbage lines are skipped."""
    import json
    from datetime import UTC, timedelta
    from pathlib import Path as _Path

    cutoff = datetime.now(UTC) - timedelta(days=days)
    entries: list[dict[str, Any]] = []
    p = _Path(path)
    if not p.exists():
        return entries
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                ts = datetime.strptime(entry["ts"], LLM_DECISIONS_TS_FMT)
            except (KeyError, TypeError, ValueError):
                continue
            if ts < cutoff or (
                not include_failures and not isinstance(entry.get("confidence"), (int, float))
            ):
                continue
            entries.append(entry)
    return entries


def _llm_threshold_counts(
    entries: list[dict[str, Any]],
    thresholds: tuple[float, ...] = LLM_REPORT_THRESHOLDS,
) -> dict[float, int]:
    """How many verdicts WOULD pass the gate at each threshold.

    Mirrors the matcher gate: pass = is_same_apartment AND confidence ≥ threshold.
    """
    return {
        t: sum(1 for e in entries if e.get("verdict")
               and e.get("outcome") not in {"uncertain", "unavailable", "invalid_evidence"}
               and e["confidence"] >= t)
        for t in thresholds
    }


def _llm_histogram(entries: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """Confidence distribution in 0.1-wide buckets; conf=1.0 folds into the last bucket."""
    counts = [0] * 10
    for e in entries:
        # clamp to ≥0: a hand-corrupted negative confidence must not index from the end
        idx = min(max(round(e["confidence"] * 100) // 10, 0), 9)
        counts[idx] += 1
    return [(f"{i / 10:.1f}–{(i + 1) / 10:.1f}", n) for i, n in enumerate(counts)]


def _llm_borderline(
    entries: list[dict[str, Any]],
    lo: float = LLM_BORDERLINE_RANGE[0],
    hi: float = LLM_BORDERLINE_RANGE[1],
) -> list[dict[str, Any]]:
    """Cases worth a manual look: confidence in [lo, hi] inclusive, any verdict."""
    return sorted(
        (e for e in entries if lo <= e["confidence"] <= hi),
        key=lambda e: e["confidence"],
    )


def _raw_listing_label(entry: dict[str, Any]) -> str:
    if entry.get("listing_id") is not None:
        return f"listing #{entry['listing_id']}"
    return f"listing {entry.get('listing_ref') or '?'}"


def _raw_inbound_label(entry: dict[str, Any]) -> str:
    if entry.get("inbound_id") is not None:
        return f"inbound #{entry['inbound_id']}"
    return f"inbound {entry.get('inbound_ref') or '?'}"


async def _resolve_llm_case_refs(entries: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(listing label, inbound label) per entry — listing URL / t.me link when the DB can
    resolve them, raw log identifiers otherwise. Tolerant: any lookup failure → raw label."""
    from src.db.session import get_session

    out: list[tuple[str, str]] = []
    try:
        async with get_session() as s:
            for entry in entries:
                listing_label = _raw_listing_label(entry)
                inbound_label = _raw_inbound_label(entry)
                try:
                    url: str | None = None
                    if entry.get("listing_id") is not None:
                        row = await s.get(Listing, int(entry["listing_id"]))
                        url = row.url if row is not None else None
                    elif entry.get("listing_ref"):
                        plat_name, _, ext = str(entry["listing_ref"]).partition("/")
                        if ext and plat_name in Platform.__members__:
                            url = (
                                await s.execute(
                                    select(Listing.url).where(
                                        Listing.platform == Platform[plat_name],
                                        Listing.external_id == ext,
                                    )
                                )
                            ).scalar_one_or_none()
                    if url:
                        listing_label = url
                except Exception:  # per-case resolution is best-effort
                    pass
                try:
                    link: str | None = None
                    if entry.get("inbound_id") is not None:
                        ib = await s.get(InboundPost, int(entry["inbound_id"]))
                        if ib is not None:
                            chan = await s.get(SourceChannel, ib.source_channel_id)
                            if chan is not None and chan.username:
                                link = f"https://t.me/{chan.username}/{ib.tg_message_id}"
                    elif entry.get("inbound_ref"):
                        chan_part, _, msg_part = str(entry["inbound_ref"]).partition("/")
                        if chan_part.isdigit() and msg_part.split("/")[0].isdigit():
                            chan = await s.get(SourceChannel, int(chan_part))
                            if chan is not None and chan.username:
                                link = f"https://t.me/{chan.username}/{int(msg_part.split('/')[0])}"
                    if link:
                        inbound_label = link
                except Exception:
                    pass
                out.append((listing_label, inbound_label))
    except Exception:  # no DB at hand: report still works off raw refs
        out = [(_raw_listing_label(e), _raw_inbound_label(e)) for e in entries]
    return out


@cli.command("llm-report")
@click.option("--days", default=7, show_default=True, type=int, help="Window size in days.")
def llm_report(days: int) -> None:
    """Analyze the LLM-validator decision log: confidence distribution + threshold what-ifs.

    Data source: data/logs/llm_decisions.jsonl (one line per verdict, written by
    src/matching/llm_validator.py). The 0.7 gate itself (LLM_VALIDATION_MIN_CONFIDENCE)
    stays untouched — this command only builds the evidence for tuning it.
    """
    run_cooperatively(_llm_report(days))


async def _llm_report(days: int) -> None:
    from src.matching.llm_validator import _decisions_log_path

    settings = get_settings()
    all_entries = _load_llm_decisions(_decisions_log_path(), days=days, include_failures=True)
    uncertain = sum(e.get("outcome") == "uncertain" for e in all_entries)
    failed = sum(e.get("outcome") in {"unavailable", "invalid_evidence"} for e in all_entries)
    entries = [e for e in all_entries if isinstance(e.get("confidence"), (int, float))
               and e.get("outcome") not in {"uncertain", "unavailable", "invalid_evidence"}]
    click.echo(f"Deferred: uncertain={uncertain}, unavailable/invalid={failed}")
    click.echo(f"=== LLM validator — последние {days} дн ===")
    if not entries:
        click.echo("(нет записей — лог пишется с момента деплоя W3-G1)")
        return

    same = sum(1 for e in entries if e.get("verdict"))
    click.echo(f"вердиктов: {len(entries)} (same={same}, different={len(entries) - same})")
    avg = sum(e["confidence"] for e in entries) / len(entries)
    lat = [e["latency_ms"] for e in entries if isinstance(e.get("latency_ms"), (int, float))]
    avg_lat = f", средняя латентность {sum(lat) / len(lat) / 1000:.1f} c" if lat else ""
    click.echo(f"средняя confidence: {avg:.2f}{avg_lat}")

    click.echo("\nРаспределение confidence:")
    peak = max(n for _, n in _llm_histogram(entries)) or 1
    for label, n in _llm_histogram(entries):
        bar = "█" * round(20 * n / peak) if n else ""
        click.echo(f"  {label}  {n:4d}  {bar}")

    click.echo("\nПрошло бы гейт (verdict=same и conf ≥ T):")
    current = settings.llm_validation_min_confidence
    for t, n in _llm_threshold_counts(entries).items():
        marker = "  ← текущий порог" if abs(t - current) < 1e-9 else ""
        click.echo(f"  T={t:.2f}: {n}{marker}")

    borderline = _llm_borderline(entries)
    lo, hi = LLM_BORDERLINE_RANGE
    click.echo(f"\nПограничные случаи ({lo}–{hi}) для ручного ревью: {len(borderline)}")
    refs = await _resolve_llm_case_refs(borderline)
    for entry, (listing_label, inbound_label) in zip(borderline, refs, strict=False):
        verdict = "same" if entry.get("verdict") else "diff"
        click.echo(
            f"  conf={entry['confidence']:.2f} [{verdict}]  {listing_label}  ⇆  {inbound_label}"
        )
    click.echo("\n(порог LLM_VALIDATION_MIN_CONFIDENCE в этом этапе не меняем — W3-G1)")


# ----------------------------------------------------------------- W3-G2 districts-report


async def _active_district_counts() -> list[tuple[str, int]]:
    """(raw district, count) among ACTIVE listings, non-empty districts only."""
    from sqlalchemy import func

    from src.db.session import get_session

    stmt = (
        select(Listing.district, func.count(Listing.id))
        .where(
            Listing.is_active.is_(True),
            Listing.district.is_not(None),
            Listing.district != "",
        )
        .group_by(Listing.district)
    )
    async with get_session() as s:
        return [(d, int(n)) for d, n in (await s.execute(stmt)).all()]


def _missing_districts(
    counts: list[tuple[str, int]], *, min_count: int = 1
) -> list[tuple[str, int]]:
    """Raw district values with no districts.yaml hashtag, most frequent first.

    Uses the production lookup (district_tag) — diacritics/token-prefix variants that
    already resolve are correctly excluded.
    """
    from src.post.hashtags import district_tag

    out = [(d, n) for d, n in counts if n >= min_count and district_tag(d) is None]
    out.sort(key=lambda t: (-t[1], t[0].lower()))
    return out


def _district_yaml_suggestion(district: str) -> str:
    """Ready-to-paste yaml line: '"Banovo brdo": BanovoBrdo'. Non-latinizable → FIXME value."""
    import re
    import unicodedata

    nfkd = unicodedata.normalize("NFKD", district)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    words = re.split(r"[^A-Za-z0-9]+", stripped)
    camel = "".join(w[:1].upper() + w[1:] for w in words if w)
    if not camel or not camel.isascii():
        camel = "FIXME"
    return f'"{district}": {camel}'


def _districts_report_lines(
    missing: list[tuple[str, int]], *, top: int
) -> list[str]:
    """Human block + paste block + the deploy reminder (the only synced data/ file)."""
    lines: list[str] = []
    if not missing:
        lines.append("Все district-значения активных listings уже покрыты districts.yaml ✅")
    else:
        shown = missing[:top]
        lines.append(f"Районы без хэштега (top {len(shown)} из {len(missing)}):")
        for district, n in shown:
            lines.append(f"  {n:5d}  {district}")
        lines.append("")
        lines.append("Готовые строки для data/districts.yaml (значение проверь руками —")
        lines.append("микрорайон обычно мапится на родительскую общину, см. примеры в файле):")
        for district, _n in shown:
            lines.append(_district_yaml_suggestion(district))
    lines.append("")
    lines.append(
        "⚠️ data/districts.yaml — ЕДИНСТВЕННЫЙ файл из data/, который синкается на прод: "
        "после правки задеплой через scripts/deploy_sync.sh"
    )
    return lines


@cli.command("districts-report")
@click.option(
    "--top", default=30, show_default=True, type=int, help="Show top N missing districts.",
)
@click.option(
    "--min-count", default=1, show_default=True, type=int,
    help="Hide districts seen on fewer than N active listings.",
)
def districts_report(top: int, min_count: int) -> None:
    """Top raw `district` values among ACTIVE listings missing from data/districts.yaml.

    Replaces the manual «new micro-district appeared» routine: frequencies + ready-to-paste
    yaml lines. Remember: districts.yaml is the only data/ file deployed by deploy_sync.sh.
    """
    run_cooperatively(_districts_report(top, min_count))


async def _districts_report(top: int, min_count: int) -> None:
    counts = await _active_district_counts()
    missing = _missing_districts(counts, min_count=min_count)
    click.echo("=== districts-report (active listings) ===")
    for line in _districts_report_lines(missing, top=top):
        click.echo(line)


@cli.command("leads-report")
@click.option(
    "--days", default=None, type=int,
    help="Only count leads created in the last N days (by created_at). Default: all time.",
)
@click.option(
    "--top", default=30, show_default=True, type=int, help="Show top N requested districts.",
)
def leads_report(days: int | None, top: int) -> None:
    """F1: lead funnel (new→contacted→closed) + district/budget demand for Дарья.

    Read-only aggregation over the `leads` table. The supply/demand mismatch surfaced here
    (which districts & budgets are most requested) is what to hunt for.
    """
    run_cooperatively(_leads_report(days, top))


async def _leads_report(days: int | None, top: int) -> None:
    from datetime import timedelta

    from src.admin.leads_report import build_leads_report, format_leads_report
    from src.db.models import Lead
    from src.db.session import get_session

    async with get_session() as s:
        stmt = select(Lead)
        if days is not None:
            # SQLite stores naive datetimes; compare against naive-UTC like the admin routes do.
            cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
            stmt = stmt.where(Lead.created_at >= cutoff)
        leads = (await s.execute(stmt)).scalars().all()

    window = f"last {days}d" if days is not None else "all time"
    click.echo(f"# Leads report — {window} — {len(leads)} leads\n")
    click.echo(format_leads_report(build_leads_report(leads, top_districts=top)))


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
    run_cooperatively(_mark_stale(days, platform))


async def _mark_stale(days: int, platform: str | None) -> None:
    from src.scrapers.orchestrator import mark_stale_inactive

    plat = PLATFORM_NAMES[platform.lower()] if platform else None
    result = await mark_stale_inactive(inactive_after_days=days, platform=plat)
    click.echo(f"marked {result.marked_inactive} listings inactive")


@cli.command("refresh")
@click.option("--force", is_flag=True, help="Ignore the 15-min throttle.")
def refresh(force: bool) -> None:
    """Manually refresh listings on all platforms (auto-triggered by each new inbound too)."""
    run_cooperatively(_refresh(force))


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
    run_cooperatively(_deliver_all(force))


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
@click.option(
    "--limit", type=click.IntRange(min=1), default=None, help="Cap number of listings.",
)
@click.option("--no-photos", is_flag=True, help="Update text fields only, skip photo downloads.")
@click.option(
    "--public-only", is_flag=True, help="Refresh due public originals with source fairness.",
)
@click.option("--budget-sec", type=click.FloatRange(min=0.001), default=None,
              help="Bound source startup, refresh work and cleanup by this time budget.")
def refetch_details(
    platform: str | None, refetch_all: bool, limit: int | None, no_photos: bool,
    public_only: bool = False, budget_sec: float | None = None,
) -> None:
    """Re-fetch detail pages for existing listings and repair their stored data.

    The default repairs broken rows; --all supports a manual backfill after parser changes.
    --public-only uses the due public queue with source fairness, retry backoff and a budget.
    It updates source fields/media without publishing or sending messages.
    """
    if public_only and (refetch_all or no_photos):
        raise click.UsageError("--public-only cannot be combined with --all or --no-photos")
    run_cooperatively(_refetch_details(
        platform, refetch_all, limit, no_photos, public_only=public_only, budget_sec=budget_sec,
    ))


async def _refetch_details(
    platform: str | None, refetch_all: bool, limit: int | None, no_photos: bool,
    *, public_only: bool = False, budget_sec: float | None = None,
) -> None:
    import time

    from src.db.models import Listing
    from src.db.session import get_session
    from src.scrapers.orchestrator import refetch_listing_details, rolling_detail_refresh

    if public_only:
        settings = get_settings()
        budget = budget_sec if budget_sec is not None else settings.public_detail_refresh_budget_sec
        cap = limit if limit is not None else settings.public_detail_refresh_limit
        selected = ({PLATFORM_NAMES[platform.lower()]: SCRAPERS[PLATFORM_NAMES[platform.lower()]]}
                    if platform else SCRAPERS)
        click.echo(f"→ public detail refresh: limit={cap}, budget={budget:g}s")
        # This path only updates source details/media. It never invokes publication,
        # subscription, price-change notices, delivery retry or summary transports.
        async with asyncio.timeout(budget):
            result = await rolling_detail_refresh(
                limit=cap, public_only=True, scrapers=selected,
                deadline=time.monotonic() + budget,
            )
        click.echo(
            f"=== PUBLIC: selected={result.selected} verified={result.refreshed} "
            f"errors={result.errors} skipped={result.skipped} "
            f"source_errors={len(result.source_errors)} ==="
        )
        if result.errors:
            click.echo(f"⚠️ Ошибки: {result.errors}")
        if result.skipped:
            click.echo(f"⚠️ пропущено по бюджету: {result.skipped}")
        # Only source-level failures fail the run; item errors and budget skips are warnings.
        if result.source_errors:
            click.echo("🚨 Ошибки площадок: " + ", ".join(
                f"{platform}: {code}" for platform, code in sorted(result.source_errors.items())
            ))
            raise SystemExit(1)
        return

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

    total = len(rows)
    click.echo(f"→ refetch-details: {total} listings ({'ALL' if refetch_all else 'broken only'})")

    def _progress(stats, row, error) -> None:  # type: ignore[no-untyped-def]
        if error is not None:
            if stats.errors <= 5:
                click.echo(
                    f"  ⚠ {row.platform.value} #{row.id}: {type(error).__name__}: {error}",
                    err=True,
                )
        elif stats.updated % 100 == 0:
            click.echo(f"  … {stats.updated}/{total} photos={stats.photos} errors={stats.errors}")

    # Shared core with the nightly heal/rolling steps (src.scrapers.orchestrator).
    stats = await refetch_listing_details(
        rows,
        download_photos=not no_photos,
        touch_refreshed_at=True,
        progress=_progress,
        deadline=time.monotonic() + budget_sec if budget_sec is not None else None,
    )

    click.echo(
        f"=== DONE: updated={stats.updated} verified={stats.verified} "
        f"photos={stats.photos} errors={stats.errors} skipped={stats.skipped} "
        f"source_errors={len(stats.source_errors)} ==="
    )


@cli.command("backfill-nekretnine-address")
@click.option("--limit", type=int, default=None, help="Cap number of listings.")
@click.option("--concurrency", type=int, default=4, help="Parallel HTTP requests.")
def backfill_nekretnine_address(limit: int | None, concurrency: int) -> None:
    """Re-fetch each nekretnine listing detail and fill address_raw.

    Skips listings that already have a non-null address_raw — safe to re-run.
    """
    run_cooperatively(_backfill_nekretnine_address(limit, concurrency))


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
    run_cooperatively(_ship_log(log_file, label, lines, session))


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
    run_cooperatively(_daily_summary(hours))


async def _photo_coverage_by_platform() -> list[tuple[str, int, int]]:
    """(platform, active_without_photo_hashes, active_total) per platform — live counts.

    Live query instead of the latest daily_metrics row: stays correct even when the
    nightly metrics step is disabled or hasn't run yet.
    """
    from sqlalchemy import func

    from src.db.session import get_session

    out: list[tuple[str, int, int]] = []
    async with get_session() as s:
        for plat in Platform:
            active = (
                await s.execute(
                    select(func.count(Listing.id)).where(
                        Listing.platform == plat, Listing.is_active.is_(True)
                    )
                )
            ).scalar_one()
            no_photo = (
                await s.execute(
                    select(func.count(Listing.id)).where(
                        Listing.platform == plat,
                        Listing.is_active.is_(True),
                        Listing.photo_phashes == [],
                    )
                )
            ).scalar_one()
            out.append((plat.value, no_photo, active))
    return out


async def _daily_summary(hours: int) -> None:
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func

    from src.db.models import InboundPost, InboundStatus, Match, PublishedPost
    from src.db.session import get_session
    from src.telegram.delivery import send_text_to_recipients

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

    from src.matching.validation_queue import unresolved_counts
    from src.telegram.run_summary import format_run_summary

    remaining = await unresolved_counts()
    errors = max(matched - delivered, 0)
    lines = format_run_summary(
        matched=matched, no_match=no_match, errors=errors, **remaining, label=label,
    ).splitlines()
    lines.insert(2, "Нерешённые NEW ниже — вся текущая очередь, включая более старые посты.")
    coverage = await _photo_coverage_by_platform()
    lines.append("")
    lines.append("📷 Активные без фото:")
    for plat_value, no_photo, active in coverage:
        lines.append(f"  • {plat_value}: {no_photo} из {active}")

    client = await _open_userbot_if_needed()
    try:
        await send_text_to_recipients(client, "\n".join(lines))
    finally:
        if client is not None:
            await client.disconnect()


@cli.command("notify-failure")
@click.option("--service", default="otz-monitor", help="Failing systemd unit name.")
@click.option("--reason", default=None, help="Optional human-readable cause for the alert.")
def notify_failure(service: str, reason: str | None) -> None:
    """Send a Telegram alert that the systemd unit named via --service failed.

    The command itself only delivers the alert — it does not restart or inspect anything.
    Units opt in by declaring `OnFailure=otz-notify-failure@%n.service` in their unit file;
    the template service passes the failing unit's name (%n) through to --service.
    """
    run_cooperatively(_notify_failure(service, reason))


_NOTIFY_MARKER_DIR = "/var/log/otz"
_NOTIFY_COOLDOWN_SECONDS = 600  # 10 min — collapse a residual N-transition storm to one message


def _notify_recently_sent(service: str) -> bool:
    """True if an alert for `service` was sent within the cooldown.

    Best-effort: any read/write error is swallowed (returns False / no-op) so a marker problem
    can never suppress *and* never crash the alert path.
    """
    import os
    import time

    marker = os.path.join(_NOTIFY_MARKER_DIR, f".last_alert_{service}")
    try:
        age = time.time() - os.path.getmtime(marker)
        return age < _NOTIFY_COOLDOWN_SECONDS
    except OSError:
        return False


def _notify_mark_sent(service: str) -> None:
    import os

    marker = os.path.join(_NOTIFY_MARKER_DIR, f".last_alert_{service}")
    try:
        os.makedirs(_NOTIFY_MARKER_DIR, exist_ok=True)
        with open(marker, "w") as fh:
            fh.write("")
    except OSError:
        logging.getLogger(__name__).warning("could not write alert marker %s", marker)


async def _notify_failure(service: str, reason: str | None) -> None:
    from datetime import datetime, timezone

    from src.telegram.delivery import send_text_to_recipients

    # De-dupe: OnFailure fires on every transition to failed, so a Restart cycle can emit several
    # in a row. Suppress repeats within the cooldown so the storm collapses to one message.
    if _notify_recently_sent(service):
        click.echo(f"↩ alert for {service} suppressed (within cooldown)")
        return

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    # OnFailure fires on EVERY transition to failed (incl. between Restart cycles), not only when
    # restarts are exhausted. We just report the failed state and what to do next.
    lines = [
        f"🚨 {service}: unit в состоянии failed.",
        f"⏱ {ts}",
    ]
    if reason:
        lines.append(f"причина: {reason}")
    lines.append("")
    lines.append(f"Логи: journalctl -u {service} -n 100")
    if "monitor" in service:
        # A revoked session can't be fixed by reset-failed/start — it needs re-auth.
        lines.append(
            f"Если сессия отозвана: python scripts/auth_qr.py, затем systemctl restart {service}"
        )
        lines.append(
            f"Иначе поднять: systemctl reset-failed {service} && systemctl start {service}"
        )

    # Never let a dead userbot session crash the notifier itself: in userbot mode a
    # revoked session raises SessionNotAuthorized here, but the alert (bot mode) must still go out.
    try:
        client = await _open_userbot_if_needed()
    except Exception as e:
        click.echo(f"⚠ userbot unavailable for alert ({e}); continuing", err=True)
        client = None
    try:
        await send_text_to_recipients(client, "\n".join(lines))
        _notify_mark_sent(service)
        click.echo(f"✓ alert sent for {service}")
    except Exception as e:
        click.echo(f"⚠ alert failed: {e}", err=True)
    finally:
        if client is not None:
            await client.disconnect()


@cli.command("check-links")
@click.option(
    "--platform",
    type=click.Choice(list(PLATFORM_NAMES.keys()), case_sensitive=False),
    default=None,
    help="Restrict the sweep to one platform (default: all).",
)
@click.option("--limit", type=int, default=None, help="Cap the number of listings checked.")
def check_links(platform: str | None, limit: int | None) -> None:
    """A3: HEAD-check ACTIVE listings with a PENDING/PUBLISHED match (potentially on the site)
    and deactivate the ones that 404/410, so a public card never links to a removed page.

    Transient failures (timeouts, 5xx) and anti-bot 403/405 responses do NOT deactivate.
    """
    from src.scrapers.orchestrator import verify_active_links

    plat = PLATFORM_NAMES[platform.lower()] if platform else None
    stats = run_cooperatively(verify_active_links(limit=limit, platform=plat))
    click.echo(
        f"=== ссылки: проверено {stats.checked}, деактивировано {stats.deactivated}, "
        f"ошибок {stats.errors}, пропущено {stats.skipped} ==="
    )


@cli.command("heal-urls")
@click.option("--platform", default="nekretnine", show_default=True,
              help="Platform whose stored URLs to canonicalise.")
@click.option("--dry-run/--no-dry-run", default=True, show_default=True,
              help="Dry-run prints the diff without writing. --no-dry-run applies the rewrite.")
@click.option("--include-inactive", is_flag=True, default=False,
              help="Escape hatch: old unguarded sweep — heal inactive rows too and skip the "
                   "canonical resolve check.")
def heal_urls(platform: str, dry_run: bool, include_inactive: bool) -> None:
    """Rewrite stored nekretnine slug-URLs to the canonical numeric /oglasi/<id>/ form.

    The platform periodically rewrites a listing's slug while the listing stays live, so a
    stored slug-URL 404s. The numeric form is slug-independent. One-off prod heal; the nightly
    walk also self-heals on re-seen rows.

    T1-2 guard: by default only ACTIVE rows are swept, and the canonical URL must resolve
    (HEAD < 400) before the rewrite is written — a dead canonical is skipped with a warning.
    """
    healed = run_cooperatively(
        _heal_urls(platform=platform, dry_run=dry_run, include_inactive=include_inactive)
    )
    verb = "would heal" if dry_run else "healed"
    click.echo(f"=== {verb} {healed} {platform} URL(s) ===")


def _canonical_url(platform: Platform, external_id: str) -> str | None:
    """Canonical slug-independent URL for a listing, or None if the platform has no rule."""
    if platform is Platform.NEKRETNINE:
        from src.scrapers.nekretnine import BASE_URL

        return f"{BASE_URL}/oglasi/{external_id}/"
    return None


async def _heal_urls(
    *,
    platform: str,
    dry_run: bool,
    include_inactive: bool = False,
    client: httpx.AsyncClient | None = None,
) -> int:
    """Rewrite non-canonical stored URLs for `platform`; return the number of rows healed.

    T1-2 guard: by default only is_active rows are swept, and before an actual write the
    canonical URL must resolve (HEAD, status < 400, redirects followed) — a dead canonical is
    skipped with a warning, so a heal never repoints a row at a removed page («не хуже, чем
    было»). `include_inactive=True` is the escape hatch restoring the old unguarded sweep:
    all rows, no resolve check.

    In dry-run nothing is written and no network check runs (only the diff is printed), so the
    count is the upper bound of what --no-dry-run would heal. `client` is injectable for tests
    (MockTransport); in production it defaults to a fresh client with the scraper User-Agent.
    """
    from sqlalchemy import update

    from src.db.session import get_session

    plat = PLATFORM_NAMES[platform.lower()]

    stmt = select(Listing.id, Listing.external_id, Listing.url).where(Listing.platform == plat)
    if not include_inactive:
        # Inactive rows are corpses awaiting purge — healing them is wasted writes plus
        # pointless HEADs at pages that are known-dead (инактивные лечить некому и незачем).
        stmt = stmt.where(Listing.is_active.is_(True))
    async with get_session() as s:
        rows = (await s.execute(stmt)).all()

    todo: list[tuple[int, str, str]] = []
    for lid, external_id, url in rows:
        canonical = _canonical_url(plat, external_id)
        if canonical is None or url == canonical:
            continue
        todo.append((lid, url, canonical))

    if dry_run:
        for _, url, canonical in todo:
            click.echo(f"  {url}  →  {canonical}")
        return len(todo)

    log = logging.getLogger(__name__)
    owns_client = False
    if not include_inactive and todo and client is None:
        from src.scrapers.base import DEFAULT_USER_AGENT

        ua = get_settings().scraper_user_agent or DEFAULT_USER_AGENT
        client = httpx.AsyncClient(
            follow_redirects=True, timeout=10.0, headers={"User-Agent": ua}
        )
        owns_client = True

    healed = 0
    try:
        for lid, _url, canonical in todo:
            if not include_inactive:
                # Write-guard: only a confirmed-live canonical gets written. Anything else —
                # 404/410, anti-bot 4xx, 5xx or a network error — is «не резолвится» → skip,
                # because overwriting a stored URL with a dead one is worse than leaving it.
                try:
                    resp = await client.head(canonical, timeout=10.0)
                    reason = None if resp.status_code < 400 else f"HTTP {resp.status_code}"
                except Exception as e:
                    reason = f"{type(e).__name__}: {e}"
                if reason is not None:
                    log.warning(
                        "[heal-urls] #%s canonical %s does not resolve (%s) — skipped",
                        lid, canonical, reason,
                    )
                    continue
            healed += 1
            async with get_session() as s:
                await s.execute(update(Listing).where(Listing.id == lid).values(url=canonical))
    finally:
        if owns_client:
            await client.aclose()

    return healed


# ------------------------------------------------------------------ T1-3 purge-inactive


class PurgeInactiveResult(NamedTuple):
    """_purge_inactive outcome — a NamedTuple so tests assert the report, not stdout."""

    candidates: int  # inactive & older than cutoff, BEFORE the FK protection
    protected: list[tuple[int, str, str]]  # (id, platform, external_id) — FK-referenced, refused
    protected_on_site: int  # of protected: exposed via site_listings (visible on the site)
    to_purge: int  # candidates minus protected — what the real run archives+deletes
    by_platform: dict[str, int]  # purge-set breakdown by source platform
    by_date: dict[str, int]  # purge-set breakdown by last_seen_at date (YYYY-MM-DD)
    archive_bytes: int  # uncompressed JSONL volume (dry-run: what WOULD be written)
    archive_path: Path | None  # None on dry-run or when the purge set is empty
    deleted: int  # rows actually deleted (always 0 on dry-run)


def _listing_archive_record(mapping: Any) -> dict[str, Any]:
    """One listings row (column mapping) → JSON-serializable dict, ALL columns present.

    photo_embeddings НАМЕРЕННО обнулены (ключ есть, значение None): это derived-данные
    (CLIP по фото, восстановимы `otz embed-all`), base64 раздул бы архив в разы и почти
    не жмётся gzip'ом. Пометка об исключении печатается в отчёте команды.
    """
    import enum as _enum

    rec: dict[str, Any] = {}
    for key, value in mapping.items():
        if isinstance(value, datetime):
            value = value.isoformat()
        elif isinstance(value, _enum.Enum):
            value = value.value
        rec[key] = value
    rec["photo_embeddings"] = None
    return rec


async def _purge_inactive(*, older_than_days: int, dry_run: bool) -> PurgeInactiveResult:
    """Archive-then-delete for long-inactive listings; dry-run = same report, ZERO mutations.

    FK-защита: строки, на которые ссылаются matches (а через matches.id — site_listings),
    не удаляются НИКОГДА — с PRAGMA foreign_keys=ON такой delete каскадом снёс бы историю
    матчей и карточки сайта. Защищённые перечисляются в отчёте поимённо.

    Порядок боевого прогона: полностью записать и закрыть архив → удалить РОВНО те id,
    что легли в архив. Падение между шагами оставляет строки на месте — повторный прогон
    безопасен (архив дописывается новым gzip-member'ом, не перетирается).
    """
    import gzip
    import json
    from datetime import timedelta

    from sqlalchemy import delete, func

    from src.db.models import ApartmentIdentity, ListingIdentityEdge, Match, SiteListing
    from src.db.session import get_session

    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    age_where = (Listing.is_active.is_(False), Listing.last_seen_at < cutoff)
    referenced = select(Match.listing_id).union(
        select(ApartmentIdentity.anchor_listing_id), select(ListingIdentityEdge.left_id),
        select(ListingIdentityEdge.right_id),
    )  # delivery history, identity anchors, and revoked-edge tombstones are protected

    archive_bytes = 0
    purged_ids: list[int] = []
    archive_path: Path | None = None
    fh = None

    async with get_session() as s:
        candidates = (
            await s.execute(select(func.count(Listing.id)).where(*age_where))
        ).scalar_one()
        protected = [
            (int(lid), plat.value, ext)
            for lid, plat, ext in (
                await s.execute(
                    select(Listing.id, Listing.platform, Listing.external_id)
                    .where(*age_where, Listing.id.in_(referenced))
                    .order_by(Listing.id)
                )
            ).all()
        ]
        protected_on_site = (
            await s.execute(
                select(func.count(func.distinct(Match.listing_id)))
                .select_from(Match)
                .join(SiteListing, SiteListing.match_id == Match.id)
                .where(Match.listing_id.in_(select(Listing.id).where(*age_where)))
            )
        ).scalar_one()

        purge_where = (*age_where, Listing.id.not_in(referenced))
        by_platform = {
            plat.value: int(n)
            for plat, n in (
                await s.execute(
                    select(Listing.platform, func.count(Listing.id))
                    .where(*purge_where)
                    .group_by(Listing.platform)
                )
            ).all()
        }
        by_date = {
            str(d): int(n)
            for d, n in (
                await s.execute(
                    select(func.date(Listing.last_seen_at), func.count(Listing.id))
                    .where(*purge_where)
                    .group_by(func.date(Listing.last_seen_at))
                    .order_by(func.date(Listing.last_seen_at))
                )
            ).all()
        }

        # Column-only select minus the BLOB, streamed: full ORM rows would drag every
        # photo_embeddings (hundreds of MB on a prod-sized purge) into memory to discard it,
        # and .all() would hold every description/raw_payload at once.
        archive_cols = [c for c in Listing.__table__.columns if c.name != "photo_embeddings"]
        stream = await s.stream(select(*archive_cols).where(*purge_where).order_by(Listing.id))
        try:
            async for row in stream:
                line = json.dumps(_listing_archive_record(row._mapping), ensure_ascii=False) + "\n"
                archive_bytes += len(line.encode("utf-8"))
                purged_ids.append(int(row.id))
                if dry_run:
                    continue  # dry-run walks the same rows for an honest volume — writes nothing
                if fh is None:
                    archive_dir = get_settings().data_dir / "archive"
                    archive_dir.mkdir(parents=True, exist_ok=True)
                    archive_path = (
                        archive_dir / f"listings-{datetime.now(timezone.utc):%Y%m%d}.jsonl.gz"
                    )
                    # "at" appends a new gzip member — a same-day re-run must not overwrite
                    # the earlier archive (gzip reads concatenated members transparently).
                    fh = gzip.open(archive_path, "at", encoding="utf-8")
                fh.write(line)
        finally:
            if fh is not None:
                fh.close()

    deleted = 0
    if not dry_run and purged_ids:
        # Fresh session AFTER the archive file is closed; delete only archived ids,
        # chunked to stay under SQLite's bound-parameter cap.
        async with get_session() as s:
            for i in range(0, len(purged_ids), 500):
                chunk = purged_ids[i : i + 500]
                result = await s.execute(
                    delete(Listing)
                    .where(Listing.id.in_(chunk))
                    # TOCTOU-гард: между архивным проходом и delete мог появиться Match
                    # (nightly-матчер) — cascade снёс бы его историю. Такие строки щадим.
                    .where(Listing.id.not_in(referenced))
                )
                deleted += result.rowcount or 0

    return PurgeInactiveResult(
        candidates=int(candidates),
        protected=protected,
        protected_on_site=int(protected_on_site),
        to_purge=len(purged_ids),
        by_platform=by_platform,
        by_date=by_date,
        archive_bytes=archive_bytes,
        archive_path=archive_path,
        deleted=deleted,
    )


def _echo_purge_report(
    res: PurgeInactiveResult, *, older_than_days: int, dry_run: bool
) -> None:
    mode = "DRY-RUN (ноль мутаций)" if dry_run else "архив → delete"
    click.echo(f"=== purge-inactive — {mode} ===")
    click.echo(f"порог: inactive, last_seen_at старше {older_than_days} дн")
    click.echo(f"кандидатов: {res.candidates}")
    click.echo(
        f"защищено FK (matches/site_listings): {len(res.protected)} — удалять ОТКАЗ"
        + (f", из них видимы на сайте: {res.protected_on_site}" if res.protected else "")
    )
    for lid, plat, ext in res.protected:
        click.echo(f"  · #{lid}  {plat}  {ext}")
    click.echo(f"к удалению: {res.to_purge}")
    if res.by_platform:
        click.echo("по площадкам:")
        for plat, n in sorted(res.by_platform.items()):
            click.echo(f"  {plat:14}  {n}")
    if res.by_date:
        click.echo("по датам last_seen_at:")
        for day, n in res.by_date.items():
            click.echo(f"  {day}  {n}")
    click.echo(
        f"объём: {res.to_purge} строк, ~{res.archive_bytes / 1024 / 1024:.1f} MB JSONL "
        "(photo_embeddings исключены: derived-данные, восстановимы `otz embed-all`)"
    )
    if dry_run:
        click.echo("(dry-run: БД не изменена, архив не записан)")
    else:
        click.echo(f"архив: {res.archive_path or '— (нечего архивировать)'}")
        click.echo(f"удалено: {res.deleted}")


@cli.command("purge-inactive")
@click.option(
    "--older-than",
    "older_than_days",
    # min=1: N<=0 сдвигает cutoff в будущее — боевой прогон снёс бы все inactive-строки.
    type=click.IntRange(min=1),
    required=True,
    help="Purge inactive listings whose last_seen_at is older than N days.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Full report (sources/dates/volume/FK-protected) with ZERO mutations.",
)
def purge_inactive(older_than_days: int, dry_run: bool) -> None:
    """T1-3: archive long-inactive listings to data/archive/*.jsonl.gz, then delete them.

    Rows referenced by matches/site_listings are REFUSED and listed in the report —
    deleting them would cascade-wipe match history and public site cards. On this stage
    prod runs dry-run ONLY (decision D5); the real run is coded and tested for a later go.
    """
    result = run_cooperatively(_purge_inactive(older_than_days=older_than_days, dry_run=dry_run))
    _echo_purge_report(result, older_than_days=older_than_days, dry_run=dry_run)


@cli.command("scrape-smoke")
@click.option(
    "--platform",
    default=None,
    type=click.Choice(list(PLATFORM_NAMES.keys()), case_sensitive=False),
    help="Platform to probe. Omit to probe every platform.",
)
def scrape_smoke(platform: str | None) -> None:
    """Live-smoke probe: list+detail health + structure-fingerprint drift, per platform.

    Positive runs are SILENT (a readable table is printed but no notice is sent). On any not-ok
    result OR fingerprint drift, a concise notice is sent to the bot recipients. Exits 1 if any
    result is not-ok (so systemd OnFailure fires); a drift-only run (all ok) exits 0 but still
    sends the WARN notice.
    """
    settings = get_settings()
    if not settings.scrape_smoke_enabled:
        click.echo("scrape-smoke disabled (SCRAPE_SMOKE_ENABLED=0) — no-op")
        return
    plats = [PLATFORM_NAMES[platform.lower()]] if platform else list(SCRAPERS.keys())
    any_not_ok = run_cooperatively(_run_smoke(plats))
    if any_not_ok:
        sys.exit(1)


async def _run_smoke(platforms: list[Platform]) -> bool:
    """Probe each platform, print a table, alert on not-ok/drift. Returns True if any not-ok."""
    from src.scrapers.smoke import SmokeResult, run_smoke

    settings = get_settings()
    results: list[SmokeResult] = []
    for plat in platforms:
        scraper_cls = SCRAPERS[plat]
        # `async with` inside the try: a Playwright __aenter__ failure (halooglasi) must become a
        # not-ok SmokeResult for that platform, not crash the whole probe.
        try:
            async with scraper_cls() as scraper:
                scraper.MIN_PREVIEWS = settings.scrape_smoke_min_previews
                results.append(await run_smoke(scraper))
        except Exception as e:  # noqa: BLE001
            logging.getLogger(__name__).exception("[smoke] %s setup failed", plat.value)
            results.append(
                SmokeResult(
                    platform=plat.value, ok=False, n_previews=0, detail_ok=False,
                    missing_fields=[], drift=[], error=str(e) or type(e).__name__,
                )
            )

    click.echo("")
    click.echo("=== Scrape smoke ===")
    for r in results:
        status = "OK " if r.ok else "FAIL"
        flag = " ⚠ DRIFT" if r.drift else ""
        click.echo(
            f"  {status} {r.platform:14} previews={r.n_previews:3d} detail_ok={r.detail_ok}"
            f"{flag}"
        )
        if r.missing_fields:
            click.echo(f"        missing detail fields: {', '.join(r.missing_fields)}")
        if r.error:
            click.echo(f"        error: {r.error}")
        for line in r.drift:
            click.echo(f"        drift: {line}")

    not_ok = [r for r in results if not r.ok]
    drifted = [r for r in results if r.drift]
    if not_ok or drifted:
        lines = ["🚨 Smoke-зонд площадок"]
        for r in not_ok:
            detail = []
            if r.n_previews < settings.scrape_smoke_min_previews:
                detail.append(f"превью={r.n_previews}")
            if not r.detail_ok:
                miss = f" ({', '.join(r.missing_fields)})" if r.missing_fields else ""
                detail.append(f"detail сломан{miss}")
            if r.error:
                detail.append(f"ошибка: {r.error}")
            lines.append(f"❌ {r.platform}: {'; '.join(detail) or 'not ok'}")
        for r in drifted:
            lines.append(f"⚠️ {r.platform}: структура изменилась — {'; '.join(r.drift)}")
        from src.telegram.bot_delivery import send_text_via_bot

        await send_text_via_bot("\n".join(lines))

    return bool(not_ok)


@cli.command("pipeline-probe")
def pipeline_probe() -> None:
    """Synthetic «golden inbound» end-to-end probe (B2).

    Seeds a deterministic golden (committed images) into an isolated in-memory DB and runs the
    REAL matcher against it — catching regressions across the whole matching pipeline
    (pHash → candidate scan → thresholds → DB), not just the parser. The golden matches through
    the pHash path alone, so no CLIP model / API key is needed.

    Prints the verdict. On a missing/low-confidence match (or an unexpected error) it sends a
    bot notice and exits 1, so a systemd OnFailure can alert. A healthy run is silent (table
    only) and exits 0.
    """
    run_cooperatively(_pipeline_probe())


@cli.command("nekretnine-shadow-audit")
@click.option("--max-pages", default=500, show_default=True, type=click.IntRange(min=1))
@click.option("--compare-details", default=100, show_default=True, type=click.IntRange(min=0))
@click.option(
    "--report", "report_path", required=True, type=click.Path(path_type=Path, dir_okay=False)
)
def nekretnine_shadow_audit(max_pages: int, compare_details: int, report_path: Path) -> None:
    """Read-only snapshot-vs-detail audit; exits non-zero on any quality failure."""
    run_cooperatively(_nekretnine_shadow_audit(max_pages, compare_details, report_path))


async def _nekretnine_shadow_audit(
    max_pages: int,
    compare_details: int,
    report_path: Path,
) -> None:
    scraper_cls = SCRAPERS[Platform.NEKRETNINE]
    async with scraper_cls() as scraper:
        report = await run_shadow_audit(
            scraper,
            max_pages=max_pages,
            compare_details=compare_details,
        )
    write_report(report, report_path)
    click.echo(
        f"shadow-audit: {'PASS' if report.passed else 'STOP'} "
        f"previewed={report.previewed} site_total={report.site_total} "
        f"coverage={report.coverage:.3f} sample={report.sample_compared} "
        f"fallback={report.fallback_count} mismatches={report.mismatched_count} "
        f"report={report_path}"
    )
    if not report.passed:
        click.echo("reasons=" + ",".join(report.failure_reasons), err=True)
        raise click.exceptions.Exit(1)


async def _pipeline_probe() -> None:
    from src.jobs.probe import GOLDEN_EXTERNAL_ID, PROBE_MIN_CONFIDENCE, run_pipeline_probe

    result = await run_pipeline_probe()

    ok = (
        result.error is None
        and result.matched
        and result.listing_external_id == GOLDEN_EXTERNAL_ID
        and result.confidence >= PROBE_MIN_CONFIDENCE
    )

    click.echo("")
    click.echo("=== Pipeline probe (golden inbound) ===")
    status = "OK  " if ok else "FAIL"
    click.echo(
        f"  {status} matched={result.matched} listing={result.listing_external_id} "
        f"conf={result.confidence:.3f} method={result.method}"
    )
    if result.error:
        click.echo(f"        error: {result.error}")

    if not ok:
        reasons = []
        if result.error:
            reasons.append(f"ошибка: {result.error}")
        elif not result.matched:
            reasons.append("матч НЕ найден")
        elif result.listing_external_id != GOLDEN_EXTERNAL_ID:
            reasons.append(f"матч указал на чужой listing: {result.listing_external_id}")
        elif result.confidence < PROBE_MIN_CONFIDENCE:
            reasons.append(
                f"confidence {result.confidence:.3f} < {PROBE_MIN_CONFIDENCE}"
            )
        msg = "🚨 Pipeline-зонд (golden inbound) упал\n" + "; ".join(reasons or ["not ok"])
        try:
            from src.telegram.bot_delivery import send_text_via_bot

            await send_text_via_bot(msg)
        except Exception:  # noqa: BLE001 — alert is best-effort; the non-zero exit still fires.
            logging.getLogger(__name__).exception("[probe] bot alert failed")
        sys.exit(1)


def main() -> None:
    try:
        cli()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
