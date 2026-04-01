from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Telegram
    telegram_api_id: int
    telegram_api_hash: str
    telethon_session: str = ""
    telegram_bot_token: str
    telegram_owner_id: int

    # AI
    ai_provider: str = "openrouter"  # gemini | openrouter | groq
    ai_model: str = "openrouter/free"
    ai_fallback_provider: str = "groq"
    ai_fallback_model: str = "llama-3.3-70b-versatile"

    gemini_api_key: str = ""
    openrouter_api_key: str = ""
    groq_api_key: str = ""

    # Database
    database_url: str

    # App
    log_level: str = "INFO"
    digest_hour: int = 9
    timezone: str = "Europe/Kyiv"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()  # type: ignore[call-arg]
