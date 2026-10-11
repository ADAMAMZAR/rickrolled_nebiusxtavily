import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr

from app.config import Settings
from scripts.setup_hermes import telegram_env, write_sync_script

HERMES = Path(__file__).parent.parent / "hermes"
CONFIG = HERMES / "config.example.yaml"


def config(token: str = "", users: str = "") -> Settings:
    return Settings(_env_file=None, telegram_bot_token=SecretStr(token), telegram_allowed_users=users)


def test_telegram_is_optional() -> None:
    assert telegram_env(config()) == {}


def test_telegram_env_for_allowed_users() -> None:
    assert telegram_env(config("999:abc", "123, 456")) == {
        "TELEGRAM_BOT_TOKEN": "999:abc",
        "TELEGRAM_ALLOWED_USERS": "123,456",
        "TELEGRAM_HOME_CHANNEL": "123",  # where the daily briefing goes
    }


@pytest.mark.parametrize("users", ["", "@adam", "123,abc"])
def test_telegram_needs_numeric_user_ids(users: str) -> None:
    with pytest.raises(SystemExit, match="numeric Telegram user id"):
        telegram_env(config("123:abc", users))


def test_telegram_rejects_the_bots_own_id() -> None:
    """The number before ':' in the token is the bot's id. Allowing it lets nobody in."""
    with pytest.raises(SystemExit, match="bot's own id"):
        telegram_env(config("123:abc", "456,123"))


def test_every_platform_gets_continuum_tools_only() -> None:
    """A platform missing here would get Hermes' default tools, terminal included."""
    toolsets = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))["platform_toolsets"]
    assert {"api_server", "cli", "telegram", "cron"} <= set(toolsets)
    assert all(tools == ["continuum"] for tools in toolsets.values())


def test_skills_are_named_after_their_folders() -> None:
    """Hermes makes each skill's `name` its slash command (/check, /briefing); setup copies the folders."""
    skills = sorted((HERMES / "skills").glob("*/SKILL.md"))
    assert [p.parent.name for p in skills] == ["briefing", "check"]
    for path in skills:
        front = yaml.safe_load(path.read_text(encoding="utf-8").split("---")[1])
        assert front["name"] == path.parent.name and front["description"]


def test_cron_wrapper_runs_sync_with_continuums_python(tmp_path: Path) -> None:
    """Hermes runs cron scripts with its own Python. The wrapper hands over to Continuum's,
    relays stdout (what Telegram gets) and the exit code, and keeps Hermes' paths out."""
    root = tmp_path / "continuum"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "sync.py").write_text(
        "import os, sys\n"
        "print('Resolve “Wait for Sarah’s reply”? ✅')\n"
        "print(os.environ.get('PYTHONPATH', 'clean'), os.path.basename(os.getcwd()), file=sys.stderr)\n"
        "sys.exit(3)\n",
        encoding="utf-8",
    )
    wrapper = write_sync_script(tmp_path / "hermes-scripts", root, sys.executable)
    hermes_env = {**os.environ, "PYTHONPATH": "C:/hermes/site-packages", "PYTHONIOENCODING": "cp1252"}
    done = subprocess.run([sys.executable, str(wrapper)], env=hermes_env, capture_output=True, encoding="utf-8")
    assert done.stdout.strip() == "Resolve “Wait for Sarah’s reply”? ✅"
    assert done.stderr.strip() == "clean continuum"
    assert done.returncode == 3


def test_cron_wrapper_explains_a_moved_project(tmp_path: Path) -> None:
    wrapper = write_sync_script(tmp_path, tmp_path / "moved", str(tmp_path / "moved" / "python.exe"))
    done = subprocess.run([sys.executable, str(wrapper)], capture_output=True, encoding="utf-8")
    assert done.returncode == 1 and "Re-run scripts/setup_hermes.py" in done.stderr
