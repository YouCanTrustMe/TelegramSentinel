"""The morning digest reads differently from the rest of the day: the heavy feed goes
last, its overnight war reports fold into one block led by the night's tally, and a story that was
reported several times shows its newest post with the earlier ones as time links."""
from collections import defaultdict
from datetime import datetime, timezone

import src.dispatcher.digest_builder as db
import src.processor.llm.classifier as cl
from src.processor.dedup.merge import _build_merged


def _item(i, summary, pub="2026-09-24T05:00:00+00:00", url=None):
    return {"id": i, "summary": summary, "raw_text": summary, "key_phrase": "",
            "published_at": pub, "original_url": url or f"https://t.me/c/1/{i}"}


def _meta(**cats):
    return {name: {"emoji": "•", "sources": defaultdict(list, sources)} for name, sources in cats.items()}


def test_morning_is_a_window_of_local_hours(monkeypatch):
    monkeypatch.setattr(db.settings, "morning_from_hour", 5)
    monkeypatch.setattr(db.settings, "morning_until_hour", 12)
    assert db._is_morning(datetime(2026, 9, 24, 9, 45))
    assert not db._is_morning(datetime(2026, 9, 24, 14, 45))
    # A digest just after midnight is the tail of the evening, not the morning.
    assert not db._is_morning(datetime(2026, 9, 24, 0, 30))


def test_morning_order_puts_the_heavy_category_last(monkeypatch):
    monkeypatch.setattr(db.settings, "morning_last_categories", ["feed"])
    meta = _meta(feed={}, hrvatska={}, finance={}, crypto={})
    assert list(db._order_for_morning(meta)) == ["hrvatska", "finance", "crypto", "feed"]


def test_morning_order_ignores_a_category_that_is_not_in_this_digest(monkeypatch):
    monkeypatch.setattr(db.settings, "morning_last_categories", ["feed"])
    assert list(db._order_for_morning(_meta(ai={}, useful={}))) == ["ai", "useful"]


def _night_feed():
    return _meta(feed={
        "Лачен": [_item(1, "Росія вночі обстріляла Київ", "2026-09-24T04:45:00+00:00"),
                  _item(2, "Україна планує право на інформаційний спокій"),
                  _item(3, "Уламки пошкодили пологовий", "2026-09-23T23:45:00+00:00")],
        "Бабель": [_item(4, "РФ атакувала Україну 282 дронами", "2026-09-24T05:34:00+00:00"),
                   _item(5, "Метро змінює маршрути зеленої лінії")],
    })


NOW = datetime(2026, 9, 24, 7, 45, tzinfo=timezone.utc)


async def test_war_reports_fold_into_one_block_led_by_the_overview(monkeypatch):
    monkeypatch.setattr(db.settings, "war_block_category", "feed")
    monkeypatch.setattr(db.settings, "war_block_min_items", 3)
    seen = []

    async def fake_pick(items):
        seen.extend(items)
        by_text = {it["text"].split(" ", 1)[1]: it["id"] for it in items}
        tally = by_text["РФ атакувала Україну 282 дронами"]
        return {by_text["Росія вночі обстріляла Київ"], by_text["Уламки пошкодили пологовий"], tally}, tally

    monkeypatch.setattr(db, "pick_war_reports", fake_pick)
    monkeypatch.setattr(db.settings, "digest_timezone", "UTC")
    meta = _night_feed()
    await db._fold_war_reports(meta, NOW)

    # The tally leads, the rest follow in time order.
    assert [it["id"] for it in meta["feed"]["war_block"]["items"]] == [4, 3, 1]
    assert [it["id"] for it in meta["feed"]["sources"]["Лачен"]] == [2]
    assert [it["id"] for it in meta["feed"]["sources"]["Бабель"]] == [5]
    # The model sees each item's time, so an evening's attack is not read as the night's.
    assert any(it["text"] == "04:45 Росія вночі обстріляла Київ" for it in seen)
    assert len(seen) == 5


async def test_without_an_overview_the_block_is_plain_time_order(monkeypatch):
    monkeypatch.setattr(db.settings, "war_block_min_items", 3)

    async def fake_pick(items):
        by_text = {it["text"].split(" ", 1)[1]: it["id"] for it in items}
        return {by_text["РФ атакувала Україну 282 дронами"], by_text["Уламки пошкодили пологовий"],
                by_text["Росія вночі обстріляла Київ"]}, None

    monkeypatch.setattr(db, "pick_war_reports", fake_pick)
    meta = _night_feed()
    await db._fold_war_reports(meta, NOW)
    assert [it["id"] for it in meta["feed"]["war_block"]["items"]] == [3, 1, 4]


