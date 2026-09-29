"""Set up the Hermes "continuum" profile from .env and the hermes/ folder.

Usage: python scripts/setup_hermes.py
Safe to re-run, e.g. after changing LLM_PROVIDER. Your default Hermes profile is not touched.
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

from app.config import settings

ROOT = Path(__file__).resolve().parent.parent
PROFILE = "continuum"


def hermes_root() -> Path:
    if home := os.environ.get("HERMES_HOME"):
        return Path(home)
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "hermes"
    return Path.home() / ".hermes"


def provider() -> tuple[dict[str, str], str, str]:
    """Hermes model block, key env var name, key value."""
    if settings.llm_provider == "deepseek":
        model = {"provider": "deepseek", "default": settings.deepseek_model, "base_url": settings.deepseek_base_url}
        return model, "DEEPSEEK_API_KEY", settings.deepseek_api_key.get_secret_value()
    model = {"provider": "nebius", "default": settings.nebius_model, "base_url": settings.nebius_base_url}
    return model, "NEBIUS_API_KEY", settings.nebius_api_key.get_secret_value()


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
    model, key_name, key = provider()
    if not key:
        sys.exit(f"{key_name} is empty in .env. Add it, then re-run.")
    hermes = shutil.which("hermes")
    if hermes is None:
        sys.exit("Hermes isn't installed. See https://hermes-agent.nousresearch.com")

    profile_dir = hermes_root() / "profiles" / PROFILE
    if not (profile_dir / "config.yaml").exists():
        subprocess.run([hermes, "profile", "create", PROFILE], check=True)

    config_path = profile_dir / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    template = yaml.safe_load((ROOT / "hermes" / "config.example.yaml").read_text(encoding="utf-8"))
    template["model"] = model
    config_path.write_text(yaml.safe_dump(merge(config, template), sort_keys=False), encoding="utf-8")
    shutil.copyfile(ROOT / "hermes" / "SOUL.md", profile_dir / "SOUL.md")

    api_key = settings.hermes_api_key.get_secret_value() or secrets.token_urlsafe(24)
    port = str(urlsplit(settings.hermes_api_url).port or 8642)
    set_env(
        profile_dir / ".env",
        {key_name: key, "API_SERVER_ENABLED": "true", "API_SERVER_KEY": api_key, "API_SERVER_PORT": port},
    )
    set_env(ROOT / ".env", {"HERMES_API_KEY": api_key})

    print(f"Hermes profile '{PROFILE}' ready at {profile_dir}")
    print(f"Model: {model['provider']} / {model['default']}")
    print("Next: start Continuum (uvicorn app.main:app), then: hermes -p continuum gateway run")


if __name__ == "__main__":
    main()
