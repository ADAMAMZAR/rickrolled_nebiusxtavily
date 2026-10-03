import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlmodel import Session

from app import engine as eng
from app.config import settings
from app.db import create_db_engine


# Tuesday evening in the user's timezone: "tomorrow" = 2026-09-30, "Friday" = 2026-10-02.
NOW = datetime(2026, 9, 29, 21, 0, tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))
# The demo's due date: Sarah's reply is "due today".
FRIDAY = datetime(2026, 10, 2, 9, 0, tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))


class FakeLLM:
    """Returns queued replies (dicts become JSON) and records every prompt."""

    def __init__(self, *replies: dict[str, Any] | str) -> None:
        self.replies = [r if isinstance(r, str) else json.dumps(r) for r in replies]
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, messages: list[dict[str, str]], json_mode: bool = False) -> str:
        self.calls.append(messages)
        return self.replies.pop(0)


@pytest.fixture
def on_friday(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze the user's "now" at FRIDAY for code that reads today's date (attention, snooze)."""
    monkeypatch.setattr(eng, "local_now", lambda: FRIDAY)


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def engine(db_url: str) -> Iterator[Engine]:
    engine = create_db_engine(db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        yield session


@pytest.fixture
def web_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Web features need TAVILY_API_KEY. Tests never call the real Tavily."""
    monkeypatch.setattr(settings, "tavily_api_key", SecretStr("tvly-test"))


@pytest.fixture
def web_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tavily_api_key", SecretStr(""))
