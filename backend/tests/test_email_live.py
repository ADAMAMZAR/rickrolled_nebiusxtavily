"""Spec §13 email extraction against real Nemotron, with canned emails instead of Gmail.
Run: pytest -m live -k email_live"""

from datetime import date

import pytest
from sqlmodel import Session

from app import engine as eng
from app.config import settings
from app.db import LoopStatus
from app.db import OpenLoop
from app.llm import LLMClient
from tests.conftest import NOW
from tests.test_email import SARAH, FakeMailbox, email, google_on  # noqa: F401 (google_on is a fixture)
from tests.test_process_message import loop, process, result

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not settings.nebius_api_key.get_secret_value(), reason="NEBIUS_API_KEY not set"),
    pytest.mark.usefixtures("google_on"),
]

INTERVIEW = "Hi,\n\nGreat news, we'd like to interview you on Oct 8 at 10am. Let me know if that works.\n\nSarah"
TRANSCRIPT = ("Hi,\n\nThanks for applying. We're still reviewing applications. Before we can move forward, "
              "please send me your latest transcript by October 5.\n\nSarah")
UNRELATED = "Hi all,\n\nOur team won the internal robotics demo day last week. Thanks everyone for cheering us on!\n\nSarah"


def linked(session: Session) -> OpenLoop:
    """The §1 demo loop, as chat extraction would create it."""
    [wait] = process(session, result(new=[loop(
        "Wait for Sarah's response", "waiting", waiting_on="Sarah", due="2026-10-02",
        summary="Sarah will get back to me about my NVIDIA internship application.",
    )])).created_loops
    eng.set_person_email(session, "Sarah", SARAH)
    return wait


def sync(session: Session, body: str) -> list[str]:
    lines, errors = eng.sync_email(session, LLMClient(settings), FakeMailbox({SARAH: [email(body=body)]}), now=NOW)
    assert errors == []
    return lines


def test_a_reply_resolves_and_proposes_the_interview(session: Session) -> None:
    wait = linked(session)
    lines = sync(session, INTERVIEW)
    assert eng.get_loop(session, wait.id).loop.status == LoopStatus.resolved, lines
    starts = [a.payload["start"] for a in eng.list_actions(session)]
    assert len(starts) == 1 and starts[0].startswith("2026-10-08T10:00"), (lines, starts)


def test_a_new_deadline_adds_a_loop(session: Session) -> None:
    wait = linked(session)
    lines = sync(session, TRANSCRIPT)
    assert eng.get_loop(session, wait.id).loop.status == LoopStatus.open, lines
    assert any(l.due == date(2026, 10, 5) and "transcript" in l.title.lower() for l in eng.list_loops(session)), lines
    assert eng.list_actions(session) == []  # a deadline isn't a meeting


def test_an_unrelated_email_changes_nothing(session: Session) -> None:
    linked(session)
    before = [(l.title, l.status, l.due) for l in eng.list_loops(session)]
    assert sync(session, UNRELATED) == []
    assert [(l.title, l.status, l.due) for l in eng.list_loops(session)] == before
