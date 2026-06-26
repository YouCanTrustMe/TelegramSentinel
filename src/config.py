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

    # Cross-source deduplication (separate provider/endpoint from Groq — see docs).
    gemini_api_key: str = ""
    gemini_embed_model: str = "gemini-embedding-001"
    dedup_enabled: bool = True
    # Shadow mode logs would-be duplicates without hiding anything. Enforcement is
    # now live: embeddings only pre-select candidates and an LLM confirms each is
    # the same event before muting (cross_dedup._confirm_mutes), so the 0.86
    # threshold can't silently drop a distinct cross-source story on vocabulary alone.
    dedup_shadow: bool = False
    # "strong" tier: pairs at/above embed as obvious same-story candidates. Pairs in
    # the confirm band [dedup_log_floor, dedup_threshold) are ALSO unioned but lean
    # entirely on the LLM (cross_dedup._confirm_mutes) to decide — Ukrainian war-news
    # rephrasings of one event routinely land at 0.80–0.86, so gating union at 0.86
    # silently dropped real merges. Used now only to tag log lines strong/confirm.
    dedup_threshold: float = 0.86
    # Compare against items sent in this window so a story repeated in a later
    # digest is still caught. 48h (was 24h) covers a story that resurfaces a day
    # or two later, which a 24h window missed.
    dedup_window_hours: int = 48
    # Union floor: any cross-source pair at/above this cosine becomes an LLM-confirmed
    # candidate. Below it pairs are never even considered. Raised 0.80→0.82 (2026-06-24)
    # to thin the B1 candidate flood (45–78/digest) that was the dominant consumer of
    # the batch model's daily token quota: when 70b's TPD died mid-digest, B1 failed
    # over to the conservative 8b (which refuses to merge) and cross-source duplicates
    # slipped through. A higher floor keeps B1 on the reliable 70b. The 0.82–0.86 band
    # of same-event war-news rephrasings is still caught (the LLM remains the arbiter).
    dedup_log_floor: float = 0.82
    # Gemini free embedding tier allows ~100 texts/min over a rolling 60s window;
    # keep steady throughput below it so a big digest's tail doesn't 429.
    embed_rpm: int = 70

    # Within-source merge: embeddings (reusing dedup vectors) pre-filter candidate
    # clusters, then the LLM decides the real same-event grouping. Embeddings alone
    # over-merge in high-overlap domains (e.g. different strikes share vocabulary),
    # so the LLM stays the arbiter. Flip off to fall back to the old all-source path.
    merge_via_embeddings: bool = True
    # Items below this are never even candidates (no LLM call); the LLM only sees
    # plausibly-related pairs.
    merge_prefilter_threshold: float = 0.85
    # Near-identical items (>= this) are merged without an LLM call — safe because
    # only true paraphrases/reposts reach this similarity.
    merge_near_dup_threshold: float = 0.95

    digest_timezone: str = "Europe/Berlin"

    database_path: str = "data/sentinel.db"


settings = Settings()
