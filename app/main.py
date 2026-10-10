import base64
import logging
import secrets
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import date
from functools import cache
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol
from uuid import UUID

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints
from sqlmodel import Session, text
from starlette.exceptions import HTTPException

from app import engine as eng
from app import investigation as inv
from app.config import settings, setup_logging
from app.connectors import google
from app.connectors.google import GoogleClient, GoogleError
from app.connectors.tavily import TavilyClient
from app.db import ActionStatus, Activity, Goal, LoopStatus, OpenLoop, PendingAction, Person, create_db_engine
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


class PersonIn(BaseModel):
    email: str | None = None  # empty or null clears it


class WatchIn(BaseModel):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


@cache
def default_hermes() -> HermesClient:
    return HermesClient(settings)


@cache
def default_web() -> TavilyClient:
    return TavilyClient(settings)


def get_session(request: Request) -> Iterator[Session]:
    with Session(request.app.state.db) as session:
        yield session


def error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


def loop_out(session: Session, loop: OpenLoop) -> dict[str, Any]:
    goal = session.get(Goal, loop.goal_id) if loop.goal_id else None
    person = session.get(Person, loop.person_id) if loop.person_id else None
    return {
        **loop.model_dump(mode="json"),
        "goal_title": goal.title if goal else None,
        "goal_status": goal.status.value if goal else None,
        "person_name": person.name if person else None,
        "person_email": person.email if person else None,
        "resolved_by": eng.resolved_by(session, loop),
    }


def activity_out(a: Activity) -> dict[str, Any]:
    return {**a.model_dump(mode="json", exclude={"undo"}), "can_undo": a.undo is not None}


def investigation_out(session: Session, detail: inv.InvestigationDetail) -> dict[str, Any]:
    def rows(items: list[Any]) -> list[dict[str, Any]]:
        return [r.model_dump(mode="json", exclude={"investigation_id"}) for r in items]

    i = detail.investigation
    loop = session.get(OpenLoop, i.loop_id) if i.loop_id else None
    return {
        **i.model_dump(mode="json", exclude={"screenshot_path"}),
        "has_screenshot": i.screenshot_path is not None,
        "loop_title": loop.title if loop else None,
        "entities": rows(detail.entities),
        "claims": rows(detail.claims),
        "evidence": rows(detail.evidence),
        "signals": rows(detail.signals),
        "graph": inv.graph(detail),
    }


def action_out(session: Session, a: PendingAction) -> dict[str, Any]:
    return {**a.model_dump(mode="json"), "summary": eng.describe_action(session, a)}


