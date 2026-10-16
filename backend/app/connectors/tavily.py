"""Tavily web search. Continuum calls it itself, so each search is tied to a watched loop or a user's question."""

import hashlib
import json
import logging
from pathlib import Path
from typing import TypeVar

import httpx
from pydantic import BaseModel

from app.config import Settings

log = logging.getLogger("continuum.tavily")

URL = "https://api.tavily.com/search"
EXTRACT_URL = "https://api.tavily.com/extract"
R = TypeVar("R", bound=BaseModel)
NOT_SET_UP = "Web lookup isn't set up. Add TAVILY_API_KEY to .env (free key at tavily.com)."


class TavilyError(Exception):
    pass


class WebResult(BaseModel):
    title: str
    url: str
    content: str = ""
    score: float = 0


class ExtractedPage(BaseModel):
    url: str
    raw_content: str = ""


class Extracted(BaseModel):
    results: list[ExtractedPage] = []


class WebSearch(BaseModel):
    answer: str | None = None
    results: list[WebResult] = []


class TavilyClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        key = settings.tavily_api_key.get_secret_value()
        if not key:
            raise TavilyError(NOT_SET_UP)
        self._http = httpx.Client(headers={"Authorization": f"Bearer {key}"}, timeout=30, transport=transport)
        self._cache = Path(settings.tavily_cache_dir)

    def search(
        self, query: str, *, answer: bool = False, time_range: str | None = None, max_results: int = 3,
        include_domains: list[str] | None = None, cached: bool = False,
    ) -> WebSearch:
        """Basic depth = 1 credit per search."""
        body: dict[str, object] = {"query": query, "search_depth": "basic", "max_results": max_results, "include_answer": answer}
        if time_range:
            body["time_range"] = time_range
        if include_domains:
            body["include_domains"] = include_domains
        return self._post(URL, body, WebSearch, cached)

    def extract(self, urls: list[str], cached: bool = False) -> list[ExtractedPage]:
        """Page text for each URL; URLs Tavily couldn't read are left out. Basic depth = 1 credit per 5 pages.
        Tavily fetches the page, so our server never opens it.
        `cached`: keep each reply; if Tavily fails later, the same request gets the kept reply (demo resilience)."""
        body = {"urls": urls, "extract_depth": "basic", "format": "text"}
        return self._post(EXTRACT_URL, body, Extracted, cached).results

    def _post(self, url: str, body: dict[str, object], reply: type[R], cached: bool = False) -> R:
        """Live call first. With `cached`, a success is kept and a failure falls back to the kept reply."""
        path = self._cache / f"{hashlib.sha256(json.dumps([url, body], sort_keys=True).encode()).hexdigest()}.json"
        try:
            result = self._live(url, body, reply)
        except TavilyError:
            if not (cached and path.exists()):
                raise
            log.warning("tavily_cache_used")
            return reply.model_validate_json(path.read_text(encoding="utf-8"))
        if cached:
            self._cache.mkdir(parents=True, exist_ok=True)
            path.write_text(result.model_dump_json(), encoding="utf-8")
        return result

    def _live(self, url: str, body: dict[str, object], reply: type[R]) -> R:
        try:
            response = self._http.post(url, json=body)
        except httpx.HTTPError as e:
            raise TavilyError(f"Couldn't reach Tavily ({type(e).__name__}).") from e
        if response.status_code == 401:
            raise TavilyError("Tavily rejected TAVILY_API_KEY.")
        if response.is_error:
            raise TavilyError(f"Tavily returned HTTP {response.status_code}.")
        try:
            return reply.model_validate(response.json())
        except ValueError as e:  # not JSON, or not the documented shape
            raise TavilyError("Tavily sent an unexpected reply.") from e
