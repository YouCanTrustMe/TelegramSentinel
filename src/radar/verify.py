import logging
from html import escape

from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import (
    ChannelInvalid,
    ChannelPrivate,
    PeerIdInvalid,
    UserNotParticipant,
    UsernameInvalid,
    UsernameNotOccupied,
)

from src.collectors.telegram_collector import userbot
from src.db.models import get_radar_chats, update_radar_chat_resolved, update_radar_chat_status
from src.dispatcher.admin_alert import admin_alert

log = logging.getLogger(__name__)


async def verify_radar_chats() -> None:
    chats = await get_radar_chats()
    log.info("Radar verify: checking %d chat(s)", len(chats))
    for row in chats:
        entry_id = row["id"]
        ref = row["chat_ref"]
        keys = row.keys()
        stored_id = row["chat_id"] if "chat_id" in keys else None

        probe = stored_id if stored_id is not None else (ref if ref.startswith("@") else _maybe_int(ref))
        try:
            chat = await userbot.get_chat(probe)
        except (UsernameNotOccupied, UsernameInvalid) as exc:
            log.warning("Radar verify: username gone for entry id=%d ref=%s: %s", entry_id, ref, exc)
            await update_radar_chat_status(entry_id, "error")
            await admin_alert(
                f"⚠️ <b>Radar chat unreachable</b>\n"
                f"<code>{escape(ref)}</code> — username no longer exists.\n"
                f"<i>{escape(str(exc))}</i>",
                key=f"radar_chat_gone:{entry_id}",
            )
            continue
        except (ChannelInvalid, ChannelPrivate, PeerIdInvalid) as exc:
            log.warning("Radar verify: inaccessible entry id=%d ref=%s: %s", entry_id, ref, exc)
            await update_radar_chat_status(entry_id, "error")
            await admin_alert(
                f"⚠️ <b>Radar chat inaccessible</b>\n"
                f"<code>{escape(ref)}</code>\n"
                f"<i>{escape(str(exc))}</i>",
                key=f"radar_chat_inacc:{entry_id}",
            )
            continue
        except Exception as exc:
            log.warning("Radar verify: error for entry id=%d ref=%s: %s", entry_id, ref, exc)
            continue

        new_ref = f"@{chat.username}" if chat.username else str(chat.id)
        new_title = chat.title or chat.first_name or None

        try:
            me = await userbot.get_me()
            member = await userbot.get_chat_member(chat.id, me.id)
            bad_statuses = {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED, ChatMemberStatus.RESTRICTED}
            if member.status in bad_statuses:
                log.warning("Radar verify: not a member id=%d ref=%s status=%s", entry_id, new_ref, member.status)
                await update_radar_chat_status(entry_id, "error")
                await admin_alert(
                    f"⚠️ <b>Radar chat: membership lost</b>\n"
                    f"<code>{escape(new_ref)}</code> — status <code>{escape(str(member.status))}</code>.",
                    key=f"radar_chat_left:{entry_id}",
                )
                continue
        except UserNotParticipant:
            log.warning("Radar verify: not participant id=%d ref=%s (pending invite?)", entry_id, new_ref)
            await update_radar_chat_status(entry_id, "error")
            await admin_alert(
                f"⚠️ <b>Radar chat: not a participant</b>\n"
                f"<code>{escape(new_ref)}</code> — join request likely still pending approval.",
                key=f"radar_chat_pending:{entry_id}",
            )
            continue
        except Exception as exc:
            log.warning("Radar verify: membership probe failed id=%d ref=%s: %s", entry_id, new_ref, exc)

        if new_ref != ref or stored_id != chat.id:
            log.info(
                "Radar verify: healed entry id=%d | old_ref=%s new_ref=%s old_id=%s new_id=%s",
                entry_id, ref, new_ref, stored_id, chat.id,
            )
            await admin_alert(
                f"ℹ️ <b>Radar chat updated</b>\n"
                f"<code>{escape(ref)}</code> → <code>{escape(new_ref)}</code>",
                key=f"radar_chat_updated:{entry_id}",
            )
        await update_radar_chat_resolved(entry_id, chat.id, new_ref, new_title)
    log.info("Radar verify: done")


def _maybe_int(s: str) -> int | str:
    try:
        return int(s)
    except (ValueError, TypeError):
        return s