async def test_the_model_sees_the_feed_oldest_first_and_without_stale_items(monkeypatch):
    monkeypatch.setattr(db.settings, "war_block_max_age_hours", 18)
    monkeypatch.setattr(db.settings, "digest_timezone", "UTC")
    seen = []

    async def fake_pick(items):
        seen.extend(it["text"] for it in items)
        return set(), None

    monkeypatch.setattr(db, "pick_war_reports", fake_pick)
    meta = _night_feed()
    meta["feed"]["sources"]["Бабель"].append(_item(6, "Удар по Харкову два дні тому", "2026-09-22T03:10:00+00:00"))
    await db._fold_war_reports(meta, NOW)
    assert [t.split(" ", 1)[0] for t in seen] == ["23:45", "04:45", "05:00", "05:00", "05:34"]
    assert not any("два дні тому" in t for t in seen)


async def test_war_block_lines_name_their_channel(monkeypatch):
    monkeypatch.setattr(db.settings, "war_block_min_items", 3)

    async def fake_pick(items):
        return {it["id"] for it in items if "Метро" not in it["text"] and "спокій" not in it["text"]}, None

    monkeypatch.setattr(db, "pick_war_reports", fake_pick)
    meta = _night_feed()
    await db._fold_war_reports(meta, NOW)
    text = db._build_digest_text(meta)[1][0]
    assert "<i>· Бабель</i>" in text and "<i>· Лачен</i>" in text


async def test_too_few_war_reports_leave_the_feed_untouched(monkeypatch):
    monkeypatch.setattr(db.settings, "war_block_min_items", 3)

    async def fake_pick(items):
        return {0, 1}, 0

    monkeypatch.setattr(db, "pick_war_reports", fake_pick)
    meta = _night_feed()
    await db._fold_war_reports(meta, NOW)
    assert "war_block" not in meta["feed"]
    assert len(meta["feed"]["sources"]["Лачен"]) == 3


async def test_media_placeholders_are_never_sent_to_the_war_pick(monkeypatch):
    monkeypatch.setattr(db.settings, "war_block_min_items", 1)
    seen = []

    async def fake_pick(items):
        seen.extend(it["text"].split(" ", 1)[1] for it in items)
        return set(), None

    monkeypatch.setattr(db, "pick_war_reports", fake_pick)
    meta = _meta(feed={"УП": [_item(1, "no text"), _item(2, "[Photo]"), _item(3, "Росія атакувала Суми")]})
    await db._fold_war_reports(meta, NOW)
    assert seen == ["Росія атакувала Суми"]


def test_war_block_renders_title_and_count_and_carries_its_ids():
    meta = _meta(feed={"Бабель": [_item(5, "Метро змінює маршрути")]})
    meta["feed"]["war_block"] = {"items": [_item(3, "Уламки пошкодили пологовий"), _item(1, "Обстріл Києва")]}
    segments = db._build_digest_text(meta)
    war_text, war_ids = segments[1]
    assert "<b>🌙 War overnight · 2</b>" in war_text
    assert war_text.index("пологовий") < war_text.index("Києва")
    assert war_ids == [3, 1]
    assert segments[2][1] == [5]


def test_a_feed_with_only_a_war_block_still_gets_its_header():
    meta = _meta(feed={"Лачен": []})
    meta["feed"]["war_block"] = {"items": [_item(1, "Обстріл Києва")]}
    segments = db._build_digest_text(meta)
    assert "F E E D" in segments[0][0]
    assert segments[1][1] == [1]


async def test_pick_war_reports_keeps_only_known_integer_ids(monkeypatch):
    async def fake_llm_json(messages, max_retries=3, task="classify"):
        assert task == "war"
        return {"war": [0, 2, 9, True, "1", None], "overview": 9}

    monkeypatch.setattr(cl, "llm_json", fake_llm_json)
    monkeypatch.setattr(cl, "is_task_dead", lambda task: False)
    picked, overview = await cl.pick_war_reports([{"id": i, "text": f"t{i}"} for i in range(3)])
    assert picked == {0, 1, 2}
    # An overview outside the picked set is not an overview.
    assert overview is None


async def test_pick_war_reports_returns_the_overview_it_picked(monkeypatch):
    async def fake_llm_json(messages, max_retries=3, task="classify"):
        return {"war": [0, 2], "overview": "2"}

    monkeypatch.setattr(cl, "llm_json", fake_llm_json)
    monkeypatch.setattr(cl, "is_task_dead", lambda task: False)
    assert await cl.pick_war_reports([{"id": i, "text": f"t{i}"} for i in range(3)]) == ({0, 2}, 2)


