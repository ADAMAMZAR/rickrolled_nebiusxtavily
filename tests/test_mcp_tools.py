"""The MCP tools over real HTTP (JSON-RPC), the way Hermes calls them. LLM is faked."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from uuid import UUID

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlmodel import Session

from app import engine as eng
from app.config import Settings
from app.connectors.tavily import TavilyClient
from app.db import create_db_engine
from app.main import create_app
from tests.conftest import FakeLLM
from tests.test_process_message import DEMO, DEMO_RESULT, result

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
HERMES_CONFIG = Path(__file__).parent.parent / "hermes" / "config.example.yaml"


class MCP:
    def __init__(self, http: TestClient) -> None:
        self.http = http
        self.id = 0

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.id += 1
        body = {"jsonrpc": "2.0", "id": self.id, "method": method, "params": params or {}}
        response = self.http.post("/mcp/", json=body, headers=HEADERS)
        assert response.status_code == 200, response.text
        return response.json()["result"]

    def call(self, tool: str, **args: Any) -> dict[str, Any]:
        """Returns the tool's JSON output, or {"error": text} for a tool error."""
        out = self.rpc("tools/call", {"name": tool, "arguments": args})
        text = out["content"][0]["text"]
        return {"error": text} if out.get("isError") else json.loads(text)


class FakeTavily:
    """A real TavilyClient over a fake transport. Set key = "" to act as if TAVILY_API_KEY is missing."""

    def __init__(self) -> None:
        self.key = "tvly-test"
        self.queries: list[str] = []

    def client(self) -> TavilyClient:
        def handler(request: httpx.Request) -> httpx.Response:
            self.queries.append(json.loads(request.content)["query"])
            return httpx.Response(200, json={"answer": "Applications close Oct 15.", "results": [
                {"title": "STEP 2027", "url": "https://x.dev/step", "content": "Apply by Oct 15. " + "x" * 600},
            ]})
        return TavilyClient(Settings(_env_file=None, tavily_api_key=SecretStr(self.key)), httpx.MockTransport(handler))


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def web() -> FakeTavily:
    return FakeTavily()


@pytest.fixture
def mcp(db_url: str, llm: FakeLLM, web: FakeTavily) -> Iterator[MCP]:
    app = create_app(db_url, get_llm=lambda: llm, get_web=web.client)
    with TestClient(app, base_url="http://127.0.0.1:8000") as http:
        client = MCP(http)
        client.rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"},
        })
        yield client


def remember_demo(mcp: MCP, llm: FakeLLM) -> dict[str, Any]:
    llm.replies.append(json.dumps(DEMO_RESULT))
    return mcp.call("remember", text=DEMO)


def test_lists_exactly_the_tools_hermes_includes(mcp: MCP) -> None:
    names = {t["name"] for t in mcp.rpc("tools/list")["tools"]}
    assert names == {
        "remember", "list_open_loops", "needs_attention", "list_goals", "inspect_loop", "resolve_loop", "snooze_loop",
        "list_pending_actions", "approve_action", "reject_action", "web_lookup", "propose_loop_update", "watch_loop",
        "set_person_email",
    }
    include = yaml.safe_load(HERMES_CONFIG.read_text(encoding="utf-8"))["mcp_servers"]["continuum"]["tools"]["include"]
    assert set(include) == names  # a tool missing from tools.include is invisible to Hermes


def test_remember_then_what_am_i_waiting_on(mcp: MCP, llm: FakeLLM) -> None:
    saved = remember_demo(mcp, llm)
    assert saved["new_goals"] == ["Secure NVIDIA internship"]
    assert [l["title"] for l in saved["new_loops"]] == ["Wait for Sarah's response", "Finish portfolio"]

    open_loops = mcp.call("list_open_loops")
    assert open_loops["today"]
    [group] = open_loops["groups"]
    assert group["goal"] == "Secure NVIDIA internship"
    wait = group["loops"][0]
    assert (wait["waiting_on"], wait["due"]) == ("Sarah", "Fri 2026-10-02")
    assert "next_action" not in wait  # empty fields dropped


def test_filter_by_goal(mcp: MCP, llm: FakeLLM) -> None:
    remember_demo(mcp, llm)
    assert len(mcp.call("list_open_loops", goal="nvidia")["groups"]) == 1
    assert mcp.call("list_open_loops", goal="datathon")["groups"] == []


