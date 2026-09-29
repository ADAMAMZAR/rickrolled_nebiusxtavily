import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from sqlmodel import Session, text

from app.config import settings
from app.db import create_db_engine
from app.extraction import Completer
from app.llm import LLMClient
from app.mcp_tools import build_mcp


def get_session(request: Request) -> Iterator[Session]:
    with Session(request.app.state.db) as session:
        yield session


def create_app(database_url: str | None = None, get_llm: Callable[[], Completer] | None = None) -> FastAPI:
    """get_llm is a factory, so a missing API key only errors when a tool needs the LLM."""
    logging.basicConfig(level=settings.log_level)
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

    @app.get("/health")
    def health(session: Session = Depends(get_session)) -> dict[str, str]:
        session.exec(text("SELECT 1"))
        return {"status": "ok"}

    return app


app = create_app()
