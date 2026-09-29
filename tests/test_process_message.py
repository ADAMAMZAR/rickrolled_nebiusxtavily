from datetime import date
from typing import Any

import pytest
from sqlmodel import Session, select

from app import engine as eng
from app.db import Goal, LoopStatus, OpenLoop, Source
from app.extraction import ExtractionError
from tests.conftest import NOW, FakeLLM

DEMO = "I'm applying for NVIDIA. Sarah said she'll get back to me next Friday. I need to finish my portfolio."


def result(
    goals: list[dict[str, Any]] | None = None,
    new: list[dict[str, Any]] | None = None,
    updated: list[dict[str, Any]] | None = None,
    resolved: list[str] | None = None,
) -> dict[str, Any]:
    return {"goals": goals or [], "new_loops": new or [], "updated_loops": updated or [], "resolved_loop_ids": resolved or []}


def loop(title: str, kind: str = "task", **extra: Any) -> dict[str, Any]:
    return {"title": title, "summary": f"{title}.", "kind": kind, **extra}


DEMO_RESULT = result(
    goals=[{"title": "Secure NVIDIA internship"}],
    new=[
        loop("Wait for Sarah's response", "waiting", waiting_on="Sarah", due="2026-10-02", goal="Secure NVIDIA internship"),
        loop("Finish portfolio", goal="Secure NVIDIA internship"),
    ],
)


def process(session: Session, reply: dict[str, Any], text: str = DEMO) -> eng.ChangeSet:
    return eng.process_message(session, FakeLLM(reply), text, now=NOW)


def test_demo_message_creates_linked_goal_and_loops(session: Session) -> None:
    changes = process(session, DEMO_RESULT)

    assert [g.title for g in changes.created_goals] == ["Secure NVIDIA internship"]
    goal_id = changes.created_goals[0].id
    assert {l.title for l in changes.created_loops} == {"Wait for Sarah's response", "Finish portfolio"}
    assert all(l.goal_id == goal_id and l.source_id == changes.source.id for l in changes.created_loops)

    wait = eng.get_loop(session, changes.created_loops[0].id)
    assert wait.loop.due == date(2026, 10, 2)
    assert wait.source.text == DEMO  # provenance


def test_same_info_twice_does_not_duplicate(session: Session) -> None:
    process(session, DEMO_RESULT)
    # Second time the model ignores rule 4 and re-sends everything, with different casing/punctuation.
    again = result(
        goals=[{"title": "secure nvidia internship."}],
        new=[loop("wait for Sarah's response!", "waiting", waiting_on="sarah", goal="Secure NVIDIA internship")],
    )
    changes = process(session, again)

    assert changes.created_goals == [] and changes.created_loops == []
    assert len(session.exec(select(Goal)).all()) == 1
    assert len(session.exec(select(OpenLoop)).all()) == 2


def test_update_and_resolve_existing_loops(session: Session) -> None:
    first = process(session, DEMO_RESULT)
    wait, portfolio = first.created_loops
    reply = result(
        updated=[{"id": str(portfolio.id), "due": "2026-10-05", "next_action": "Add projects"}],
        resolved=[str(wait.id)],
    )
    changes = process(session, reply, "Sarah replied! Portfolio is due Monday, need to add projects.")

    assert [l.id for l in changes.resolved_loops] == [wait.id]
    assert eng.get_loop(session, wait.id).loop.status == LoopStatus.resolved
    updated = eng.get_loop(session, portfolio.id).loop
    assert (updated.due, updated.next_action) == (date(2026, 10, 5), "Add projects")
    assert updated.title == "Finish portfolio"  # untouched fields stay


def test_unknown_ids_from_model_are_dropped(session: Session) -> None:
    process(session, DEMO_RESULT)
    reply = result(updated=[{"id": "made-up", "due": "2026-12-01"}], resolved=["also-made-up"])
    changes = process(session, reply, "whatever")

    assert changes.updated_loops == [] and changes.resolved_loops == []
    assert len(eng.list_loops(session)) == 2


def test_loop_with_new_goal_title_creates_that_goal(session: Session) -> None:
    changes = process(session, result(new=[loop("Book venue", goal="Run the datathon")]), "Need to book a venue for the datathon.")
    assert [g.title for g in changes.created_goals] == ["Run the datathon"]
    assert changes.created_loops[0].goal_id == changes.created_goals[0].id


def test_prompt_sees_existing_goals_and_loop_ids(session: Session) -> None:
    first = process(session, DEMO_RESULT)
    llm = FakeLLM(result())
    eng.process_message(session, llm, "Any news?", now=NOW)
    user = llm.calls[0][1]["content"]
    assert "Secure NVIDIA internship" in user
    assert str(first.created_loops[0].id) in user


def test_message_with_nothing_to_save_is_not_stored(session: Session) -> None:
    changes = process(session, result(), "I like pizza.")
    assert changes.source is None
    assert session.exec(select(Source)).all() == []


def test_failed_extraction_writes_nothing(session: Session) -> None:
    with pytest.raises(ExtractionError):
        eng.process_message(session, FakeLLM("nope", "still nope"), DEMO, now=NOW)
    assert session.exec(select(Source)).all() == []
    assert session.exec(select(OpenLoop)).all() == []
