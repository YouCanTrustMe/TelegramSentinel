"""The source screen exists to answer one question — is this source still worth
keeping — so freshness, weekly volume and the open link are what is checked here."""
import src.bot.keyboards as keyboards
from src.bot.keyboards import _source_view_keyboard, _source_view_text
from src.common.util import ago, source_link


def _src(**over):
    src = {"id": 1, "name": "Financial Times", "type": "rss", "status": "active",
           "category": "feed", "url": "https://www.ft.com/companies?format=rss",
           "prompt_extra": None}
    src.update(over)
    return src


def _health(hours=0.6, total=28, muted=3):
    return {"hours_since": hours, "week_total": total, "week_muted": muted}


def test_ago_picks_one_unit():
    assert ago(0.6) == "36m ago"
    assert ago(6) == "6h ago"
    assert ago(150) == "6d ago"
    assert ago(None) == "never"
    assert ago(0.001) == "1m ago"  # never "0m ago"


def test_source_link_handles_each_shape():
    assert source_link("telegram", "@lachen") == "https://t.me/lachen"
    assert source_link("telegram", "-1001234567") is None
    assert source_link("telegram", "https://t.me/+abc") == "https://t.me/+abc"
    assert source_link("rss", "https://decrypt.co/feed") == "https://decrypt.co/feed"
    assert source_link("rss", "") is None


def test_source_text_leads_with_freshness_and_volume():
    text = _source_view_text(_src(), _health())
    assert "Last item <b>36m ago</b> · <b>28</b> in 7d · 3 muted as dupes" in text
    assert "rss · feed" in text


def test_source_text_flags_a_silent_source():
    text = _source_view_text(_src(), _health(hours=150, total=0, muted=0))
    assert "💤 silent 6d ago" in text  # ⏸ now means paused-by-hand, not silent
    assert "muted" not in text


def test_source_text_says_pending_instead_of_faking_freshness():
    text = _source_view_text(_src(status="pending"), _health(hours=None, total=0, muted=0))
    assert "⏳ Pending" in text
    assert "in 7d" not in text


def test_source_text_shows_the_handle_only_when_it_cannot_be_linked():
    linkable = _source_view_text(_src(type="telegram", url="@lachen"), _health())
    assert "<code>" not in linkable
    private = _source_view_text(_src(type="telegram", url="-1001234567"), _health())
    assert "<code>-1001234567</code>" in private


def test_source_keyboard_opens_the_source():
    kb = _source_view_keyboard(1, "feed", "https://www.ft.com/companies")
    open_btn = kb.inline_keyboard[0][0]
    assert open_btn.url == "https://www.ft.com/companies"
    assert open_btn.callback_data is None


def test_source_keyboard_drops_the_open_button_when_there_is_no_link():
    kb = _source_view_keyboard(1, "feed", None)
    assert all(b.url is None for row in kb.inline_keyboard for b in row)
    assert kb.inline_keyboard[0][0].callback_data == "src_prompt:1"


def test_paused_source_screen_says_what_pause_means_and_offers_resume():
    """Pause is the reversible half of Remove: the source, its history and the
    membership stay, only the pipeline stops."""
    source = {"name": "CoinDesk", "type": "rss", "category": "crypto", "status": "paused",
              "url": "https://coindesk.com/feed", "prompt_extra": None}

    text = keyboards._source_view_text(source, {"hours_since": 3, "week_total": 40, "week_muted": 5})
    kb = keyboards._source_view_keyboard(7, "crypto", "https://coindesk.com/feed", paused=True)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    actions = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]

    assert text.startswith("<b>⏸ CoinDesk</b>")
    assert "Paused" in text
    assert "▶️ Resume" in labels and "⏸ Pause" not in labels
    assert "src_resume:7" in actions


