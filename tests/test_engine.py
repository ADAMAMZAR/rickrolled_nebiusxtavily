import sqlite3
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app import engine as eng
from app.db import (
    ActionKind, ActionStatus, Activity, ActivityAction, ActivityBy, Goal, GoalStatus, LoopKind, LoopStatus, OpenLoop,
    PendingAction, Person, Setting, Source, create_db_engine,
)


def seed(session: Session) -> tuple[Goal, OpenLoop, OpenLoop]:
    source = eng.add_source(session, "Sarah said she'll get back to me Friday. Need to finish portfolio.")
    goal = eng.add_goal(session, "Secure NVIDIA internship")
    wait = eng.add_loop(
        session,
        source_id=source.id,
        goal_id=goal.id,
        title="Wait for Sarah's response",
        summary="Sarah will respond about the application",
        kind=LoopKind.waiting,
        waiting_on="Sarah",
        due=date(2026, 10, 2),
    )
    task = eng.add_loop(
        session,
        source_id=source.id,
        goal_id=goal.id,
        title="Finish portfolio",
        summary="Finish portfolio before the interview",
        kind=LoopKind.task,
    )
    session.commit()
    return goal, wait, task


def test_goal_lists_open_loop_count(session: Session) -> None:
    goal, wait, _ = seed(session)
    eng.add_goal(session, "Empty goal")
    session.commit()

    summaries = {s.goal.title: s.open_loops for s in eng.list_goals(session)}
    assert summaries == {"Secure NVIDIA internship": 2, "Empty goal": 0}

    eng.resolve_loop(session, wait.id)
    counts = {s.goal.id: s.open_loops for s in eng.list_goals(session)}
    assert counts[goal.id] == 1


def test_list_loops_sorts_dated_first_and_filters(session: Session) -> None:
    goal, wait, task = seed(session)

    assert [l.id for l in eng.list_loops(session)] == [wait.id, task.id]
    assert [l.id for l in eng.list_loops(session, goal_id=goal.id)] == [wait.id, task.id]
    assert eng.list_loops(session, goal_id=uuid4()) == []

    eng.resolve_loop(session, task.id)
    assert [l.id for l in eng.list_loops(session)] == [wait.id]
    assert [l.id for l in eng.list_loops(session, status=LoopStatus.resolved)] == [task.id]
    assert len(eng.list_loops(session, status=None)) == 2


def test_get_loop_includes_goal_and_source(session: Session) -> None:
    goal, wait, _ = seed(session)

    detail = eng.get_loop(session, wait.id)
    assert detail.loop.waiting_on == "Sarah"
    assert detail.goal is not None and detail.goal.id == goal.id
    assert "Sarah said" in detail.source.text
    assert detail.source.kind == "chat"


def test_resolve_then_reopen(session: Session) -> None:
    _, wait, _ = seed(session)
    before = wait.updated_at

    resolved = eng.resolve_loop(session, wait.id)
    assert resolved.status == LoopStatus.resolved
    assert resolved.resolved_at is not None
    assert resolved.updated_at >= before

    reopened = eng.reopen_loop(session, wait.id)
    assert reopened.status == LoopStatus.open
    assert reopened.resolved_at is None


def test_complete_goal_resolves_its_loops_and_reopen_undoes_it(session: Session) -> None:
    goal, wait, task = seed(session)
    eng.resolve_loop(session, wait.id)

    done = eng.complete_goal(session, goal.id)
    assert done.status == GoalStatus.done
    assert eng.list_goals(session) == []
    assert eng.list_loops(session) == []
    assert eng.get_loop(session, task.id).loop.resolved_at is not None

    eng.reopen_loop(session, task.id)
    assert [s.goal.id for s in eng.list_goals(session)] == [goal.id]
    assert [l.id for l in eng.list_loops(session)] == [task.id]  # only the reopened loop


def test_delete_keeps_shared_source(session: Session) -> None:
    _, wait, task = seed(session)

    eng.delete_loop(session, wait.id)
    with pytest.raises(eng.NotFound):
        eng.get_loop(session, wait.id)
    assert eng.get_loop(session, task.id).source.text  # other loop's source still there


def test_missing_loop_raises_not_found(session: Session) -> None:
    for action in (eng.get_loop, eng.resolve_loop, eng.reopen_loop, eng.delete_loop, eng.unsnooze_loop, eng.complete_goal):
        with pytest.raises(eng.NotFound):
            action(session, uuid4())


def test_data_survives_restart(db_url: str) -> None:
    first = create_db_engine(db_url)
    with Session(first) as session:
        _, wait, _ = seed(session)
        wait_id = wait.id
    first.dispose()

    second = create_db_engine(db_url)  # simulates an app restart
    with Session(second) as session:
        detail = eng.get_loop(session, wait_id)
        assert detail.loop.title == "Wait for Sarah's response"
        assert detail.loop.due == date(2026, 10, 2)
        assert detail.loop.kind == LoopKind.waiting
        assert detail.loop.created_at.tzinfo is not None
    second.dispose()


def test_outdated_database_fails_with_clear_message(tmp_path: Path) -> None:
    """create_all doesn't add new columns, so a pre-snooze DB used to give a bare HTTP 500 on every list."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE openloop (id TEXT PRIMARY KEY, title TEXT)")
    old.close()
    with pytest.raises(RuntimeError, match=r"old\.db .*openloop\.\w+.*Delete it"):
        create_db_engine(f"sqlite:///{path}")


def test_phase3_tables_round_trip(session: Session) -> None:
    _, wait, _ = seed(session)
    session.add(Person(name="Sarah", email="sarah@nvidia.com"))
    session.add(Activity(loop_id=wait.id, action=ActivityAction.resolved, by=ActivityBy.chat,
                         undo={"status": "open", "resolved_at": None}))
    session.add(PendingAction(loop_id=wait.id, kind=ActionKind.loop_update,
                              payload={"resolve": True, "source_url": "https://devpost.com/x"}))
    session.add(Setting(key="last_sync", value="2026-10-03T08:00:00+00:00"))
    session.commit()
    session.expire_all()
    assert session.exec(select(Activity)).one().undo == {"status": "open", "resolved_at": None}
    assert session.exec(select(PendingAction)).one().status == ActionStatus.proposed


def test_gmail_message_is_stored_once(session: Session) -> None:
    session.add(Source(text="a", kind="email", external_id="gmail-1"))
    session.commit()
    session.add(Source(text="b", kind="email", external_id="gmail-1"))
    with pytest.raises(IntegrityError):
        session.commit()
