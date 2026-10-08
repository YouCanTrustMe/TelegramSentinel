"""Folder maintenance resolves a source by whatever ref it is given. Deletion passes
the stored numeric chat_id (not the username) so a renamed channel — whose old
username no longer resolves — is still removed from the Sentinel folder."""
from pyrogram import raw

import src.collectors.folder_manager as fm
from src.common.util import plain_text


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


def test_audit_folder_reports_both_directions():
    """The leftover that matters: a folder entry whose source is gone means the
    userbot may still sit in a channel nobody tracks."""
    from src.collectors.folder_manager import audit_folder

    sources = [
        {"id": 1, "name": "Бабель", "chat_id": -1001234567890, "status": "active"},
        {"id": 2, "name": "Лачен", "chat_id": -1009876543210, "status": "active"},
        {"id": 3, "name": "Zagreb up to you", "chat_id": None, "status": "active"},
    ]
    report = audit_folder({1234567890, 555555555}, sources)

    assert report["stale"] == [555555555]                    # in folder, no source
    assert [s["name"] for s in report["missing"]] == ["Лачен"]
    assert [s["name"] for s in report["unknown"]] == ["Zagreb up to you"]
    assert (report["in_folder"], report["tracked"]) == (2, 2)


def test_audit_folder_is_clean_when_both_sides_agree():
    from src.collectors.folder_manager import audit_folder

    report = audit_folder({1234567890}, [{"id": 1, "name": "Бабель", "chat_id": -1001234567890, "status": "active"}])

    assert report["stale"] == [] and report["missing"] == [] and report["unknown"] == []


def test_raw_channel_id_strips_the_pyrogram_prefix():
    from src.collectors.folder_manager import _raw_channel_id

    assert _raw_channel_id(-1001232032465) == 1232032465
    assert _raw_channel_id(1232032465) == 1232032465   # already raw
    assert _raw_channel_id(None) is None


async def test_folder_channel_ids_counts_pinned_chats_too():
    """Pinning a chat inside the folder moves it out of include_peers; reading only
    that list reports a pinned source as missing from its own folder."""
    import src.collectors.folder_manager as fm

    class _Peer:
        def __init__(self, cid): self.channel_id = cid

    class _Folder:
        include_peers = [_Peer(111)]
        pinned_peers = [_Peer(222)]

    async def _folder(title):
        return _Folder()

    fm._get_folder = _folder
    assert await fm.folder_channel_ids() == {111, 222}


def test_audit_folder_keeps_both_sources_that_share_one_channel():
    """Two sources on one channel is a warned-about but supported state; keying the
    audit by channel would hide one of them and undercount the total."""
    from src.collectors.folder_manager import audit_folder

    sources = [
        {"id": 1, "name": "Бабель", "chat_id": -1001234567890, "status": "active"},
        {"id": 2, "name": "Бабель (dup)", "chat_id": -1001234567890, "status": "active"},
    ]
    report = audit_folder(set(), sources)

    assert [s["name"] for s in report["missing"]] == ["Бабель", "Бабель (dup)"]
    assert report["tracked"] == 2


async def test_remove_from_folder_strips_a_pinned_chat_too(monkeypatch):
    """A removed source that had been pinned inside the folder used to stay there, and
    the audit then flagged it as a leftover membership for good."""
    import src.collectors.folder_manager as fm

    class _Peer:
        def __init__(self, cid): self.channel_id = cid

    class _Folder:
        id = 5
        include_peers = [_Peer(111)]
        pinned_peers = [_Peer(222)]

    folder = _Folder()

    async def _get_folder(title): return folder
    async def _resolve(ref): return _Peer(222)
    async def _invoke(req): return None

    monkeypatch.setattr(fm, "_get_folder", _get_folder)
    monkeypatch.setattr(fm.userbot, "resolve_peer", _resolve, raising=False)
    monkeypatch.setattr(fm.userbot, "invoke", _invoke, raising=False)

    await fm.remove_from_folder(222)

    assert [p.channel_id for p in folder.pinned_peers] == []


def test_a_folder_title_is_found_in_either_library_shape():
    """pyrogram 2.0 (layer 158) sends the title as a string; newer forks wrap it in
    TextWithEntities, which never equals "Sentinel" when compared as-is."""
    from types import SimpleNamespace
    assert plain_text("Sentinel") == "Sentinel"
    assert plain_text(SimpleNamespace(text="Sentinel", entities=[])) == "Sentinel"


def test_a_new_folder_title_takes_the_shape_its_library_declares():
    class OldFilter:
        def __init__(self, *, title: str): ...

    class NewFilter:
        def __init__(self, *, title: "raw.base.TextWithEntities"): ...

    assert fm._folder_title("Sentinel", OldFilter) == "Sentinel"
    wrapped = fm._folder_title("Sentinel", NewFilter)
    assert isinstance(wrapped, raw.types.TextWithEntities) and wrapped.text == "Sentinel"


def test_a_new_folder_serialises_with_the_installed_library():
    """Whichever pyrogram is installed, the folder we build must write and read back."""
    from io import BytesIO
    folder = raw.types.DialogFilter(
        id=5, title=fm._folder_title("Sentinel"), pinned_peers=[],
        include_peers=[raw.types.InputPeerChannel(channel_id=1, access_hash=2)], exclude_peers=[])
    back = raw.types.DialogFilter.read(BytesIO(folder.write()[4:]))
    assert plain_text(back.title) == "Sentinel" and len(back.include_peers) == 1
