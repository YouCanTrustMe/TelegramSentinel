import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.bot.state import _DEFAULT_DIGEST_TIME
from src.config import settings
from src.db.models import activate_source, get_categories, get_pending_sources, set_source_pending_msg_id, update_source_url
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

        pending_msg_id = s["pending_msg_id"] if "pending_msg_id" in s.keys() else None
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
        try:
            h, m = map(int, t.split(":"))
            result.append((h, m))
        except (ValueError, AttributeError):
            log.warning("Invalid time in schedule string: %r", t)
    return result


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

    default_times = _parse_times(_DEFAULT_DIGEST_TIME)
    default_time_set = frozenset(f"{h:02d}:{m:02d}" for h, m in default_times)

    for h, m in default_times:
        time_str = f"{h:02d}:{m:02d}"
        _scheduler.add_job(
            send_digest,
            CronTrigger(hour=h, minute=m, timezone=settings.digest_timezone),
            id=f"digest_{time_str}",
            replace_existing=True,
        )

    # Extra jobs for categories with a custom (non-default) digest time
    categories = await get_categories()
    custom: dict[str, list[str]] = {}
    for cat in categories:
        cat_time_set = frozenset(x.strip() for x in cat["digest_time"].split(","))
        if cat_time_set != default_time_set:
            for time_part in cat_time_set:
                custom.setdefault(time_part, []).append(cat["name"])

    for time_str, cat_names in custom.items():
        try:
            h, m = map(int, time_str.split(":"))
        except ValueError:
            log.warning("Skipping invalid custom digest time: %r", time_str)
            continue
        _scheduler.add_job(
            send_digest,
            CronTrigger(hour=h, minute=m, timezone=settings.digest_timezone),
            id=f"digest_{time_str}_cats",
            replace_existing=True,
            kwargs={"categories": cat_names},
        )

    job_ids = [j.id for j in _scheduler.get_jobs()]
    log.info("Digest jobs: %s", job_ids)
