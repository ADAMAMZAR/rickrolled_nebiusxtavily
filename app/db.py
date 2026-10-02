"""SQLite tables. Timestamps are timezone-aware UTC."""

from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import Engine, inspect
from sqlmodel import Field, SQLModel, create_engine


def utcnow() -> datetime:
    return datetime.now(UTC)


class GoalStatus(StrEnum):
    active = "active"
    done = "done"


class LoopKind(StrEnum):
    task = "task"
    waiting = "waiting"
    commitment = "commitment"


class LoopStatus(StrEnum):
    open = "open"
    resolved = "resolved"


class Source(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    kind: str = "chat"
    text: str
    created_at: datetime = Field(default_factory=utcnow)


class Goal(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    title: str
    description: str | None = None
    status: GoalStatus = GoalStatus.active
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class OpenLoop(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    goal_id: UUID | None = Field(default=None, foreign_key="goal.id", index=True)
    source_id: UUID = Field(foreign_key="source.id")
    title: str
    summary: str
    kind: LoopKind
    waiting_on: str | None = None
    due: date | None = None
    next_action: str | None = None
    snoozed_until: date | None = None  # hidden while this is after today
    status: LoopStatus = Field(default=LoopStatus.open, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    resolved_at: datetime | None = None


def create_db_engine(url: str) -> Engine:
    """Create the engine and any missing tables. There are no migrations, so an older file fails here."""
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    db = inspect(engine)
    for table in SQLModel.metadata.sorted_tables:
        existing = {c["name"] for c in db.get_columns(table.name)}
        missing = [c.name for c in table.columns if c.name not in existing]
        if missing:
            engine.dispose()
            raise RuntimeError(
                f"{engine.url.database} is from an older version of Continuum (no column {table.name}.{missing[0]}). "
                "Delete it and restart; a new one is created."
            )
    return engine
