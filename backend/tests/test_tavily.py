import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings, settings
from app.connectors.tavily import TavilyClient, TavilyError

REPLY = {"query": "q", "answer": "Oct 15.", "results": [
    {"title": "Winners", "url": "https://devpost.com/w", "content": "The winners are…", "score": 0.9},
]}


def client(handler: Callable[[httpx.Request], httpx.Response]) -> TavilyClient:
    return TavilyClient(Settings(_env_file=None, tavily_api_key=SecretStr("tvly-test")), httpx.MockTransport(handler))


def test_search_sends_key_and_options() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=REPLY)

    found = client(handler).search("hackathon results", answer=True, time_range="week")
    assert seen[0].headers["Authorization"] == "Bearer tvly-test"
    assert json.loads(seen[0].content) == {"query": "hackathon results", "search_depth": "basic", "max_results": 3,
                                           "include_answer": True, "time_range": "week"}
    assert found.answer == "Oct 15."
    assert [(r.title, r.url) for r in found.results] == [("Winners", "https://devpost.com/w")]


def test_missing_key_is_a_clear_error() -> None:
    with pytest.raises(TavilyError, match="TAVILY_API_KEY"):
        TavilyClient(Settings(_env_file=None, tavily_api_key=SecretStr("")))


@pytest.mark.parametrize(("status", "message"), [(401, "rejected TAVILY_API_KEY"), (432, "HTTP 432")])
def test_http_errors_are_clear(status: int, message: str) -> None:
    with pytest.raises(TavilyError, match=message):
        client(lambda r: httpx.Response(status)).search("q")


def test_unreachable_is_clear() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network")
    with pytest.raises(TavilyError, match="Couldn't reach Tavily"):
        client(down).search("q")


def test_unexpected_reply_is_clear() -> None:
    with pytest.raises(TavilyError, match="unexpected reply"):
        client(lambda r: httpx.Response(200, text="<html>maintenance</html>")).search("q")


@pytest.mark.live
@pytest.mark.skipif(not settings.tavily_api_key.get_secret_value(), reason="TAVILY_API_KEY not set")
def test_live_tavily_search() -> None:
    """The Tavily gate: one real search (1 credit)."""
    found = TavilyClient(settings).search("NVIDIA Nemotron", max_results=1)
    assert found.results and found.results[0].url.startswith("http")


def test_extract_and_include_domains() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if request.url.path == "/extract":
            return httpx.Response(200, json={"results": [{"url": "https://a.com", "raw_content": "Hello"}],
                                             "failed_results": [{"url": "https://b.com", "error": "timeout"}]})
        return httpx.Response(200, json=REPLY)

    tavily = client(handler)
    tavily.search("FalconRise Capital", include_domains=["bnm.gov.my"])
    pages = tavily.extract(["https://a.com", "https://b.com"])
    assert seen[0]["include_domains"] == ["bnm.gov.my"]
    assert seen[1] == {"urls": ["https://a.com", "https://b.com"], "extract_depth": "basic", "format": "text"}
    assert [(p.url, p.raw_content) for p in pages] == [("https://a.com", "Hello")]  # failed URLs left out


def test_cache_replays_the_last_good_reply(tmp_path) -> None:  # type: ignore[no-untyped-def]
    up = [True]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=REPLY) if up[0] else httpx.Response(500)

    tavily = TavilyClient(
        Settings(_env_file=None, tavily_api_key=SecretStr("tvly-test"), tavily_cache_dir=str(tmp_path)),
        httpx.MockTransport(handler),
    )
    assert tavily.search("q", cached=True).answer == "Oct 15."
    up[0] = False
    assert tavily.search("q", cached=True).answer == "Oct 15."  # from the cache
    with pytest.raises(TavilyError, match="HTTP 500"):
        tavily.search("q")  # not cached: the error stands
    with pytest.raises(TavilyError, match="HTTP 500"):
        tavily.search("other", cached=True)  # nothing kept for this request
    assert len(list(tmp_path.iterdir())) == 1


def test_uncached_calls_write_nothing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    tavily = TavilyClient(
        Settings(_env_file=None, tavily_api_key=SecretStr("tvly-test"), tavily_cache_dir=str(tmp_path / "c")),
        httpx.MockTransport(lambda r: httpx.Response(200, json=REPLY)),
    )
    tavily.search("q")
    assert not (tmp_path / "c").exists()
