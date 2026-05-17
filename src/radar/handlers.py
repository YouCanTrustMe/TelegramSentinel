import logging
from html import escape

from src.config import settings
from src.db.models import (
    get_keyword_ids_for_chat,
    get_radar_blacklist,
    get_radar_keywords,
    log_radar_alert,
)
from src.dispatcher.sender import send_to
from src.radar.matcher import match_keywords

log = logging.getLogger(__name__)


async def process_radar_message(message, chat_row) -> bool:
    text = message.text or message.caption or ""
    if not text:
        return False

    blacklist = await get_radar_blacklist()
    blacklisted_ids = {row["user_id"] for row in blacklist}
    if message.from_user and message.from_user.id in blacklisted_ids:
        return False

    keywords = await get_radar_keywords()
    linked_kw_ids = await get_keyword_ids_for_chat(chat_row["id"])
    chat_keywords = [row["keyword"] for row in keywords if row["id"] in linked_kw_ids]
    if not chat_keywords:
        return False
    matched = match_keywords(text, chat_keywords)
    if not matched:
        return False

    chat_id = message.chat.id
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
    return True
