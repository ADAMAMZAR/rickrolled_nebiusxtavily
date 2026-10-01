from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # nebius = the real target (Nemotron). deepseek = dev stand-in until a Nebius key exists.
    llm_provider: Literal["nebius", "deepseek"] = "nebius"
    llm_timeout: float = 60

    nebius_api_key: SecretStr = SecretStr("")
    nebius_base_url: str = "https://api.tokenfactory.nebius.com/v1/"
    nebius_model: str = "nvidia/nemotron-3-super-120b-a12b"

    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"

    hermes_api_url: str = "http://127.0.0.1:8642/v1"
    hermes_api_key: SecretStr = SecretStr("")  # written by scripts/setup_hermes.py
    hermes_timeout: float = 120
    timezone: str = "Asia/Kuala_Lumpur"
    database_url: str = "sqlite:///./data/continuum.db"
    log_level: str = "INFO"


settings = Settings()
