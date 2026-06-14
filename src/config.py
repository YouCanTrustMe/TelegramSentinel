from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_api_id: int
    telegram_api_hash: str
    telegram_phone: str

    telegram_bot_token: str
    telegram_supergroup_id: int
    telegram_admin_id: int

    groq_api_key: str
    # single-item summaries (high volume, plain {summary,key_phrase} JSON, no id arrays)
    groq_model_classify: str = "openai/gpt-oss-120b"
    # batch summarise + topic grouping (returns id arrays — needs a model that emits them reliably)
    groq_model_batch: str = "llama-3.3-70b-versatile"
    # shared safety net: whichever model's daily quota dies, calls fall over to this
    groq_model_fallback: str = "llama-3.1-8b-instant"

    # Cross-source deduplication (separate provider/endpoint from Groq — see docs).
    gemini_api_key: str = ""
    gemini_embed_model: str = "gemini-embedding-001"
    dedup_enabled: bool = True
    # Shadow mode logs would-be duplicates without hiding anything; flip off once
    # the threshold is validated against real digests.
    dedup_shadow: bool = True
    dedup_threshold: float = 0.86
    dedup_window_hours: int = 24
    # Tuning window: log every cross-source candidate pair at/above this cosine
    # (wider than dedup_threshold) so a week of logs can pin the ideal threshold.
    dedup_log_floor: float = 0.80
    # Gemini free embedding tier allows ~100 texts/min; cap to stay under it.
    embed_rpm: int = 90

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
