"""Google proposals (spec §8): proposing never calls Google, approve calls it exactly once, nothing is sent."""

from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlmodel import Session

from app import engine as eng
from app.config import settings
from app.connectors.google import Event, GoogleError
from app.db import ActionStatus
from tests.test_activity import demo
from tests.test_email import SARAH, FakeMailbox, email, linked
from tests.conftest import FakeLLM
from tests.test_process_message import result

EVENT = eng.CalendarEvent(title="NVIDIA interview", start=datetime(2026, 10, 8, 10, 0))
DRAFT = eng.GmailDraft(to=SARAH, subject="Thank you", body="Thanks, Sarah! See you on the 8th.")


class FakeGoogle:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.events: list[tuple[str, datetime, datetime | None]] = []
        self.drafts: list[tuple[str, str, str, str | None]] = []

    def create_event(self, title: str, start: datetime, end: datetime | None = None) -> Event:
        if self.fail:
            raise GoogleError("Google returned HTTP 500: oops")
        self.events.append((title, start, end))
        return Event(id="e1", link="https://calendar.google.com/event?eid=e1")

    def create_draft(self, to: str, subject: str, body: str, thread_id: str | None = None) -> str:
        self.drafts.append((to, subject, body, thread_id))
        return "d1"


@pytest.fixture(autouse=True)
def google_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token = tmp_path / "google_token.json"
    token.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(settings, "google_token_file", str(token))


def test_propose_then_approve_creates_the_event_once(session: Session) -> None:
    wait, _ = demo(session)
    google = FakeGoogle()
    action = eng.propose_google(session, wait.id, EVENT)
    assert eng.describe_action(session, action) == 'Add "NVIDIA interview" to your calendar, Thu Oct 8 10:00?'
    assert google.events == []  # proposing doesn't call Google

    done = eng.approve_action(session, action.id, lambda: google)
    assert (done.status, done.external_id) == (ActionStatus.done, "e1")
    assert google.events == [("NVIDIA interview", datetime(2026, 10, 8, 10, 0), None)]
    with pytest.raises(eng.InvalidRequest, match="already done"):
        eng.approve_action(session, action.id, lambda: google)
    assert len(google.events) == 1
    logged = next(a for a in eng.list_activity(session, wait.id) if a.action == "action_done")
    assert (logged.detail, logged.undo) == ("https://calendar.google.com/event?eid=e1", None)


def test_reject_never_calls_google(session: Session) -> None:
    wait, _ = demo(session)
    google = FakeGoogle()
    action = eng.propose_google(session, wait.id, DRAFT)
    eng.reject_action(session, action.id)
    with pytest.raises(eng.InvalidRequest, match="already rejected"):
        eng.approve_action(session, action.id, lambda: google)
    assert google.drafts == []


def test_a_google_failure_marks_it_failed(session: Session) -> None:
    wait, _ = demo(session)
    action = eng.propose_google(session, wait.id, EVENT)
    with pytest.raises(eng.InvalidRequest, match="HTTP 500"):
        eng.approve_action(session, action.id, lambda: FakeGoogle(fail=True))
    assert eng.list_actions(session, ActionStatus.failed)[0].id == action.id
    assert [a for a in eng.list_activity(session, wait.id) if a.action == "action_done"] == []


def test_a_draft_to_the_sender_replies_in_their_thread(session: Session) -> None:
    wait, _ = linked(session)
    eng.sync_email(session, FakeLLM(result(resolved=[str(wait.id)])), FakeMailbox({SARAH: [email()]}))
    action = eng.propose_google(session, wait.id, DRAFT)  # resolved loops still get drafts
    assert eng.describe_action(session, action) == f'Save a Gmail draft to {SARAH}: "Thank you"? It won\'t be sent.'
    google = FakeGoogle()
    eng.approve_action(session, action.id, lambda: google)
    assert google.drafts == [(SARAH, "Thank you", DRAFT.body, "t-m1")]
    other = eng.propose_google(session, wait.id, DRAFT.model_copy(update={"to": "hr@nvidia.com"}))
    assert other.payload["thread_id"] is None


