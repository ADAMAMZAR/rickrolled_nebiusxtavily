from datetime import date

import pytest
from sqlmodel import Session

from app import engine as eng
from app.db import ActivityBy, GoalStatus, LoopStatus, OpenLoop
from tests.test_process_message import DEMO_RESULT, process, result


def demo(session: Session) -> tuple[OpenLoop, OpenLoop]:
    wait, portfolio = process(session, DEMO_RESULT).created_loops
    return wait, portfolio


def history(session: Session, loop: OpenLoop) -> list[tuple[str, str, bool]]:
    return sorted((a.action.value, a.by.value, a.undo is not None) for a in eng.list_activity(session, loop.id))


def test_chat_changes_are_logged_with_undo(session: Session) -> None:
    wait, _ = demo(session)
    process(session, result(updated=[{"id": str(wait.id), "due": "2026-10-09"}]), "Sarah moved it to next Friday.")
    process(session, result(resolved=[str(wait.id)]), "Sarah got back to me.")
    assert history(session, wait) == [("created", "chat", False), ("resolved", "chat", True), ("updated", "chat", True)]


def test_undo_reopens_a_loop_resolved_by_chat(session: Session) -> None:
    wait, _ = demo(session)
    process(session, result(resolved=[str(wait.id)]), "Sarah got back to me.")
    resolved = next(a for a in eng.list_activity(session, wait.id) if a.action == "resolved")
    loop = eng.undo_activity(session, resolved.id)
    assert (loop.status, loop.resolved_at) == (LoopStatus.open, None)
    with pytest.raises(eng.InvalidRequest, match="can't be undone"):
        eng.undo_activity(session, resolved.id)  # once only


def test_undo_restores_the_previous_due_date(session: Session) -> None:
    wait, _ = demo(session)
    process(session, result(updated=[{"id": str(wait.id), "due": "2026-10-09"}]), "Moved to next Friday.")
    updated = next(a for a in eng.list_activity(session, wait.id) if a.action == "updated")
    assert eng.undo_activity(session, updated.id).due == date(2026, 10, 2)


def test_user_changes_are_logged_but_not_undoable(session: Session) -> None:
    wait, _ = demo(session)
    eng.resolve_loop(session, wait.id)
    eng.reopen_loop(session, wait.id)
    assert ("resolved", "user", False) in history(session, wait)
    assert ("reopened", "user", False) in history(session, wait)


def test_snooze_from_chat_can_be_undone(session: Session) -> None:
    wait, _ = demo(session)
    eng.snooze_loop(session, wait.id, date(2026, 10, 5), today=date(2026, 9, 29), by=ActivityBy.chat)
    snoozed = next(a for a in eng.list_activity(session, wait.id) if a.by == "chat" and a.action == "updated")
    assert eng.undo_activity(session, snoozed.id).snoozed_until is None


def test_delete_removes_the_loops_history(session: Session) -> None:
    wait, _ = demo(session)
    eng.delete_loop(session, wait.id)
    assert eng.list_activity(session, wait.id) == []


def test_undo_under_a_done_goal_makes_the_goal_active(session: Session) -> None:
    """Like Reopen: an open loop under a done goal would be hidden from the LLM's goal list."""
    wait, portfolio = demo(session)
    process(session, result(resolved=[str(wait.id)]), "Sarah got back to me.")
    eng.complete_goal(session, portfolio.goal_id)
    resolved = next(a for a in eng.list_activity(session, wait.id) if a.by == "chat" and a.action == "resolved")
    eng.undo_activity(session, resolved.id)
    assert eng.get_loop(session, wait.id).goal.status == GoalStatus.active
    assert ("reopened", "user", False) in history(session, wait)


def test_same_values_again_are_not_an_update(session: Session) -> None:
    """The model often repeats a loop's current due date. That's no change, so no history row with Undo."""
    wait, _ = demo(session)
    changes = process(session, result(updated=[{"id": str(wait.id), "due": "2026-10-02"}]), "Sarah still says Friday.")
    assert changes.updated_loops == [] and changes.source is None
    assert history(session, wait) == [("created", "chat", False)]


def test_resolving_twice_logs_once(session: Session) -> None:
    wait, _ = demo(session)
    process(session, result(resolved=[str(wait.id), str(wait.id)]), "Sarah got back to me.")
    eng.resolve_loop(session, wait.id, by=ActivityBy.chat)  # Hermes resolving it again
    eng.reopen_loop(session, eng.reopen_loop(session, wait.id).id)  # reopening an open loop changes nothing
    assert history(session, wait) == [("created", "chat", False), ("reopened", "user", False), ("resolved", "chat", True)]
