"""Settings: Export my data and Delete everything (troth_improvements.md §3.5)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import engine as eng
from app.db import Investigation, InvestigationStatus, Setting
from app.main import create_app
from tests.conftest import FakeLLM
from tests.test_email import FakeMailbox, linked, pearl, sync
from tests.test_process_message import loop, result


def full(session: Session, tmp_path: Path) -> Path:
    """Loops, a goal, people, a link, an email suggestion and a finished check with a screenshot."""
    linked(session)
    sync(session, FakeLLM(result(new=[loop("Sign contract", quote="please sign and return the contract by Friday")])),
         FakeMailbox(new=[pearl()]))
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"png")
    session.add(Investigation(status=InvestigationStatus.done, input_text="Pay RM50 now", screenshot_path=str(shot)))
    session.commit()
    return shot


def test_export_holds_everything_but_settings_and_file_paths(session: Session, tmp_path: Path) -> None:
    full(session, tmp_path)
    data = eng.export_data(session)
    assert {k: len(v) for k, v in data.items() if isinstance(v, list)} == {
        "sources": 2, "people": 1, "goals": 1, "loops": 2, "dependencies": 0, "activity": 2, "actions": 1,
        "checks": 1, "check_entities": 0, "check_claims": 0, "check_evidence": 0, "check_signals": 0}
    assert "screenshot_path" not in data["checks"][0] and data["checks"][0]["input_text"] == "Pay RM50 now"
    assert data["people"][0]["email"] == "sarah@nvidia.com"


def test_delete_everything_keeps_google_and_the_sync_time(session: Session, tmp_path: Path) -> None:
    shot = full(session, tmp_path)
    assert eng.delete_everything(session) == 10
    assert eng.export_data(session) | {"exported_at": None} == {"exported_at": None} | {
        name: [] for name, _ in eng.ALL_DATA}
    assert not shot.exists()
    assert session.get(Setting, eng.LAST_EMAIL_SYNC) is not None  # old mail isn't read again


def test_delete_everything_waits_for_a_running_check(session: Session) -> None:
    session.add(Investigation(input_text="x"))
    session.commit()
    with pytest.raises(eng.InvalidRequest, match="still running"):
        eng.delete_everything(session)
    assert session.exec(select(Investigation)).all()


def test_export_and_delete_routes(db_url: str) -> None:
    with TestClient(create_app(db_url, get_llm=FakeLLM), base_url="http://127.0.0.1:8000",
                    headers={"X-Troth": "1"}) as http:
        response = http.get("/api/export")
        assert response.headers["content-disposition"].startswith('attachment; filename="troth-export-')
        assert response.json()["loops"] == []
        assert http.post("/api/data/forget").json() == {"deleted": 0}
