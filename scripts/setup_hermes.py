"""Set up the Hermes "continuum" profile from .env and the hermes/ folder.

Usage: python scripts/setup_hermes.py
Safe to re-run, e.g. after changing keys. Your default Hermes profile is not touched.
Note: rewriting the profile's config.yaml drops its comments.
"""

import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # so `app` imports without installing the package

from app.config import Settings, settings  # noqa: E402

PROFILE = "continuum"
SYNC_SCRIPT = "continuum_sync.py"  # what the sync cron job runs (README: Watch the web)

# Hermes runs cron scripts from its own scripts/ folder with its own Python, so this hands over to
# Continuum's Python. Its stdout is what the cron job delivers; empty stdout sends nothing.
SYNC_WRAPPER = """\"\"\"Runs Continuum's scripts/sync.py for the Hermes cron job. Written by Continuum's
scripts/setup_hermes.py: re-run that after moving the project or its .venv.\"\"\"

import os
import subprocess
import sys

ROOT = {root!r}
PYTHON = {python!r}

# Keep Hermes' Python paths out of Continuum's, and pass text through as UTF-8 (titles can be any language).
env = {{k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}}
env["PYTHONIOENCODING"] = "utf-8"
try:
    done = subprocess.run(
        [PYTHON, os.path.join("scripts", "sync.py")], cwd=ROOT, env=env, capture_output=True, encoding="utf-8", errors="replace"
    )
except OSError as e:  # the project or its .venv moved
    sys.exit(f"Can't run Continuum's sync ({{e}}). Re-run scripts/setup_hermes.py in the Continuum folder.")
for stream, text in ((sys.stdout, done.stdout), (sys.stderr, done.stderr)):
    stream.reconfigure(encoding="utf-8")
    stream.write(text)
sys.exit(done.returncode)
"""


def hermes_root() -> Path:
    if home := os.environ.get("HERMES_HOME"):
        return Path(home)
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "hermes"
    return Path.home() / ".hermes"


def telegram_env(config: Settings) -> dict[str, str]:
    """Profile env for Hermes' Telegram adapter. Empty when Telegram isn't set up (it's optional)."""
    token = config.telegram_bot_token.get_secret_value()
    if not token:
        return {}
    users = config.telegram_allowed_users.replace(" ", "")
    if not users or not all(user.isdigit() for user in users.split(",")):
        sys.exit(
            "TELEGRAM_ALLOWED_USERS must be your numeric Telegram user id (message @userinfobot to get it). "
            "Separate several with commas."
        )
    bot_id = token.split(":", 1)[0]
    if bot_id in users.split(","):
        sys.exit(
            f"TELEGRAM_ALLOWED_USERS has {bot_id}, your bot's own id (the start of the token). "
            "Use your own numeric id from @userinfobot."
        )
    # The daily briefing goes to the home channel: the first user's DM (a DM's chat id is the user id).
    return {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_ALLOWED_USERS": users, "TELEGRAM_HOME_CHANNEL": users.split(",")[0]}


def write_sync_script(scripts_dir: Path, root: Path, python: str) -> Path:
    scripts_dir.mkdir(parents=True, exist_ok=True)
    path = scripts_dir / SYNC_SCRIPT
    path.write_text(SYNC_WRAPPER.format(root=str(root), python=python), encoding="utf-8")
    return path


def merge(base: dict, extra: dict) -> dict:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge(base[key], value)
        else:
            base[key] = value
    return base


def set_env(path: Path, values: dict[str, str]) -> None:
    """Set KEY=value lines in a .env file, keeping every other line."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(values)
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        out.append(f"{key}={pending.pop(key)}" if key in pending else line)
    out += [f"{key}={value}" for key, value in pending.items()]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> None:
    key = settings.nebius_api_key.get_secret_value()
    if not key:
        sys.exit("NEBIUS_API_KEY is empty in .env. Add it, then re-run.")
    model = {"provider": "nebius", "default": settings.nebius_model, "base_url": settings.nebius_base_url}
    telegram = telegram_env(settings)
    hermes = shutil.which("hermes")
    if hermes is None:
        sys.exit("Hermes isn't installed. See https://hermes-agent.nousresearch.com")

    profile_dir = hermes_root() / "profiles" / PROFILE
    if not (profile_dir / "config.yaml").exists():
        subprocess.run([hermes, "profile", "create", PROFILE, "--no-skills"], check=True)
    # Hermes' bundled skills need tools this profile doesn't have; dropping them keeps the Telegram menu to
    # Continuum's own (/check, /briefing). Only unmodified bundled skills are removed.
    subprocess.run([hermes, "-p", PROFILE, "skills", "opt-out", "--remove", "--yes"], check=True, stdout=subprocess.DEVNULL)

    config_path = profile_dir / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    template = yaml.safe_load((ROOT / "hermes" / "config.example.yaml").read_text(encoding="utf-8"))
    template["model"] = model
    template["timezone"] = settings.timezone  # Hermes' clock and cron schedules follow it
    config_path.write_text(yaml.safe_dump(merge(config, template), sort_keys=False), encoding="utf-8")
    shutil.copyfile(ROOT / "hermes" / "SOUL.md", profile_dir / "SOUL.md")
    shutil.copytree(ROOT / "hermes" / "skills", profile_dir / "skills" / "continuum", dirs_exist_ok=True)
    write_sync_script(profile_dir / "scripts", ROOT, sys.executable)  # this Python has Continuum's packages

    api_key = settings.hermes_api_key.get_secret_value() or secrets.token_urlsafe(24)
    port = str(urlsplit(settings.hermes_api_url).port or 8642)
    set_env(
        profile_dir / ".env",
        {"NEBIUS_API_KEY": key, "API_SERVER_ENABLED": "true", "API_SERVER_KEY": api_key, "API_SERVER_PORT": port, **telegram},
    )
    set_env(ROOT / ".env", {"HERMES_API_KEY": api_key})

    print(f"Hermes profile '{PROFILE}' ready at {profile_dir}")
    print(f"Model: {model['provider']} / {model['default']}")
    if telegram:
        print(f"Telegram: on for user(s) {telegram['TELEGRAM_ALLOWED_USERS']}")
    else:
        print("Telegram: off (optional, see README)")
    print("Next: start Continuum (uvicorn app.main:app), then: hermes -p continuum gateway run")


if __name__ == "__main__":
    main()
