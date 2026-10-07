"""Digest assembly helpers: message splitting under the Telegram limit, the
per-digest chrome that separates one digest from the next, the quiet-source link
builder, and the empty-item defer/fallback phase."""
from datetime import datetime, timedelta, timezone

import pytest

from src.dispatcher import digest_builder
from src.dispatcher.digest_builder import (
    _build_digest_text,
    _chrome_reserve,
    _filter_title,
    _decorate_messages,
    _defer_empty_items,
    _quiet_source_url,
    _slow_digest_warning,
    _split_into_messages,
)


def test_slow_digest_warning_none_when_fast():
    assert _slow_digest_warning(3.2, 4, threshold_s=90.0) is None


def test_slow_digest_warning_none_at_exact_threshold():
    # Boundary is inclusive: exactly at the threshold is still healthy.
    assert _slow_digest_warning(90.0, 40, threshold_s=90.0) is None


def test_slow_digest_warning_fires_when_slow():
    msg = _slow_digest_warning(140.0, 40, threshold_s=90.0)
    assert msg is not None
    assert "140s" in msg and "90s" in msg


def test_slow_digest_threshold_scales_with_the_item_count():
    """A big healthy digest must not alert: 116 items in 101.6s is 0.88s/item, while
    the regression that alerted for real was 2.6s/item."""
    assert _slow_digest_warning(101.6, 116) is None
    assert _slow_digest_warning(374.0, 144) is not None


def test_slow_digest_threshold_has_a_floor_for_tiny_digests():
    # 2 items x 1.5s would be a 3s threshold; a small digest still gets the floor.
    assert _slow_digest_warning(20.0, 2) is None
    assert _slow_digest_warning(190.0, 2) is not None


def test_a_rate_limited_but_delivered_digest_stays_quiet():
    """2026-09-29: 124s for 72 items, five Mistral rate limits, every item delivered."""
    assert _slow_digest_warning(124.0, 72) is None


def test_slow_digest_warning_survives_an_empty_digest():
    assert _slow_digest_warning(1.0, 0) is None


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


def test_key_phrase_absent_falls_back_past_the_leading_entity():
    # key_phrase not present in any form (Fed vs Фед): the fallback skips the
    # opening entity and anchors on the verb, not on the summary's first word.
    line = _line("Фед підтримує ставки", "Fed")
    assert line == 'Фед <a href="https://t.me/x/1">підтримує</a> ставки'


def test_reworded_key_phrase_is_matched_by_stem():
    # The model re-words its own phrase ("вибухи в Полтаві" for a summary that
    # says "У Полтаві чутно вибухи"); the anchor still lands on those words.
    line = _line("У Полтаві чутно вибухи", "вибухи в Полтаві")
    assert line == 'У <a href="https://t.me/x/1">Полтаві чутно вибухи</a>'


def test_reinflected_key_phrase_is_matched_by_stem():
    line = _line("У Києві загорівся 16-поверховий будинок", "16-поверхівка загорілася")
    assert line == 'У Києві <a href="https://t.me/x/1">загорівся 16-поверховий</a> будинок'


def test_scattered_key_phrase_words_anchor_on_one_word_not_the_whole_line():
    # Matches at both ends of the summary are not one phrase: anchor the longest
    # matched word rather than wrapping the entire line in the link.
    line = _line("Bitmine Tom Lee купив 51 000 ETH на 126 мільйонів", "Bitmine купив ETH")
    assert line == '<a href="https://t.me/x/1">Bitmine</a> Tom Lee купив 51 000 ETH на 126 мільйонів'


def test_missing_key_phrase_skips_the_stopword_and_the_place():
    # No key phrase at all: the verb carries the news, the leading preposition
    # and place name do not.
    line = _line("У Полтаві пролунали вибухи", "")
    assert line == 'У Полтаві <a href="https://t.me/x/1">пролунали</a> вибухи'


def test_summary_that_is_only_an_entity_keeps_the_entity_as_anchor():
    line = _line("Microsoft", "")
    assert line == '<a href="https://t.me/x/1">Microsoft</a>'


