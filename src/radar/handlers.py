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
    text = str(message.text or message.caption or "")
    sender_chat = getattr(message, "sender_chat", None)
    from_user = message.from_user
    auto_fwd = getattr(message, "is_automatic_forward", None)
    fwd_chat = getattr(message, "forward_from_chat", None)
    log.debug(
        "Radar: msg=%s chat=%s from_user=%s sender_chat=%s auto_forward=%s fwd_from_chat=%s text_len=%d text=%r",
        message.id,
        message.chat.id,
        from_user.id if from_user else None,
        sender_chat.id if sender_chat else None,
        auto_fwd,
        fwd_chat.id if fwd_chat else None,
        len(text),
        text[:120],
    )
    if not text:
        log.debug("Radar: msg=%s skipped — no text/caption", message.id)
        return False

    blacklist = await get_radar_blacklist()
    blacklisted_ids = {row["user_id"] for row in blacklist}
    if from_user and from_user.id in blacklisted_ids:
        log.debug("Radar: msg=%s skipped — from_user %s is blacklisted", message.id, from_user.id)
        return False
    if sender_chat and sender_chat.id in blacklisted_ids:
        log.debug("Radar: msg=%s skipped — sender_chat %s is blacklisted", message.id, sender_chat.id)
        return False

    keywords = await get_radar_keywords()
    linked_kw_ids = await get_keyword_ids_for_chat(chat_row["id"])
    chat_keywords = [row["keyword"] for row in keywords if row["id"] in linked_kw_ids]
    if not chat_keywords:
        log.debug("Radar: msg=%s skipped — no keywords linked to chat db_id=%s", message.id, chat_row["id"])
        return False
    matched = match_keywords(text, chat_keywords)
    if not matched:
        log.debug("Radar: msg=%s skipped — no keyword match (checked: %s)", message.id, chat_keywords)
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
    if author:
        first = author.first_name or ""
        last = author.last_name or ""
        username = f"@{author.username}" if author.username else "—"
        author_id = author.id
        from_str = f"{escape(first)} {escape(last)} ({escape(username)}) · id: <code>{author_id}</code>"
    elif sender_chat:
        title = sender_chat.title or ""
        uname = f"@{sender_chat.username}" if sender_chat.username else "—"
        author_id = sender_chat.id
        from_str = f"📢 {escape(title)} ({escape(uname)}) · id: <code>{author_id}</code>"
    else:
        author_id = None
        from_str = "—"
    ts = message.date.strftime("%Y-%m-%d %H:%M") if message.date else "—"
    short_text = text[:500] + ("..." if len(text) > 500 else "")

    kw_label = "Keyword" if len(matched) == 1 else "Keywords"
    kw_str = ", ".join(f"<b>{escape(kw)}</b>" for kw in matched)
    alert_body = (
        f"🔍 {kw_label}: {kw_str}\n"
        f"💬 <b>Chat:</b> {escape(chat_title)}\n"
        f"👤 <b>From:</b> {from_str}\n"
        f"🔗 <a href=\"{escape(msg_link, quote=True)}\">Open message</a> · ⏱️ {ts} UTC\n"
        f"<blockquote expandable>{escape(short_text)}</blockquote>"
    )
    await send_to(settings.telegram_admin_id, alert_body)
    for kw in matched:
        await log_radar_alert(
            kw,
            chat_ref_str,
            author_id,
            text,
            msg_link,
        )
    log.info(
        "Radar alert sent: keywords=%s chat=%s author_id=%s",
        matched,
        chat_title,
        author_id,
    )
    return True
