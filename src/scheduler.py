import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.bot.state import _DEFAULT_DIGEST_TIME
from src.config import settings
from src.db.models import activate_source, get_categories, get_pending_sources
from src.dispatcher.digest_builder import send_digest

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def start_scheduler() -> None:
    global _scheduler
    _scheduler = AsyncIOScheduler(timezone=settings.digest_timezone)
    await _rebuild_jobs()
    _scheduler.start()


async def rebuild_digest_jobs() -> None:
    if _scheduler is None:
        return
    await _rebuild_jobs()


async def _check_pending_sources() -> None:
    from src.collectors.folder_manager import add_to_folder
    from src.collectors.telegram_collector import userbot

    pending = await get_pending_sources()
    for s in pending:
        if s["type"] != "telegram":
            continue
        raw = s["url"].lstrip("@")
        try:
            await userbot.join_chat(raw)
            await add_to_folder(raw)
            await activate_source(s["id"])
            log.info("Pending source activated: id=%s url=%s", s["id"], s["url"])
        except Exception as exc:
            log.debug("Pending source not yet accessible: id=%s | %s", s["id"], exc)


async def _rebuild_jobs() -> None:
    for job in _scheduler.get_jobs():
        if job.id.startswith("digest_"):
            _scheduler.remove_job(job.id)

    _scheduler.add_job(
        _check_pending_sources,
        CronTrigger(minute=0),
        id="pending_check",
        replace_existing=True,
    )

    h, m = map(int, _DEFAULT_DIGEST_TIME.split(":"))
    _scheduler.add_job(
        send_digest,
        CronTrigger(hour=h, minute=m, timezone=settings.digest_timezone),
        id=f"digest_{_DEFAULT_DIGEST_TIME}",
        replace_existing=True,
    )

    # Extra jobs for categories with a custom (non-default) digest time
    categories = await get_categories()
    custom: dict[str, list[str]] = {}
    for cat in categories:
        t = cat["digest_time"]
        if t != _DEFAULT_DIGEST_TIME:
            custom.setdefault(t, []).append(cat["name"])

    for time_str, cat_names in custom.items():
        h, m = map(int, time_str.split(":"))
        _scheduler.add_job(
            send_digest,
            CronTrigger(hour=h, minute=m, timezone=settings.digest_timezone),
            id=f"digest_{time_str}",
            replace_existing=True,
            kwargs={"categories": cat_names},
        )

    job_ids = [j.id for j in _scheduler.get_jobs()]
    log.info("Digest jobs: %s", job_ids)
