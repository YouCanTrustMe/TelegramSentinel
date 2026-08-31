"""Digest assembly helpers: message splitting under the Telegram limit, the
per-digest chrome that separates one digest from the next, the quiet-source link
builder, and the empty-item defer/fallback phase."""
from datetime import datetime, timedelta, timezone

import pytest

from src.dispatcher import digest_builder
from src.dispatcher.digest_builder import (
    _chrome_reserve,
    _decorate_messages,
    _defer_empty_items,
    _quiet_source_url,
    _slow_digest_warning,
    _split_into_messages,
)


def test_slow_digest_warning_none_when_fast():
    assert _slow_digest_warning(3.2, threshold_s=90.0) is None


def test_slow_digest_warning_none_at_exact_threshold():
    # Boundary is inclusive: exactly at the threshold is still healthy.
    assert _slow_digest_warning(90.0, threshold_s=90.0) is None


def test_slow_digest_warning_fires_when_slow():
    msg = _slow_digest_warning(140.0, threshold_s=90.0)
    assert msg is not None
    assert "140s" in msg and "90s" in msg


def test_split_keeps_small_segments_in_one_message():
    segments = [("a", [1]), ("b", [2]), ("c", [3])]
    messages = _split_into_messages(segments)
    assert messages == [("a\nb\nc", [1, 2, 3])]


def test_digest_chrome_marks_start_parts_and_end():
    now = datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc)
    messages = _decorate_messages([("a", [1]), ("b", [2]), ("c", [3])], now, "🌍 world", 34)
    assert [ids for _, ids in messages] == [[1], [2], [3]]
    # 28 July 2026 is day 209; every message carries the number, so a continuation
    # can never be read as the start of the next digest.
    assert all("#209" in text for text, _ in messages)
    assert "<b>Digest</b>" in messages[0][0] and "09:00" in messages[0][0]
    assert "2/3" in messages[1][0] and "3/3" in messages[2][0]
    assert "Digest</b>" not in messages[1][0]
    assert "end #209" in messages[2][0] and "34 items" in messages[2][0]
    assert "end #209" not in messages[0][0]
    # Chrome uses only bold/italic: a <code> chip renders as a grey monospace box
    # and drew more attention than the digest it labels.
    assert all("<code>" not in text for text, _ in messages)


def test_digest_chrome_on_a_single_message_has_both_ends():
    now = datetime(2026, 1, 1, 21, 30, tzinfo=timezone.utc)
    (text, _), = _decorate_messages([("body", [1])], now, "", 1)
    assert "#1" in text and "21:30" in text
    assert "end #1" in text and "1 item" in text
    assert "1/1" not in text  # a lone message needs no part marker


def test_split_reserves_room_for_the_chrome():
    now = datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc)
    tags = "🌍 world · 🪙 crypto"
    reserve = _chrome_reserve(now, tags, 34)
    segments = [("x" * 1900, [1]), ("y" * 1900, [2]), ("z" * 1900, [3])]
    messages = _decorate_messages(_split_into_messages(segments, reserve=reserve), now, tags, 34)
    # Decorating must not push any message past what send_message can deliver.
    assert all(len(text) <= 4096 for text, _ in messages)


def test_split_breaks_when_over_limit():
    segments = [("x" * 3000, [1]), ("y" * 2000, [2])]
    messages = _split_into_messages(segments)
    assert len(messages) == 2
    assert messages[0][1] == [1]
    assert messages[1][1] == [2]


def test_media_token_renders_as_emoji_chip():
    item = {"original_url": "https://t.me/x/1", "summary": "[Video note]",
            "raw_text": "[Video note]", "published_at": None, "key_phrase": ""}
    line = digest_builder._format_item_base(item)
    # Emoji + word so the link has a real, tappable anchor (not a lone emoji).
    assert '<a href="https://t.me/x/1">🔵 Video</a>' == line
    assert "[Video note]" not in line and "no text" not in line


def test_unmapped_media_renders_as_generic_chip_not_literal():
    item = {"original_url": "https://t.me/x/2", "summary": "no text",
            "raw_text": "[Media]", "published_at": None, "key_phrase": ""}
    line = digest_builder._format_item_base(item)
    assert '<a href="https://t.me/x/2">📦 Media</a>' == line
    assert "no text" not in line  # never show the literal marker to the user


def _line(summary, key_phrase, url="https://t.me/x/1"):
    item = {"original_url": url, "summary": summary, "raw_text": summary,
            "published_at": None, "key_phrase": key_phrase}
    return digest_builder._format_item_base(item)


