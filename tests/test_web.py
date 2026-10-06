"""Web watch (spec §7): only watched open loops are searched, once a day, and findings only become proposals."""

from datetime import date, timedelta

import pytest
from sqlmodel import Session

from app import engine as eng
from app.connectors.tavily import TavilyError, WebResult
from app.db import ActivityBy, LoopStatus, OpenLoop
from app.extraction import WEB_NOTE
from tests.conftest import NOW, FakeLLM
from tests.test_activity import demo, history
from tests.test_process_message import result

PAGE = WebResult(title="Hackathon winners announced", url="https://devpost.com/winners", content="The winners are...")
OTHER = WebResult(title="Some blog", url="https://blog.example.com/post", content="Nothing about it.")


class FakeWeb:
    """Returns queued result lists per query, or raises TavilyError for queries in `fail`."""

    def __init__(self, results: list[WebResult] | None = None, fail: set[str] | None = None) -> None:
        self.results = results or []
        self.fail = fail or set()
        self.queries: list[str] = []

    def __call__(self, query: str) -> list[WebResult]:
        self.queries.append(query)
        if query in self.fail:
            raise TavilyError("Tavily returned HTTP 432.")
        return self.results


def watched(session: Session) -> tuple[OpenLoop, OpenLoop]:
    wait, portfolio = demo(session)
    eng.watch_loop(session, wait.id, "NVIDIA hackathon winners")
    return wait, portfolio


def run(session: Session, llm: FakeLLM, web: FakeWeb, now=NOW) -> tuple[list[str], list[str]]:
    proposals, errors = eng.watch_the_web(session, llm, web, now=now)
    return [eng.describe_action(session, a) for a in proposals], errors


@pytest.mark.usefixtures("web_on")
def test_a_finding_becomes_a_proposal_not_a_change(session: Session) -> None:
    wait, _ = watched(session)
    llm = FakeLLM(result(resolved=[str(wait.id)]))
    found, errors = run(session, llm, FakeWeb([PAGE]))
    assert found == ["Resolve \"Wait for Sarah's response\"? Found on the web: Hackathon winners announced (devpost.com)"]
    assert errors == []
    assert eng.get_loop(session, wait.id).loop.status == LoopStatus.open
    prompt = llm.calls[0][1]["content"]
    assert WEB_NOTE in prompt and str(wait.id) in prompt and "devpost.com/winners" in prompt


@pytest.mark.usefixtures("web_on")
def test_only_watched_open_loops_once_a_day(session: Session) -> None:
    wait, portfolio = watched(session)
    web = FakeWeb()
    run(session, FakeLLM(), web)
    run(session, FakeLLM(), web)  # same day: no second search
    assert web.queries == ["NVIDIA hackathon winners"]

    run(session, FakeLLM(), web, now=NOW + timedelta(days=1))
    assert len(web.queries) == 2
    eng.resolve_loop(session, wait.id)  # resolved loops aren't searched
    run(session, FakeLLM(), web, now=NOW + timedelta(days=2))
    assert len(web.queries) == 2


@pytest.mark.usefixtures("web_on")
def test_a_page_seen_before_is_skipped(session: Session) -> None:
    wait, _ = watched(session)
    run(session, FakeLLM(result(resolved=[str(wait.id)])), FakeWeb([PAGE]))
    [action] = eng.list_actions(session)
    eng.reject_action(session, action.id)
    llm = FakeLLM()  # no replies queued: any LLM call would fail the test
    assert run(session, llm, FakeWeb([PAGE]), now=NOW + timedelta(days=1)) == ([], [])
    assert llm.calls == []


@pytest.mark.usefixtures("web_on")
def test_a_result_can_only_touch_its_own_loop(session: Session) -> None:
    wait, portfolio = watched(session)
    reply = result(
        goals=[{"title": "Win the hackathon"}],
        new=[{"title": "Book flights", "summary": "x", "kind": "task"}],
        updated=[{"id": str(portfolio.id), "due": "2026-10-20"}],
        resolved=[str(portfolio.id)],
    )
    assert run(session, FakeLLM(reply), FakeWeb([PAGE])) == ([], [])
    assert eng.list_actions(session) == []
    assert len(eng.list_loops(session)) == 2


@pytest.mark.usefixtures("web_on")
def test_a_new_due_date_is_proposed_per_page(session: Session) -> None:
    wait, _ = watched(session)
    same_due = result(updated=[{"id": str(wait.id), "due": "2026-10-02"}])  # not a change
    new_due = result(updated=[{"id": str(wait.id), "due": "2026-10-16", "summary": "ignored"}])
    found, _ = run(session, FakeLLM(same_due, new_due), FakeWeb([OTHER, PAGE]))
    assert found == ["Set \"Wait for Sarah's response\" due Fri Oct 16? Found on the web: Hackathon winners announced (devpost.com)"]
    assert eng.get_loop(session, wait.id).loop.due == date(2026, 10, 2)


@pytest.mark.usefixtures("web_on")
def test_a_tavily_error_skips_that_loop_only(session: Session) -> None:
    wait, portfolio = watched(session)
    eng.watch_loop(session, portfolio.id, "portfolio review date")
    web = FakeWeb([PAGE], fail={"NVIDIA hackathon winners"})
    found, errors = run(session, FakeLLM(result(resolved=[str(portfolio.id)])), web)
    assert found == ['Resolve "Finish portfolio"? Found on the web: Hackathon winners announced (devpost.com)']
    assert errors == ["Couldn't check the web for \"Wait for Sarah's response\": Tavily returned HTTP 432."]
    run(session, FakeLLM(), web)
    assert len(web.queries) == 2  # the failed loop waits until tomorrow too


@pytest.mark.usefixtures("web_on")
def test_an_unreadable_result_is_skipped(session: Session) -> None:
    wait, _ = watched(session)
    llm = FakeLLM("nope", "still nope", result(resolved=[str(wait.id)]))
    found, _ = run(session, llm, FakeWeb([OTHER, PAGE]))
    assert len(found) == 1


@pytest.mark.usefixtures("web_on")
def test_watch_start_change_stop(session: Session) -> None:
    wait, _ = demo(session)
    loop = eng.watch_loop(session, wait.id, "  NVIDIA   hackathon  ", by=ActivityBy.chat)
    assert (loop.watch_query, loop.watch_checked_on) == ("NVIDIA hackathon", None)
    assert ("updated", "chat", True) in history(session, wait)
    assert eng.watch_loop(session, wait.id, "").watch_query is None  # empty = stop
    with pytest.raises(eng.InvalidRequest, match="400 characters"):
        eng.watch_loop(session, wait.id, "x" * 401)
    eng.resolve_loop(session, wait.id)
    with pytest.raises(eng.InvalidRequest, match="already resolved"):
        eng.watch_loop(session, wait.id, "NVIDIA hackathon")


@pytest.mark.usefixtures("web_off")
def test_watching_needs_a_tavily_key(session: Session) -> None:
    wait, _ = demo(session)
    with pytest.raises(eng.InvalidRequest, match="TAVILY_API_KEY"):
        eng.watch_loop(session, wait.id, "NVIDIA hackathon")
    assert eng.watch_loop(session, wait.id, None).watch_query is None  # stopping always works