def test_the_same_proposal_is_stored_once(session: Session) -> None:
    wait, _ = demo(session)
    assert eng.propose_google(session, wait.id, EVENT).id == eng.propose_google(session, wait.id, EVENT).id


def test_google_must_be_connected(session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    wait, _ = demo(session)
    monkeypatch.setattr(settings, "google_token_file", str(tmp_path / "missing.json"))
    with pytest.raises(eng.InvalidRequest, match="Google isn't connected"):
        eng.propose_google(session, wait.id, EVENT)


def test_payloads_are_validated() -> None:
    with pytest.raises(ValidationError):
        eng.CalendarEvent(title="x", start=datetime(2026, 10, 8, 10), end=datetime(2026, 10, 8, 9))
    with pytest.raises(ValidationError):
        eng.GmailDraft(to="not an address", subject="s", body="b")
    with pytest.raises(ValidationError):
        eng.GmailDraft(to=SARAH, subject="Hi\nBcc: x@evil.co", body="b")


def test_times_with_an_offset_become_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "timezone", "Asia/Kuala_Lumpur")
    event = eng.CalendarEvent(title="x", start="2026-10-08T02:00:00Z")  # 10:00 in Asia/Kuala_Lumpur
    assert event.start == datetime(2026, 10, 8, 10, 0)


# --- the same through MCP (Hermes) and REST (dashboard) ---


def test_from_chat_and_dashboard(db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.test_mcp_tools import MCP, remember_demo

    google, llm = FakeGoogle(), FakeLLM()
    app = create_app(db_url, get_llm=lambda: llm, get_google=lambda: google)
    with TestClient(app, base_url="http://127.0.0.1:8000") as http:
        mcp = MCP(http)
        mcp.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
        loop_id = remember_demo(mcp, llm)["new_loops"][0]["id"]

        assert "ask the user" in next(t for t in mcp.rpc("tools/list")["tools"] if t["name"] == "propose_calendar_event")["description"]
        event = mcp.call("propose_calendar_event", loop_id=loop_id, title="NVIDIA interview", start="2026-10-08T10:00")
        assert event["proposed"] == 'Add "NVIDIA interview" to your calendar, Thu Oct 8 10:00?'
        assert "end after it starts" in mcp.call("propose_calendar_event", loop_id=loop_id, title="x",
                                                 start="2026-10-08T10:00", end="2026-10-08T09:00")["error"]
        draft = mcp.call("propose_gmail_draft", loop_id=loop_id, to=SARAH, subject="Thanks", body="Thank you!")
        assert google.events == google.drafts == []

        assert mcp.call("approve_action", action_id=event["id"]) == {"done": event["proposed"]}
        assert http.post(f"/api/actions/{draft['id']}/approve").json()["status"] == "done"
        assert len(google.events) == len(google.drafts) == 1
        assert http.post(f"/api/actions/{draft['id']}/approve").status_code == 422

        monkeypatch.setattr(settings, "google_token_file", str(tmp_path / "missing.json"))
        assert "Google isn't connected" in mcp.call("propose_gmail_draft", loop_id=loop_id, to=SARAH, subject="s", body="b")["error"]


def test_google_settings_routes(db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from app.connectors import google
    from app.main import create_app

    monkeypatch.setattr(settings, "google_client_secret_file", str(tmp_path / "client_secret.json"))
    disconnected: list[bool] = []
    monkeypatch.setattr(google, "disconnect", lambda s: (disconnected.append(True), Path(s.google_token_file).unlink()))
    with TestClient(create_app(db_url), base_url="http://127.0.0.1:8000") as http:
        assert http.get("/api/google/status").json() == {"connected": True, "set_up": False, "last_sync": None}
        assert http.post("/api/google/connect").json()["message"].startswith("Google isn't set up: no OAuth client file")
        assert http.post("/api/google/disconnect").json()["connected"] is False and disconnected
        assert http.post("/api/google/forget").json() == {"forgotten": 0}
