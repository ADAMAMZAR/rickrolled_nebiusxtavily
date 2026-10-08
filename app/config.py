from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # NVIDIA Nemotron on Nebius Token Factory, for both extraction and Hermes.
    nebius_api_key: SecretStr = SecretStr("")
    nebius_base_url: str = "https://api.tokenfactory.nebius.com/v1/"
    nebius_model: str = "nvidia/nemotron-3-super-120b-a12b"
    # No Nemotron model on Token Factory reads images, so screenshots alone go here (Phase 4 §0.5).
    nebius_vision_model: str = "google/gemma-3-27b-it"
    llm_timeout: float = 60

    hermes_api_url: str = "http://127.0.0.1:8642/v1"
    hermes_api_key: SecretStr = SecretStr("")  # written by scripts/setup_hermes.py
    hermes_timeout: float = 120
    # Telegram via Hermes. Read only by scripts/setup_hermes.py, which copies them into the Hermes profile.
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_allowed_users: str = ""  # comma-separated numeric Telegram user ids
    tavily_api_key: SecretStr = SecretStr("")  # optional: web watch + lookup
    tavily_cache_dir: str = "./data/tavily_cache"  # investigations: last good reply per request, used if Tavily fails
    # Google (optional): read mail from linked people, Gmail drafts, Calendar events. README: Google setup.
    google_client_secret_file: str = "./data/client_secret.json"
    google_token_file: str = "./data/google_token.json"  # written when you connect; never commit it
    sync_lookback_days: int = 7  # how far back the first email sync reads
    uploads_dir: str = "./data/uploads"  # investigation screenshots; never served publicly
    timezone: str = "Asia/Kuala_Lumpur"
    stale_days: int = 4  # days without an update before an undated loop needs attention
    database_url: str = "sqlite:///./data/continuum.db"
    log_level: str = "INFO"


settings = Settings()
