"""Links between loops against real Nemotron (troth_improvements.md §2 and §6). Run: pytest -m live -k dependency

Today is Tue 2026-09-29 (conftest.NOW): Thu = Oct 1, Fri = Oct 2.
Measured 2026-10-19 with the final prompt (extraction only, 8 runs each): "…writing my reference letter for it" linked
7/8, "…only send it once Dr. Lim sends my reference letter" 8/8, the same-topic control (application + hackathon) 0/8.
Every link found pointed the right way. This file: 3 of 3 tests passed in each of 3 runs.
"""

from datetime import date

import pytest
from sqlmodel import Session, select

from app import engine as eng
from app.config import settings
from app.db import ActivityBy, Dependency, LoopKind, Source
from app.llm import LLMClient
from tests.conftest import NOW

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not settings.nebius_api_key.get_secret_value(), reason="NEBIUS_API_KEY not set"),
]


@pytest.fixture
def llm() -> LLMClient:
    return LLMClient(settings)


def links(session: Session) -> list[tuple[LoopKind, LoopKind]]:
    kinds = {l.id: l.kind for l in eng.list_loops(session, status=None)}
    return [(kinds[d.loop_id], kinds[d.needs_id]) for d in session.exec(select(Dependency)).all()]


def test_the_story_links_the_right_way_and_a_delay_puts_it_at_risk(session: Session, llm: LLMClient) -> None:
    first = eng.process_message(session, llm, "I promised Sarah I'd send my NVIDIA application by Friday. "
                                "Dr. Lim is writing my reference letter for it, he said Thursday.", NOW)
    kinds = {l.kind: l for l in first.created_loops}
    assert set(kinds) == {LoopKind.commitment, LoopKind.waiting}, [l.title for l in first.created_loops]
    app, letter = kinds[LoopKind.commitment], kinds[LoopKind.waiting]
    assert (app.waiting_on, app.due, letter.due) == ("Sarah", date(2026, 10, 2), date(2026, 10, 1))
    assert links(session) == [(LoopKind.commitment, LoopKind.waiting)]  # the application needs the letter

    lim = next(p for p in eng.list_people(session) if p.id == letter.person_id)
    # A date, not "next Wednesday": said on a Tuesday, that means tomorrow by rule 3, which isn't what people mean.
    text = "Subject: Reference letter\n\nSorry, I can only send the reference letter on Wednesday, October 7."
    source = Source(kind="email", text=text, external_id="live-1", url="https://mail.google.com/mail/u/0/#all/t1")
    moved = eng.process_message(session, llm, text, NOW, ActivityBy.email, person=lim, source=source)
    assert eng.get_loop(session, letter.id).loop.due == date(2026, 10, 7)
    assert [r.loop.id for r in moved.at_risk] == [app.id], eng.risk_lines(moved)


def test_a_link_to_an_existing_loop(session: Session, llm: LLMClient) -> None:
    eng.process_message(session, llm, "I need to finish my portfolio.", NOW)
    changes = eng.process_message(session, llm, "I can only send the Google application once my portfolio is done.", NOW)
    assert len(changes.created_loops) == 1 and len(changes.linked) == 1, eng.risk_lines(changes)
    loop, needed = changes.linked[0]
    assert loop.id == changes.created_loops[0].id and "portfolio" in needed.title.lower()


def test_the_same_topic_is_not_a_link(session: Session, llm: LLMClient) -> None:
    changes = eng.process_message(session, llm, "I need to submit my NVIDIA internship application by Friday, "
                                  "and I'm also joining the NVIDIA hackathon next week.", NOW)
    assert len(changes.created_loops) == 2 and changes.linked == []
