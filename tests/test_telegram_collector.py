"""_process_message: a post whose media our pinned Pyrogram is too old to decode
(MessageMediaUnsupported — high-level Message exposes nothing) must still be kept
as a 📦 placeholder with a link, while genuinely empty / service messages are
dropped as before."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import src.collectors.telegram_collector as tc
from src.media import GENERIC_MEDIA_TOKEN, NO_TEXT

CHAT = "-1002568789348"
SOURCE = {"id": 20, "category": "hrvatska"}


def _msg(**overrides):
    """A pyrogram-Message-shaped stub with everything the collector reads, all
    falsy by default; override per case."""
    base = dict(
        id=239,
        date=datetime(2026, 6, 21, 6, 43, 45),
        poll=None,
        text=None,
        caption=None,
        media=None,
        web_page=None,
        service=None,
        empty=None,
        forward_from_chat=None,
        reply_to_message_id=None,
        media_group_id=None,
    )
    for _attr, _token, _emoji in tc.MEDIA_TYPES:
        base[_attr] = None
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def captured(monkeypatch):
    saved = {}

    async def fake_is_duplicate(_mid):
        return False

    async def fake_save_item(**kwargs):
        saved.update(kwargs)

    monkeypatch.setattr(tc, "is_duplicate", fake_is_duplicate)
    monkeypatch.setattr(tc, "save_item", fake_save_item)
    return saved


async def test_unsupported_media_post_kept_as_placeholder(captured):
    # All content None (MessageMediaUnsupported looks empty up here) but not a
    # service/empty message → keep as 📦 placeholder, never drop.
    kept = await tc._process_message(CHAT, SOURCE, _msg())
    assert kept is True
    assert captured["raw_text"] == GENERIC_MEDIA_TOKEN
    assert captured["summary"] == NO_TEXT
    assert captured["original_url"].endswith("/239")


async def test_service_message_dropped(captured):
    kept = await tc._process_message(CHAT, SOURCE, _msg(service="NEW_CHAT_MEMBERS"))
    assert kept is False
    assert captured == {}


async def test_empty_message_dropped(captured):
    kept = await tc._process_message(CHAT, SOURCE, _msg(empty=True))
    assert kept is False
    assert captured == {}


async def test_plain_text_post_still_saved(captured):
    kept = await tc._process_message(CHAT, SOURCE, _msg(text="Нарешті літо в Хорватії"))
    assert kept is True
    assert captured["raw_text"] == "Нарешті літо в Хорватії"
    assert captured["summary"] == ""  # long enough to need classification later
