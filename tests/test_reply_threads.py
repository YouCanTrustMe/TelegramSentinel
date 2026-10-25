"""A channel's reply to its own earlier post: stored with its parent, shown under it in
one digest, or with "↳ to <time> <opening words>" when the original is elsewhere."""
from datetime import datetime
from types import SimpleNamespace

import pytest

from src.collectors import telegram_collector as tc
from src.dispatcher import digest_builder as db


def _line(i, summary, pub, url=None):
    return {"id": i, "summary": summary, "raw_text": summary, "key_phrase": "",
            "published_at": pub, "original_url": url or f"https://t.me/l/{i}"}


def test_a_reply_moves_under_its_original_in_the_same_block():
    lines = [_line(1, "Франція на межі: до 800 тисяч людей на вулицях", "2026-10-18T04:46:00+00:00"),
             _line(2, "Росія атакувала енергооб'єкти", "2026-10-18T05:20:00+00:00"),
             _line(3, "Слідом за Францією протести дійшли до Італії", "2026-10-18T06:04:00+00:00")]
    out = db._thread_lines(lines, {3: {"id": 1}})
    assert [line["id"] for line in out] == [1, 3, 2]
    assert out[1]["_nested"] and not db.row_get(out[0], "_nested")
    rendered = db._format_item(out[1])
    assert rendered.startswith("  <i>↳ ") and "Італії" in rendered


def test_a_chain_of_replies_stays_in_order_under_the_first_post():
    lines = [_line(1, "a", "2026-10-18T04:00:00+00:00"), _line(2, "b", "2026-10-18T05:00:00+00:00"),
             _line(3, "c", "2026-10-18T06:00:00+00:00"), _line(4, "d", "2026-10-18T07:00:00+00:00")]
    out = db._thread_lines(lines, {2: {"id": 1}, 4: {"id": 2}})
    assert [line["id"] for line in out] == [1, 2, 4, 3]


def test_merged_lines_that_answer_each_other_both_go_out():
    first = {**_line(1, "a", "2026-10-18T04:00:00+00:00"), "_item_ids": [1, 4]}
    second = {**_line(2, "b", "2026-10-18T05:00:00+00:00"), "_item_ids": [2, 3]}
    out = db._thread_lines([first, second], {3: {"id": 1}, 4: {"id": 2}})
    assert sorted(line["id"] for line in out) == [1, 2]


def test_a_reply_to_a_post_shown_earlier_says_what_it_answers():
    parent = {"id": 9, "summary": "У росіян — мінус 6 винищувачів Су-34М на аеродромі в Мар'їнівці",
              "raw_text": "", "original_url": "https://t.me/uc/10597", "published_at": "2026-10-15T04:53:00+00:00"}
    out = db._thread_lines([_line(5, "4 літаки знищені повністю, 2 — пошкоджені.", "2026-10-15T08:41:00+00:00")],
                           {5: parent})
    text = db._format_item(out[0])
    first, second = text.split("\n")
    assert "4 літаки" in first and "↳" not in first
    assert second.startswith('<i>↳ to <a href="https://t.me/uc/10597">')
    assert second.endswith("У росіян — мінус 6 винищувачів Су-34М…</i>")


def test_an_original_older_than_a_digest_gap_carries_its_date():
    parent = {"id": 9, "summary": "Трамп підписав указ", "raw_text": "",
              "original_url": "https://t.me/l/69040", "published_at": "2026-10-16T03:44:00+00:00"}
    item = {**_line(5, "Трамп не скасував санкції", "2026-10-18T16:02:00+00:00"), "_reply_parent": parent}
    assert ">16/10 " in db._reply_line(item)


def test_an_original_from_the_day_before_reads_yday():
    parent = {"id": 9, "summary": "Трамп підписав указ", "raw_text": "",
              "original_url": "https://t.me/l/69040", "published_at": "2026-10-16T09:44:00+00:00"}
    item = {**_line(5, "Трамп не скасував санкції", "2026-10-17T16:02:00+00:00"), "_reply_parent": parent}
    assert ">yday 11:44</a>" in db._reply_line(item)


