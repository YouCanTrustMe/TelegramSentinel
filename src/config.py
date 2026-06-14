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

    digest_timezone: str = "Europe/Berlin"

    database_path: str = "data/sentinel.db"


settings = Settings()
