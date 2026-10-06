from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # NVIDIA Nemotron on Nebius Token Factory, for both extraction and Hermes.
    nebius_api_key: SecretStr = SecretStr("")
    nebius_base_url: str = "https://api.tokenfactory.nebius.com/v1/"
    nebius_model: str = "nvidia/nemotron-3-super-120b-a12b"
    llm_timeout: float = 60

    hermes_api_url: str = "http://127.0.0.1:8642/v1"
    hermes_api_key: SecretStr = SecretStr("")  # written by scripts/setup_hermes.py
    hermes_timeout: float = 120
    # Telegram via Hermes. Read only by scripts/setup_hermes.py, which copies them into the Hermes profile.
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_allowed_users: str = ""  # comma-separated numeric Telegram user ids
    tavily_api_key: SecretStr = SecretStr("")  # optional: web watch + lookup
    # Google (optional): read mail from linked people, Gmail drafts, Calendar events. README: Google setup.
    google_client_secret_file: str = "./data/client_secret.json"
    google_token_file: str = "./data/google_token.json"  # written when you connect; never commit it
    timezone: str = "Asia/Kuala_Lumpur"
    stale_days: int = 4  # days without an update before an undated loop needs attention
    database_url: str = "sqlite:///./data/continuum.db"
    log_level: str = "INFO"


settings = Settings()