class Pages(StaticFiles):
    """The dashboard files. Browsers re-check them on each load (an unchanged file is a cheap 304),
    so an update shows up without a hard refresh."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(
    database_url: str | None = None,
    get_llm: Callable[[], Completer] | None = None,
    get_hermes: Callable[[], Agent] | None = None,
    get_web: Callable[[], TavilyClient] | None = None,
    get_google: Callable[[], GoogleClient] | None = None,
) -> FastAPI:
    """get_llm / get_hermes / get_web / get_google are factories, so a missing key only errors when it's needed."""
    setup_logging(settings.log_level)
    get_hermes = get_hermes or default_hermes
    get_google = get_google or (lambda: GoogleClient.from_settings(settings))
    get_llm = get_llm or (lambda: LLMClient(settings))
    get_web = get_web or default_web
    mcp = build_mcp(lambda: app.state.db, get_llm, get_web, get_google)
    # Served at /mcp/. Also creates the session manager that lifespan runs.
    mcp_app = mcp.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.db = create_db_engine(database_url or settings.database_url)
        async with mcp.session_manager.run():
            yield
        app.state.db.dispose()

    app = FastAPI(title="Continuum", lifespan=lifespan)

    @app.middleware("http")
    async def password(request: Request, call_next: Callable[[Request], Any]) -> Any:
        """With APP_PASSWORD set (a public server), everything needs HTTP Basic auth, any username.
        /mcp has no password for Hermes, so it only answers this machine."""
        expected = settings.app_password.get_secret_value()
        if not expected:
            return await call_next(request)
        if request.url.path.startswith("/mcp"):
            if request.client and request.client.host in ("127.0.0.1", "::1"):
                return await call_next(request)
            return error(403, "forbidden", "MCP only answers this machine.")
        scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
        try:
            given = base64.b64decode(encoded).decode().partition(":")[2] if scheme.lower() == "basic" else ""
        except (ValueError, UnicodeDecodeError):
            given = ""
        if secrets.compare_digest(given.encode(), expected.encode()):
            return await call_next(request)
        response = error(401, "unauthorized", "Password needed.")
        response.headers["WWW-Authenticate"] = 'Basic realm="Continuum"'
        return response
    app.mount("/mcp", mcp_app)

    @app.exception_handler(eng.NotFound)
    def not_found(request: Request, e: eng.NotFound) -> JSONResponse:
        return error(404, "not_found", str(e))

    @app.exception_handler(eng.InvalidRequest)
    def invalid_action(request: Request, e: eng.InvalidRequest) -> JSONResponse:
        return error(422, "invalid_request", str(e))

    @app.exception_handler(GoogleError)
    def google_error(request: Request, e: GoogleError) -> JSONResponse:
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
        levels = eng.check_levels(session, [loop.id for loop in found])  # the ledger flags loops a check found risky
        return [{**loop_out(session, loop), "risk_level": levels.get(loop.id)} for loop in found]

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

    @app.get("/api/people")
    def people(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
        return [p.model_dump(mode="json") for p in eng.list_people(session)]

    @app.patch("/api/people/{person_id}")
    def update_person(person_id: UUID, body: PersonIn, session: Session = Depends(get_session)) -> dict[str, Any]:
        person = eng.get_person(session, person_id)
        return eng.set_person_email(session, person.name, body.email).model_dump(mode="json")

    @app.get("/api/web/status")
    def web_status() -> dict[str, bool]:
        return {"enabled": eng.web_enabled()}

    @app.put("/api/loops/{loop_id}/watch")
    def watch(loop_id: UUID, body: WatchIn, session: Session = Depends(get_session)) -> dict[str, Any]:
        return loop_out(session, eng.watch_loop(session, loop_id, body.query))

    @app.delete("/api/loops/{loop_id}/watch")
    def unwatch(loop_id: UUID, session: Session = Depends(get_session)) -> dict[str, Any]:
        return loop_out(session, eng.watch_loop(session, loop_id, None))

    @app.get("/api/activity")
    def activity(loop_id: UUID | None = None, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
        return [activity_out(a) for a in eng.list_activity(session, loop_id)]

    @app.post("/api/activity/{activity_id}/undo")
    def undo(activity_id: UUID, session: Session = Depends(get_session)) -> dict[str, Any]:
        return loop_out(session, eng.undo_activity(session, activity_id))

    @app.get("/api/actions")
    def actions(
        status: Literal["proposed", "done", "rejected", "failed", "all"] = "proposed",
        session: Session = Depends(get_session),
    ) -> list[dict[str, Any]]:
        found = eng.list_actions(session, None if status == "all" else ActionStatus(status))
        return [action_out(session, a) for a in found]

    @app.post("/api/actions/{action_id}/approve")
    def approve(action_id: UUID, session: Session = Depends(get_session)) -> dict[str, Any]:
        return action_out(session, eng.approve_action(session, action_id, get_google))

    @app.post("/api/actions/{action_id}/reject")
    def reject(action_id: UUID, session: Session = Depends(get_session)) -> dict[str, Any]:
        return action_out(session, eng.reject_action(session, action_id))

    @app.post("/api/investigations", status_code=201)
    def create_investigation(
        background: BackgroundTasks,
        text: Annotated[str | None, Form()] = None,
        url: Annotated[str | None, Form()] = None,
        screenshot: Annotated[UploadFile | None, File()] = None,
        loop_id: Annotated[UUID | None, Form()] = None,
        session: Session = Depends(get_session),
    ) -> dict[str, Any]:
        # Read one byte past the limit, so an oversized file is rejected without reading all of it.
        data = screenshot.file.read(inv.MAX_SCREENSHOT + 1) if screenshot and screenshot.filename else None
        created = inv.create_investigation(session, text, url, data, loop_id)
        background.add_task(inv.run_investigation, app.state.db, created.id, get_llm, get_web)
        return {"id": str(created.id)}

    @app.get("/api/investigations")
    def investigations(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
        return [
            i.model_dump(mode="json", include={"id", "status", "risk_level", "input_text", "input_url", "created_at"})
            for i in inv.list_investigations(session)
        ]

    @app.get("/api/investigations/{investigation_id}")
    def investigation(investigation_id: UUID, session: Session = Depends(get_session)) -> dict[str, Any]:
        return investigation_out(session, inv.get_investigation(session, investigation_id))

    @app.get("/api/google/status")
    def google_status(session: Session = Depends(get_session)) -> dict[str, Any]:
        return {
            "connected": google.connected(settings),
            "set_up": Path(settings.google_client_secret_file).exists(),  # the OAuth client file is there
            "last_sync": eng.get_setting(session, eng.LAST_EMAIL_SYNC),
        }

    @app.post("/api/google/connect")
    def google_connect(session: Session = Depends(get_session)) -> dict[str, Any]:
        """Opens Google's consent screen in this computer's browser and waits (up to 5 minutes)."""
        google.connect(settings)
        log.info("google_connected")
        return google_status(session)

    @app.post("/api/google/disconnect")
    def google_disconnect(session: Session = Depends(get_session)) -> dict[str, Any]:
        google.disconnect(settings)
        log.info("google_disconnected")
        return google_status(session)

    @app.post("/api/google/forget")
    def forget_email(session: Session = Depends(get_session)) -> dict[str, int]:
        return {"forgotten": eng.forget_email_data(session)}

    app.mount("/", Pages(directory=STATIC, html=True), name="ui")
    return app


app = create_app()
