import logging

from pyrogram import raw

from src.collectors.telegram_collector import userbot

log = logging.getLogger(__name__)

SENTINEL_FOLDER = "Sentinel"


async def _get_folder(title: str) -> raw.types.DialogFilter | None:
    result = await userbot.invoke(raw.functions.messages.GetDialogFilters())
    filters = result.filters if hasattr(result, "filters") else result
    for f in filters:
        if isinstance(f, raw.types.DialogFilter) and f.title == title:
            return f
    return None


async def _next_folder_id() -> int:
    result = await userbot.invoke(raw.functions.messages.GetDialogFilters())
    filters = result.filters if hasattr(result, "filters") else result
    ids = [f.id for f in filters if isinstance(f, (raw.types.DialogFilter, raw.types.DialogFilterChatlist))]
    return max(ids, default=1) + 1


async def add_to_folder(username: str, folder_title: str = SENTINEL_FOLDER) -> None:
    try:
        peer = await userbot.resolve_peer(username)
        folder = await _get_folder(folder_title)
        if folder is None:
            folder_id = await _next_folder_id()
            folder = raw.types.DialogFilter(
                id=folder_id,
                title=folder_title,
                pinned_peers=[],
                include_peers=[peer],
                exclude_peers=[],
            )
            log.info("Created Telegram folder '%s' (id=%d)", folder_title, folder_id)
        else:
            existing_ids = {p.channel_id for p in folder.include_peers if hasattr(p, "channel_id")}
            if hasattr(peer, "channel_id") and peer.channel_id in existing_ids:
                return
            folder.include_peers.append(peer)

        await userbot.invoke(raw.functions.messages.UpdateDialogFilter(id=folder.id, filter=folder))
        log.info("Added @%s to folder '%s'", username, folder_title)
    except Exception as exc:
        log.warning("Could not add @%s to folder '%s': %s", username, folder_title, exc)


async def remove_from_folder(ref: str | int, folder_title: str = SENTINEL_FOLDER) -> None:
    # `ref` may be a username or a numeric chat_id; a renamed channel's username no longer
    # resolves (USERNAME_NOT_OCCUPIED), so callers pass the chat_id where they have it.
    try:
        peer = await userbot.resolve_peer(ref)
        folder = await _get_folder(folder_title)
        if folder is None:
            return
        channel_id = getattr(peer, "channel_id", None)
        if channel_id:
            folder.include_peers = [p for p in folder.include_peers if getattr(p, "channel_id", None) != channel_id]
        await userbot.invoke(raw.functions.messages.UpdateDialogFilter(id=folder.id, filter=folder))
        log.info("Removed %s from folder '%s'", ref, folder_title)
    except Exception as exc:
        log.warning("Could not remove %s from folder '%s': %s", ref, folder_title, exc)
