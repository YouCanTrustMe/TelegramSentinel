import asyncio
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.config import settings
from src.db.models import activate_source, get_pending_sources, get_schedule_slots, set_source_pending_msg_id, update_source_url
from src.common.util import row_get
from src.dispatcher.digest_builder import send_digest

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None

# Nightly prune horizon for sent items; see prune_old_items for the RSS-dedup trade-off.
_RETENTION_DAYS = 30


async def _prune_items_job() -> None:
    from src.db.models import prune_old_items

    deleted = await prune_old_items(_RETENTION_DAYS)
    if deleted:
        log.info("Retention: pruned %d sent item(s) older than %d days", deleted, _RETENTION_DAYS)


# A digest fires several times a day, and each run with items writes a digest_log
# row, so a full day with zero rows means the pipeline itself is stuck (the kind of
# silent stall that once left a 224-item backlog). 26h spans the whole previous day.
_DIGEST_HEALTH_WINDOW_HOURS = 26


def _summarize_digest_health(rows: list, window_hours: int) -> tuple[str, str | None]:
    """Fold recent digest_log rows into a one-line health summary and an optional
    alert string. The alert is set when nothing went out at all (dead-man's switch)
    or when a run ended non-ok; it is logged at WARNING, which the admin-alert log
    handler forwards to the admin (no separate admin_alert call, or the admin would
    be notified twice). Pure so it can be unit-tested without a DB or scheduler."""
    count = len(rows)
    if count == 0:
        msg = f"No digest sent in the last {window_hours}h — the digest pipeline may be stuck"
        return msg, msg
    total_items = sum(int(row_get(r, "items_total") or 0) for r in rows)
    non_ok = [r for r in rows if (row_get(r, "status") or "ok") != "ok"]
    line = (
        f"Digest health (last {window_hours}h): {count} digest(s), "
        f"{total_items} item(s) total, {len(non_ok)} non-ok"
    )
    if non_ok:
        statuses = ", ".join(sorted({(row_get(r, "status") or "ok") for r in non_ok}))
        return line, f"{len(non_ok)}/{count} digest(s) ended non-ok ({statuses}) in the last {window_hours}h"
    return line, None


async def _digest_health_job() -> None:
    from src.db.models import get_recent_digests

    rows = await get_recent_digests(_DIGEST_HEALTH_WINDOW_HOURS)
    line, alert = _summarize_digest_health(rows, _DIGEST_HEALTH_WINDOW_HOURS)
    if alert:
        log.warning(alert)
    else:
        log.info(line)


# The digest, home and /stats already list sources quiet for 5 days, but a passive
# list is read past: MarketWatch sat in it for a year (HTTP 200, fail_count 0, last
# item 2025-07-03) before anyone noticed. This second, much longer tier pushes once.
_SILENT_SOURCE_ALERT_HOURS = 336


def _too_young_to_judge(row, now: datetime, threshold_hours: int) -> bool:
    """A source that has never produced anything is only suspicious once it has had the
    full window to produce: a source added this morning is silent by definition."""
    if row_get(row, "last_item_at"):
        return False
    created = row_get(row, "created_at")
    if not created:
        return False
    try:
        stamp = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (now - stamp) < timedelta(hours=threshold_hours)


def _new_silent_sources(rows: list, already_alerted: set[int], now: datetime | None = None,
                        threshold_hours: int = _SILENT_SOURCE_ALERT_HOURS) -> tuple[list, set[int]]:
    """Split the long-silent sources into the ones worth pushing now and the full set
    to remember. A source drops out of the memo when it publishes again, so it can
    raise the alarm a second time if it dies again. Pure: unit-testable without a DB."""
    now = now or datetime.now(timezone.utc)
    old_enough = [r for r in rows if not _too_young_to_judge(r, now, threshold_hours)]
    current = {int(row_get(r, "id")) for r in old_enough}
    fresh = [r for r in old_enough if int(row_get(r, "id")) not in already_alerted]
    return fresh, current


def _silent_source_line(row) -> str:
    last = row_get(row, "last_item_at")
    hours = row_get(row, "hours_silent")
    age = f"{int(hours) // 24}d" if hours is not None else "never"
    return f"{row_get(row, 'name')} ({row_get(row, 'type')}, last item {last or 'never'}, silent {age})"


