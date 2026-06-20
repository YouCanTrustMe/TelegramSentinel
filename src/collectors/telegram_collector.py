import asyncio
import logging
from datetime import datetime, timezone

from pyrogram import Client, raw as tg_raw
from pyrogram.errors import ChannelBanned, ChannelInvalid, ChannelPrivate, ChatForbidden, UserBannedInChannel, UserKicked, UsernameInvalid, UsernameNotOccupied
from pyrogram.types import Message

from src.config import settings
from src.db.models import find_sources_by_chat_id, get_active_sources, increment_source_fail_count, reset_source_fail_count, save_item, set_source_chat_id, set_source_last_message_id, update_source_status, update_source_url
from src.dispatcher.admin_alert import admin_alert
from src.processor.deduplicator import is_duplicate, make_message_id
from src.util import row_get

log = logging.getLogger(__name__)

POLL_INTERVAL = 300  # seconds between channel polls
_INVITE_FAIL_THRESHOLD = 20  # mark source as 'error' after this many consecutive invite-resolve failures
# Bootstrap a brand-new source with only its latest few posts; on subsequent polls
# fetch up to this many to catch up. Bounded so a stale/reset last_message_id can't
# pull a channel's whole history; a burst beyond it is reported, not silently lost.
_BOOTSTRAP_LIMIT = 20
_CATCHUP_LIMIT = 200

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


async def _find_chat_by_title_in_dialogs(name: str) -> int | None:
    """Fallback: scan userbot dialogs for a channel whose title matches `name` (case-insensitive)."""
    needle = name.strip().lower()
    if not needle:
        return None
    try:
        async for dialog in userbot.get_dialogs():
            chat = dialog.chat
            if chat is None or not chat.title:
                continue
            title = chat.title.strip().lower()
            if title == needle or needle in title:
                return chat.id
    except Exception as exc:
        log.warning("Dialog scan failed while looking for %r: %s", name, exc)
    return None


async def resolve_chat_id(url: str) -> int | None:
    """Resolve a Telegram URL/username/invite link to a numeric pyrogram chat_id, or None if unreachable."""
    try:
        if url.lstrip("-").isdigit():
            return int(url)
        if _is_invite_link(url):
            result = await userbot.invoke(
                tg_raw.functions.messages.CheckChatInvite(hash=_invite_hash(url))
            )
            if isinstance(result, tg_raw.types.ChatInviteAlready):
                chat = result.chat
                if isinstance(chat, tg_raw.types.Channel):
                    return int(f"-100{chat.id}")
                return -chat.id
            return None
        chat = await userbot.get_chat(url.lstrip("@"))
        return chat.id
    except Exception as exc:
        log.debug("Could not resolve chat_id for %s: %s", url, exc)
        return None


async def _warn_if_duplicate_chat(source_id: int, source_name: str, chat_id: int) -> None:
    """Alert admin if another source already points to the same chat_id."""
    dupes = await find_sources_by_chat_id(chat_id, exclude_id=source_id)
    if not dupes:
        return
    lines = [
        "⚠️ <b>Duplicate source detected</b>",
        f"<b>{source_name}</b> (id={source_id}) resolved to chat_id <code>{chat_id}</code>,",
        "which is already used by:",
    ]
    for d in dupes:
        lines.append(f"  • <b>{d['name']}</b> (id={d['id']}, status={d['status']}, url=<code>{d['url']}</code>)")
    lines.append("Remove one via /sources to stop double-collecting.")
    await admin_alert("\n".join(lines), key=f"source_dup:{chat_id}")


