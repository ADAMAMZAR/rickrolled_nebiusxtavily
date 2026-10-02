import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlmodel import Session, text
from starlette.exceptions import HTTPException

from app import engine as eng
from app.config import settings
from app.db import Goal, LoopStatus, OpenLoop, create_db_engine
from app.extraction import Completer
from app.hermes import HermesClient, HermesError, HermesReply
from app.llm import LLMClient
from app.mcp_tools import build_mcp

log = logging.getLogger("continuum.api")
STATIC = Path(__file__).parent / "static"


class Agent(Protocol):
    def ask(self, message: str, conversation: str = ...) -> HermesReply: ...


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    conversation: str = Field(default="continuum", max_length=100)  # the UI sends one per page load


class SnoozeIn(BaseModel):
    until: date


@cache
def default_hermes() -> HermesClient:
    return HermesClient(settings)


def get_session(request: Request) -> Iterator[Session]:
    with Session(request.app.state.db) as session:
        yield session


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


def loop_out(session: Session, loop: OpenLoop) -> dict[str, Any]:
    goal = session.get(Goal, loop.goal_id) if loop.goal_id else None
    return {
        **loop.model_dump(mode="json"),
        "goal_title": goal.title if goal else None,
        "goal_status": goal.status.value if goal else None,
    }


def create_app(
    database_url: str | None = None,
    get_llm: Callable[[], Completer] | None = None,
    get_hermes: Callable[[], Agent] | None = None,
) -> FastAPI:
    """get_llm / get_hermes are factories, so a missing key only errors when it's needed."""
    logging.basicConfig(level=settings.log_level)
    get_hermes = get_hermes or default_hermes
    mcp = build_mcp(lambda: app.state.db, get_llm or (lambda: LLMClient(settings)))
    # Served at /mcp/. Also creates the session manager that lifespan runs.
    mcp_app = mcp.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.db = create_db_engine(database_url or settings.database_url)
        async with mcp.session_manager.run():
            yield
        app.state.db.dispose()

    app = FastAPI(title="Continuum", lifespan=lifespan)
    app.mount("/mcp", mcp_app)

    @app.exception_handler(eng.NotFound)
    def not_found(request: Request, e: eng.NotFound) -> JSONResponse:
        return error(404, "not_found", str(e))

    @app.exception_handler(eng.InvalidRequest)
    def invalid_action(request: Request, e: eng.InvalidRequest) -> JSONResponse:
        return error(422, "invalid_request", str(e))

    @app.exception_handler(HTTPException)  # unknown paths, wrong methods
    def http_error(request: Request, e: HTTPException) -> JSONResponse:
        return error(e.status_code, "not_found" if e.status_code == 404 else "invalid_request", str(e.detail))

    @app.exception_handler(RequestValidationError)
    def invalid(request: Request, e: RequestValidationError) -> JSONResponse:
        first = e.errors()[0]
        return error(422, "invalid_request", f"{'.'.join(map(str, first['loc']))}: {first['msg']}")

    @app.get("/health")
    def health(session: Session = Depends(get_session)) -> dict[str, str]:
        session.exec(text("SELECT 1"))
        return {"status": "ok"}

    @app.post("/api/chat", response_model=None)
    def chat(body: ChatIn) -> dict[str, str] | JSONResponse:
        try:
            reply = get_hermes().ask(body.message, conversation=body.conversation)
        except HermesError as e:
            log.warning("agent_unavailable")
            return error(503, "agent_unavailable", str(e))
        log.info("chat_ok tools=%s", ",".join(reply.tools_called) or "none")
        return {"reply": reply.text}

    @app.get("/api/goals")
    def goals(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
        return [{**g.goal.model_dump(mode="json"), "open_loops": g.open_loops} for g in eng.list_goals(session)]

    @app.post("/api/goals/{goal_id}/complete")
    def complete_goal(goal_id: UUID, session: Session = Depends(get_session)) -> dict[str, Any]:
        return eng.complete_goal(session, goal_id).model_dump(mode="json")

    @app.get("/api/attention")
    def attention(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
        items = eng.needs_attention(session, eng.local_now().date())
        return [{"loop": loop_out(session, a.loop), "reason": a.reason} for a in items]

    @app.get("/api/loops")
    def loops(
        status: Literal["open", "resolved", "all"] = "open",
        goal_id: UUID | None = None,
        include_snoozed: bool = False,
        session: Session = Depends(get_session),
    ) -> list[dict[str, Any]]:
        found = eng.list_loops(
            session, None if status == "all" else LoopStatus(status), goal_id, include_snoozed=include_snoozed
        )
        return [loop_out(session, loop) for loop in found]

    @app.get("/api/loops/{loop_id}")
    def loop(loop_id: UUID, session: Session = Depends(get_session)) -> dict[str, Any]:
        detail = eng.get_loop(session, loop_id)
        return {**loop_out(session, detail.loop), "source": detail.source.model_dump(mode="json")}

    @app.post("/api/loops/{loop_id}/resolve")
    def resolve(loop_id: UUID, session: Session = Depends(get_session)) -> dict[str, Any]:
        return loop_out(session, eng.resolve_loop(session, loop_id))

    @app.post("/api/loops/{loop_id}/reopen")
    def reopen(loop_id: UUID, session: Session = Depends(get_session)) -> dict[str, Any]:
        return loop_out(session, eng.reopen_loop(session, loop_id))

    @app.post("/api/loops/{loop_id}/snooze")
    def snooze(loop_id: UUID, body: SnoozeIn, session: Session = Depends(get_session)) -> dict[str, Any]:
        return loop_out(session, eng.snooze_loop(session, loop_id, body.until))

    @app.post("/api/loops/{loop_id}/unsnooze")
    def unsnooze(loop_id: UUID, session: Session = Depends(get_session)) -> dict[str, Any]:
        return loop_out(session, eng.unsnooze_loop(session, loop_id))

    @app.delete("/api/loops/{loop_id}", status_code=204)
    def delete(loop_id: UUID, session: Session = Depends(get_session)) -> Response:
        eng.delete_loop(session, loop_id)
        return Response(status_code=204)

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="ui")
    return app


app = create_app()
