import logging

from pyrogram import filters

from src.collectors.telegram_collector import userbot
from src.config import settings
from src.db.models import (
    get_radar_blacklist,
    get_radar_chats,
    get_radar_keywords,
    log_radar_alert,
)
from src.dispatcher.sender import bot
from src.radar.cooldown import is_on_cooldown, set_cooldown
from src.radar.matcher import match_keywords

log = logging.getLogger(__name__)

_my_id: int | None = None


def register_radar_handlers() -> None:

    async def _handle(client, message) -> None:
        try:
            global _my_id
            if _my_id is None:
                _my_id = (await client.get_me()).id

            text = message.text or message.caption or ""
            if not text:
                return

            if message.from_user and message.from_user.id == _my_id:
                return

            chat_id = message.chat.id

            chats = await get_radar_chats()
            monitored: set[str | int] = set()
            for row in chats:
                ref = row["chat_ref"]
                if ref.startswith("@"):
                    monitored.add(ref.lower())
                else:
                    try:
                        monitored.add(int(ref))
                    except ValueError:
                        pass

            chat_username = (
                f"@{message.chat.username.lower()}" if message.chat.username else None
            )
            if chat_id not in monitored and (
                chat_username is None or chat_username not in monitored
            ):
                return

            blacklist = await get_radar_blacklist()
            blacklisted_ids = {row["user_id"] for row in blacklist}
            if message.from_user and message.from_user.id in blacklisted_ids:
                return

            keywords = await get_radar_keywords()
            matched = match_keywords(text, [row["keyword"] for row in keywords])
            if not matched:
                return

            if message.chat.username:
                msg_link = f"https://t.me/{message.chat.username}/{message.id}"
                chat_ref_str = f"@{message.chat.username}"
            else:
                pure_id = abs(chat_id) - 1000000000000
                msg_link = f"https://t.me/c/{pure_id}/{message.id}"
                chat_ref_str = str(chat_id)

            chat_title = message.chat.title or chat_ref_str
            author = message.from_user
            first = (author.first_name or "") if author else ""
            last = (author.last_name or "") if author else ""
            username = f"@{author.username}" if author and author.username else "—"
            ts = message.date.strftime("%Y-%m-%d %H:%M") if message.date else "—"
            short_text = text[:500] + ("..." if len(text) > 500 else "")

            for kw in matched:
                if is_on_cooldown(kw, chat_id, settings.radar_cooldown_seconds):
                    log.debug("Radar cooldown: keyword=%s chat_id=%d", kw, chat_id)
                    continue
                set_cooldown(kw, chat_id)

                alert = (
                    f"🔍 <b>Keyword:</b> {kw}\n"
                    f"💬 <b>Chat:</b> {chat_title}\n"
                    f"👤 <b>From:</b> {first} {last} ({username})\n"
                    f"📝 <b>Text:</b> {short_text}\n"
                    f"🔗 <a href=\"{msg_link}\">Open message</a>\n"
                    f"⏱️ {ts} UTC"
                )
                await bot.send_message(
                    settings.telegram_admin_id,
                    alert,
                    parse_mode="html",
                    disable_web_page_preview=True,
                )
                await log_radar_alert(
                    kw,
                    chat_ref_str,
                    author.id if author else None,
                    text,
                    msg_link,
                )
                log.info(
                    "Radar alert sent: keyword=%s chat=%s author=%s",
                    kw,
                    chat_title,
                    username,
                )

        except Exception:
            log.exception("Radar handler error")

    userbot.on_message(filters.incoming)(_handle)
    userbot.on_edited_message(filters.incoming)(_handle)
