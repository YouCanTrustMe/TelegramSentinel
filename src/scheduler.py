import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.config import settings
from src.db.models import activate_source, get_categories, get_pending_sources, set_source_pending_msg_id, update_source_url
from src.dispatcher.digest_builder import send_digest
from src.common.util import row_get

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None

# Nightly prune horizon for sent items; see prune_old_items for the RSS-dedup trade-off.
_RETENTION_DAYS = 30


async def _prune_items_job() -> None:
    from src.db.models import prune_old_items

    deleted = await prune_old_items(_RETENTION_DAYS)
    if deleted:
        log.info("Retention: pruned %d sent item(s) older than %d days", deleted, _RETENTION_DAYS)


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
    await _rebuild_jobs()
    _scheduler.start()
    asyncio.create_task(_startup_provider_check())


async def rebuild_digest_jobs() -> None:
    if _scheduler is None:
        return
    await _rebuild_jobs()


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


def _parse_times(time_str: str) -> list[tuple[int, int]]:
    result = []
    for t in time_str.split(","):
        t = t.strip()
        if not t:
            continue
        try:
            h, m = map(int, t.split(":"))
            result.append((h, m))
        except (ValueError, AttributeError):
            log.warning("Invalid time in schedule string: %r", t)
    return result


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


async def _rebuild_jobs() -> None:
    for job in _scheduler.get_jobs():
        if job.id.startswith("digest_") or job.id.startswith("pre_collect_") or job.id.startswith("pre_classify_"):
            _scheduler.remove_job(job.id)

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

    # One digest job per distinct category time. No catch-all default schedule:
    # the schedule is driven entirely by each category's digest_time.
    categories = await get_categories()
    by_time: dict[str, list[str]] = {}
    for cat in categories:
        for h, m in _parse_times(cat["digest_time"]):
            by_time.setdefault(f"{h:02d}:{m:02d}", []).append(cat["name"])

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