def test_goals_inspect_and_resolve(mcp: MCP, llm: FakeLLM) -> None:
    saved = remember_demo(mcp, llm)
    assert mcp.call("list_goals")["goals"] == [{"title": "Secure NVIDIA internship", "open_loops": 2}]

    loop_id = saved["new_loops"][0]["id"]
    detail = mcp.call("inspect_loop", loop_id=loop_id)
    assert detail["source"]["text"] == DEMO
    assert detail["status"] == "open"

    assert mcp.call("resolve_loop", loop_id=loop_id) == {"resolved": "Wait for Sarah's response"}
    remaining = mcp.call("list_open_loops")["groups"][0]["loops"]
    assert [l["title"] for l in remaining] == ["Finish portfolio"]


@pytest.mark.usefixtures("on_friday")
def test_needs_attention_then_snooze(mcp: MCP, llm: FakeLLM) -> None:
    saved = remember_demo(mcp, llm)
    urgent = mcp.call("needs_attention")
    assert urgent["today"] == "Fri 2026-10-02"
    assert [(i["title"], i["reason"]) for i in urgent["items"]] == [("Wait for Sarah's response", "due today")]

    loop_id = saved["new_loops"][0]["id"]
    snoozed = mcp.call("snooze_loop", loop_id=loop_id, until="2026-10-05")
    assert snoozed == {"snoozed": "Wait for Sarah's response", "until": "Mon 2026-10-05"}
    assert mcp.call("needs_attention")["items"] == []

    visible = mcp.call("list_open_loops")["groups"][0]["loops"]
    assert [l["title"] for l in visible] == ["Finish portfolio"]
    everything = mcp.call("list_open_loops", include_snoozed=True)["groups"][0]["loops"]
    assert everything[0]["snoozed_until"] == "Mon 2026-10-05"


@pytest.mark.usefixtures("on_friday")
def test_snooze_errors_are_readable(mcp: MCP, llm: FakeLLM) -> None:
    loop_id = remember_demo(mcp, llm)["new_loops"][0]["id"]
    assert "must be a date" in mcp.call("snooze_loop", loop_id=loop_id, until="monday")["error"]
    assert "after today" in mcp.call("snooze_loop", loop_id=loop_id, until="2026-10-02")["error"]


def test_bad_ids_give_readable_errors(mcp: MCP) -> None:
    assert "No loop with id" in mcp.call("inspect_loop", loop_id="not-a-uuid")["error"]
    assert "not found" in mcp.call("resolve_loop", loop_id="00000000-0000-0000-0000-000000000000")["error"]


def test_remember_nothing_and_failure(mcp: MCP, llm: FakeLLM) -> None:
    llm.replies.append(json.dumps(result()))
    assert mcp.call("remember", text="I like pizza.") == {
        "new_goals": [], "new_loops": [], "updated_loops": [], "resolved_loops": [],
    }

    llm.replies += ["broken", "still broken"]
    assert "Couldn't save that" in mcp.call("remember", text="Sarah will reply Friday.")["error"]


def test_resolve_from_chat_is_undoable(mcp: MCP, llm: FakeLLM) -> None:
    llm.replies.append(json.dumps(DEMO_RESULT))
    mcp.call("remember", text=DEMO)
    loop_id = mcp.call("list_open_loops")["groups"][0]["loops"][0]["id"]
    mcp.call("resolve_loop", loop_id=loop_id)
    items = mcp.http.get(f"/api/activity?loop_id={loop_id}").json()
    assert ("resolved", "chat", True) in {(a["action"], a["by"], a["can_undo"]) for a in items}


def test_list_and_approve_pending_action(mcp: MCP, llm: FakeLLM, db_url: str) -> None:
    llm.replies.append(json.dumps(DEMO_RESULT))
    mcp.call("remember", text=DEMO)
    loop_id = mcp.call("list_open_loops")["groups"][0]["loops"][0]["id"]
    with Session(create_db_engine(db_url)) as s:
        eng.propose_loop_update(s, UUID(loop_id), eng.LoopUpdate(resolve=True, source_url="https://x.dev"))
    [pending] = mcp.call("list_pending_actions")["actions"]
    assert mcp.call("approve_action", action_id=pending["id"])["done"].startswith("Resolve")
    assert "already done" in mcp.call("approve_action", action_id=pending["id"])["error"]
    assert mcp.call("list_pending_actions") == {"actions": []}