async def test_pick_war_reports_rejects_float_and_infinite_ids(monkeypatch):
    async def fake_llm_json(messages, max_retries=3, task="classify"):
        return {"war": [1.9, float("inf"), 0], "overview": 0.0}

    monkeypatch.setattr(cl, "llm_json", fake_llm_json)
    monkeypatch.setattr(cl, "is_task_dead", lambda task: False)
    assert await cl.pick_war_reports([{"id": i, "text": f"t{i}"} for i in range(3)]) == ({0}, None)


async def test_pick_war_reports_asks_in_small_chunks_and_keeps_the_latest_tally(monkeypatch):
    calls = []

    async def fake_llm_json(messages, max_retries=3, task="classify"):
        ids = [int(line.split(":", 1)[0]) for line in messages[1]["content"].split("\n")]
        calls.append(ids)
        return {"war": ids[:2], "overview": ids[0]}

    monkeypatch.setattr(cl, "llm_json", fake_llm_json)
    monkeypatch.setattr(cl, "is_task_dead", lambda task: False)
    picked, overview = await cl.pick_war_reports([{"id": i, "text": f"t{i}"} for i in range(45)])
    assert [len(c) for c in calls] == [cl._WAR_CHUNK, cl._WAR_CHUNK, 45 - 2 * cl._WAR_CHUNK]
    assert picked == {0, 1, 20, 21, 40, 41}
    assert overview == 40


async def test_a_chunk_nobody_answered_unfolds_the_whole_feed(monkeypatch):
    # llm_json answers {} when every provider failed; half a block would pass for
    # the whole night.
    answers = iter([{"war": [0, 1], "overview": None}, {}])

    async def fake_llm_json(messages, max_retries=3, task="classify"):
        return next(answers)

    monkeypatch.setattr(cl, "llm_json", fake_llm_json)
    monkeypatch.setattr(cl, "is_task_dead", lambda task: False)
    assert await cl.pick_war_reports([{"id": i, "text": f"t{i}"} for i in range(30)]) == (set(), None)


async def test_pick_war_reports_rejects_non_ascii_digit_ids(monkeypatch):
    async def fake_llm_json(messages, max_retries=3, task="classify"):
        return {"war": ["²", "1"], "overview": None}

    monkeypatch.setattr(cl, "llm_json", fake_llm_json)
    monkeypatch.setattr(cl, "is_task_dead", lambda task: False)
    assert await cl.pick_war_reports([{"id": i, "text": f"t{i}"} for i in range(3)]) == ({1}, None)


async def test_pick_war_reports_fails_open(monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(cl, "llm_json", boom)
    monkeypatch.setattr(cl, "is_task_dead", lambda task: False)
    assert await cl.pick_war_reports([{"id": 0, "text": "t"}]) == (set(), None)


async def test_an_afternoon_digest_folds_war_reports_under_the_day_title(monkeypatch):
    """Strikes on cities are routine: every digest folds them now, not only the morning one."""
    monkeypatch.setattr(db.settings, "war_block_min_items", 3)
    monkeypatch.setattr(db.settings, "morning_from_hour", 5)
    monkeypatch.setattr(db.settings, "morning_until_hour", 12)
    monkeypatch.setattr(db.settings, "digest_timezone", "UTC")

    async def fake_pick(items):
        return {0, 1, 2}, None

    monkeypatch.setattr(db, "pick_war_reports", fake_pick)
    meta = _night_feed()
    await db._fold_war_reports(meta, datetime(2026, 9, 24, 14, 45, tzinfo=timezone.utc))

    assert meta["feed"]["war_block"]["title"] == "⚔️ War"
    assert "<b>⚔️ War · 3</b>" in db._build_digest_text(meta)[1][0]


async def test_a_morning_digest_keeps_the_overnight_title(monkeypatch):
    monkeypatch.setattr(db.settings, "war_block_min_items", 3)
    monkeypatch.setattr(db.settings, "morning_from_hour", 5)
    monkeypatch.setattr(db.settings, "morning_until_hour", 12)
    monkeypatch.setattr(db.settings, "digest_timezone", "UTC")

    async def fake_pick(items):
        return {0, 1, 2}, None

    monkeypatch.setattr(db, "pick_war_reports", fake_pick)
    meta = _night_feed()
    await db._fold_war_reports(meta, NOW)

    assert meta["feed"]["war_block"]["title"] == "🌙 War overnight"
