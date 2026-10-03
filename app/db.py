"""SQLite tables. Timestamps are timezone-aware UTC."""

from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, Column, Engine, inspect
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


class ActivityAction(StrEnum):
    created = "created"
    updated = "updated"
    resolved = "resolved"
    reopened = "reopened"
    action_done = "action_done"


class ActivityBy(StrEnum):
    user = "user"
    chat = "chat"
    email = "email"
    web = "web"


class ActionKind(StrEnum):
    gmail_draft = "gmail_draft"
    calendar_event = "calendar_event"
    loop_update = "loop_update"  # resolve or a new due date, found on the web


class ActionStatus(StrEnum):
    proposed = "proposed"
    done = "done"
    rejected = "rejected"
    failed = "failed"


class Source(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    kind: str = "chat"
    text: str
    external_id: str | None = Field(default=None, unique=True)  # Gmail message id: makes sync idempotent
    url: str | None = None  # link to the Gmail thread
    sender: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Person(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str
    email: str | None = None


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
    person_id: UUID | None = Field(default=None, foreign_key="person.id", index=True)
    watch_query: str | None = None  # set = check the web for this loop
    watch_checked_on: date | None = None
    status: LoopStatus = Field(default=LoopStatus.open, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    resolved_at: datetime | None = None


class Activity(SQLModel, table=True):
    """What changed a loop, and how to undo it."""

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    loop_id: UUID | None = Field(default=None, foreign_key="openloop.id", index=True)
    action: ActivityAction
    by: ActivityBy
    detail: str = ""
    undo: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))  # previous values; None = can't undo
    created_at: datetime = Field(default_factory=utcnow)


class PendingAction(SQLModel, table=True):
    """Something Continuum wants to do. Runs only after the user's yes."""

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    loop_id: UUID | None = Field(default=None, foreign_key="openloop.id", index=True)
    kind: ActionKind
    payload: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    status: ActionStatus = Field(default=ActionStatus.proposed, index=True)
    external_id: str | None = None  # created draft/event id
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str


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
