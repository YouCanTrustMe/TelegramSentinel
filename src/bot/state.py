_pending: dict[int, dict] = {}

# Template digest_time seeded into newly-added categories (and the "Default"
# button in the category wizard). No longer drives any catch-all schedule —
# digests run only at each category's own digest_time.
_DEFAULT_DIGEST_TIME = "11:00,21:00"
