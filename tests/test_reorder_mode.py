"""Reordering is a mode, not a permanent pair of columns on every row. The parse
side matters as much as the render side: a callback the handler regexes miss is a
dead button, and that is exactly how two bugs shipped before."""
import re

import pytest

from src.bot.keyboards import _categories_keyboard, _category_view_keyboard

_CAT_LIST_RE = re.compile(r"^cat_list(:\d+)?$")
_CAT_REORDER_RE = re.compile(r"^cat_reorder(:\d+)?$")
_SRC_REORDER_RE = re.compile(r"^src_reorder:")


def _cats():
    return [
        {"name": "world", "emoji": "🌍", "digest_time": "09:00"},
        {"name": "crypto", "emoji": "💰", "digest_time": "19:00"},
        {"name": "tech", "emoji": "🖥", "digest_time": "19:00"},
    ]


def _sources(n=3):
    return [{"id": i, "name": f"src{i}", "type": "rss", "status": "active"} for i in range(n)]


def _flat(kb):
    return [b for row in kb.inline_keyboard for b in row]


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    from src.bot import keyboards
    async def _none(*a, **kw):
        return []
    monkeypatch.setattr(keyboards, "get_active_sources", _none)
    monkeypatch.setattr(keyboards, "get_pending_sources", _none)
    monkeypatch.setattr(keyboards, "get_silent_sources", _none)
    monkeypatch.setattr(keyboards, "get_error_sources", _none)
    monkeypatch.setattr(keyboards, "get_paused_sources", _none)


@pytest.mark.asyncio
async def test_normal_category_rows_are_full_width():
    kb = await _categories_keyboard(_cats())
    rows = [r for r in kb.inline_keyboard if any(b.callback_data.startswith("cat_view:") for b in r)]
    assert len(rows) == 3
    assert all(len(r) == 1 for r in rows)
    # No arrows, and no blank placeholder buttons, outside the mode.
    assert not any(b.text in ("↑", "↓", " ") for b in _flat(kb))


@pytest.mark.asyncio
async def test_reorder_mode_grows_arrow_columns():
    kb = await _categories_keyboard(_cats(), reorder=True)
    ups = [b for b in _flat(kb) if b.callback_data.startswith("cat_order_up:")]
    downs = [b for b in _flat(kb) if b.callback_data.startswith("cat_order_down:")]
    # The first row cannot move up, the last cannot move down.
    assert len(ups) == 2 and len(downs) == 2


@pytest.mark.asyncio
async def test_reorder_mode_does_not_open_a_category_by_mistake():
    kb = await _categories_keyboard(_cats(), reorder=True)
    assert not any(b.callback_data.startswith("cat_view:") for b in _flat(kb))


@pytest.mark.asyncio
async def test_both_modes_are_reachable_from_each_other():
    normal = _flat(await _categories_keyboard(_cats()))
    enter = next(b for b in normal if b.text == "⇅ Reorder")
    assert _CAT_REORDER_RE.match(enter.callback_data)
    done = next(b for b in _flat(await _categories_keyboard(_cats(), reorder=True)) if b.text == "✅ Done")
    assert _CAT_LIST_RE.match(done.callback_data)


@pytest.mark.asyncio
async def test_reorder_paging_stays_in_the_mode():
    kb = await _categories_keyboard([*_cats(), *_cats(), *_cats()], page=0, reorder=True)
    nav = [b for b in _flat(kb) if b.text in ("‹", "›")]
    assert nav and all(_CAT_REORDER_RE.match(b.callback_data) for b in nav)


def test_source_rows_follow_the_same_rule():
    normal = _flat(_category_view_keyboard("world", _sources()))
    assert not any(b.text in ("↑", "↓") for b in normal)
    enter = next(b for b in normal if b.text == "⇅ Reorder")
    assert _SRC_REORDER_RE.match(enter.callback_data)

    mode = _flat(_category_view_keyboard("world", _sources(), reorder=True))
    assert any(b.callback_data.startswith("src_order_up:") for b in mode)
    assert not any(b.callback_data.startswith("src_view:") for b in mode)


def test_source_reorder_done_returns_to_the_same_page():
    kb = _category_view_keyboard("world", _sources(20), page=1, reorder=True)
    done = next(b for b in _flat(kb) if b.text == "✅ Done")
    assert done.callback_data == "cat_view:world:1"


@pytest.mark.asyncio
async def test_reorder_keeps_the_page_it_was_used_on():
    # Without the page in the payload the list jumped back to page 1 after a move,
    # with the arrows then bound to different rows.
    kb = await _categories_keyboard(_cats() * 4, page=1, reorder=True)
    moves = [b.callback_data for b in _flat(kb) if b.callback_data.startswith("cat_order_")]
    assert moves and all(d.split(":")[1] == "1" for d in moves)


def test_source_reorder_carries_the_page_too():
    kb = _category_view_keyboard("world", _sources(20), page=1, reorder=True)
    moves = [b.callback_data for b in _flat(kb) if b.callback_data.startswith("src_order_")]
    assert moves and all(d.split(":")[2] == "1" for d in moves)


def test_a_category_name_with_a_colon_still_parses():
    from src.bot.keyboards import split_name_page
    assert split_name_page("a:b") == ("a:b", 0)
    assert split_name_page("a:b:2") == ("a:b", 2)
    assert split_name_page("world") == ("world", 0)
    assert split_name_page("world:3") == ("world", 3)


def test_reorder_payload_keeps_the_name_last_so_a_colon_cannot_shift_the_page():
    from src.bot.keyboards import _category_view_keyboard as kb_fn
    kb = kb_fn("a:b", _sources(3), page=0, reorder=True)
    up = next(b.callback_data for b in _flat(kb) if b.callback_data.startswith("src_order_up:"))
    action, src_id, page, cat = up.split(":", 3)
    assert (src_id, page, cat) == ("1", "0", "a:b")


@pytest.mark.asyncio
async def test_a_move_across_a_page_boundary_follows_the_row():
    from src.bot.keyboards import _PAGE_SIZE_CATS, page_of
    order = [{"name": f"c{i}"} for i in range(20)]
    assert page_of(order, "name", "c0", _PAGE_SIZE_CATS) == 0
    assert page_of(order, "name", "c8", _PAGE_SIZE_CATS) == 1
    assert page_of(order, "name", "missing", _PAGE_SIZE_CATS) == 0