def test_active_source_screen_offers_pause():
    kb = keyboards._source_view_keyboard(7, "crypto", None, paused=False)
    labels = [b.text for row in kb.inline_keyboard for b in row]
    actions = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]

    assert "⏸ Pause" in labels and "▶️ Resume" not in labels
    assert "src_pause:7" in actions
    assert "src_del:7" in actions          # pause did not displace remove


def test_paused_source_row_carries_the_pause_icon():
    assert keyboards._source_label(
        {"name": "CoinDesk", "type": "rss", "status": "paused"}).startswith("⏸")


def test_paused_sources_stop_feeding_the_digest_immediately():
    """Pause has to silence the queue too, not only the collector — otherwise the
    already-collected items keep arriving in the next digest."""
    import inspect
    from src.db import items

    sql = inspect.getsource(items.get_unsent_items)
    # COALESCE, not a bare compare: an orphan item (LEFT JOIN found no source) has a
    # NULL status, and `NULL != 'paused'` is NULL, which would drop it for good.
    assert sql.count("COALESCE(sources.status, '') != 'paused'") == 2


async def test_pausing_retires_the_queue_so_resume_does_not_ship_stale_news(tmp_path, monkeypatch):
    """Pause has to silence the source NOW. A queue left behind is stranded — nothing
    classifies it, home counts it as waiting forever, retention only prunes sent rows —
    and resuming would then ship a stale backlog, the opposite of what resume promises."""
    import src.db.base as base
    from src.db.base import init_db
    from src.db.models import add_source, discard_unsent_items, save_item, get_unsent_items

    monkeypatch.setattr(base.settings, "database_path", str(tmp_path / "t.db"))
    await init_db()
    sid = await add_source("rss", "CoinDesk", "https://coindesk.com/feed", "crypto")
    for n in range(3):
        await save_item(source_id=sid, message_id=f"m{n}", raw_text="t", original_url=None,
                        published_at=None, summary="s", category="crypto", processed_at="2026-09-04")

    assert len(await get_unsent_items()) == 3
    assert await discard_unsent_items(sid) == 3
    assert await get_unsent_items() == []


async def test_an_item_whose_source_is_gone_still_reaches_the_digest(tmp_path, monkeypatch):
    """The LEFT JOIN is deliberately tolerant of an orphan item; the paused filter must
    not quietly turn that tolerance into a permanent drop."""
    import src.db.base as base
    from src.db.base import init_db
    from src.db.models import get_unsent_items, save_item

    monkeypatch.setattr(base.settings, "database_path", str(tmp_path / "t.db"))
    await init_db()
    async with base.get_db() as db:
        await db.execute("PRAGMA foreign_keys = OFF")
        await db.execute(
            "INSERT INTO items (source_id, message_id, raw_text, summary, category, sent)"
            " VALUES (999, 'orphan', 'raw', 'summary', 'feed', 0)")
        await db.commit()

    assert [row["message_id"] for row in await get_unsent_items()] == ["orphan"]


async def test_a_paused_source_stays_reachable_from_its_category(monkeypatch):
    """Resume lives on the source screen, and the only way there is the category list —
    so a paused source that is not listed is a one-way door."""
    from src.bot import keyboards

    async def _none(*a, **kw):
        return []

    async def _paused(*a, **kw):
        return [{"id": 7, "name": "CoinDesk", "type": "rss", "status": "paused",
                 "category": "crypto", "sort_order": 0}]

    for name in ("get_active_sources", "get_pending_sources", "get_error_sources", "get_silent_sources"):
        monkeypatch.setattr(keyboards, name, _none)
    monkeypatch.setattr(keyboards, "get_paused_sources", _paused)
    monkeypatch.setattr(keyboards, "get_categories",
                        lambda: _cats_stub())

    text, rows = await keyboards._cat_view_text("crypto")

    assert any(r["id"] == 7 for r in rows)


async def _cats_stub():
    return [{"name": "crypto", "emoji": "💰", "digest_time": "19:00"}]