def test_dedup_source_links_wrapped_in_italic_parentheses():
    item = {"id": 1, "original_url": "https://t.me/x/1", "summary": "Big news",
            "raw_text": "", "published_at": None, "key_phrase": ""}
    dup_links = {1: [("Бабель", "https://t.me/b/2"), ("Лачен", "https://t.me/l/3")]}
    line = digest_builder._format_item(item, dup_links)
    assert line.endswith(
        ' <i>(<a href="https://t.me/b/2">Бабель</a>, <a href="https://t.me/l/3">Лачен</a>)</i>'
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


def test_anchor_before_punctuation_keeps_the_comma_tight():
    # Re-joining the three parts with a space used to put one before every comma
    # that followed an anchor — now common, since the fallback anchors mid-sentence.
    line = _line("Кабмін звільнив, потім призначив міністра", "звільнив")
    assert line == 'Кабмін <a href="https://t.me/x/1">звільнив</a>, потім призначив міністра'


def test_anchor_never_starts_inside_an_apostrophised_word():
    # A bare \\w+ split "Прем'єр" and anchored from "єр".
    line = _line("Прем'єр підписав угоду з ЄС", "")
    assert line == 'Прем&#x27;єр <a href="https://t.me/x/1">підписав</a> угоду з ЄС'


def test_hyphenated_place_is_one_word_for_the_anchor():
    line = _line("У Івано-Франківську відкрили міст", "")
    assert line == 'У Івано-Франківську <a href="https://t.me/x/1">відкрили</a> міст'


@pytest.mark.asyncio
async def test_a_deferred_item_does_not_warn_but_a_shipped_raw_one_does(monkeypatch):
    """The alerting was inverted: an item that merely waits for the next background
    classify (id=33860 gave up at 19:30, classified at 19:40) woke the admin, while an
    item old enough to go out as truncated raw text was silent. Prod 2026-09-08."""
    import logging
    from datetime import datetime, timedelta, timezone
    from src.dispatcher import digest_builder as db

    async def noop_update(*a, **kw):
        return None

    monkeypatch.setattr(db, "update_item_classification", noop_update)

    now = datetime.now(timezone.utc)
    fresh = {"id": 1, "summary": "", "raw_text": "x" * 200, "key_phrase": "",
             "processed_at": (now - timedelta(hours=2)).isoformat(), "published_at": None}
    stale = {"id": 2, "summary": "", "raw_text": "y" * 200, "key_phrase": "",
             "processed_at": (now - timedelta(days=db._DEFER_MAX_DAYS + 1)).isoformat(),
             "published_at": None}

    caplog = logging.getLogger(db.log.name)
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    caplog.addHandler(handler)
    try:
        kept, deferred = await db._defer_empty_items([fresh, stale])
    finally:
        caplog.removeHandler(handler)

    assert deferred == 1                       # the fresh one waits, no alert
    assert [i["id"] for i in kept] == [2]      # the stale one ships as raw text
    warnings = [r for r in records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1 and "never classified" in warnings[0].getMessage()


def test_filter_title_keeps_only_the_rules_first_clause():
    rule = ("рекламні та промо пости: заклики купити або зареєструватись, реферальні посилання. "
            "НЕ блокувати: новини ПРО гроші")
    assert _filter_title(rule) == "рекламні та промо пости…"


def test_filter_title_never_ends_on_a_preposition():
    rule = "прохання зібрати кошти або пожертвувати на військову техніку — лише якщо є заклик"
    assert _filter_title(rule) == "прохання зібрати кошти або пожертвувати…"


def test_filter_title_leaves_a_short_rule_whole():
    assert _filter_title("спам") == "спам"


def test_literal_rules_share_one_filtered_block():
    def blocked(i, rule):
        return {"id": i, "summary": f"тривога {i}", "raw_text": f"тривога {i}", "key_phrase": "",
                "published_at": "2026-10-06T17:39:00+00:00", "original_url": f"https://t.me/c/1/{i}",
                "blocked_by": rule}

    segments = _build_digest_text({}, blocked_items=[
        blocked(1, "= повітряна тривога!"), blocked(2, "= загроза застосування бпла"),
        blocked(3, "= відбій тривоги"), blocked(4, "рекламні та промо пости: заклики купити"),
    ])
    text = "\n".join(t for t, _ in segments)

    assert text.count("= exact phrases") == 1
    assert "рекламні та промо пости…" in text
    assert "загроза застосування бпла" not in text.split("= exact phrases")[0]


def test_filter_title_keeps_a_short_word_that_carries_meaning():
    # Cut at 45 chars this ends "...бригад ЗСУ та": drop the conjunction, keep "ЗСУ".
    rule = "збори на техніку для бригад ЗСУ та підрозділів ТрО: реквізити"
    assert _filter_title(rule) == "збори на техніку для бригад ЗСУ…"


def test_two_rules_that_open_alike_keep_separate_blocks():
    def blocked(i, rule):
        return {"id": i, "summary": f"s{i}", "raw_text": f"s{i}", "key_phrase": "",
                "published_at": "2026-10-06T17:39:00+00:00", "original_url": f"https://t.me/c/1/{i}",
                "blocked_by": rule}

    segments = _build_digest_text({}, blocked_items=[blocked(1, "реклама: магазини"), blocked(2, "реклама: казино")])
    assert "\n".join(t for t, _ in segments).count("реклама…") == 2


def test_filter_title_of_a_rule_that_opens_with_a_separator_is_not_empty():
    assert _filter_title(": реклама каналів і магазинів") == "реклама каналів і магазинів"


async def test_the_digest_builds_under_the_classify_lock(monkeypatch):
    """2026-10-07: the pre-digest pass was still summarising a 100-entry flood when the
    digest re-classified the same items, so each was summarised twice and the quota died."""
    import src.dispatcher.digest_builder as db_mod
    order, queue = [], [{"id": 1}]

    async def fake_acquire(timeout):
        order.append("acquire")
        return True

    async def fake_unsent(categories=None):
        order.append("read")
        return queue

    async def fake_build(categories, include_quiet, status_fn, reclassify=True):
        order.append("build" if reclassify else "build-no-reclassify")
        return True

    async def fake_status(text):
        order.append("status")

    monkeypatch.setattr(db_mod, "acquire_classify_lock", fake_acquire)
    monkeypatch.setattr(db_mod, "release_classify_lock", lambda: order.append("release"))
    monkeypatch.setattr(db_mod, "get_unsent_items", fake_unsent)
    monkeypatch.setattr(db_mod, "_build_and_send_digest", fake_build)
    monkeypatch.setattr(db_mod, "background_classify_running", lambda: False)
    assert await db_mod._send_digest_locked() is True
    assert order == ["read", "acquire", "build", "release"]

    # A manual /digest says why it is waiting instead of sitting silent.
    order.clear()
    monkeypatch.setattr(db_mod, "background_classify_running", lambda: True)
    assert await db_mod._send_digest_locked(status_fn=fake_status) is True
    assert order == ["read", "status", "acquire", "build", "release"]

    # Still running after the wait: build, but never summarise the same items beside it.
    order.clear()

    async def timed_out(timeout):
        order.append("acquire")
        return False

    monkeypatch.setattr(db_mod, "acquire_classify_lock", timed_out)
    assert await db_mod._send_digest_locked() is True
    assert order == ["read", "acquire", "build-no-reclassify"]

    # An empty queue answers at once instead of waiting for the lock.
    order.clear()
    queue.clear()
    assert await db_mod._send_digest_locked(status_fn=fake_status) is False
    assert order == ["read"]


async def test_reclassify_summarises_a_flood_in_batches_not_one_call_per_item(monkeypatch):
    """2026-10-07: 75 empty items went out as 75 calls in a row; the quota died at 34."""
    import src.dispatcher.digest_builder as db_mod
    from src.processor.llm.classifier import ClassificationResult

    items = [{"id": n, "source_id": 1, "raw_text": f"Post number {n}, long enough that no short-text rule would ever show it as written", "summary": "", "key_phrase": ""}
             for n in range(30)]
    # Same rules as the background pass: a short post is shown as written.
    items.append({"id": 99, "source_id": 1, "raw_text": "Київ від ранку під ударом безпілотників.", "summary": "", "key_phrase": ""})
    calls, stored = [], {}

    async def fake_batch(payload):
        calls.append([row["id"] for row in payload])
        return {row["id"]: ClassificationResult(summary=f"s{row['id']}") for row in payload if row["id"] != 7}

    async def fake_store(item_id, summary, key_phrase):
        stored[item_id] = summary

    async def no_status(_text):
        pass

    monkeypatch.setattr(db_mod, "classify_batch", fake_batch)
    monkeypatch.setattr(db_mod, "update_item_classification", fake_store)
    monkeypatch.setattr(db_mod, "is_task_dead", lambda *_: False)

    out = await db_mod._reclassify_empty_summaries(items, no_status)
    assert [len(c) for c in calls] == [25, 5]
    assert stored[99] == "Київ від ранку під ударом безпілотників." and stored[3] == "s3"
    assert 7 not in stored and out[7]["summary"] == ""   # left for _defer_empty_items
