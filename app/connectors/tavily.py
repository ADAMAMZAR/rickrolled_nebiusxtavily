"""Tavily web search. Continuum calls it itself, so each search is tied to a watched loop or a user's question."""

import httpx
from pydantic import BaseModel

from app.config import Settings

URL = "https://api.tavily.com/search"
NOT_SET_UP = "Web lookup isn't set up. Add TAVILY_API_KEY to .env (free key at tavily.com)."


class TavilyError(Exception):
    pass


class WebResult(BaseModel):
    title: str
    url: str
    content: str = ""


class WebSearch(BaseModel):
    answer: str | None = None
    results: list[WebResult] = []


class TavilyClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        key = settings.tavily_api_key.get_secret_value()
        if not key:
            raise TavilyError(NOT_SET_UP)
        self._http = httpx.Client(headers={"Authorization": f"Bearer {key}"}, timeout=30, transport=transport)

    def search(self, query: str, *, answer: bool = False, time_range: str | None = None, max_results: int = 3) -> WebSearch:
        """Basic depth = 1 credit per search."""
        body: dict[str, object] = {"query": query, "search_depth": "basic", "max_results": max_results, "include_answer": answer}
        if time_range:
            body["time_range"] = time_range
        try:
            response = self._http.post(URL, json=body)
        except httpx.HTTPError as e:
            raise TavilyError(f"Couldn't reach Tavily ({type(e).__name__}).") from e
        if response.status_code == 401:
            raise TavilyError("Tavily rejected TAVILY_API_KEY.")
        if response.is_error:
            raise TavilyError(f"Tavily returned HTTP {response.status_code}.")
        try:
            return WebSearch.model_validate(response.json())
        except ValueError as e:  # not JSON, or not the documented shape
            raise TavilyError("Tavily sent an unexpected reply.") from e