def test_reject_pending_action_and_bad_ids(mcp: MCP, llm: FakeLLM, db_url: str) -> None:
    loop_id = remember_demo(mcp, llm)["new_loops"][0]["id"]
    with Session(create_db_engine(db_url)) as s:
        eng.propose_loop_update(s, UUID(loop_id), eng.LoopUpdate(resolve=True, source_url="https://x.dev"))
    [pending] = mcp.call("list_pending_actions")["actions"]
    assert mcp.call("reject_action", action_id=pending["id"])["rejected"].startswith("Resolve")
    assert mcp.call("list_open_loops")["groups"][0]["loops"][0]["id"] == loop_id  # still open
    assert "No action with id nope. Get ids from list_pending_actions" in mcp.call("approve_action", action_id="nope")["error"]


def test_web_lookup(mcp: MCP, web: FakeTavily) -> None:
    found = mcp.call("web_lookup", query="When does Google STEP close?")
    assert found["answer"] == "Applications close Oct 15."
    [page] = found["results"]
    assert (page["title"], page["url"]) == ("STEP 2027", "https://x.dev/step")
    assert len(page["snippet"]) <= 500  # keeps tool output short
    assert web.queries == ["When does Google STEP close?"]
    web.key = ""
    assert "TAVILY_API_KEY" in mcp.call("web_lookup", query="anything")["error"]


def test_propose_loop_update_from_chat(mcp: MCP, llm: FakeLLM) -> None:
    loop_id = remember_demo(mcp, llm)["new_loops"][0]["id"]
    proposed = mcp.call("propose_loop_update", loop_id=loop_id, due="2026-10-15",
                        source_url="https://x.dev/step", source_title="STEP 2027")
    assert proposed["proposed"] == "Set \"Wait for Sarah's response\" due Thu Oct 15? Found on the web: STEP 2027 (x.dev)"
    assert mcp.call("list_pending_actions")["actions"][0]["id"] == proposed["id"]
    assert mcp.call("list_open_loops")["groups"][0]["loops"][0]["due"] == "Fri 2026-10-02"  # unchanged until the yes
    for bad in ({"source_url": "https://x.dev"}, {"resolve": True, "source_url": "file:///etc/passwd"},
                {"due": "friday", "source_url": "https://x.dev"}):
        assert "Can't propose that" in mcp.call("propose_loop_update", loop_id=loop_id, **bad)["error"], bad


@pytest.mark.usefixtures("web_on")
def test_watch_loop_from_chat(mcp: MCP, llm: FakeLLM) -> None:
    loop_id = remember_demo(mcp, llm)["new_loops"][0]["id"]
    started = mcp.call("watch_loop", loop_id=loop_id, query="NVIDIA hackathon winners")
    assert started == {"watching": "Wait for Sarah's response", "query": "NVIDIA hackathon winners"}
    assert mcp.call("list_open_loops")["groups"][0]["loops"][0]["watching_web"] == "NVIDIA hackathon winners"
    assert mcp.call("watch_loop", loop_id=loop_id) == {"stopped_watching": "Wait for Sarah's response"}


@pytest.mark.usefixtures("web_off")
def test_watch_loop_without_tavily_key(mcp: MCP, llm: FakeLLM) -> None:
    loop_id = remember_demo(mcp, llm)["new_loops"][0]["id"]
    assert "TAVILY_API_KEY" in mcp.call("watch_loop", loop_id=loop_id, query="NVIDIA hackathon")["error"]


def test_set_person_email_from_chat(mcp: MCP, llm: FakeLLM) -> None:
    remember_demo(mcp, llm)
    saved = mcp.call("set_person_email", name="sarah", email="sarah@nvidia.com")
    assert saved == {"person": "Sarah", "email": "sarah@nvidia.com", "waiting_on_them": ["Wait for Sarah's response"]}
    loop_id = mcp.call("list_open_loops")["groups"][0]["loops"][0]["id"]
    assert mcp.call("inspect_loop", loop_id=loop_id)["contact_email"] == "sarah@nvidia.com"
    assert "isn't an email address" in mcp.call("set_person_email", name="Sarah", email="sarah")["error"]