async def _resolve_invite_link(url: str, source_id: int, source_name: str) -> str | None:
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
            await set_source_chat_id(source_id, pyrogram_id)
            await reset_source_fail_count(source_id)
            log.info("Resolved invite link source id=%d → chat_id=%d", source_id, pyrogram_id)
            await _warn_if_duplicate_chat(source_id, source_name, pyrogram_id)
            return str(pyrogram_id)
        log.warning("Invite link not yet joined for source id=%d", source_id)
    except Exception as exc:
        # Invite hash may be dead (one-time / revoked) but userbot may already be a member.
        # Try a dialog scan by source name.
        dialog_id = await _find_chat_by_title_in_dialogs(source_name)
        if dialog_id is not None:
            await update_source_url(source_id, str(dialog_id))
            await set_source_chat_id(source_id, dialog_id)
            await reset_source_fail_count(source_id)
            log.info(
                "Invite hash dead for source id=%d (%s), but found member chat via dialog title → id=%d",
                source_id, source_name, dialog_id,
            )
            await admin_alert(
                f"ℹ️ <b>Source self-healed</b>\n"
                f"<b>{source_name}</b>: invite link expired, but userbot is already a member.\n"
                f"URL updated to <code>{dialog_id}</code>.",
                key=f"source_selfheal:{source_id}",
            )
            await _warn_if_duplicate_chat(source_id, source_name, dialog_id)
            return str(dialog_id)
        fails = await increment_source_fail_count(source_id)
        log.warning("Could not resolve invite link source id=%d (fail %d/%d): %s",
                    source_id, fails, _INVITE_FAIL_THRESHOLD, exc)
        if fails >= _INVITE_FAIL_THRESHOLD:
            await update_source_status(source_id, "error")
            await reset_source_fail_count(source_id)
            await admin_alert(
                f"⚠️ <b>Invite link dead</b>\n"
                f"<b>{source_name}</b> ({url})\n"
                f"Could not resolve after {fails} attempts and userbot is not a member.\n"
                f"Status set to <b>error</b>. Replace URL with a fresh invite link.",
                key=f"invite_dead:{source_id}",
            )
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
        elif message.video_note:
            media_prefix = "[Video note] "
        elif message.sticker:
            media_prefix = "[Sticker] "
        elif message.document:
            media_prefix = "[Doc] "
        elif message.audio:
            media_prefix = "[Audio] "
        elif message.voice:
            media_prefix = "[Voice] "
        elif getattr(message, "media", None) and not message.web_page:
            media_prefix = "[Media] "
        else:
            media_prefix = ""

        raw_text = (media_prefix + caption).strip()
        if not raw_text or raw_text in ("[Photo]", "[Video]", "[GIF]", "[Video note]", "[Sticker]", "[Doc]", "[Audio]", "[Voice]", "[Media]"):
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
    limit = _BOOTSTRAP_LIMIT if last_msg_id is None else _CATCHUP_LIMIT

    saved = 0
    try:
        messages = []
        async for message in userbot.get_chat_history(chat_id, limit=limit):
            if last_msg_id is not None and message.id <= last_msg_id:
                break
            messages.append(message)
        else:
            # Iterator hit the catch-up limit without reaching the last seen id:
            # the channel posted more than _CATCHUP_LIMIT since the previous poll,
            # so the overflow beyond it is not collected this cycle.
            if last_msg_id is not None and len(messages) >= _CATCHUP_LIMIT:
                log.warning("Source '%s' burst > %d messages since last poll (last_seen id=%s); "
                            "older overflow skipped this cycle", source["name"], _CATCHUP_LIMIT, last_msg_id)

        if messages and not source.get("chat_id"):
            resolved_chat_id = messages[0].chat.id if messages[0].chat else None
            if resolved_chat_id:
                await set_source_chat_id(source["id"], resolved_chat_id)
                source["chat_id"] = resolved_chat_id
                await _warn_if_duplicate_chat(source["id"], source["name"], resolved_chat_id)

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
    except (UsernameNotOccupied, UsernameInvalid) as exc:
        log.error("Source '%s' username gone: %s | marking error", source["name"], exc)
        await update_source_status(source["id"], "error")
        await admin_alert(
            f"⚠️ <b>Source username gone</b>\n"
            f"<b>{source['name']}</b> ({chat_ref}) — username no longer exists.\n"
            f"Status set to <b>error</b>. Check if the channel was renamed.\n"
            f"<i>{exc}</i>",
            key=f"source_username_gone:{source['id']}",
        )
    except (ChannelInvalid, ChannelPrivate, ChannelBanned, ChatForbidden, UserBannedInChannel, UserKicked) as exc:
        log.error("Source '%s' permanently inaccessible: %s | marking error", source["name"], exc)
        await update_source_status(source["id"], "error")
        await admin_alert(
            f"⚠️ <b>Source error</b>\n"
            f"<b>{source['name']}</b> ({chat_ref}) is no longer accessible.\n"
            f"Status set to <b>error</b>.\n"
            f"<i>{exc}</i>",
            key=f"source_inaccessible:{source['id']}",
        )
    except Exception as exc:
        log.error("Failed to poll %s: %s", chat_ref, exc)
    return saved


async def keep_userbot_online() -> None:
    """Ping Telegram every 4 min so the userbot account shows as online while collectors run."""
    from pyrogram.raw.functions.account import UpdateStatus
    log.info("Userbot online keepalive started (interval=240s)")
    while True:
        try:
            await userbot.invoke(UpdateStatus(offline=False))
            log.debug("Userbot online status refreshed")
        except Exception as exc:
            log.warning("Online keepalive ping failed: %s", exc)
        await asyncio.sleep(240)


async def poll_telegram_once() -> None:
    try:
        sources = await get_active_sources(type_="telegram")
        for row in sources:
            stored_chat_id = row_get(row, "chat_id")
            if stored_chat_id:
                chat_ref = str(stored_chat_id)
            else:
                chat_ref = row["url"].lower() if not row["url"].lstrip("-").isdigit() else row["url"]
            source = {
                "id": row["id"],
                "name": row["name"],
                "category": row["category"],
                "last_message_id": row_get(row, "last_message_id"),
                "chat_id": row_get(row, "chat_id"),
            }

            if _is_invite_link(chat_ref):
                chat_ref = await _resolve_invite_link(chat_ref, row["id"], row["name"])
                if not chat_ref:
                    continue

            saved = await _poll_channel(chat_ref, source)
            if saved:
                log.info("Telegram poll %s: %d new items", chat_ref, saved)
            else:
                log.debug("Telegram poll %s: 0 new items", chat_ref)
    except Exception as exc:
        log.exception("Telegram collector iteration failed: %s", exc)


async def run_telegram_collector() -> None:
    log.info("Telegram collector started (interval=%ds)", POLL_INTERVAL)
    while True:
        await poll_telegram_once()
        await asyncio.sleep(POLL_INTERVAL)