async def _silent_sources_job() -> None:
    """Alert once per source that keeps answering but stopped publishing. Logged at
    WARNING, which the admin-alert handler forwards — no second admin_alert call, or
    the admin gets it twice."""
    from src.db.models import get_app_setting, get_silent_sources, set_app_setting

    rows = await get_silent_sources(_SILENT_SOURCE_ALERT_HOURS)
    stored = await get_app_setting("silent_sources_alerted") or ""
    already = {int(part) for part in stored.split(",") if part.strip().isdigit()}
    fresh, current = _new_silent_sources(rows, already)
    if fresh:
        log.warning("Silent source(s) past %dh: %s",
                    _SILENT_SOURCE_ALERT_HOURS, "; ".join(_silent_source_line(r) for r in fresh))
    else:
        log.info("Silent-source check: %d source(s) past %dh, none new",
                 len(rows), _SILENT_SOURCE_ALERT_HOURS)
    if current != already:
        await set_app_setting("silent_sources_alerted", ",".join(str(i) for i in sorted(current)))


async def _revive_rss_job() -> None:
    from src.db.models import revive_error_rss_sources

    revived = await revive_error_rss_sources()
    if revived:
        log.info("RSS revive: re-activated %d error source(s) for re-probe: %s", len(revived), ", ".join(revived))


async def _startup_provider_check() -> None:
    """Verify LLM provider keys shortly after boot so a missing/expired key is
    surfaced right after a deploy, not only at the daily 04:30 check."""
    from src.processor.llm.llm_client import verify_llm_providers

    await asyncio.sleep(60)
    await verify_llm_providers()


async def start_scheduler() -> None:
    global _scheduler
    _scheduler = AsyncIOScheduler(timezone=settings.digest_timezone)
    await _add_maintenance_jobs()
    await _rebuild_digest_jobs()
    _scheduler.start()
    asyncio.create_task(_startup_provider_check())


async def rebuild_digest_jobs() -> None:
    if _scheduler is None:
        return
    await _rebuild_digest_jobs()


async def _pre_digest_classify() -> None:
    from src.processor.llm.classifier import classify_pending_items

    log.info("Pre-digest classify started")
    await classify_pending_items(limit=999)
    log.info("Pre-digest classify done")


async def _pre_digest_collect() -> None:
    from src.collectors.rss_collector import poll_rss_once
    from src.collectors.telegram_collector import poll_telegram_once

    log.info("Pre-digest collection started")
    await asyncio.gather(poll_telegram_once(), poll_rss_once())
    log.info("Pre-digest collection done")


async def _check_pending_sources() -> None:
    from src.collectors.folder_manager import add_to_folder
    from src.collectors.telegram_collector import userbot

    from src.collectors.telegram_collector import _is_invite_link

    pending = await get_pending_sources()
    for s in pending:
        if s["type"] != "telegram":
            continue
        raw = s["url"].lstrip("@")
        try:
            chat = await userbot.join_chat(raw)
        except Exception as exc:
            exc_str = str(exc).upper()
            if "ALREADY" not in exc_str and "USER_ALREADY_PARTICIPANT" not in exc_str:
                log.debug("Pending source not yet accessible: id=%s | %s", s["id"], exc)
                continue
            log.info("Pending source already a member: id=%s url=%s", s["id"], s["url"])
            chat = None

        if _is_invite_link(s["url"]) and chat is not None and hasattr(chat, "id"):
            await update_source_url(s["id"], str(chat.id))
            log.info("Stored resolved chat_id=%d for source id=%s", chat.id, s["id"])

        await add_to_folder(raw)
        await activate_source(s["id"])
        log.info("Pending source activated: id=%s url=%s", s["id"], s["url"])

        pending_msg_id = row_get(s, "pending_msg_id")
        if pending_msg_id:
            try:
                await userbot.delete_messages("me", [pending_msg_id])
                await set_source_pending_msg_id(s["id"], None)
                log.info("Deleted pending notice from Saved Messages for source id=%s", s["id"])
            except Exception as exc:
                log.warning("Could not delete pending notice from Saved Messages: %s", exc)