def test_short_key_phrase_anchor_grows_to_next_word():
    # A 2-char key phrase is too small to tap, so the anchor pulls in the next word.
    line = _line("РФ атакувала школу", "РФ")
    assert line == '<a href="https://t.me/x/1">РФ атакувала</a> школу'


def test_short_key_phrase_mid_summary_grows_keeping_prefix():
    line = _line("Лідери G7 готові передати зброю", "G7")
    assert line == 'Лідери <a href="https://t.me/x/1">G7 готові</a> передати зброю'


def test_long_key_phrase_anchor_is_left_intact():
    line = _line("Microsoft скоротить 650 працівників", "Microsoft")
    assert line == '<a href="https://t.me/x/1">Microsoft</a> скоротить 650 працівників'


def test_key_phrase_absent_falls_back_to_grown_first_word():
    # key_phrase not present verbatim (Fed vs Фед) → first word, grown to tappable size.
    line = _line("Фед підтримує ставки", "Fed")
    assert line == '<a href="https://t.me/x/1">Фед підтримує</a> ставки'


def test_dedup_source_links_wrapped_in_parentheses():
    item = {"id": 1, "original_url": "https://t.me/x/1", "summary": "Big news",
            "raw_text": "", "published_at": None, "key_phrase": ""}
    dup_links = {1: [("Бабель", "https://t.me/b/2"), ("Лачен", "https://t.me/l/3")]}
    line = digest_builder._format_item(item, dup_links)
    assert line.endswith(
        ' (<a href="https://t.me/b/2">Бабель</a>, <a href="https://t.me/l/3">Лачен</a>)'
    )


def test_quiet_source_url_telegram_handle():
    assert _quiet_source_url({"type": "telegram", "url": "@lachen"}) == "https://t.me/lachen"


def test_quiet_source_url_telegram_numeric_or_invite_has_no_public_link():
    assert _quiet_source_url({"type": "telegram", "url": "-1001234567"}) is None
    assert _quiet_source_url({"type": "telegram", "url": "https://t.me/+abc"}) == "https://t.me/+abc"


def test_quiet_source_url_rss_uses_feed_url():
    assert _quiet_source_url({"type": "rss", "url": "https://decrypt.co/feed"}) == "https://decrypt.co/feed"


def test_quiet_source_url_empty():
    assert _quiet_source_url({"type": "telegram", "url": ""}) is None


@pytest.mark.asyncio
async def test_defer_empty_items_keeps_summarised_and_branches_empties(monkeypatch):
    """Items with a summary are kept untouched; still-empty items are deferred
    when young and given a raw-text fallback when older than the defer window."""
    written: list[tuple[int, str]] = []

    async def _fake_update(item_id, summary, key_phrase):
        written.append((item_id, summary))

    monkeypatch.setattr(digest_builder, "update_item_classification", _fake_update)

    now = datetime.now(timezone.utc)
    young = (now - timedelta(hours=1)).isoformat()
    old = (now - timedelta(days=digest_builder._DEFER_MAX_DAYS + 1)).isoformat()
    items = [
        {"id": 1, "summary": "has summary", "raw_text": "x", "processed_at": young, "published_at": None},
        {"id": 2, "summary": "", "raw_text": "fresh news body", "processed_at": young, "published_at": None},
        {"id": 3, "summary": "", "raw_text": "stale news body", "processed_at": old, "published_at": None},
        {"id": 4, "summary": "", "raw_text": "", "processed_at": young, "published_at": None},
    ]

    kept, deferred = await _defer_empty_items(items)

    kept_ids = [it["id"] for it in kept]
    assert deferred == 1  # item 2 (young + empty) is held back
    assert 2 not in kept_ids
    assert kept_ids == [1, 3, 4]
    # The stale empty item got a ⚠️ fallback persisted and surfaced.
    fallback = next(it for it in kept if it["id"] == 3)
    assert fallback["summary"].startswith("⚠️ stale news body")
    assert written == [(3, fallback["summary"])]


def _stub_pinning(monkeypatch):
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(digest_builder, "get_app_setting", _noop)
    monkeypatch.setattr(digest_builder, "set_app_setting", _noop)
    monkeypatch.setattr(digest_builder, "pin_message", _noop)
    monkeypatch.setattr(digest_builder, "unpin_message", _noop)


