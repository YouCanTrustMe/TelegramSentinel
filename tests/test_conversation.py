"""The text wizards' parse side: what an admin pastes or types, and what the
callbacks that open each wizard leave in _pending for it to read."""
import pathlib
import re

import pytest

from src.bot.handlers.conversation import _parse_new_time, _parse_source_input

HANDLERS = pathlib.Path(__file__).resolve().parent.parent / "src" / "bot" / "handlers"


@pytest.mark.parametrize("pasted, stored", [
    ("https://t.me/babel", "@babel"),
    ("t.me/babel/", "@babel"),
    ("https://t.me/s/babel", "@babel"),                 # the web preview a browser shows
    ("https://t.me/babel/41234", "@babel"),             # a link to one post
    ("https://t.me/s/babel/41234?before=1", "@babel"),
    ("@babel", "@babel"),
    ("babel", "@babel"),
    ("  @babel  ", "@babel"),
    ("-1001372862774", "-1001372862774"),               # a private channel's chat id
    ("https://t.me/c/1372862774/20236", "-1001372862774"),  # a private channel's post
    ("https://t.me/+AbCdEf123", "https://t.me/+AbCdEf123"),
    ("https://t.me/joinchat/AbCdEf123", "https://t.me/joinchat/AbCdEf123"),
])
def test_a_pasted_channel_is_stored_in_one_form(pasted, stored):
    assert _parse_source_input(pasted) == (stored, "telegram")


@pytest.mark.parametrize("pasted", ["https://t.me/", "https://t.me/s/"])
def test_a_link_without_a_channel_is_passed_on_as_typed(pasted):
    assert _parse_source_input(pasted) == (pasted, "telegram")


@pytest.mark.parametrize("url", ["https://01portal.hr/mjesto/grad-zagreb/feed/", "https://about.me/feed.xml"])
def test_a_feed_url_is_rss(url):
    """Only the t.me host is Telegram, not any URL with "t.me/" inside it."""
    assert _parse_source_input(url) == (url, "rss")


@pytest.mark.parametrize("typed, time", [
    ("8:30", "08:30"),
    ("08:30", "08:30"),
    (" 21:05 ", "21:05"),
])
def test_the_new_time_prompt_accepts_one_time(typed, time):
    assert _parse_new_time(typed) == time


@pytest.mark.parametrize("typed", ["", "soon", "25:00", "08:30, 21:00"])
def test_the_new_time_prompt_reasks_otherwise(typed):
    assert _parse_new_time(typed) is None


def _produced() -> dict[str, set[str]]:
    """action -> data keys, from every `_pending[...] = {...}` outside the wizard."""
    out: dict[str, set[str]] = {}
    for path in HANDLERS.glob("*.py"):
        if path.name == "conversation.py":
            continue
        src = path.read_text()
        for m in re.finditer(r'"action":\s*"(\w+)"', src):
            data = re.search(r'"data":\s*(\{[^}]*\}|\w+)', src[m.end():m.end() + 300]).group(1)
            if data.startswith("{"):
                keys = set(re.findall(r'"(\w+)":', data))
            else:  # a dict built just above, key by key
                keys = set(re.findall(rf'{data}\["(\w+)"\]\s*=', src[max(0, m.start() - 400):m.start()]))
            out.setdefault(m.group(1), set()).update(keys)
    return out


def test_every_wizard_a_button_opens_is_handled():
    """A callback that sets an action the wizard does not know leaves the admin typing
    into nothing."""
    handled = set(re.findall(r'action == "(\w+)"', (HANDLERS / "conversation.py").read_text()))
    produced = _produced()

    assert produced
    assert set(produced) <= handled


def test_the_wizard_reads_only_data_keys_its_opener_sets():
    src = (HANDLERS / "conversation.py").read_text()
    produced = _produced()
    for action, block in re.findall(r'action == "(\w+)":(.*?)(?=\n        elif action ==|\Z)', src, re.S):
        # Keys a multi-step wizard writes itself (add_category's name, then emoji) are its own.
        written = set(re.findall(r'data\["(\w+)"\]\s*=(?!=)', block))
        read = set(re.findall(r'data\["(\w+)"\]', block)) - written
        assert read <= produced.get(action, set()), (action, read - produced.get(action, set()))
