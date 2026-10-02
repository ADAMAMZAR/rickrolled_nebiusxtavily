"""The MCP tools over real HTTP (JSON-RPC), the way Hermes calls them. LLM is faked."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

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


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def mcp(db_url: str, llm: FakeLLM) -> Iterator[MCP]:
    with TestClient(create_app(db_url, get_llm=lambda: llm), base_url="http://127.0.0.1:8000") as http:
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
