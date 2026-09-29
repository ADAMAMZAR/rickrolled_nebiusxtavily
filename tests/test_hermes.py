import json
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings, settings
from app.hermes import HermesClient, HermesError, parse_reply

RESPONSE = {
    "output": [
        {"type": "message", "phase": "commentary", "content": [{"type": "output_text", "text": "Checking..."}]},
        {"type": "function_call", "name": "mcp__continuum__ping", "arguments": "{}", "call_id": "c0"},
        {"type": "function_call_output", "call_id": "c0", "output": "Tool 'mcp__continuum__ping' does not exist."},
        # Real Hermes 0.21 shape: calls are batched inside a `tool_call` wrapper.
        {"type": "function_call", "name": "tool_call", "arguments": '{"calls": [{"name": "mcp__continuum__ping", "arguments": {}}]}'},
        {"type": "function_call_output", "output": "Continuum is connected. 0 open loops."},
        {"type": "function_call", "name": "mcp__continuum__list_goals", "arguments": "{}"},
        {"type": "message", "content": [{"type": "output_text", "text": "Connected, 0 open loops."}]},
    ]
}


def client(handler: httpx.MockTransport) -> HermesClient:
    config = Settings(_env_file=None, hermes_api_key=SecretStr("test-key"))
    return HermesClient(config, transport=handler)


def test_parse_reply_keeps_final_text_and_tool_names() -> None:
    reply = parse_reply(RESPONSE)
    assert reply.text == "Connected, 0 open loops."
    assert reply.tools_called == ["mcp__continuum__ping", "mcp__continuum__list_goals"]


def test_ask_sends_auth_and_conversation() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=RESPONSE)

    reply = client(httpx.MockTransport(handler)).ask("ping?", conversation="ui")
    assert "mcp__continuum__ping" in reply.tools_called
    assert seen[0].url.path == "/v1/responses"
    assert seen[0].headers["Authorization"] == "Bearer test-key"
    assert json.loads(seen[0].content)["conversation"] == "ui"


def test_clear_errors() -> None:
    with pytest.raises(HermesError, match="HERMES_API_KEY is not set"):
        HermesClient(Settings(_env_file=None, hermes_api_key=SecretStr("")))

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(HermesError, match="isn't running"):
        client(httpx.MockTransport(refuse)).ask("hi")

    with pytest.raises(HermesError, match="rejected"):
        client(httpx.MockTransport(lambda r: httpx.Response(401))).ask("hi")


@pytest.mark.live
@pytest.mark.skipif(not settings.hermes_api_key.get_secret_value(), reason="HERMES_API_KEY not set")
def test_live_hermes_calls_continuum() -> None:
    """Hermes → MCP gate. Read-only, so it's safe against the real DB.
    Needs both running: uvicorn app.main:app, and hermes -p continuum gateway run."""
    # Fresh conversation each run, so the answer can't come from an earlier turn's history.
    reply = HermesClient(settings).ask("What are my goals?", conversation=f"gate-{uuid4()}")
    assert "mcp__continuum__list_goals" in reply.tools_called, reply
    assert reply.text, reply
