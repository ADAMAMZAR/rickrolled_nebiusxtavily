"""Phase 4 curated evaluation set (spec §13) against real Nemotron (and Gemma for the screenshot).
Rerun after any change to the pipeline, ranking or rules. Run: pytest -m live -k scam_live

H1: extraction only. Text inputs make no Tavily calls."""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlmodel import Session

from app import investigation as inv
from app.config import settings
from app.connectors.tavily import TavilyClient
from app.llm import LLMClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not settings.nebius_api_key.get_secret_value(), reason="NEBIUS_API_KEY not set"),
]

SCENARIOS = Path(__file__).parent / "scenarios"


def sample(name: str) -> str:
    """The message, without the first paragraph that labels the file as a test sample."""
    return (SCENARIOS / name).read_text(encoding="utf-8").split("\n\n", 1)[1].strip()


def run(engine: Engine, text: str | None = None, screenshot: bytes | None = None) -> dict[str, Any]:
    with Session(engine) as s:
        made = inv.create_investigation(s, text=text, screenshot=screenshot).id
    inv.run_investigation(engine, made, lambda: LLMClient(settings), lambda: TavilyClient(settings))
    with Session(engine) as s:
        d = inv.get_investigation(s, made)
        assert d.investigation.status == "done", d.investigation.error
        return {
            "text": d.investigation.input_text,
            "entities": {(e.type.value, e.canonical) for e in d.entities},
            "orgs": " | ".join(e.canonical for e in d.entities if e.type == "org"),
            "claims": {c.category.value for c in d.claims},
            "signals": {s.kind for s in d.signals},
        }


@pytest.fixture(autouse=True)
def uploads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path / "uploads"))


def test_a_known_warning(engine: Engine) -> None:
    got = run(engine, sample("a_known_warning.txt"))
    assert "falconrise capital" in got["orgs"]
    assert ("phone", "601100001234") in got["entities"]
    assert "regulatory" in got["claims"]
    assert got["signals"] == {"payment_pressure", "guaranteed_returns"}


def test_b_clone_company(engine: Engine) -> None:
    got = run(engine, sample("b_clone_company.txt"))
    assert "rowe price" in got["orgs"]
    assert ("domain", "troweprice-my-invest.com") in got["entities"]
    assert {"identity", "regulatory"} <= got["claims"]
    assert "payment_pressure" in got["signals"]


def test_c_legitimate(engine: Engine) -> None:
    got = run(engine, sample("c_legitimate.txt"))
    assert "maybank" in got["orgs"]
    assert ("domain", "maybank2u.com.my") in got["entities"]
    assert "guaranteed_returns" not in got["signals"]


def test_d_sparse(engine: Engine) -> None:
    got = run(engine, sample("d_sparse.txt"))
    assert got["orgs"] == ""  # no identifiable entity
    assert "payment_pressure" in got["signals"]


def test_e_conflicting(engine: Engine) -> None:
    got = run(engine, sample("e_conflicting.txt"))
    assert "doo prime" in got["orgs"]
    assert ("domain", "dooprime-malaysia-invest.com") in got["entities"]
    assert "guaranteed_returns" in got["signals"]


def test_screenshot_is_read(engine: Engine) -> None:
    """Scenario D as an image: Gemma reads it, Nemotron extracts from the text."""
    got = run(engine, screenshot=(SCENARIOS / "d_sparse.png").read_bytes())
    assert "rm100 registration fee" in " ".join(got["text"].split()).casefold()
    assert "payment_pressure" in got["signals"]
