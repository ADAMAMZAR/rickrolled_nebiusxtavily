from datetime import date

import pytest
from sqlmodel import Session, select

from app import engine as eng
from app.db import ActivityBy, GoalStatus, LoopKind, LoopStatus, OpenLoop, Source
from tests.test_process_message import DEMO, DEMO_RESULT, process, result


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


def test_history_keeps_the_words_behind_each_change(session: Session) -> None:
    """The message that resolved a loop is linked from its history, not stored where nothing reads it."""
    wait, _ = demo(session)
    process(session, result(resolved=[str(wait.id)]), "Sarah got back to me.")
    said = {a.action.value: session.get(Source, a.source_id).text for a in eng.list_activity(session, wait.id)}  # type: ignore[union-attr]
    assert said == {"created": DEMO, "resolved": "Sarah got back to me."}


def test_delete_forgets_the_words_only_it_used(session: Session) -> None:
    wait, portfolio = demo(session)
    process(session, result(resolved=[str(wait.id)]), "Sarah got back to me.")
    eng.delete_loop(session, wait.id)
    assert [s.text for s in session.exec(select(Source)).all()] == [DEMO]  # still behind the portfolio loop
    eng.delete_loop(session, portfolio.id)
    assert session.exec(select(Source)).all() == []


def test_delete_keeps_an_emails_id_but_not_its_text(session: Session) -> None:
    """Sync skips emails it has a Gmail id for, so a deleted loop's email isn't read again."""
    source = Source(kind="email", text="Hi, decision next week.", external_id="m1", sender="Sarah", url="https://x/t1")
    session.add(source)
    loop = eng.add_loop(session, source_id=source.id, title="Wait for Sarah", summary="s", kind=LoopKind.waiting)
    session.commit()
    eng.delete_loop(session, loop.id)
    [kept] = session.exec(select(Source)).all()
    assert (kept.external_id, kept.text, kept.sender, kept.url) == ("m1", "", None, None)
