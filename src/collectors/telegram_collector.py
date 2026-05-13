import asyncio
import logging
from datetime import datetime, timezone

from pyrogram import Client, raw as tg_raw
from pyrogram.errors import ChannelBanned, ChannelInvalid, ChannelPrivate, ChatForbidden, UserBannedInChannel, UserKicked
from pyrogram.types import Message

from src.config import settings
from src.db.models import get_active_sources, save_item, set_source_last_message_id, update_source_status, update_source_url
from src.dispatcher.sender import send_to
from src.processor.deduplicator import is_duplicate, make_message_id

log = logging.getLogger(__name__)

POLL_INTERVAL = 300  # seconds between channel polls

userbot = Client(
    "sessions/sentinel_userbot",
    api_id=settings.telegram_api_id,
    api_hash=settings.telegram_api_hash,
    phone_number=settings.telegram_phone,
)


def _is_invite_link(url: str) -> bool:
    return "t.me/+" in url or "telegram.me/joinchat/" in url


def _invite_hash(url: str) -> str:
    if "t.me/+" in url:
        return url.split("t.me/+", 1)[1]
    return url.split("/joinchat/", 1)[1]


async def _resolve_invite_link(url: str, source_id: int) -> str | None:
    try:
        result = await userbot.invoke(
            tg_raw.functions.messages.CheckChatInvite(hash=_invite_hash(url))
        )
        if isinstance(result, tg_raw.types.ChatInviteAlready):
            chat = result.chat
            if isinstance(chat, tg_raw.types.Channel):
                pyrogram_id = int(f"-100{chat.id}")
            else:
                pyrogram_id = -chat.id
            await update_source_url(source_id, str(pyrogram_id))
            log.info("Resolved invite link source id=%d → chat_id=%d", source_id, pyrogram_id)
            return str(pyrogram_id)
        log.warning("Invite link not yet joined for source id=%d", source_id)
    except Exception as exc:
        log.warning("Could not resolve invite link source id=%d: %s", source_id, exc)
    return None


async def _process_message(chat_ref: str, source: dict, message: Message, parent_msg: "Message | None" = None) -> bool:
    no_caption = False
    if message.poll:
        poll = message.poll
        opts = ", ".join(opt.text for opt in (poll.options or [])[:4])
        raw_text = f"[Poll] {poll.question}" + (f" ({opts})" if opts else "")
    else:
        caption = message.text or message.caption or ""
        if message.photo:
            media_prefix = "[Photo] "
        elif message.video:
            media_prefix = "[Video] "
        elif message.animation:
            media_prefix = "[GIF] "
        elif message.document:
            media_prefix = "[Doc] "
        elif message.audio:
            media_prefix = "[Audio] "
        elif message.voice:
            media_prefix = "[Voice] "
        else:
            media_prefix = ""

        raw_text = (media_prefix + caption).strip()
        if not raw_text or raw_text in ("[Photo]", "[Video]", "[GIF]", "[Doc]", "[Audio]", "[Voice]"):
            if not media_prefix:
                return False
            raw_text = media_prefix.strip()
            no_caption = True
        else:
            if message.forward_from_chat and message.forward_from_chat.title:
                fwd_title = message.forward_from_chat.title.strip()
                if fwd_title:
                    raw_text = f"[Forwarded from {fwd_title}] {raw_text}"

            if parent_msg is not None:
                parent_text = (parent_msg.text or parent_msg.caption or "").strip()
                if parent_text:
                    raw_text = f"[Context: {parent_text[:200].split(chr(10))[0]}]\n{raw_text}"

    message_id = make_message_id("telegram", chat_ref, str(message.id))
    if await is_duplicate(message_id):
        return False

    if chat_ref.lstrip("-").isdigit():
        raw_channel_id = abs(int(chat_ref)) - 1000000000000
        original_url = f"https://t.me/c/{raw_channel_id}/{message.id}"
    else:
        username = chat_ref.lstrip("@")
        original_url = f"https://t.me/{username}/{message.id}"

    published_at = message.date.replace(tzinfo=timezone.utc).isoformat() if message.date else None

    if no_caption:
        summary = "no text"
        key_phrase = ""
    elif len(raw_text.strip()) < 15:
        summary = raw_text.strip()
        key_phrase = ""
    else:
        summary = ""
        key_phrase = ""

    await save_item(
        source_id=source["id"],
        message_id=message_id,
        raw_text=raw_text,
        original_url=original_url,
        published_at=published_at,
        summary=summary,
        category=source["category"],
        processed_at=datetime.now(timezone.utc).isoformat(),
        key_phrase=key_phrase,
    )
    log.info("Saved item from %s | category=%s | %s", chat_ref, source["category"], original_url)
    return True