def test_an_unnamed_media_original_is_linked_without_a_quote():
    parent = {"id": 9, "summary": "no text", "raw_text": "[Media]",
              "original_url": "https://t.me/l/39447", "published_at": "2026-10-19T12:15:00+00:00"}
    line = db._reply_line({**_line(5, "Схеми перекриття руху", "2026-10-19T14:17:00+00:00"), "_reply_parent": parent})
    assert line.endswith("</a></i>") and "📦" not in line


def test_an_unsummarised_media_original_is_linked_without_a_quote():
    parent = {"id": 9, "summary": None, "raw_text": "[Media]",
              "original_url": "https://t.me/l/39447", "published_at": "2026-10-19T12:15:00+00:00"}
    line = db._reply_line({**_line(5, "Схеми перекриття руху", "2026-10-19T14:17:00+00:00"), "_reply_parent": parent})
    assert line.endswith("</a></i>") and "[Media]" not in line


def test_an_unsummarised_photo_original_is_quoted_from_its_caption():
    parent = {"id": 9, "summary": "", "raw_text": "[Photo] Схема перекриття руху на Хрещатику",
              "original_url": "https://t.me/l/39447", "published_at": "2026-10-19T12:15:00+00:00"}
    line = db._reply_line({**_line(5, "Рух відновили", "2026-10-19T14:17:00+00:00"), "_reply_parent": parent})
    assert "Схема перекриття" in line and "[Photo]" not in line


def test_a_photo_caption_kept_as_its_summary_is_quoted_without_the_token():
    parent = {"id": 9, "summary": "[Photo] Тривога у Києві", "raw_text": "[Photo] Тривога у Києві",
              "original_url": "https://t.me/l/39447", "published_at": "2026-10-19T12:15:00+00:00"}
    line = db._reply_line({**_line(5, "Відбій", "2026-10-19T14:17:00+00:00"), "_reply_parent": parent})
    assert "Тривога у Києві" in line and "[Photo]" not in line


def test_an_original_with_no_summary_is_quoted_from_its_text():
    parent = {"id": 9, "summary": "", "raw_text": "[Context: older]\nМікроавтобус наїхав на людину на перехресті вулиць",
              "original_url": None, "published_at": "2026-10-16T12:50:00+00:00"}
    line = db._reply_line({**_line(5, "x", "2026-10-17T03:38:00+00:00"), "_reply_parent": parent})
    assert "Мікроавтобус наїхав на людину на перехресті…" in line and "Context" not in line


def test_a_reply_merged_into_its_original_needs_no_mark():
    merged = {"_item_ids": [1, 3], "summary": "s", "original_url": "u", "published_at": "p", "raw_text": None}
    out = db._thread_lines([merged], {3: {"id": 1}})
    assert out == [merged]


# --- collector -----------------------------------------------------------------------------

def _message(reply_to=None, chat_id=-100, mid=50):
    return SimpleNamespace(id=mid, reply_to_message_id=reply_to, chat=SimpleNamespace(id=chat_id))


class _Bot:
    def __init__(self, parent=None, error=None):
        self.parent, self.error, self.calls = parent, error, []

    async def get_messages(self, chat_id, message_ids=None, reply=False, pinned=False, replies=1, **_):  # kurigram
        self.calls.append((chat_id, {"message_ids": message_ids, "reply": reply, "replies": replies}))
        if self.error:
            raise self.error
        return self.parent


class _OldBot(_Bot):
    async def get_messages(self, chat_id, message_ids=None, reply_to_message_ids=None, replies=1):  # pyrogram 2.0
        self.calls.append((chat_id, {"reply_to_message_ids": reply_to_message_ids, "replies": replies}))
        if self.error:
            raise self.error
        return self.parent


async def test_the_parent_outside_the_batch_is_fetched_from_the_same_channel(monkeypatch):
    bot = _Bot(parent=SimpleNamespace(id=40, empty=False, chat=SimpleNamespace(id=-100)))
    monkeypatch.setattr(tc, "userbot", bot)
    parent = await tc._fetch_parent("-1001536630827", _message(reply_to=40))
    assert parent.id == 40
    assert bot.calls == [(-1001536630827, {"message_ids": 50, "reply": True, "replies": 0})]


