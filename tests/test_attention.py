"""Phase 2: snooze + attention rules. `today` is fixed, so results don't depend on the real date."""

from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import Session

from app import engine as eng
from app.config import settings
from app.db import LoopKind, OpenLoop
from tests.conftest import FakeLLM

TODAY = date(2026, 10, 2)
DAY = timedelta(days=1)


def add(
    session: Session,
    title: str,
    kind: LoopKind = LoopKind.task,
    idle_days: int = 0,
    snoozed_until: date | None = None,
    **extra: Any,
) -> OpenLoop:
    """An open loop last updated `idle_days` before TODAY (noon, user's timezone)."""
    source = eng.add_source(session, title)
    loop = eng.add_loop(session, source_id=source.id, title=title, summary=title, kind=kind, **extra)
    loop.updated_at = datetime.combine(TODAY - idle_days * DAY, time(12), tzinfo=ZoneInfo(settings.timezone))
    loop.snoozed_until = snoozed_until
    session.commit()
    return loop


def attention(session: Session) -> list[tuple[str, str]]:
    return [(a.loop.title, a.reason) for a in eng.needs_attention(session, TODAY, stale_days=4)]


def test_each_rule_fires_in_order_with_boundaries(session: Session) -> None:
    add(session, "Stale wait", LoopKind.waiting, idle_days=4, waiting_on="Sarah")  # exactly STALE_DAYS
    add(session, "Stale promise", LoopKind.commitment, idle_days=6)
    add(session, "Fresh task", idle_days=3)  # one day short
    add(session, "Due later", due=TODAY + 2 * DAY, idle_days=10)  # dated loops never go stale
    add(session, "Due tomorrow", due=TODAY + DAY)
    add(session, "Due today", due=TODAY)
    add(session, "Overdue 1", due=TODAY - DAY)
    add(session, "Overdue 3", due=TODAY - 3 * DAY)

    assert attention(session) == [
        ("Overdue 3", "overdue by 3 days"),
        ("Overdue 1", "overdue by 1 day"),
        ("Due today", "due today"),
        ("Due tomorrow", "due tomorrow"),
        ("Stale promise", "no update in 6 days"),
        ("Stale wait", "waiting 4 days, no reply"),
    ]


def test_snoozed_and_resolved_loops_are_skipped(session: Session) -> None:
    snoozed = add(session, "Snoozed", due=TODAY - DAY, snoozed_until=TODAY + DAY)
    add(session, "Snooze ended", due=TODAY - DAY, snoozed_until=TODAY)  # back on that date
    done = add(session, "Done", due=TODAY - DAY)
    eng.resolve_loop(session, done.id)

    assert attention(session) == [("Snooze ended", "overdue by 1 day")]
    assert snoozed.status == "open"


def test_snooze_hides_until_the_date_and_resets_stale_timer(session: Session) -> None:
    loop = add(session, "Finish portfolio", idle_days=5)
    before = loop.updated_at
    assert attention(session) == [("Finish portfolio", "no update in 5 days")]

    snoozed = eng.snooze_loop(session, loop.id, TODAY + 3 * DAY, today=TODAY)
    assert snoozed.snoozed_until == TODAY + 3 * DAY and snoozed.updated_at > before
    assert eng.list_loops(session, include_snoozed=False, today=TODAY) == []
    assert [l.id for l in eng.list_loops(session, include_snoozed=False, today=TODAY + 3 * DAY)] == [loop.id]
    assert [l.id for l in eng.list_loops(session)] == [loop.id]  # still open; shown when asked

    unsnoozed = eng.unsnooze_loop(session, loop.id)
    assert unsnoozed.snoozed_until is None
    assert [l.id for l in eng.list_loops(session, include_snoozed=False, today=TODAY)] == [loop.id]
    assert attention(session) == []  # updated_at moved, so it isn't stale anymore


def test_snooze_date_must_be_after_today(session: Session) -> None:
    loop = add(session, "Task")
    with pytest.raises(eng.InvalidRequest):
        eng.snooze_loop(session, loop.id, TODAY, today=TODAY)
    with pytest.raises(eng.NotFound):
        eng.snooze_loop(session, uuid4(), TODAY + DAY, today=TODAY)


def test_snoozed_loops_still_reach_extraction(session: Session) -> None:
    """So "Alex sent the dataset" can resolve a snoozed loop instead of duplicating it."""
    loop = add(session, "Get dataset from Alex", LoopKind.waiting, waiting_on="Alex", snoozed_until=TODAY + 5 * DAY)
    llm = FakeLLM({"resolved_loop_ids": [str(loop.id)]})
    changes = eng.process_message(session, llm, "Alex sent the dataset.")
    assert [l.id for l in changes.resolved_loops] == [loop.id]
