"""The source screen exists to answer one question — is this source still worth
keeping — so freshness, weekly volume and the open link are what is checked here."""
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
    assert "⏸ silent 6d ago" in text
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
