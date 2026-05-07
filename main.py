import asyncio
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from pyrogram import idle

from src.bot.commands import register_commands
from src.collectors.rss_collector import run_rss_collector
from src.collectors.telegram_collector import run_telegram_collector, userbot
from src.db.models import init_db
from src.dispatcher.sender import bot
from src.radar.handlers import register_radar_handlers
from src.scheduler import start_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

_log_dir = Path("data/logs")
_log_dir.mkdir(parents=True, exist_ok=True)
_file_handler = TimedRotatingFileHandler(
    _log_dir / "sentinel.log",
    when="midnight",
    backupCount=7,
    encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
logging.getLogger().addHandler(_file_handler)

log = logging.getLogger(__name__)


async def main() -> None:
    running_loop = asyncio.get_running_loop()
    bot.dispatcher.loop = running_loop
    userbot.dispatcher.loop = running_loop

    Path("sessions").mkdir(exist_ok=True)
    await init_db()
    log.info("Database initialized")

    register_commands()
    register_radar_handlers()

    await bot.start()
    await userbot.start()
    log.info("Clients started")

    await start_scheduler()
    log.info("Scheduler started")

    _background_tasks: set[asyncio.Task] = set()
    for coro in (run_rss_collector(), run_telegram_collector()):
        task = asyncio.create_task(coro)
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    log.info("Collectors running")

    log.info("TelegramSentinel is running")
    await idle()

    await bot.stop()
    await userbot.stop()


if __name__ == "__main__":
    asyncio.run(main())
