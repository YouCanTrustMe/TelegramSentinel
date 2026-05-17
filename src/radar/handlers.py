import logging
from html import escape

from pyrogram import filters

from src.collectors.telegram_collector import userbot
from src.config import settings
from src.db.models import (
    get_keyword_ids_for_chat,
    get_radar_blacklist,
    get_radar_chats,
    get_radar_keywords,
    log_radar_alert,
)
from src.dispatcher.sender import send_to
from src.radar.matcher import match_keywords

log = logging.getLogger(__name__)

_my_id: int | None = None
_seen: set[tuple[int, int]] = set()
_seen_chats: set[int] = set()


def register_radar_handlers() -> None:

    async def _handle(client, message) -> None:
        try:
            global _my_id
            chat_obj = getattr(message, "chat", None)
            chat_id_for_log = getattr(chat_obj, "id", None)
            if chat_id_for_log is not None and chat_id_for_log not in _seen_chats:
                _seen_chats.add(chat_id_for_log)
                chat_title_for_log = getattr(chat_obj, "title", None) or getattr(chat_obj, "username", None) or "?"
                log.info(
                    "Radar handler: first update from chat | chat_id=%s title=%s (total seen=%d)",
                    chat_id_for_log,
                    chat_title_for_log,
                    len(_seen_chats),
                )
            if _my_id is None:
                _my_id = (await client.get_me()).id

            text = message.text or message.caption or ""
            if not text:
                return

            if message.from_user and message.from_user.id == _my_id:
                return

            chat_id = message.chat.id
            chat_username = (
                f"@{message.chat.username.lower()}" if message.chat.username else None
            )

            if (chat_id, message.id) in _seen:
                return
            _seen.add((chat_id, message.id))

            chats = await get_radar_chats()
            matched_chat_row = None
            for row in chats:
                row_keys = row.keys()
                resolved = row["chat_id"] if "chat_id" in row_keys else None
                if resolved is not None and int(resolved) == chat_id:
                    matched_chat_row = row
                    break
                ref = row["chat_ref"]
                if ref.startswith("@") and chat_username == ref.lower():
                    matched_chat_row = row
                    break
                if not ref.startswith("@"):
                    try:
                        if int(ref) == chat_id:
                            matched_chat_row = row
                            break
                    except ValueError:
                        pass

            if matched_chat_row is None:
                return

            log.info("Radar: message in monitored chat | chat=%s id=%d", chat_username or chat_id, message.id)

            blacklist = await get_radar_blacklist()
            blacklisted_ids = {row["user_id"] for row in blacklist}
            if message.from_user and message.from_user.id in blacklisted_ids:
                return

            keywords = await get_radar_keywords()
            linked_kw_ids = await get_keyword_ids_for_chat(matched_chat_row["id"])
            chat_keywords = [row["keyword"] for row in keywords if row["id"] in linked_kw_ids]
            if not chat_keywords:
                return
            matched = match_keywords(text, chat_keywords)
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

            kw_label = "Keyword" if len(matched) == 1 else "Keywords"
            kw_str = ", ".join(f"<b>{escape(kw)}</b>" for kw in matched)
            alert_body = (
                f"🔍 {kw_label}: {kw_str}\n"
                f"💬 <b>Chat:</b> {escape(chat_title)}\n"
                f"👤 <b>From:</b> {escape(first)} {escape(last)} ({escape(username)})\n"
                f"🔗 <a href=\"{escape(msg_link, quote=True)}\">Open message</a> · ⏱️ {ts} UTC\n"
                f"<blockquote expandable>{escape(short_text)}</blockquote>"
            )
            await send_to(settings.telegram_admin_id, alert_body)
            for kw in matched:
                await log_radar_alert(
                    kw,
                    chat_ref_str,
                    author.id if author else None,
                    text,
                    msg_link,
                )
            log.info(
                "Radar alert sent: keywords=%s chat=%s author=%s",
                matched,
                chat_title,
                username,
            )

        except Exception:
            log.exception("Radar handler error")

    userbot.on_message(filters.all)(_handle)
    userbot.on_edited_message(filters.all)(_handle)

    async def _raw(client, update, users, chats) -> None:
        try:
            cid = None
            msg = getattr(update, "message", None)
            peer = getattr(msg, "peer_id", None) if msg is not None else None
            if peer is not None:
                if hasattr(peer, "channel_id") and peer.channel_id is not None:
                    cid = -1000000000000 - peer.channel_id
                elif hasattr(peer, "chat_id") and peer.chat_id is not None:
                    cid = -peer.chat_id
                elif hasattr(peer, "user_id") and peer.user_id is not None:
                    cid = peer.user_id
            if cid is None:
                channel_id = getattr(update, "channel_id", None)
                if channel_id is not None:
                    cid = -1000000000000 - channel_id
            if cid is None:
                return
            monitored = {row["chat_id"] for row in await get_radar_chats() if row["chat_id"] is not None}
            if cid in monitored:
                log.info("Radar RAW update: type=%s chat_id=%s", type(update).__name__, cid)
        except Exception:
            log.exception("Radar raw handler error")

    userbot.on_raw_update()(_raw)