async def test_the_old_library_is_asked_by_its_own_keyword(monkeypatch):
    bot = _OldBot(parent=SimpleNamespace(id=40, empty=False, chat=SimpleNamespace(id=-100)))
    monkeypatch.setattr(tc, "userbot", bot)
    assert (await tc._fetch_parent("@lachentyt", _message(reply_to=40))).id == 40
    assert bot.calls == [("lachentyt", {"reply_to_message_ids": 50, "replies": 0})]


async def test_an_error_inside_the_call_is_not_taken_for_the_other_library(monkeypatch):
    bot = _Bot(error=TypeError("bad entity in the parent"))
    monkeypatch.setattr(tc, "userbot", bot)
    assert await tc._fetch_parent("@lachentyt", _message(reply_to=40)) is None
    assert len(bot.calls) == 1


@pytest.mark.parametrize("parent,error", [
    (SimpleNamespace(id=40, empty=False, chat=SimpleNamespace(id=-999)), None),  # another chat's post
    (SimpleNamespace(id=40, empty=True, chat=None), None),                        # deleted original
    (None, RuntimeError("flood")),
])
async def test_anything_but_the_channels_own_original_gives_no_parent(monkeypatch, parent, error):
    monkeypatch.setattr(tc, "userbot", _Bot(parent=parent, error=error))
    assert await tc._fetch_parent("@lachentyt", _message(reply_to=40)) is None


async def test_a_reply_already_stored_costs_no_parent_request(monkeypatch):
    bot = _Bot(parent=SimpleNamespace(id=40, empty=False, chat=SimpleNamespace(id=-100)))
    stored = {"tg_@lachentyt_50"}

    async def fake_is_duplicate(mid):
        return mid in stored

    monkeypatch.setattr(tc, "userbot", bot)
    monkeypatch.setattr(tc, "is_duplicate", fake_is_duplicate)
    assert await tc._reply_parent("@lachentyt", _message(reply_to=40), {}) is None
    assert bot.calls == []
    assert (await tc._reply_parent("@lachentyt", _message(reply_to=40, mid=51), {})).id == 40
    in_batch = SimpleNamespace(id=40)
    assert await tc._reply_parent("@lachentyt", _message(reply_to=40), {40: in_batch}) is in_batch


async def test_a_reply_is_stored_with_its_parents_message_id(monkeypatch):
    saved = {}

    async def fake_is_duplicate(_mid):
        return False

    async def fake_save_item(**kwargs):
        saved.update(kwargs)

    monkeypatch.setattr(tc, "is_duplicate", fake_is_duplicate)
    monkeypatch.setattr(tc, "save_item", fake_save_item)
    from tests.test_telegram_collector import SOURCE, _msg
    parent = _msg(id=40, text="Мікроавтобус наїхав на людину")
    assert await tc._process_message("@khmlv", SOURCE, _msg(text="Жінка отримала травми", reply_to_message_id=40),
                                     parent_msg=parent)
    assert saved["reply_to"] == "tg_@khmlv_40"
    assert saved["raw_text"].startswith("[Context: Мікроавтобус наїхав на людину]")


async def test_reply_to_round_trips_through_the_database(tmp_path, monkeypatch):
    from src.config import settings
    from src.db.base import init_db
    from src.db.models import add_source, get_items_by_message_ids, get_unsent_items, save_item

    monkeypatch.setattr(settings, "database_path", str(tmp_path / "t.db"))
    await init_db()
    sid = await add_source("telegram", "Лачен", "@lachentyt", "feed")
    common = dict(source_id=sid, original_url=None, published_at=datetime(2026, 10, 18).isoformat(),
                  summary="s", category="feed", processed_at="2026-10-18")
    await save_item(message_id="tg_@lachentyt_40", raw_text="parent", **common)
    await save_item(message_id="tg_@lachentyt_50", raw_text="reply", reply_to="tg_@lachentyt_40", **common)
    reply = next(r for r in await get_unsent_items() if r["raw_text"] == "reply")
    assert reply["reply_to"] == "tg_@lachentyt_40"
    parents = await db._reply_parents([reply])
    assert parents[reply["id"]]["raw_text"] == "parent"
    assert await get_items_by_message_ids([]) == {}