@pytest.mark.asyncio
async def test_deliver_marks_only_delivered_items(monkeypatch):
    """The no-loss invariant: mark_sent receives exactly the ids of messages that
    actually reached Telegram — never more. If this breaks, items would be marked
    sent without being delivered and silently vanish from every future digest."""
    marked: list[int] = []

    async def _fake_send(text, disable_notification=False):
        return 111

    async def _fake_mark(ids):
        marked.extend(ids)

    monkeypatch.setattr(digest_builder, "send_message", _fake_send)
    monkeypatch.setattr(digest_builder, "mark_sent", _fake_mark)
    _stub_pinning(monkeypatch)

    messages = [("m1", [1, 2]), ("m2", [3]), ("m3", [4, 5])]
    sent, total, confirmed, failed = await digest_builder._deliver(messages)

    assert not failed
    assert sent == total == 3
    assert sorted(marked) == [1, 2, 3, 4, 5]  # every id, exactly once


@pytest.mark.asyncio
async def test_deliver_leaves_undelivered_items_unmarked_on_failure(monkeypatch):
    """A mid-batch send failure must leave the remaining items sent=0 so the next
    digest retries them — no silent loss, no double-send."""
    marked: list[int] = []
    calls = {"n": 0}

    async def _fake_send(text, disable_notification=False):
        calls["n"] += 1
        if calls["n"] == 2:        # second message fails
            raise RuntimeError("Telegram 500")
        return 111

    async def _fake_mark(ids):
        marked.extend(ids)

    monkeypatch.setattr(digest_builder, "send_message", _fake_send)
    monkeypatch.setattr(digest_builder, "mark_sent", _fake_mark)
    _stub_pinning(monkeypatch)

    messages = [("m1", [1, 2]), ("m2", [3]), ("m3", [4, 5])]
    sent, total, confirmed, failed = await digest_builder._deliver(messages)

    assert failed
    assert sent == 1
    assert marked == [1, 2]            # only the delivered message's items
    assert 3 not in marked and 4 not in marked and 5 not in marked


@pytest.mark.asyncio
async def test_media_placeholder_skips_semantic_filter(monkeypatch):
    """A media-only post (no readable text) is never handed to the content filter — we
    can't judge content we can't see, so it is kept unconditionally instead of being
    blind-blocked (the filter had been mis-flagging these as link-lists)."""
    seen: dict = {}

    async def fake_get_blocked_words():
        return [{"id": 1, "rule": "block everything"}]

    async def fake_scope_map():
        return {}  # rule 1 has no scope -> applies to every category

    async def fake_check(check_input, rules, rule_scopes):
        seen["ids"] = [c["id"] for c in check_input]
        return {c["id"]: rules[0] for c in check_input}  # block everything it is shown

    async def fake_mark_blocked(pairs):
        seen["marked"] = pairs

    monkeypatch.setattr(digest_builder, "get_blocked_words", fake_get_blocked_words)
    monkeypatch.setattr(digest_builder, "get_word_category_map", fake_scope_map)
    monkeypatch.setattr(digest_builder, "check_blocked_filters", fake_check)
    monkeypatch.setattr(digest_builder, "mark_blocked", fake_mark_blocked)

    items = [
        {"id": 1, "summary": "no text", "raw_text": "[Video]", "category": "feed",
         "source_name": "A", "source_prompt_extra": None},
        {"id": 2, "summary": "Real news", "raw_text": "body", "category": "feed",
         "source_name": "B", "source_prompt_extra": None},
    ]
    kept, blocked = await digest_builder._apply_semantic_filter(items)

    assert seen["ids"] == [2]                        # placeholder never reached the filter
    assert 1 in [k["id"] for k in kept]              # placeholder kept despite "block everything"
    assert [b["id"] for b in blocked] == [2]         # real item still blocked as usual


def test_published_time_prefix_is_italic_hh_mm(monkeypatch):
    # The stamp carries minutes and rides in the italic the chrome uses, so an
    # item line never reads as "13⏰" — a bare hour with the clock trailing it.
    monkeypatch.setattr(digest_builder.settings, "digest_timezone", "UTC")
    item = {"original_url": "https://t.me/x/1", "summary": "Fed holds rates",
            "raw_text": "", "published_at": "2026-08-31T13:40:00+00:00", "key_phrase": "Fed"}
    line = digest_builder._format_item_base(item)
    assert line.startswith("<i>13:40</i>  ")
    assert "⏰" not in line


def test_unparsable_published_time_leaves_no_prefix():
    item = {"original_url": "https://t.me/x/1", "summary": "Fed holds rates",
            "raw_text": "", "published_at": "not-a-date", "key_phrase": "Fed"}
    assert digest_builder._format_item_base(item).startswith('<a href=')
