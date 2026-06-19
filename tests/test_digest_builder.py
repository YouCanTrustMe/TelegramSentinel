"""Digest assembly helpers: message splitting under the Telegram limit and the
quiet-source link builder."""
from src.dispatcher.digest_builder import _quiet_source_url, _split_into_messages


def test_split_keeps_small_segments_in_one_message():
    segments = [("a", [1]), ("b", [2]), ("c", [3])]
    messages = _split_into_messages(segments)
    assert messages == [("a\nb\nc", [1, 2, 3])]


def test_split_breaks_when_over_limit():
    segments = [("x" * 3000, [1]), ("y" * 2000, [2])]
    messages = _split_into_messages(segments)
    assert len(messages) == 2
    assert messages[0][1] == [1]
    assert messages[1][1] == [2]


def test_quiet_source_url_telegram_handle():
    assert _quiet_source_url({"type": "telegram", "url": "@lachen"}) == "https://t.me/lachen"


def test_quiet_source_url_telegram_numeric_or_invite_has_no_public_link():
    assert _quiet_source_url({"type": "telegram", "url": "-1001234567"}) is None
    assert _quiet_source_url({"type": "telegram", "url": "https://t.me/+abc"}) == "https://t.me/+abc"


def test_quiet_source_url_rss_uses_feed_url():
    assert _quiet_source_url({"type": "rss", "url": "https://decrypt.co/feed"}) == "https://decrypt.co/feed"


def test_quiet_source_url_empty():
    assert _quiet_source_url({"type": "telegram", "url": ""}) is None
