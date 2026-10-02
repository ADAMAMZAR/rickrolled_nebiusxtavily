"""Client for the Hermes API server. The dashboard chat goes through here."""

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings

START_HINT = "Start it with: hermes -p continuum gateway run"


class HermesError(Exception):
    pass


@dataclass
class HermesReply:
    text: str
    tools_called: list[str] = field(default_factory=list)


class HermesClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        key = settings.hermes_api_key.get_secret_value()
        if not key:
            raise HermesError("HERMES_API_KEY is not set. Run: python scripts/setup_hermes.py")
        self._http = httpx.Client(
            base_url=settings.hermes_api_url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=settings.hermes_timeout,
            transport=transport,
        )

    def ask(self, message: str, conversation: str = "continuum") -> HermesReply:
        """One agent turn. Hermes keeps the history for each named conversation."""
        body = {"model": "continuum", "input": message, "conversation": conversation, "store": True}
        try:
            response = self._http.post("responses", json=body)
        except httpx.ConnectError as e:
            raise HermesError(f"Hermes isn't running. {START_HINT}") from e
        except httpx.TimeoutException as e:
            raise HermesError("Hermes timed out.") from e
        except httpx.HTTPError as e:
            raise HermesError(f"Couldn't reach Hermes ({type(e).__name__}).") from e
        if response.status_code == 401:
            raise HermesError("Hermes rejected HERMES_API_KEY. Re-run: python scripts/setup_hermes.py")
        if response.is_error:
            raise HermesError(f"Hermes returned HTTP {response.status_code}.")
        try:
            data = response.json()
        except ValueError as e:
            raise HermesError("Hermes sent a reply that isn't JSON. Check HERMES_API_URL in .env.") from e
        return parse_reply(data)


def parse_reply(data: dict[str, Any]) -> HermesReply:
    """Final answer text + names of tools Hermes ran (from a /v1/responses payload)."""
    output = data.get("output", [])
    rejected = {  # calls Hermes refused, e.g. "Tool 'x' does not exist"
        item.get("call_id")
        for item in output
        if item.get("type") == "function_call_output" and "does not exist" in str(item.get("output", ""))[:200]
    }
    texts: list[str] = []
    tools: list[str] = []
    for item in output:
        if item.get("type") == "function_call" and item.get("call_id") not in rejected:
            tools += _tool_names(item)
        elif item.get("type") == "message" and item.get("phase") != "commentary":
            texts += [part.get("text", "") for part in item.get("content", []) if part.get("type") == "output_text"]
    return HermesReply(text="\n".join(t for t in texts if t).strip(), tools_called=tools)


def _tool_names(item: dict[str, Any]) -> list[str]:
    """Hermes batches calls in a `tool_call` wrapper; the real names are in its arguments."""
    if item.get("name") != "tool_call":
        return [item.get("name", "")]
    try:
        calls = json.loads(item.get("arguments") or "{}").get("calls", [])
    except json.JSONDecodeError:
        return ["tool_call"]
    return [call.get("name", "") for call in calls]
