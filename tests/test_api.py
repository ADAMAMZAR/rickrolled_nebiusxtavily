import json
from collections.abc import Iterator
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import engine as eng
from app.db import create_db_engine
from app.hermes import HermesError, HermesReply
from app.main import create_app
from tests.conftest import FakeLLM
from tests.test_mcp_tools import HEADERS
from tests.test_process_message import DEMO, DEMO_RESULT


class FakeHermes:
    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []
        self.down = False

    def ask(self, message: str, conversation: str = "continuum") -> HermesReply:
        if self.down:
            raise HermesError("Hermes isn't running.")
        self.asked.append((message, conversation))
        return HermesReply(text="Saved.")


@pytest.fixture
def hermes() -> FakeHermes:
    return FakeHermes()


@pytest.fixture
def client(db_url: str, hermes: FakeHermes) -> Iterator[TestClient]:
    llm = FakeLLM(DEMO_RESULT)
    app = create_app(db_url, get_llm=lambda: llm, get_hermes=lambda: hermes)
    with TestClient(app, base_url="http://127.0.0.1:8000") as http:
        yield http


def seed(client: TestClient) -> list[dict]:
    """Save the demo through the real MCP `remember` tool, then return open loops via REST."""
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "remember", "arguments": {"text": DEMO}}}
    assert client.post("/mcp/", json=call, headers=HEADERS).status_code == 200
    return client.get("/api/loops").json()


def test_health(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}


