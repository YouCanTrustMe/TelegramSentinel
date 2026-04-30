from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.config import settings
from src.dispatcher.digest_builder import send_digest

_scheduler = AsyncIOScheduler(timezone=settings.digest_timezone)
_JOB_ID = "daily_digest"


def start_scheduler() -> None:
    h, m = map(int, settings.digest_time.split(":"))
    _scheduler.add_job(
        send_digest,
        CronTrigger(hour=h, minute=m, timezone=settings.digest_timezone),
        id=_JOB_ID,
        replace_existing=True,
    )
    _scheduler.start()


def reschedule_digest(time_str: str) -> None:
    h, m = map(int, time_str.split(":"))
    _scheduler.reschedule_job(
        _JOB_ID,
        trigger=CronTrigger(hour=h, minute=m, timezone=settings.digest_timezone),
    )
