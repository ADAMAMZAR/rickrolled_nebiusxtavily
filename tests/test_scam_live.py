"""Phase 4 curated evaluation set (spec §13) against real Nemotron (and Gemma for the screenshot).
Rerun after any change to the pipeline, ranking or rules. Run: pytest -m live -k scam_live

Each scenario runs real Tavily research: up to 8 searches + 1 extract (about 10 credits)."""

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
            "level": d.investigation.risk_level, "identity": d.investigation.identity,
            # H3 gate: every signal cites evidence or an input quote; every finding cites something
            "cited": all(s.evidence_id or s.input_quote for s in d.signals)
                     and all(f["evidence_ids"] or f["signal_ids"] for f in d.investigation.findings),
            "evidence": [(e.tier.value, e.direction.value, e.host, e.quote) for e in d.evidence],
            "steps": [s["text"] for s in d.investigation.steps],
        }


@pytest.fixture(autouse=True)
def uploads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path / "uploads"))


def test_a_known_warning(engine: Engine) -> None:
    got = run(engine, sample("a_known_warning.txt"))
    assert "falconrise capital" in got["orgs"]
    assert ("phone", "601100001234") in got["entities"]
    assert "regulatory" in got["claims"]
    assert {"payment_pressure", "guaranteed_returns", "regulatory_warning"} <= got["signals"]
    # H2 gate: a regulator source is attached automatically.
    assert any(tier == "A" and direction == "warns" for tier, direction, _, _ in got["evidence"]), got["steps"]
    assert got["level"] in ("HIGH", "CRITICAL") and got["cited"]


def test_b_clone_company(engine: Engine) -> None:
    got = run(engine, sample("b_clone_company.txt"))
    assert "rowe price" in got["orgs"]
    assert ("domain", "troweprice-my-invest.com") in got["entities"]
    assert {"identity", "regulatory"} <= got["claims"]
    assert "payment_pressure" in got["signals"]
    assert got["identity"] == "mismatch" and got["cited"]


def test_c_legitimate(engine: Engine) -> None:
    got = run(engine, sample("c_legitimate.txt"))
    assert "maybank" in got["orgs"]
    assert ("domain", "maybank2u.com.my") in got["entities"]
    assert "guaranteed_returns" not in got["signals"]
    # H3 gate: no invented warning
    assert not got["signals"] & {"regulatory_warning", "confirmed_impersonation", "false_regulatory_claim"}
    assert got["level"] not in ("HIGH", "CRITICAL") and got["cited"]


def test_d_sparse(engine: Engine) -> None:
    got = run(engine, sample("d_sparse.txt"))
    assert got["orgs"] == ""  # no identifiable entity
    assert "payment_pressure" in got["signals"]
    assert got["identity"] == "unverified" and got["cited"]


def test_e_conflicting(engine: Engine) -> None:
    got = run(engine, sample("e_conflicting.txt"))
    assert "doo prime" in got["orgs"]
    assert ("domain", "dooprime-malaysia-invest.com") in got["entities"]
    assert "guaranteed_returns" in got["signals"]
    assert got["cited"]


def test_screenshot_is_read(engine: Engine) -> None:
    """Scenario D as an image: Gemma reads it, Nemotron extracts from the text."""
    got = run(engine, screenshot=(SCENARIOS / "d_sparse.png").read_bytes())
    assert "rm100 registration fee" in " ".join(got["text"].split()).casefold()
    assert "payment_pressure" in got["signals"]
