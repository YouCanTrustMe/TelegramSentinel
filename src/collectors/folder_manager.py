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
            # Pinning a chat inside the folder moves it to pinned_peers, so stripping only
            # include_peers left a removed source in the folder for good — and the audit
            # then reports it as a leftover membership forever.
            folder.include_peers = [p for p in folder.include_peers if getattr(p, "channel_id", None) != channel_id]
            if getattr(folder, "pinned_peers", None):
                folder.pinned_peers = [p for p in folder.pinned_peers if getattr(p, "channel_id", None) != channel_id]
        await userbot.invoke(raw.functions.messages.UpdateDialogFilter(id=folder.id, filter=folder))
        log.info("Removed %s from folder '%s'", ref, folder_title)
    except Exception as exc:
        log.warning("Could not remove %s from folder '%s': %s", ref, folder_title, exc)


def _raw_channel_id(chat_id: int | None) -> int | None:
    """Pyrogram exposes a channel as -100<raw id>; the folder stores the raw id."""
    if chat_id is None:
        return None
    value = abs(int(chat_id))
    return value - 1000000000000 if value > 1000000000000 else value


def audit_folder(folder_channel_ids: set[int], sources: list) -> dict:
    """Compare the Sentinel folder against the telegram sources we track.

    A source removed while its channel was renamed used to leave the userbot a member
    of a chat nobody tracks (USERNAME_NOT_OCCUPIED made the leave a no-op), and there
    is no way to see that from outside the running process. Returns the two mismatches
    plus the sources that cannot be checked at all because no chat_id was ever stored.
    Pure so the comparison is testable without Telegram."""
    # A channel can legitimately back two sources (the collector warns about it rather
    # than forbidding it), so this maps to a LIST: keying by source would hide one of
    # them from the "not in folder" list and undercount the tracked total.
    tracked: dict[int, list[dict]] = {}
    unknown: list[dict] = []
    for source in sources:
        raw_id = _raw_channel_id(source.get("chat_id"))
        if raw_id is None:
            unknown.append(source)
        else:
            tracked.setdefault(raw_id, []).append(source)
    missing = [s for i in sorted(set(tracked) - folder_channel_ids) for s in tracked[i]]
    return {
        "stale": sorted(folder_channel_ids - set(tracked)),
        "missing": missing,
        "unknown": unknown,
        "in_folder": len(folder_channel_ids),
        "tracked": sum(len(v) for v in tracked.values()),
    }


async def folder_channel_ids(folder_title: str = SENTINEL_FOLDER) -> set[int] | None:
    """Raw channel ids the folder holds, or None when the folder does not exist.

    Pinning a chat inside the folder moves it from include_peers to pinned_peers, so
    reading only the former reports a pinned source as missing from its own folder."""
    folder = await _get_folder(folder_title)
    if folder is None:
        return None
    peers = list(folder.include_peers) + list(getattr(folder, "pinned_peers", None) or [])
    return {p.channel_id for p in peers if hasattr(p, "channel_id")}
