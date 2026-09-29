"""Spec §11 acceptance inputs against the real LLM (LLM_PROVIDER). Run: pytest -m live -k acceptance"""

from datetime import date

import pytest
from sqlmodel import Session

from app import engine as eng
from app.config import settings
from app.db import LoopKind, LoopStatus
from app.llm import LLMClient
from tests.conftest import NOW

_key = settings.deepseek_api_key if settings.llm_provider == "deepseek" else settings.nebius_api_key
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not _key.get_secret_value(), reason="API key for LLM_PROVIDER not set"),
]

TOMORROW, FRIDAY = date(2026, 9, 30), date(2026, 10, 2)


@pytest.fixture
def llm() -> LLMClient:
    return LLMClient(settings)


def say(session: Session, llm: LLMClient, text: str) -> eng.ChangeSet:
    return eng.process_message(session, llm, text, now=NOW)


def test_demo_message(session: Session, llm: LLMClient) -> None:
    changes = say(
        session, llm,
        "I'm applying for an NVIDIA internship. I submitted my application yesterday. "
        "Sarah said she'll get back to me next Friday. I also need to finish my portfolio before the interview.",
    )
    assert len(changes.created_goals) == 1 and "nvidia" in changes.created_goals[0].title.lower()
    loops = {l.kind: l for l in changes.created_loops}
    assert set(loops) == {LoopKind.waiting, LoopKind.task}, [l.title for l in changes.created_loops]
    assert loops[LoopKind.waiting].waiting_on == "Sarah"
    assert loops[LoopKind.waiting].due == FRIDAY
    assert all(l.goal_id == changes.created_goals[0].id for l in changes.created_loops)


def test_waiting_on_someone_then_resolved(session: Session, llm: LLMClient) -> None:
    first = say(session, llm, "Alex said he'll send me the dataset tomorrow.")
    assert len(first.created_loops) == 1
    alex = first.created_loops[0]
    assert (alex.kind, alex.waiting_on, alex.due) == (LoopKind.waiting, "Alex", TOMORROW)

    done = say(session, llm, "Alex sent the dataset.")
    assert [l.id for l in done.resolved_loops] == [alex.id]
    assert eng.get_loop(session, alex.id).loop.status == LoopStatus.resolved


def test_personal_commitment_with_deadline(session: Session, llm: LLMClient) -> None:
    changes = say(session, llm, "I need to finish the presentation before Friday.")
    assert len(changes.created_loops) == 1
    loop = changes.created_loops[0]
    assert loop.kind in (LoopKind.task, LoopKind.commitment)
    assert loop.due in (date(2026, 10, 1), FRIDAY)  # "before Friday" may be read as Thursday


def test_no_actionable_memory(session: Session, llm: LLMClient) -> None:
    changes = say(session, llm, "The weather was nice today.")
    assert changes.created_loops == [] and changes.created_goals == []


def test_same_message_twice_is_one_loop(session: Session, llm: LLMClient) -> None:
    say(session, llm, "Sarah said she'll respond Friday.")
    say(session, llm, "Sarah said she'll respond Friday.")
    assert len(eng.list_loops(session)) == 1
