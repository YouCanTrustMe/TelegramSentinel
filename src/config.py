from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_api_id: int
    telegram_api_hash: str
    telegram_phone: str

    telegram_bot_token: str
    telegram_supergroup_id: int
    telegram_admin_id: int

    # LLM provider API keys — each a SEPARATE free-tier quota. Per-task model
    # routing + failover across these lives in src/processor/llm_client.py
    # (TASK_ROUTING). Groq is the always-present last resort; the others are
    # optional — a missing key just drops that provider from every chain.
    groq_api_key: str
    cerebras_api_key: str = ""
    mistral_api_key: str = ""
    zhipu_api_key: str = ""

    # Cross-source deduplication (separate provider/endpoint from the text chains).
    gemini_api_key: str = ""
    # Google's free embedding tier collapsed on 2026-09-01: the quota is a shared
    # pool ("global_embed_content_requests_per_minute_per_base_model"), so even our
    # 3-texts-per-20-minutes load 429s about half the time, and a 638-text bench run
    # could not get a single chunk through in 14 minutes — on gemini-embedding-001
    # AND on its successor gemini-embedding-2. mistral-embed did 20 bursts in 13s
    # with zero failures on the key we already hold. Flip back to "gemini" here if
    # Google's free tier ever recovers.
    embed_provider: str = "mistral"
    embed_model: str = "mistral-embed"
    gemini_embed_model: str = "gemini-embedding-001"
    dedup_enabled: bool = True
    # Shadow mode logs would-be duplicates without hiding anything. Enforcement is
    # now live: embeddings only pre-select candidates and an LLM confirms each is
    # the same event before muting (cross_dedup._confirm_mutes), so the 0.86
    # threshold can't silently drop a distinct cross-source story on vocabulary alone.
    dedup_shadow: bool = False
    # Which source of a duplicated story stays visible while the others move into the
    # parenthesised links. A Telegram original is one tap inside the app; an RSS one
    # leaves for a browser, so it wins over the manual sort_order unless turned off.
    primary_prefers_telegram: bool = True
    # EVERY threshold below is on the ACTIVE model's cosine scale and does not carry
    # over to another one: mistral-embed puts unrelated same-category news at ~0.83
    # where gemini-embedding-001 put it near 0.60, so the old 0.82 floor would have
    # unioned almost everything. The values here come from .bench/bench_embed_calibrate.py
    # over 235 prod pairs labelled by an independent LLM judge (2026-09-01). Re-run it
    # before changing the model, never re-use numbers across models.
    #
    # "strong" tier: pairs at/above are obvious same-story candidates (71% precision
    # measured). Pairs in the confirm band [dedup_log_floor, dedup_threshold) are ALSO
    # unioned but lean entirely on the LLM (cross_dedup._confirm_mutes) to decide.
    # Used now only to tag log lines strong/confirm.
    dedup_threshold: float = 0.92
    # Compare against items sent in this window so a story repeated in a later
    # digest is still caught. 48h (was 24h) covers a story that resurfaces a day
    # or two later, which a 24h window missed.
    dedup_window_hours: int = 48
    # Union floor: any cross-source pair at/above this cosine becomes an LLM-confirmed
    # candidate. Below it pairs are never even considered. On mistral-embed 0.86 catches
    # 88% of true duplicates at ~41 candidate pairs per digest — the same candidate
    # volume the old 0.82 gemini floor produced, so B1's token budget is unchanged.
    # Raising it to 0.88 would halve the candidates but drop recall to 79%.
    dedup_log_floor: float = 0.86
    # Texts per rolling minute. Mistral's free tier absorbed 20 back-to-back calls in
    # 13s (median 0.35s), so this ceiling is politeness, not a measured limit.
    embed_rpm: int = 300

    # Within-source merge: embeddings (reusing dedup vectors) pre-filter candidate
    # clusters, then the LLM decides the real same-event grouping. Embeddings alone
    # over-merge in high-overlap domains (e.g. different strikes share vocabulary),
    # so the LLM stays the arbiter. Flip off to fall back to the old all-source path.
    merge_via_embeddings: bool = True
    # Items below this are never even candidates (no LLM call); the LLM only sees
    # plausibly-related pairs.
    merge_prefilter_threshold: float = 0.88
    # Near-identical items (>= this) are merged without an LLM call, so this one must
    # not be wrong: 0.975 is the lowest cosine at which the labelled set has ZERO false
    # pairs (100% precision). It fires rarely by design — reposts, not rewrites.
    merge_near_dup_threshold: float = 0.975

    digest_timezone: str = "Europe/Berlin"

    database_path: str = "data/sentinel.db"


settings = Settings()
