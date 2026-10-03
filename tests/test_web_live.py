"""Spec §13 web extraction against real Nemotron, with canned pages instead of Tavily (no credits).
Run: pytest -m live -k web_live"""

from datetime import date

import pytest
from pydantic import SecretStr
from sqlmodel import Session

from app import engine as eng
from app.config import settings
from app.connectors.tavily import WebResult
from app.db import OpenLoop
from app.llm import LLMClient
from tests.conftest import NOW

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not settings.nebius_api_key.get_secret_value(), reason="NEBIUS_API_KEY not set"),
]

WINNERS = WebResult(
    title="NVIDIA Agents Hackathon 2026: winners announced",
    url="https://devpost.example/nvidia-agents-2026/winners",
    content="The judging is complete. Today we announced the winners of the NVIDIA Agents Hackathon 2026: "
            "first place goes to Team Lumen, second to Continuum, third to PaperTrail. Thanks to all 412 teams.",
)
DELAYED = WebResult(
    title="NVIDIA Agents Hackathon 2026: results date moved",
    url="https://devpost.example/nvidia-agents-2026/update",
    content="Update: judging is taking longer than planned. Results of the NVIDIA Agents Hackathon 2026 will now be "
            "announced on October 20, 2026.",
)
UNRELATED = WebResult(
    title="10 tips for a better developer portfolio",
    url="https://blog.example/portfolio-tips",
    content="Show your best three projects, write short case studies, and keep the design simple.",
)


@pytest.fixture
def hackathon(session: Session, monkeypatch: pytest.MonkeyPatch) -> OpenLoop:
    monkeypatch.setattr(settings, "tavily_api_key", SecretStr("tvly-test"))
    source = eng.add_source(session, "I'm waiting for the NVIDIA Agents Hackathon 2026 results.")
    loop = eng.add_loop(session, source_id=source.id, title="Wait for hackathon results", kind="waiting",
                        summary="Results of the NVIDIA Agents Hackathon 2026.", waiting_on="NVIDIA")
    session.commit()
    eng.watch_loop(session, loop.id, "NVIDIA Agents Hackathon 2026 results")
    return loop


def check(session: Session, page: WebResult) -> list[dict]:
    proposals, errors = eng.watch_the_web(session, LLMClient(settings), lambda q: [page], now=NOW)
    assert errors == []
    return [a.payload for a in proposals]


def test_announced_winners_propose_resolve(session: Session, hackathon: OpenLoop) -> None:
    [payload] = check(session, WINNERS)
    assert payload["resolve"] and payload["source_url"] == WINNERS.url


def test_a_new_results_date_proposes_a_due_date(session: Session, hackathon: OpenLoop) -> None:
    [payload] = check(session, DELAYED)
    assert not payload["resolve"] and payload["due"] == date(2026, 10, 20).isoformat()


def test_an_unrelated_page_proposes_nothing(session: Session, hackathon: OpenLoop) -> None:
    assert check(session, UNRELATED) == []
