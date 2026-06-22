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

# Bare tokens that, with no caption, mean a media-only post.
MEDIA_TOKENS: set[str] = {token for _, token, _ in MEDIA_TYPES} | {GENERIC_MEDIA_TOKEN}
