"""Single source of truth for caption-less media posts: the token the collector
tags them with and the emoji chip the digest renders. Driving both sides from this
one table means a new media type is added in exactly one place."""

# Ordered: caption detection picks the first matching Pyrogram Message attribute.
MEDIA_TYPES: list[tuple[str, str, str]] = [
    # (pyrogram Message attribute, token, digest emoji)
    ("photo", "[Photo]", "📷"),
    ("video", "[Video]", "🎬"),
    ("animation", "[GIF]", "🎞️"),
    ("video_note", "[Video note]", "🔵"),
    ("sticker", "[Sticker]", "🔖"),
    ("document", "[Doc]", "📎"),
    ("audio", "[Audio]", "🎵"),
    ("voice", "[Voice]", "🎤"),
]

# Media Pyrogram reports but we don't recognise specifically yet (dice, location…).
GENERIC_MEDIA_TOKEN = "[Media]"
# Marker value stored for a generic-media post; never shown literally (renders 📦).
NO_TEXT = "no text"

MEDIA_EMOJI: dict[str, str] = {token: emoji for _, token, emoji in MEDIA_TYPES}
MEDIA_EMOJI[NO_TEXT] = "📦"
MEDIA_EMOJI[GENERIC_MEDIA_TOKEN] = "📦"

# A bare emoji is too small a tap target for the digest link, so a media-only post
# renders as "<emoji> <word>" (e.g. "📷 Photo") and the link anchors on the word.
_MEDIA_WORD: dict[str, str] = {
    "[Photo]": "Photo",
    "[Video]": "Video",
    "[GIF]": "GIF",
    "[Video note]": "Video",
    "[Sticker]": "Sticker",
    "[Doc]": "File",
    "[Audio]": "Audio",
    "[Voice]": "Voice",
    GENERIC_MEDIA_TOKEN: "Media",
    NO_TEXT: "Media",
}

MEDIA_LABEL: dict[str, str] = {
    token: f"{emoji} {_MEDIA_WORD.get(token, '')}".strip() for token, emoji in MEDIA_EMOJI.items()
}

# Bare tokens that, with no caption, mean a media-only post.
MEDIA_TOKENS: set[str] = {token for _, token, _ in MEDIA_TYPES} | {GENERIC_MEDIA_TOKEN}


def is_media_placeholder(summary: str | None) -> bool:
    """True when a post carries no readable text — a media token, the no-text marker,
    or empty. The content filter must never judge (or block) these: we can't see what
    they contain, so a block would be a blind guess."""
    t = (summary or "").strip()
    return t == "" or t == NO_TEXT or t in MEDIA_TOKENS
