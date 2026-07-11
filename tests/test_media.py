"""The shared media table that drives both collector tagging and digest emoji
chips — kept consistent so a new media type only has to be added once."""
from src.common.media import (
    GENERIC_MEDIA_TOKEN,
    MEDIA_EMOJI,
    MEDIA_LABEL,
    MEDIA_TOKENS,
    MEDIA_TYPES,
    NO_TEXT,
    is_media_placeholder,
)


def test_every_media_type_has_its_emoji():
    for _attr, token, emoji in MEDIA_TYPES:
        assert MEDIA_EMOJI[token] == emoji
    assert MEDIA_EMOJI[NO_TEXT] == "📦"


def test_media_label_is_emoji_plus_word():
    # Every renderable media token maps to "<emoji> <Word>" so the digest link has
    # a real word to tap on, not a bare one-character emoji.
    for token, emoji in MEDIA_EMOJI.items():
        label = MEDIA_LABEL[token]
        assert label.startswith(emoji + " ")
        word = label[len(emoji) + 1:]
        assert word and word[0].isalpha()
    assert MEDIA_LABEL["[Photo]"] == "📷 Photo"
    assert MEDIA_LABEL[NO_TEXT] == "📦 Media"
    assert MEDIA_LABEL[GENERIC_MEDIA_TOKEN] == "📦 Media"


def test_is_media_placeholder():
    # A post with no readable text — the content filter must skip these (blind block otherwise).
    assert is_media_placeholder("[Video]")
    assert is_media_placeholder(GENERIC_MEDIA_TOKEN)
    assert is_media_placeholder(NO_TEXT)
    assert is_media_placeholder("")
    assert is_media_placeholder("  ")
    assert is_media_placeholder(None)
    # A real summary is judged normally.
    assert not is_media_placeholder("Russia launched 12 missiles at Kyiv")


def test_media_tokens_cover_known_and_generic():
    for _attr, token, _emoji in MEDIA_TYPES:
        assert token in MEDIA_TOKENS
    assert GENERIC_MEDIA_TOKEN in MEDIA_TOKENS
    # NO_TEXT is a stored marker, not a media token the collector detects.
    assert NO_TEXT not in MEDIA_TOKENS
