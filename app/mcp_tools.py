"""MCP tools that Hermes calls. Thin wrappers around the engine that shape output for the model.

Tool docstrings are what Hermes reads to decide which tool to use, so keep them precise.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import Engine
from sqlmodel import Session

from app import engine as eng
from app.db import Goal, OpenLoop
from app.extraction import Completer, ExtractionError
from app.llm import LLMError


def build_mcp(get_db: Callable[[], Engine], get_llm: Callable[[], Completer]) -> MCPServer:
    mcp = MCPServer("continuum")

    @contextmanager
    def session() -> Iterator[Session]:
        with Session(get_db()) as s:
            try:
                yield s
            except eng.NotFound as e:
                raise ToolError(str(e)) from e

    @mcp.tool()
    def remember(text: str) -> dict[str, Any]:
        """Save what the user said about their goals, tasks, promises, deadlines or things they're
        waiting on. Also updates or closes existing loops the message is about (e.g. "Sarah replied").
        Pass the user's message word for word."""
        with session() as s:
            try:
                changes = eng.process_message(s, get_llm(), text)
            except (ExtractionError, LLMError) as e:
                raise ToolError(f"Couldn't save that: {e}") from e
            return {
                "new_goals": [g.title for g in changes.created_goals],
                "new_loops": [_loop(s, loop) for loop in changes.created_loops],
                "updated_loops": [_loop(s, loop) for loop in changes.updated_loops],
                "resolved_loops": [loop.title for loop in changes.resolved_loops],
            }

    @mcp.tool()
    def list_open_loops(goal: str | None = None) -> dict[str, Any]:
        """The user's open loops (tasks, promises, things they're waiting on), grouped by goal.
        Use for "what am I waiting on?", "what's open?", "what do I need to do?".
        Optional `goal` filters by goal title."""
        with session() as s:
            groups: dict[str, list[dict[str, Any]]] = {}
            for loop in eng.list_loops(s):
                title = _goal_title(s, loop) or "No goal"
                if goal and eng.normalize(goal) not in eng.normalize(title):
                    continue
                groups.setdefault(title, []).append(_loop(s, loop, with_goal=False))
            return {
                "today": f"{eng.local_now():%a %Y-%m-%d}",
                "groups": [{"goal": title, "loops": loops} for title, loops in groups.items()],
            }

    @mcp.tool()
    def list_goals() -> dict[str, Any]:
        """The user's active goals, each with its number of open loops."""
        with session() as s:
            return {
                "goals": [
                    _clean({"title": g.goal.title, "description": g.goal.description, "open_loops": g.open_loops})
                    for g in eng.list_goals(s)
                ]
            }

    @mcp.tool()
    def inspect_loop(loop_id: str) -> dict[str, Any]:
        """Everything about one loop, including the user's original words it came from.
        Use for details or "why do you know this?". Get the id from list_open_loops."""
        with session() as s:
            detail = eng.get_loop(s, _uuid(loop_id))
            loop = detail.loop
            return {
                **_loop(s, loop),
                "summary": loop.summary,
                "status": loop.status.value,
                "source": {
                    "kind": detail.source.kind,
                    "said_at": f"{detail.source.created_at:%Y-%m-%d %H:%M} UTC",
                    "text": detail.source.text,
                },
            }

    @mcp.tool()
    def resolve_loop(loop_id: str) -> dict[str, Any]:
        """Mark one open loop as done. Get the id from list_open_loops."""
        with session() as s:
            return {"resolved": eng.resolve_loop(s, _uuid(loop_id)).title}

    return mcp


def _uuid(loop_id: str) -> UUID:
    try:
        return UUID(loop_id)
    except ValueError as e:
        raise ToolError(f"No loop with id {loop_id}. Get ids from list_open_loops.") from e


def _goal_title(session: Session, loop: OpenLoop) -> str | None:
    goal = session.get(Goal, loop.goal_id) if loop.goal_id else None
    return goal.title if goal else None


def _loop(session: Session, loop: OpenLoop, with_goal: bool = True) -> dict[str, Any]:
    return _clean(
        {
            "id": str(loop.id),
            "title": loop.title,
            "kind": loop.kind.value,
            "waiting_on": loop.waiting_on,
            "due": f"{loop.due:%a %Y-%m-%d}" if loop.due else None,
            "next_action": loop.next_action,
            "goal": _goal_title(session, loop) if with_goal else None,
        }
    )


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    """Drop empty fields to keep tool output short."""
    return {k: v for k, v in data.items() if v is not None}
