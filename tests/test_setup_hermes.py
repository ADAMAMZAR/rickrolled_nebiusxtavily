from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr

from app.config import Settings
from scripts.setup_hermes import telegram_env

CONFIG = Path(__file__).parent.parent / "hermes" / "config.example.yaml"


def config(token: str = "", users: str = "") -> Settings:
    return Settings(_env_file=None, telegram_bot_token=SecretStr(token), telegram_allowed_users=users)


def test_telegram_is_optional() -> None:
    assert telegram_env(config()) == {}


def test_telegram_env_for_allowed_users() -> None:
    assert telegram_env(config("123:abc", "123, 456")) == {
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "TELEGRAM_ALLOWED_USERS": "123,456",
    }


@pytest.mark.parametrize("users", ["", "@adam", "123,abc"])
def test_telegram_needs_numeric_user_ids(users: str) -> None:
    with pytest.raises(SystemExit, match="numeric Telegram user id"):
        telegram_env(config("123:abc", users))


def test_every_platform_gets_continuum_tools_only() -> None:
    """A platform missing here would get Hermes' default tools, terminal included."""
    toolsets = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))["platform_toolsets"]
    assert {"api_server", "cli", "telegram"} <= set(toolsets)
    assert all(tools == ["continuum"] for tools in toolsets.values())
