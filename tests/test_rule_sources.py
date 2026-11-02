"""A filter rule limited to some sources: stored per rule, offered from its categories."""
from src.bot.handlers.blocked import rule_source_choices


def _src(i, name, cat):
    return {"id": i, "name": name, "category": cat}


def test_a_scoped_rule_offers_only_the_sources_of_its_categories():
    sources = [_src(1, "The Block", "crypto"), _src(2, "Kiwi News", "crypto"), _src(3, "Лачен", "feed")]
    assert [s["name"] for s in rule_source_choices(sources, {"crypto"})] == ["Kiwi News", "The Block"]
    assert len(rule_source_choices(sources, set())) == 3


async def test_a_rules_sources_round_trip_and_a_removed_last_source_keeps_the_limit(tmp_path, monkeypatch):
    from src.config import settings
    from src.db.base import init_db
    from src.db.models import (add_blocked_word, add_source, get_blocked_words, get_sources_for_word,
                               get_word_source_map, remove_source, toggle_word_source)

    monkeypatch.setattr(settings, "database_path", str(tmp_path / "t.db"))
    await init_db()
    kiwi = await add_source("rss", "Kiwi News", "https://news.kiwistand.com/new.xml", "crypto")
    block = await add_source("rss", "The Block", "https://www.theblock.co/rss.xml", "crypto")
    await add_blocked_word("tweets with no event")
    rule = (await get_blocked_words())[0]["id"]

    assert await toggle_word_source(rule, kiwi) is True
    assert await toggle_word_source(rule, block) is True
    assert await toggle_word_source(rule, block) is False
    assert await get_sources_for_word(rule) == [kiwi]
    assert await get_word_source_map() == {rule: {kiwi}}

    await toggle_word_source(rule, block)
    await remove_source(block)
    assert await get_word_source_map() == {rule: {kiwi}}
    # The last source of a rule stays: an empty limit would apply the rule to every source.
    await remove_source(kiwi)
    assert await get_word_source_map() == {rule: {kiwi}}


def test_a_chosen_source_outside_the_rules_categories_stays_visible():
    """Kiwi moved out of crypto (or was paused) while a rule was limited to it: hiding it
    from the picker left a rule that never fires and a limit nobody could take off."""
    active = [_src(1, "The Block", "crypto")]
    kiwi = _src(2, "Kiwi News", "tech")
    names = [s["name"] for s in rule_source_choices(active, {"crypto"}, [kiwi])]
    assert names == ["The Block", "Kiwi News"]


async def test_the_rule_screens_show_a_removed_last_source_so_it_can_be_taken_off(tmp_path, monkeypatch):
    from src.bot.handlers import blocked
    from src.config import settings
    from src.db.base import init_db
    from src.db.models import add_blocked_word, add_source, get_blocked_words, remove_source, toggle_word_source

    monkeypatch.setattr(settings, "database_path", str(tmp_path / "t.db"))
    await init_db()
    kiwi = await add_source("rss", "Kiwi News", "https://news.kiwistand.com/new.xml", "crypto")
    await add_source("rss", "The Block", "https://www.theblock.co/rss.xml", "crypto")
    await add_blocked_word("tweets with no event")
    rule = (await get_blocked_words())[0]["id"]
    await toggle_word_source(rule, kiwi)
    await remove_source(kiwi)

    text, _ = await blocked._render_blocked_view(rule)
    assert "removed source" in text
    _, kb = await blocked._render_rule_sources(rule, 0)
    labels = [row[0].text for row in kb.inline_keyboard if row[0].callback_data.startswith("blocked_src_toggle")]
    # The Block is still pending, not active, and is offered all the same.
    assert labels == [f"✅ removed source #{kiwi}", "⬜ The Block · crypto"]
