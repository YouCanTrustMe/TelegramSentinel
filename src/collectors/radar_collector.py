import asyncio
import logging

import aiosqlite

from src.collectors.telegram_collector import userbot
from src.db.models import get_db, get_radar_chats
from src.radar.handlers import process_radar_message

log = logging.getLogger(__name__)

POLL_INTERVAL = 60


async def _set_last_seen(entry_id: int, msg_id: int) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE radar_chats SET last_seen_msg_id = ? WHERE id = ? "
            "AND (last_seen_msg_id IS NULL OR last_seen_msg_id < ?)",
            (msg_id, entry_id, msg_id),
        )
        await db.commit()


async def _poll_chat(row: aiosqlite.Row) -> int:
    keys = row.keys()
    chat_id = row["chat_id"] if "chat_id" in keys else None
    if chat_id is None:
        return 0
    last_seen = row["last_seen_msg_id"] if "last_seen_msg_id" in keys else None
    limit = 5 if last_seen is None else 50

    new_messages = []
    max_id = 0
    try:
        async for msg in userbot.get_chat_history(chat_id, limit=limit):
            if last_seen is not None and msg.id <= last_seen:
                break
            new_messages.append(msg)
            if msg.id > max_id:
                max_id = msg.id
    except Exception as exc:
        log.warning("Radar poll failed for chat %s: %s", chat_id, exc)
        return 0

    if last_seen is None:
        if max_id:
            await _set_last_seen(row["id"], max_id)
        return 0

    alerts = 0
    for msg in reversed(new_messages):
        try:
            if await process_radar_message(msg, row):
                alerts += 1
        except Exception:
            log.exception("Radar processing failed for chat=%s msg=%s", chat_id, msg.id)

    if max_id:
        await _set_last_seen(row["id"], max_id)
    return alerts


async def run_radar_collector() -> None:
    log.info("Radar collector started (interval=%ds)", POLL_INTERVAL)
    while True:
        try:
            chats = await get_radar_chats()
            total_alerts = 0
            for row in chats:
                if "status" in row.keys() and row["status"] != "active":
                    continue
                total_alerts += await _poll_chat(row)
            if total_alerts:
                log.info("Radar poll cycle: %d alert(s) sent", total_alerts)
        except Exception:
            log.exception("Radar collector iteration failed")
        await asyncio.sleep(POLL_INTERVAL)
