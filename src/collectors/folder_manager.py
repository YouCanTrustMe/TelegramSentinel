import logging

from pyrogram import raw

from src.collectors.telegram_collector import userbot

log = logging.getLogger(__name__)

FOLDER_TITLE = "Sentinel"


async def _get_folder() -> raw.types.DialogFilter | None:
    result = await userbot.invoke(raw.functions.messages.GetDialogFilters())
    filters = result.filters if hasattr(result, "filters") else result
    for f in filters:
        if isinstance(f, raw.types.DialogFilter) and f.title == FOLDER_TITLE:
            return f
    return None


async def _next_folder_id() -> int:
    result = await userbot.invoke(raw.functions.messages.GetDialogFilters())
    filters = result.filters if hasattr(result, "filters") else result
    ids = [f.id for f in filters if isinstance(f, (raw.types.DialogFilter, raw.types.DialogFilterChatlist))]
    return max(ids, default=1) + 1


async def add_to_folder(username: str) -> None:
    try:
        peer = await userbot.resolve_peer(username)
        folder = await _get_folder()
        if folder is None:
            folder_id = await _next_folder_id()
            folder = raw.types.DialogFilter(
                id=folder_id,
                title=FOLDER_TITLE,
                pinned_peers=[],
                include_peers=[peer],
                exclude_peers=[],
            )
            log.info("Created Telegram folder '%s' (id=%d)", FOLDER_TITLE, folder_id)
        else:
            existing_ids = {p.channel_id for p in folder.include_peers if hasattr(p, "channel_id")}
            if hasattr(peer, "channel_id") and peer.channel_id in existing_ids:
                return
            folder.include_peers.append(peer)

        await userbot.invoke(raw.functions.messages.UpdateDialogFilter(id=folder.id, filter=folder))
        log.info("Added @%s to folder '%s'", username, FOLDER_TITLE)
    except Exception as exc:
        log.warning("Could not add @%s to folder: %s", username, exc)


async def remove_from_folder(username: str) -> None:
    try:
        peer = await userbot.resolve_peer(username)
        folder = await _get_folder()
        if folder is None:
            return
        channel_id = getattr(peer, "channel_id", None)
        if channel_id:
            folder.include_peers = [p for p in folder.include_peers if getattr(p, "channel_id", None) != channel_id]
        await userbot.invoke(raw.functions.messages.UpdateDialogFilter(id=folder.id, filter=folder))
        log.info("Removed @%s from folder '%s'", username, FOLDER_TITLE)
    except Exception as exc:
        log.warning("Could not remove @%s from folder: %s", username, exc)
