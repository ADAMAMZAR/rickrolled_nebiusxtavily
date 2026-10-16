from datetime import date

import pytest
from pydantic import ValidationError
from sqlmodel import Session

from app import engine as eng
from app.db import ActionStatus, LoopStatus
from tests.test_activity import demo, history

PAGE = "https://devpost.com/hackathon/winners"


def test_approve_resolves_and_logs_web_activity(session: Session) -> None:
    wait, _ = demo(session)
    action = eng.propose_loop_update(session, wait.id, eng.LoopUpdate(resolve=True, source_url=PAGE))
    assert eng.get_loop(session, wait.id).loop.status == LoopStatus.open  # nothing changes before the yes
    assert eng.describe_action(session, action).startswith("Resolve \"Wait for Sarah's response\"?")

    assert eng.approve_action(session, action.id).status == ActionStatus.done
    assert eng.get_loop(session, wait.id).loop.status == LoopStatus.resolved
    web = next(a for a in eng.list_activity(session, wait.id) if a.by == "web")
    assert web.detail == PAGE
    assert eng.undo_activity(session, web.id).status == LoopStatus.open


def test_approve_twice_does_nothing_twice(session: Session) -> None:
    wait, _ = demo(session)
    action = eng.propose_loop_update(session, wait.id, eng.LoopUpdate(due=date(2026, 10, 9), source_url=PAGE))
    assert eng.describe_action(session, action).startswith("Set \"Wait for Sarah's response\" due Fri Oct 9?")
    eng.approve_action(session, action.id)
    with pytest.raises(eng.InvalidRequest, match="already done"):
        eng.approve_action(session, action.id)
    assert [a for a in history(session, wait) if a[1] == "web"] == [("updated", "web", True)]


def test_a_stale_approve_from_another_session_does_nothing(engine, session: Session) -> None:
    """Double click, or dashboard and Telegram at once: both requests read "proposed" before either writes."""
    wait, _ = demo(session)
    action = eng.propose_loop_update(session, wait.id, eng.LoopUpdate(due=date(2026, 10, 9), source_url=PAGE))
    with Session(engine) as other:
        eng._get_action(other, action.id)  # the second request has already loaded it as "proposed"
        eng.approve_action(session, action.id)
        with pytest.raises(eng.InvalidRequest, match="already done"):
            eng.approve_action(other, action.id)
    assert [a for a in history(session, wait) if a[1] == "web"] == [("updated", "web", True)]


def test_reject_never_applies(session: Session) -> None:
    wait, _ = demo(session)
    action = eng.propose_loop_update(session, wait.id, eng.LoopUpdate(resolve=True, source_url=PAGE))
    assert eng.reject_action(session, action.id).status == ActionStatus.rejected
    with pytest.raises(eng.InvalidRequest, match="already rejected"):
        eng.approve_action(session, action.id)
    assert eng.get_loop(session, wait.id).loop.status == LoopStatus.open


def test_failure_marks_failed(session: Session) -> None:
    wait, _ = demo(session)
    action = eng.propose_loop_update(session, wait.id, eng.LoopUpdate(resolve=True, source_url=PAGE))
    eng.resolve_loop(session, wait.id)  # resolved some other way before the yes
    with pytest.raises(eng.InvalidRequest, match="already resolved"):
        eng.approve_action(session, action.id)
    assert eng.list_actions(session, ActionStatus.failed)[0].id == action.id
    assert [a for a in history(session, wait) if a[1] == "web"] == []


def test_a_proposal_must_change_something() -> None:
    with pytest.raises(ValidationError):
        eng.LoopUpdate(source_url=PAGE)


def test_a_proposal_links_only_to_web_pages() -> None:
    with pytest.raises(ValidationError):
        eng.LoopUpdate(resolve=True, source_url="javascript:alert(1)")


def test_the_same_proposal_is_stored_once(session: Session) -> None:
    wait, _ = demo(session)
    first = eng.propose_loop_update(session, wait.id, eng.LoopUpdate(resolve=True, source_url=PAGE))
    again = eng.propose_loop_update(session, wait.id, eng.LoopUpdate(resolve=True, source_url=PAGE + "?ref=2"))
    assert again.id == first.id and len(eng.list_actions(session)) == 1


def test_no_proposals_for_resolved_or_missing_loops(session: Session) -> None:
    wait, _ = demo(session)
    eng.resolve_loop(session, wait.id)
    with pytest.raises(eng.InvalidRequest, match="already resolved"):
        eng.propose_loop_update(session, wait.id, eng.LoopUpdate(resolve=True, source_url=PAGE))
    eng.delete_loop(session, wait.id)
    with pytest.raises(eng.NotFound):
        eng.propose_loop_update(session, wait.id, eng.LoopUpdate(resolve=True, source_url=PAGE))


def test_delete_removes_the_loops_proposals(session: Session) -> None:
    wait, _ = demo(session)
    eng.propose_loop_update(session, wait.id, eng.LoopUpdate(resolve=True, source_url=PAGE))
    eng.delete_loop(session, wait.id)
    assert eng.list_actions(session, None) == []


def test_undoing_a_web_due_change_only_restores_the_due_date(session: Session) -> None:
    wait, _ = demo(session)
    action = eng.propose_loop_update(session, wait.id, eng.LoopUpdate(due=date(2026, 10, 9), source_url=PAGE))
    eng.approve_action(session, action.id)
    eng.resolve_loop(session, wait.id)  # the user closes it later
    web = next(a for a in eng.list_activity(session, wait.id) if a.by == "web")
    loop = eng.undo_activity(session, web.id)
    assert (loop.due, loop.status) == (date(2026, 10, 2), LoopStatus.resolved)