def test_a_pending_source_is_not_offered_pause():
    """Pending means the userbot has not joined yet: pausing would drop it out of the
    activation sweep, and resuming would mark it active with nobody ever having joined."""
    kb = keyboards._source_view_keyboard(7, "crypto", None, paused=False, pending=True)
    actions = [b.callback_data for row in kb.inline_keyboard for b in row if b.callback_data]

    assert not any(a.startswith("src_pause") or a.startswith("src_resume") for a in actions)
    assert "src_del:7" in actions       # it can still be removed


async def test_resuming_an_rss_source_retires_what_it_published_during_the_pause(tmp_path, monkeypatch):
    """RSS has no bookmark, so "not back-filled" has to mean storing the current entries
    and retiring them — otherwise a week's pause dumps a week of news on resume."""
    import src.db.base as base
    from src.db.base import init_db
    from src.db.models import add_source, discard_unsent_items, get_unsent_items
    import src.collectors.rss_collector as rss

    monkeypatch.setattr(base.settings, "database_path", str(tmp_path / "t.db"))
    await init_db()
    sid = await add_source("rss", "CoinDesk", "https://coindesk.com/feed", "crypto")

    class _Feed:
        entries = [{"link": f"https://coindesk.com/{n}", "title": f"Story {n}", "summary": "body"}
                   for n in range(4)]

    async def _parse(url, name):
        return _Feed()

    monkeypatch.setattr(rss, "_parse_with_ua_fallback", _parse)
    stored = await rss.skip_feed_to_head(sid, "CoinDesk", "https://coindesk.com/feed", "crypto")
    await discard_unsent_items(sid)

    assert stored == 4
    assert await get_unsent_items() == []      # seen, never shown


async def test_a_retired_item_cannot_become_a_dedup_primary(tmp_path, monkeypatch):
    """Retiring the queue with `sent = 1` alone would enrol those rows in the
    cross-digest comparison pool: a real story could then be muted as a duplicate of an
    item nobody ever saw, and a mute against the sent pool renders no link at all."""
    import src.db.base as base
    from src.db.base import init_db
    from src.db.models import (add_source, discard_unsent_items, get_recent_embedded_items,
                               save_item, set_item_embeddings)
    from src.processor.dedup.embedder import to_blob
    import numpy as np

    monkeypatch.setattr(base.settings, "database_path", str(tmp_path / "t.db"))
    await init_db()
    sid = await add_source("rss", "CoinDesk", "https://coindesk.com/feed", "crypto")
    iid = await save_item(source_id=sid, message_id="m1", raw_text="t", original_url=None,
                          published_at=None, summary="A real story", category="crypto",
                          processed_at="2026-09-04T10:00:00+00:00")
    await set_item_embeddings([(iid, to_blob(np.ones(1024, dtype=np.float32)))])
    assert len(await get_recent_embedded_items(48)) == 1

    await discard_unsent_items(sid)

    assert await get_recent_embedded_items(48) == []


async def test_a_retired_item_is_not_resurrected_by_the_empty_summary_backfill(tmp_path, monkeypatch):
    """`sent = 1` with an empty summary is exactly what the classifier's backfill hunts
    for. Re-summarising an item we deliberately never showed would re-embed it and hand
    it back to the dedup comparison pool — the very thing retiring it prevents."""
    import src.db.base as base
    from src.db.base import init_db
    from src.db.models import add_source, discard_unsent_items, get_sent_empty_items, save_item

    monkeypatch.setattr(base.settings, "database_path", str(tmp_path / "t.db"))
    await init_db()
    sid = await add_source("rss", "CoinDesk", "https://coindesk.com/feed", "crypto")
    await save_item(source_id=sid, message_id="m1", raw_text="A story nobody will see",
                    original_url=None, published_at=None, summary="", category="crypto",
                    processed_at="2026-09-04")

    await discard_unsent_items(sid)

    assert await get_sent_empty_items(10) == []
