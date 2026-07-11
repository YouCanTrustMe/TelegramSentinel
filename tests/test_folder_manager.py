"""Folder maintenance resolves a source by whatever ref it is given. Deletion passes
the stored numeric chat_id (not the username) so a renamed channel — whose old
username no longer resolves — is still removed from the Sentinel folder."""
from pyrogram import raw

import src.collectors.folder_manager as fm


async def test_remove_from_folder_resolves_by_numeric_ref(monkeypatch):
    seen = {}
    captured = {}
    peer = raw.types.InputPeerChannel(channel_id=555, access_hash=0)
    folder = raw.types.DialogFilter(
        id=2, title=fm.SENTINEL_FOLDER, pinned_peers=[], include_peers=[peer], exclude_peers=[]
    )

    class FakeUserbot:
        async def resolve_peer(self, ref):
            seen["ref"] = ref
            return raw.types.InputPeerChannel(channel_id=555, access_hash=0)

        async def invoke(self, query):
            if isinstance(query, raw.functions.messages.GetDialogFilters):
                return [folder]
            if isinstance(query, raw.functions.messages.UpdateDialogFilter):
                captured["filter"] = query.filter
            return None

    monkeypatch.setattr(fm, "userbot", FakeUserbot())

    await fm.remove_from_folder(-1005550000000)

    assert seen["ref"] == -1005550000000          # resolved by numeric id, never a username
    assert captured["filter"].include_peers == []  # matching peer stripped from the folder