def _pre_collect_time(h: int, m: int) -> tuple[int, int]:
    # Collect first (T-2) so the pre-classify pass (T-1) can summarise the fresh
    # items before the digest, instead of leaving them to the slower inline
    # reclassify inside send_digest (which risks the reclassify timeout).
    m -= 2
    if m < 0:
        m += 60
        h = (h - 1) % 24
    return h, m


def _pre_classify_time(h: int, m: int) -> tuple[int, int]:
    m -= 1
    if m < 0:
        m = 59
        h = (h - 1) % 24
    return h, m


async def _add_maintenance_jobs() -> None:
    """Fixed-schedule jobs, added once at startup. Kept apart from the digest jobs
    so a schedule edit — which can be a tap per category — only touches the cron
    entries it actually changes.
    An id here must not start with "digest_"/"pre_collect_"/"pre_classify_", or the
    sweep in _rebuild_digest_jobs would remove it."""
    _scheduler.add_job(
        _check_pending_sources,
        CronTrigger(minute=0),
        id="pending_check",
        replace_existing=True,
    )

    from src.processor.llm.classifier import classify_pending_items
    _scheduler.add_job(
        classify_pending_items,
        CronTrigger(minute="*/20"),
        id="classify_pending",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    _scheduler.add_job(
        _prune_items_job,
        CronTrigger(hour=4, minute=0, timezone=settings.digest_timezone),
        id="prune_items",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    _scheduler.add_job(
        _revive_rss_job,
        CronTrigger(hour=4, minute=15, timezone=settings.digest_timezone),
        id="revive_rss",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    from src.processor.llm.llm_client import verify_llm_providers
    _scheduler.add_job(
        verify_llm_providers,
        CronTrigger(hour=4, minute=30, timezone=settings.digest_timezone),
        id="verify_llm",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    _scheduler.add_job(
        _silent_sources_job,
        CronTrigger(hour=5, minute=15, timezone=settings.digest_timezone),
        id="silent_sources",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    _scheduler.add_job(
        _digest_health_job,
        CronTrigger(hour=5, minute=0, timezone=settings.digest_timezone),
        id="health_check",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )


async def _rebuild_digest_jobs() -> None:
    """Digest jobs are rebuilt from scratch on every schedule change; maintenance
    jobs are left alone."""
    for job in _scheduler.get_jobs():
        if job.id.startswith(("digest_", "pre_collect_", "pre_classify_")):
            _scheduler.remove_job(job.id)

    scheduled_pre_collect: set[str] = set()
    scheduled_pre_classify: set[str] = set()

    def _add_pre_collect(h: int, m: int) -> None:
        ph, pm = _pre_collect_time(h, m)
        job_id = f"pre_collect_{ph:02d}:{pm:02d}"
        if job_id not in scheduled_pre_collect:
            _scheduler.add_job(
                _pre_digest_collect,
                CronTrigger(hour=ph, minute=pm, timezone=settings.digest_timezone),
                id=job_id,
                replace_existing=True,
            )
            scheduled_pre_collect.add(job_id)

    def _add_pre_classify(h: int, m: int) -> None:
        ph, pm = _pre_classify_time(h, m)
        job_id = f"pre_classify_{ph:02d}:{pm:02d}"
        if job_id not in scheduled_pre_classify:
            _scheduler.add_job(
                _pre_digest_classify,
                CronTrigger(hour=ph, minute=pm, timezone=settings.digest_timezone),
                id=job_id,
                replace_existing=True,
            )
            scheduled_pre_classify.add(job_id)

    # One digest job per distinct time. No catch-all default schedule: the day is
    # exactly what category_times says it is.
    by_time = await get_schedule_slots()

    # Quiet-sources block goes to the last digest of the day (HH:MM is
    # zero-padded, so lexical max == chronological latest).
    last_time = max(by_time) if by_time else None

    for time_str, cat_names in by_time.items():
        h, m = map(int, time_str.split(":"))
        _scheduler.add_job(
            send_digest,
            CronTrigger(hour=h, minute=m, timezone=settings.digest_timezone),
            id=f"digest_{time_str}",
            replace_existing=True,
            kwargs={"categories": cat_names, "include_quiet": time_str == last_time},
        )
        _add_pre_collect(h, m)
        _add_pre_classify(h, m)

    job_ids = [j.id for j in _scheduler.get_jobs()]
    log.info("Digest jobs: %s", job_ids)
