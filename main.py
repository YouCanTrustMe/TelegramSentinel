import asyncio
import logging
from pathlib import Path

from pyrogram import idle

from src.bot.commands import register_commands
from src.collectors.rss_collector import run_rss_collector
from src.collectors.telegram_collector import load_watched_channels, userbot
from src.db.models import init_db
from src.dispatcher.sender import bot
from src.scheduler import start_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


async def main() -> None:
    Path("sessions").mkdir(exist_ok=True)
    await init_db()
    log.info("Database initialized")

    await bot.start()
    await userbot.start()
    log.info("Clients started")

    await load_watched_channels()
    register_commands()
    start_scheduler()
    log.info("Scheduler started")

    asyncio.create_task(run_rss_collector())
    log.info("RSS collector running")

    log.info("TelegramSentinel is running")
    await idle()

    await bot.stop()
    await userbot.stop()


if __name__ == "__main__":
    asyncio.run(main())