async def _poll_channel(chat_ref: str, source: dict) -> int:
    if chat_ref.lstrip("-").isdigit():
        chat_id: int | str = int(chat_ref)
    else:
        chat_id = chat_ref if chat_ref.startswith("@") else f"@{chat_ref}"

    last_msg_id = source.get("last_message_id")
    limit = 20 if last_msg_id is None else 100

    saved = 0
    try:
        messages = []
        async for message in userbot.get_chat_history(chat_id, limit=limit):
            if last_msg_id is not None and message.id <= last_msg_id:
                break
            messages.append(message)

        max_seen_id = 0
        seen_group_ids: set = set()
        messages_by_id = {m.id: m for m in messages}
        for message in messages:
            if message.id > max_seen_id:
                max_seen_id = message.id
            group_id = message.media_group_id
            if group_id is not None:
                if group_id in seen_group_ids:
                    continue
                seen_group_ids.add(group_id)
                group_msgs = [m for m in messages if m.media_group_id == group_id]
                message = next((m for m in group_msgs if (m.text or m.caption)), group_msgs[0])
            parent_msg = messages_by_id.get(message.reply_to_message_id) if message.reply_to_message_id else None
            if await _process_message(chat_ref, source, message, parent_msg=parent_msg):
                saved += 1

        if max_seen_id:
            await set_source_last_message_id(source["id"], max_seen_id)
    except (ChannelInvalid, ChannelPrivate, ChannelBanned, ChatForbidden, UserBannedInChannel, UserKicked) as exc:
        log.error("Source '%s' permanently inaccessible: %s | marking error", source["name"], exc)
        await update_source_status(source["id"], "error")
        await send_to(
            settings.telegram_admin_id,
            f"⚠️ <b>Source error</b>\n"
            f"<b>{source['name']}</b> ({chat_ref}) is no longer accessible.\n"
            f"Status set to <b>error</b>.\n"
            f"<i>{exc}</i>",
        )
    except Exception as exc:
        log.error("Failed to poll %s: %s", chat_ref, exc)
    return saved


async def run_telegram_collector() -> None:
    log.info("Telegram collector started (interval=%ds)", POLL_INTERVAL)
    while True:
        try:
            sources = await get_active_sources(type_="telegram")
            for row in sources:
                chat_ref = row["url"].lower() if not row["url"].lstrip("-").isdigit() else row["url"]
                source = {
                    "id": row["id"],
                    "name": row["name"],
                    "category": row["category"],
                    "last_message_id": row["last_message_id"] if "last_message_id" in row.keys() else None,
                }

                if _is_invite_link(chat_ref):
                    chat_ref = await _resolve_invite_link(chat_ref, row["id"])
                    if not chat_ref:
                        continue

                saved = await _poll_channel(chat_ref, source)
                if saved:
                    log.info("Telegram poll %s: %d new items", chat_ref, saved)
                else:
                    log.debug("Telegram poll %s: 0 new items", chat_ref)
        except Exception as exc:
            log.exception("Telegram collector iteration failed: %s", exc)
        await asyncio.sleep(POLL_INTERVAL)