def test_ui_is_served(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200 and "Continuum" in response.text


def test_chat_proxies_to_hermes(client: TestClient, hermes: FakeHermes) -> None:
    response = client.post("/api/chat", json={"message": "What am I waiting on?", "conversation": "ui-1"})
    assert response.json() == {"reply": "Saved."}
    assert hermes.asked == [("What am I waiting on?", "ui-1")]


def test_chat_when_hermes_down(client: TestClient, hermes: FakeHermes) -> None:
    hermes.down = True
    response = client.post("/api/chat", json={"message": "hi"})
    assert response.status_code == 503
    assert response.json()["error"] == "agent_unavailable"


def test_goals_and_loops(client: TestClient) -> None:
    loops = seed(client)
    assert [l["title"] for l in loops] == ["Wait for Sarah's response", "Finish portfolio"]
    assert loops[0]["goal_title"] == "Secure NVIDIA internship"
    assert (loops[0]["kind"], loops[0]["waiting_on"], loops[0]["due"]) == ("waiting", "Sarah", "2026-10-02")

    [goal] = client.get("/api/goals").json()
    assert (goal["title"], goal["open_loops"]) == ("Secure NVIDIA internship", 2)
    assert len(client.get(f"/api/loops?goal_id={goal['id']}").json()) == 2


def test_detail_resolve_reopen_delete(client: TestClient) -> None:
    loop_id = seed(client)[0]["id"]

    detail = client.get(f"/api/loops/{loop_id}").json()
    assert detail["source"]["text"] == DEMO and detail["source"]["created_at"]

    resolved = client.post(f"/api/loops/{loop_id}/resolve").json()
    assert resolved["status"] == "resolved" and resolved["resolved_at"]
    assert [l["id"] for l in client.get("/api/loops?status=resolved").json()] == [loop_id]
    assert len(client.get("/api/loops?status=all").json()) == 2

    reopened = client.post(f"/api/loops/{loop_id}/reopen").json()
    assert reopened["status"] == "open" and reopened["resolved_at"] is None

    assert client.delete(f"/api/loops/{loop_id}").status_code == 204
    assert client.get(f"/api/loops/{loop_id}").status_code == 404


@pytest.mark.usefixtures("on_friday")
def test_attention_snooze_unsnooze(client: TestClient) -> None:
    wait = seed(client)[0]
    attention = client.get("/api/attention").json()
    assert [(a["loop"]["title"], a["reason"]) for a in attention] == [("Wait for Sarah's response", "due today")]
    assert attention[0]["loop"]["goal_title"] == "Secure NVIDIA internship"

    snoozed = client.post(f"/api/loops/{wait['id']}/snooze", json={"until": "2026-10-05"}).json()
    assert snoozed["snoozed_until"] == "2026-10-05"
    assert client.get("/api/attention").json() == []
    assert [l["title"] for l in client.get("/api/loops").json()] == ["Finish portfolio"]
    assert len(client.get("/api/loops?include_snoozed=true").json()) == 2

    assert client.post(f"/api/loops/{wait['id']}/unsnooze").json()["snoozed_until"] is None
    assert len(client.get("/api/attention").json()) == 1


@pytest.mark.usefixtures("on_friday")
def test_snooze_needs_a_future_date(client: TestClient) -> None:
    loop_id = seed(client)[0]["id"]
    for until in ("2026-10-02", "monday"):
        response = client.post(f"/api/loops/{loop_id}/snooze", json={"until": until})
        assert response.status_code == 422 and response.json()["error"] == "invalid_request", until


def test_complete_goal(client: TestClient) -> None:
    loops = seed(client)
    assert loops[0]["goal_status"] == "active"
    goal_id = loops[0]["goal_id"]

    done = client.post(f"/api/goals/{goal_id}/complete").json()
    assert done["status"] == "done"
    assert client.get("/api/goals").json() == []
    assert client.get("/api/loops").json() == []
    resolved = client.get("/api/loops?status=resolved").json()
    assert len(resolved) == 2 and {l["goal_status"] for l in resolved} == {"done"}

    missing = client.post("/api/goals/00000000-0000-0000-0000-000000000000/complete")
    assert missing.status_code == 404 and missing.json()["error"] == "not_found"


def test_errors_are_json(client: TestClient) -> None:
    missing = client.post("/api/loops/00000000-0000-0000-0000-000000000000/resolve")
    assert missing.status_code == 404 and missing.json()["error"] == "not_found"
    bad = client.get("/api/loops/not-a-uuid")
    assert bad.status_code == 422 and bad.json()["error"] == "invalid_request"
    assert json.loads(client.get("/api/loops?status=nope").text)["error"] == "invalid_request"
    unknown = client.get("/api/nope")
    assert unknown.status_code == 404 and unknown.json()["error"] == "not_found"
    wrong_method = client.put("/api/goals")
    assert wrong_method.status_code == 405 and wrong_method.json()["error"] == "invalid_request"


def test_activity_and_undo(client: TestClient) -> None:
    wait = next(l for l in seed(client) if l["waiting_on"] == "Sarah")
    client.post(f"/api/loops/{wait['id']}/resolve")
    items = client.get(f"/api/activity?loop_id={wait['id']}").json()
    assert {(a["action"], a["by"], a["can_undo"]) for a in items} == {("created", "chat", False), ("resolved", "user", False)}
    assert "undo" not in items[0]
    created = next(a for a in items if a["action"] == "created")
    assert client.post(f"/api/activity/{created['id']}/undo").status_code == 422
    assert client.post("/api/activity/00000000-0000-0000-0000-000000000000/undo").status_code == 404


def test_actions_approve_and_reject(client: TestClient, db_url: str) -> None:
    wait, portfolio = sorted(seed(client), key=lambda l: l["title"], reverse=True)
    with Session(create_db_engine(db_url)) as s:
        for loop in (wait, portfolio):
            eng.propose_loop_update(s, UUID(loop["id"]), eng.LoopUpdate(resolve=True, source_url="https://x.dev"))
    first, second = client.get("/api/actions").json()
    assert first["summary"].startswith("Resolve") and first["payload"]["source_url"] == "https://x.dev"
    assert client.post(f"/api/actions/{first['id']}/approve").json()["status"] == "done"
    assert client.post(f"/api/actions/{second['id']}/reject").json()["status"] == "rejected"
    assert client.get("/api/actions").json() == []
    assert [a["status"] for a in client.get("/api/actions?status=all").json()] == ["done", "rejected"]
    again = client.post(f"/api/actions/{first['id']}/approve")
    assert again.status_code == 422 and "already done" in again.json()["message"]
    assert client.post("/api/actions/00000000-0000-0000-0000-000000000000/reject").status_code == 404


@pytest.mark.usefixtures("web_on")
def test_watch_the_web(client: TestClient) -> None:
    wait = seed(client)[0]
    assert client.get("/api/web/status").json() == {"enabled": True}
    watched = client.put(f"/api/loops/{wait['id']}/watch", json={"query": " NVIDIA hackathon "}).json()
    assert (watched["watch_query"], watched["watch_checked_on"]) == ("NVIDIA hackathon", None)
    assert client.put(f"/api/loops/{wait['id']}/watch", json={"query": "   "}).status_code == 422
    assert client.delete(f"/api/loops/{wait['id']}/watch").json()["watch_query"] is None


@pytest.mark.usefixtures("web_off")
def test_web_features_are_off_without_a_key(client: TestClient) -> None:
    wait = seed(client)[0]
    assert client.get("/api/web/status").json() == {"enabled": False}
    response = client.put(f"/api/loops/{wait['id']}/watch", json={"query": "NVIDIA hackathon"})
    assert response.status_code == 422 and "TAVILY_API_KEY" in response.json()["message"]


def test_people_and_contact_email(client: TestClient) -> None:
    wait = next(l for l in seed(client) if l["waiting_on"] == "Sarah")
    assert (wait["person_name"], wait["person_email"]) == ("Sarah", None)
    [sarah] = client.get("/api/people").json()
    updated = client.patch(f"/api/people/{sarah['id']}", json={"email": "sarah@nvidia.com"}).json()
    assert updated["email"] == "sarah@nvidia.com"
    assert client.get(f"/api/loops/{wait['id']}").json()["person_email"] == "sarah@nvidia.com"
    assert client.patch(f"/api/people/{sarah['id']}", json={"email": "nope"}).status_code == 422
    assert client.patch(f"/api/people/{sarah['id']}", json={"email": None}).json()["email"] is None
    assert client.patch("/api/people/00000000-0000-0000-0000-000000000000", json={"email": None}).status_code == 404
