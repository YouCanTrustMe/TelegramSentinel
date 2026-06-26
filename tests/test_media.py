"""The shared media table that drives both collector tagging and digest emoji
chips — kept consistent so a new media type only has to be added once."""
from src.common.media import GENERIC_MEDIA_TOKEN, MEDIA_EMOJI, MEDIA_TOKENS, MEDIA_TYPES, NO_TEXT


def test_every_media_type_has_its_emoji():
    for _attr, token, emoji in MEDIA_TYPES:
        assert MEDIA_EMOJI[token] == emoji
    assert MEDIA_EMOJI[NO_TEXT] == "📦"


def test_media_tokens_cover_known_and_generic():
    for _attr, token, _emoji in MEDIA_TYPES:
        assert token in MEDIA_TOKENS
    assert GENERIC_MEDIA_TOKEN in MEDIA_TOKENS
    # NO_TEXT is a stored marker, not a media token the collector detects.
    assert NO_TEXT not in MEDIA_TOKENS
