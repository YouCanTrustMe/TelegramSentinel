"""A channel deletes a post after it was collected: the digest line keeps it, marked 🗑."""
from types import SimpleNamespace

from src.collectors import telegram_collector as tc
from src.dispatcher import digest_builder as db


def test_post_links_resolve_to_a_peer_and_message():
    assert tc._post_ref("https://t.me/c/1536630827/69385") == (-1001536630827, 69385)
    assert tc._post_ref("https://t.me/lachentyt/69385") == ("lachentyt", 69385)
    assert tc._post_ref("https://www.wsj.com/a/1") is None


async def test_only_posts_the_channel_reports_empty_are_gone(monkeypatch):
    asked = []

    async def fake_get(peer, ids):
        asked.append((peer, ids))
        if peer == "broken":
            raise RuntimeError("CHANNEL_PRIVATE")
        return [SimpleNamespace(id=i, empty=(i == 2)) for i in ids]

    monkeypatch.setattr(tc, "userbot", SimpleNamespace(get_messages=fake_get))
    gone = await tc.deleted_posts(["https://t.me/a/1", "https://t.me/a/2", "https://t.me/broken/5"])
    assert gone == {"https://t.me/a/2"}
    assert ("a", [1, 2]) in asked


def test_a_deleted_line_carries_the_bin_before_its_text():
    line = {"id": 1, "summary": "Апеляція у справі", "raw_text": "", "key_phrase": "",
            "published_at": "", "original_url": "https://t.me/a/2", "_deleted": True}
    assert "🗑 " in db._format_item_base(line)
    assert "🗑" not in db._format_item_base({**line, "_deleted": False})


async def test_mark_deleted_flags_lines_in_source_and_war_blocks(monkeypatch):
    async def fake_gone(urls):
        return {"https://t.me/a/2"}

    monkeypatch.setattr(tc, "deleted_posts", fake_gone)
    kept, gone = {"original_url": "https://t.me/a/1"}, {"original_url": "https://t.me/a/2"}
    meta = {"feed": {"sources": {"A": [kept, gone]}, "war_block": {"items": [dict(gone)]}}}
    await db._mark_deleted(meta)
    assert [db.row_get(x, "_deleted") for x in meta["feed"]["sources"]["A"]] == [None, True]
    assert meta["feed"]["war_block"]["items"][0]["_deleted"]


async def test_a_hanging_telegram_never_holds_the_digest(monkeypatch):
    import asyncio

    async def hang(peer, ids):
        await asyncio.sleep(60)

    monkeypatch.setattr(tc, "userbot", SimpleNamespace(get_messages=hang))
    monkeypatch.setattr(tc, "_DELETED_CHECK_BUDGET", 0.05)
    assert await tc.deleted_posts(["https://t.me/a/1"]) == set()


async def test_a_channel_whose_every_post_reads_empty_marks_none(monkeypatch):
    """Renamed channel: the old username now answers for someone else's chat, every id empty."""
    async def fake_get(peer, ids):
        return [SimpleNamespace(id=i, empty=True) for i in ids]

    monkeypatch.setattr(tc, "userbot", SimpleNamespace(get_messages=fake_get))
    assert await tc.deleted_posts(["https://t.me/a/1", "https://t.me/a/2"]) == set()
