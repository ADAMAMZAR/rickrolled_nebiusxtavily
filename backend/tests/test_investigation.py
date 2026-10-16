import json
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app import engine as eng
from app import investigation as inv
from app.config import settings
from app.connectors.tavily import ExtractedPage, TavilyError, WebResult, WebSearch
from app.db import Entity, EntityType, Investigation, LoopKind, RiskLevel
from app.extraction import SCAM_INVALID, ScamExtraction
from app.investigation import appears, canonical
from app.llm import LLMError
from app.main import create_app

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
MESSAGE = (
    "T. Rowe Price Group Sdn. Bhd. here. Our fund pays a fixed 12% monthly return, approved by the Securities "
    "Commission. Register at troweprice-my-invest.com and transfer before Friday. Call +60 12-000 5678."
)
EXTRACTION = {
    "entities": [
        {"type": "org", "value": "T. Rowe Price Group Sdn. Bhd."},
        {"type": "domain", "value": "troweprice-my-invest.com"},
        {"type": "phone", "value": "+60120005678"},  # same digits, different spacing: kept
        {"type": "org", "value": "Securities Commission"},
        {"type": "org", "value": "t. rowe price group sdn. bhd."},  # duplicate: once
        {"type": "email", "value": "advisor@troweprice-my-invest.com"},  # not in the message: dropped
        {"type": "wallet", "value": "x"},  # unknown type: dropped
    ],
    "claims": [
        {"text": "The fund pays a fixed 12% monthly return.", "category": "investment"},
        {"text": "The fund is approved by the Securities Commission.", "category": "regulatory"},
    ],
    "behaviours": [
        {"kind": "payment_pressure", "quote": "transfer before Friday"},
        {"kind": "guaranteed_returns", "quote": "guaranteed 50% weekly"},  # not in the message: dropped
    ],
}
EMPTY = {"entities": [], "claims": [], "behaviours": []}


class ScamLLM:
    """Plain text for the vision model. Nemotron: queued extraction replies, one plan, and page checks by URL
    (pages are checked in parallel, so those can't be a queue)."""

    def __init__(self, *replies: dict[str, Any] | str | Exception, screenshot_text: str = "") -> None:
        self.replies = list(replies)
        self.screenshot_text = screenshot_text
        self.plan: Any = {"searches": []}
        self.summary: Any = {"findings": [], "next_steps": []}  # a reply, an Exception, or a function of the facts
        self.pages: dict[str, dict[str, Any] | Exception] = {}  # url -> page check reply
        self.slow: dict[str, float] = {}  # url -> seconds the page check takes
        self.calls: list[tuple[str | None, list[dict[str, Any]]]] = []

    def complete(self, messages: list[dict[str, Any]], json_mode: bool = False, model: str | None = None) -> str:
        self.calls.append((model, messages))
        if model == settings.nebius_vision_model:
            return self.screenshot_text
        system = messages[0]["content"]
        if system.startswith("You plan ScamGraph"):
            reply: Any = self.plan.pop(0) if isinstance(self.plan, list) else self.plan  # a list: one per round
        elif system.startswith("You write ScamGraph"):
            reply = self.summary(json.loads(messages[1]["content"])) if callable(self.summary) else self.summary
        elif system.startswith("You check one web page"):
            url = next((u for u in self.pages | self.slow if u in messages[1]["content"]), "")
            time.sleep(self.slow.get(url, 0))
            reply = self.pages.get(url, {"evidence": [], "official": []})
            if callable(reply):
                reply = reply(messages[1]["content"])
        else:
            reply = self.replies.pop(0) if self.replies else EMPTY
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)

    def kinds(self) -> list[str]:
        """Which prompt each call was, in order."""
        def kind(model: str | None, system: Any) -> str:
            if model:
                return "vision"
            starts = {"You plan": "plan", "You check": "check", "You write": "summary"}
            return next((k for p, k in starts.items() if system.startswith(p)), "extract")
        return [kind(m, msgs[0]["content"]) for m, msgs in self.calls]


class FakeWeb:
    def __init__(self, texts: dict[str, str] | None = None, error: str | None = None) -> None:
        self.texts = texts or {}  # url -> page text for extract
        self.results: dict[str, list[WebResult]] = {}  # query -> search results
        self.error = error  # every call fails
        self.failing: set[str] = set()  # queries that fail
        self.searched: list[tuple[str, list[str] | None]] = []
        self.extracted: list[list[str]] = []

    def search(self, query: str, *, max_results: int = 3, include_domains: list[str] | None = None,
               cached: bool = False) -> WebSearch:
        self.searched.append((query, include_domains))
        if self.error or query in self.failing:
            raise TavilyError(self.error or "Tavily returned HTTP 500.")
        key = f"{query} @{include_domains[0]}" if include_domains else query
        return WebSearch(results=self.results.get(key, []))

    def extract(self, urls: list[str], cached: bool = False) -> list[ExtractedPage]:
        self.extracted.append(urls)
        if self.error:
            raise TavilyError(self.error)
        return [ExtractedPage(url=u, raw_content=self.texts[u]) for u in urls if u in self.texts]


@pytest.fixture
def uploads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "uploads"
    monkeypatch.setattr(settings, "uploads_dir", str(folder))
    return folder


@pytest.fixture
def llm() -> ScamLLM:
    return ScamLLM(EXTRACTION)


@pytest.fixture
def web() -> FakeWeb:
    return FakeWeb({"https://www.troweprice-my-invest.com/join?ref=1": "Welcome to T Rowe Price Asia. Deposit now."})


@pytest.fixture
def client(db_url: str, uploads: Path, llm: ScamLLM, web: FakeWeb) -> Iterator[TestClient]:
    app = create_app(db_url, get_llm=lambda: llm, get_web=lambda: web)  # type: ignore[arg-type, return-value]
    with TestClient(app, base_url="http://127.0.0.1:8000") as http:
        yield http


def investigate(client: TestClient, **data: Any) -> dict[str, Any]:
    """Create one; TestClient runs the background pipeline before returning."""
    files = data.pop("files", None)
    response = client.post("/api/investigations", data=data, files=files)
    assert response.status_code == 201, response.text
    return client.get(f"/api/investigations/{response.json()['id']}").json()


# --- H0: intake and storage ---


def test_list_newest_first(client: TestClient) -> None:
    first = investigate(client, text="one")["id"]
    second = investigate(client, text="two")["id"]
    assert [i["id"] for i in client.get("/api/investigations").json()] == [second, first]


def test_persists_across_restart(db_url: str, uploads: Path) -> None:
    """H0 gate: an investigation survives an app restart."""
    app = lambda: create_app(db_url, get_llm=lambda: ScamLLM(EMPTY), get_web=FakeWeb)  # type: ignore[arg-type, return-value]
    with TestClient(app()) as http:
        made = http.post("/api/investigations", data={"text": MESSAGE}).json()["id"]
    with TestClient(app()) as http:
        assert http.get(f"/api/investigations/{made}").json()["input_text"] == MESSAGE


def test_screenshot_saved_outside_static(client: TestClient, uploads: Path) -> None:
    got = investigate(client, files={"screenshot": ("shot.png", PNG, "image/png")})
    assert (uploads / f"{got['id']}.png").read_bytes() == PNG
    assert got["has_screenshot"] is True and "screenshot_path" not in got
    assert client.get(f"/{got['id']}.png").status_code == 404


@pytest.mark.parametrize(
    ("data", "files", "message"),
    [
        ({}, None, "Paste a message"),
        ({"text": "   "}, None, "Paste a message"),
        ({"text": "x" * 8001}, None, "8000"),
        ({"url": "ftp://example.com"}, None, "http"),
        ({"url": "javascript:alert(1)"}, None, "http"),
        ({}, {"screenshot": ("a.png", b"not an image", "image/png")}, "PNG, JPEG or WebP"),
        ({}, {"screenshot": ("a.png", PNG + b"\x00" * (5 * 1024 * 1024), "image/png")}, "5 MB"),
    ],
)
def test_invalid_input_rejected(
    client: TestClient, uploads: Path, llm: ScamLLM, data: dict, files: dict | None, message: str
) -> None:
    response = client.post("/api/investigations", data=data, files=files)
    assert response.status_code == 422
    assert message in response.json()["message"]
    assert client.get("/api/investigations").json() == []
    assert not uploads.exists() or not any(uploads.iterdir())
    assert llm.calls == []


def test_unknown_investigation_404(client: TestClient) -> None:
    response = client.get("/api/investigations/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404 and response.json()["error"] == "not_found"


def test_page_served(client: TestClient) -> None:
    """Both pages are the React build in app/static (frontend/: npm run build); every asset they load is served."""
    for page in ("/", "/investigate.html"):
        html = client.get(page).text
        assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)
        assert '<div id="root">' in html and any(a.endswith(".js") for a in assets)
        assert all(client.get(a).status_code == 200 for a in assets), assets
    assert "ScamGraph" in client.get("/investigate.html").text
    # Browsers re-check pages, so an update shows without a hard refresh (2026-10-10: a stale tab hid one).
    assert client.get("/investigate.html").headers["cache-control"] == "no-cache"


# --- H1: extraction ---


def test_text_is_extracted_and_checked(client: TestClient, llm: ScamLLM, web: FakeWeb) -> None:
    got = investigate(client, text=MESSAGE)
    assert got["status"] == "done" and got["error"] is None
    assert {(e["type"], e["canonical"]) for e in got["entities"]} == {
        ("org", "t. rowe price group sdn. bhd."),
        ("domain", "troweprice-my-invest.com"),
        ("phone", "60120005678"),
        ("org", "securities commission"),
    }
    assert [(c["category"], c["verdict"]) for c in got["claims"]] == [
        ("investment", "insufficient_evidence"), ("regulatory", "insufficient_evidence"),
    ]
    assert [(s["kind"], s["weight"], s["input_quote"]) for s in got["signals"]] == [
        ("payment_pressure", 15, "transfer before Friday"),
    ]
    assert [s["text"] for s in got["steps"]] == [
        "Received", "Reading the message with Nemotron",
        "Found 4 names and contacts, 2 claims, 1 pressure tactic",
        "Planning the research with Nemotron",
        "Checking SC's Investor Alert List for T. Rowe Price Group Sdn. Bhd.",
        "Checking Bank Negara's alert list for T. Rowe Price Group Sdn. Bhd.",
        "Finding T. Rowe Price Group Sdn. Bhd.'s official website",
        "0 sources found",
        "Claims: 2 without evidence",
        "Risk GUARDED (score 15), confidence LOW",
        "Writing the summary with Nemotron",
        "Done",
    ]
    assert web.extracted == []  # no link, no Tavily
    model, messages = llm.calls[0]
    assert model is None  # NEBIUS_MODEL: Nemotron
    assert "untrusted" in messages[0]["content"] and MESSAGE in messages[1]["content"]


def test_link_is_read_through_tavily(client: TestClient, web: FakeWeb, llm: ScamLLM) -> None:
    llm.replies = [EMPTY]
    got = investigate(client, url="https://www.troweprice-my-invest.com/join?ref=1")
    assert web.extracted == [["https://www.troweprice-my-invest.com/join?ref=1"]]
    assert "[Text on the linked page]\nWelcome to T Rowe Price Asia." in got["input_text"]
    assert {(e["type"], e["canonical"]) for e in got["entities"]} == {
        ("url", "https://www.troweprice-my-invest.com/join?ref=1"),
        ("domain", "troweprice-my-invest.com"),
    }
    assert "Reading the link through Tavily" in [s["text"] for s in got["steps"]]


def test_unreadable_link_keeps_going_with_the_text(client: TestClient, web: FakeWeb) -> None:
    web.error = "Tavily returned HTTP 500."
    got = investigate(client, text=MESSAGE, url="https://troweprice-my-invest.com")
    assert got["status"] == "done"
    assert "Couldn't read the link: Tavily returned HTTP 500." in [s["text"] for s in got["steps"]]
    assert got["input_text"] == MESSAGE


def test_link_only_and_unreadable_fails(client: TestClient, web: FakeWeb, llm: ScamLLM) -> None:
    web.error = "Tavily returned HTTP 500."
    got = investigate(client, url="https://troweprice-my-invest.com")
    assert got["status"] == "failed" and got["error"] == "There was no text to investigate."
    assert got["risk_level"] is None and got["entities"] == []
    assert llm.calls == []


def test_screenshot_goes_to_the_vision_model(client: TestClient, llm: ScamLLM) -> None:
    llm.screenshot_text = MESSAGE
    got = investigate(client, files={"screenshot": ("shot.png", PNG, "image/png")})
    assert got["status"] == "done"
    assert got["input_text"] == f"[Text in the screenshot]\n{MESSAGE}"
    (vision, messages), (nemotron, _) = llm.calls[:2]
    assert vision == settings.nebius_vision_model and nemotron is None
    image = messages[0]["content"][1]["image_url"]["url"]
    assert image.startswith("data:image/png;base64,")
    assert got["entities"]  # extracted from the screenshot's text


def test_bad_json_twice_fails_and_saves_nothing(client: TestClient, llm: ScamLLM) -> None:
    llm.replies = ["not json", '{"entities": "nope"}x']
    got = investigate(client, text=MESSAGE)
    assert got["status"] == "failed" and got["error"] == SCAM_INVALID
    assert got["entities"] == got["claims"] == got["signals"] == [] and got["risk_level"] is None
    assert got["steps"][-1]["text"] == f"Stopped: {SCAM_INVALID}"
    assert len(llm.calls) == 2  # one retry


def test_nebius_error_fails_clearly(client: TestClient, llm: ScamLLM) -> None:
    llm.replies = [LLMError("Nebius timed out.")]
    got = investigate(client, text=MESSAGE)
    assert got["status"] == "failed" and got["error"] == "Nebius timed out."


def test_missing_key_fails_clearly(db_url: str, uploads: Path) -> None:
    """The real factory with no NEBIUS_API_KEY (blanked for offline tests)."""
    with TestClient(create_app(db_url)) as http:
        made = http.post("/api/investigations", data={"text": MESSAGE}).json()["id"]
        got = http.get(f"/api/investigations/{made}").json()
    assert got["status"] == "failed" and "NEBIUS_API_KEY" in got["error"]


@pytest.mark.parametrize(
    ("needle", "text", "phone", "found"),
    [
        ("Transfer   TODAY", "please transfer today ok", False, True),
        ("transfer tomorrow", "please transfer today", False, False),
        ("", "anything", False, False),
        ("+60 12-000 5678", "call +6012 0005678", True, True),
        ("+60 12-999 5678", "call +6012 0005678", True, False),
        ("12345", "id 12345", True, False),  # too short to count as a phone
        ("T Rowe Price Group Sdn. Bhd", "| T Rowe Price Group Sdn.Bhd (potential clone entity) |", False, True),
    ],
)
def test_appears(needle: str, text: str, phone: bool, found: bool) -> None:
    assert appears(needle, text, phone) is found


@pytest.mark.parametrize(
    ("kind", "value", "expected"),
    [
        (EntityType.domain, "https://WWW.Abc-Invest.com/x?y", "abc-invest.com"),
        (EntityType.domain, "maybank2u.com.my", "maybank2u.com.my"),
        (EntityType.phone, "+60 12-000 5678", "60120005678"),
        (EntityType.email, " Advisor@Example.COM ", "advisor@example.com"),
        (EntityType.org, "T. Rowe  Price", "t. rowe price"),
    ],
)
def test_canonical(kind: EntityType, value: str, expected: str) -> None:
    assert canonical(kind, value) == expected


# --- H2: research ---

ORG = "T. Rowe Price Group Sdn. Bhd."
ALERT = "https://www.bnm.gov.my/financial-consumer-alert-list"
OFFICIAL = "https://www.troweprice.com/about"
FB = "https://www.facebook.com/trp-my"
OWN = "https://troweprice-my-invest.com/"  # the message's own site
TEXTS = {
    ALERT: "Financial Consumer Alert List\n| T Rowe Price Group Sdn.Bhd (potential clone entity) |  | 3 Aug 2026 |",
    OFFICIAL: "T. Rowe Price Group, Inc. Visit us at troweprice.com. Call +1 410-345-2000.",
    FB: "Official T Rowe Price Malaysia page. Website troweprice-asia.com",
    OWN: "Welcome investor. We are licensed.",
}


def id_of(content: str, value: str) -> str:
    """The id the prompt gave a name or claim (ids depend on row order, so tests look them up)."""
    names = json.loads(content.split("Names and contacts: ", 1)[1].split("\n\nClaims: ", 1)[0])
    claims = json.loads(content.split("\n\nClaims: ", 1)[1].split("\n\nPage: ", 1)[0])
    return next(i["id"] for i in names + claims if value in (i.get("value"), i.get("text")))


@pytest.fixture
def researched(web: FakeWeb, llm: ScamLLM) -> FakeWeb:
    web.texts |= TEXTS
    web.results = {
        f"{ORG} @bnm.gov.my": [WebResult(title="FCA list", url=ALERT, score=0.9)],
        f"{ORG} official website": [
            WebResult(title="Join", url=OWN, score=0.99),
            WebResult(title="About", url=OFFICIAL, score=0.8),
            WebResult(title="TRP MY", url=FB, score=0.95),
            WebResult(title="FCA list", url=ALERT, score=0.5),  # duplicate
        ],
    }
    llm.plan = {"searches": [{"query": "T Rowe Price Malaysia licence", "include_domains": ["sc.com.my"]}]}
    llm.pages = {
        ALERT: lambda c: {"evidence": [
            {"about": id_of(c, ORG), "direction": "warns", "quote": "T Rowe Price Group Sdn.Bhd (potential clone entity)"},
            {"about": id_of(c, "The fund is approved by the Securities Commission."), "direction": "contradicts",
             "quote": "Not approved by anyone."},  # not on the page: dropped
            {"about": "e99", "direction": "warns", "quote": "Financial Consumer Alert List"},  # unknown id: dropped
            {"about": id_of(c, "The fund is approved by the Securities Commission."), "direction": "contradicts",
             "quote": "Financial Consumer Alert List"},  # on the page, but names nothing from the message: dropped
        ], "official": [
            {"org": id_of(c, ORG), "type": "domain", "value": "bnm.gov.my",
             "quote": "Financial Consumer Alert List"},  # value not in quote: dropped
        ]},
        OFFICIAL: lambda c: {"evidence": [], "official": [
            {"org": id_of(c, ORG), "type": "domain", "value": "troweprice.com", "quote": "Visit us at troweprice.com."},
            {"org": id_of(c, ORG), "type": "phone", "value": "+1 410-345-2000", "quote": "Call +1 410-345-2000."},
            {"org": id_of(c, ORG), "type": "domain", "value": "trp.com", "quote": "Visit us at troweprice.com."},  # not in quote
            {"org": id_of(c, "troweprice-my-invest.com"), "type": "domain", "value": "troweprice.com",
             "quote": "Visit us at troweprice.com."},  # not an organisation
        ]},
        FB: lambda c: {"evidence": [], "official": [  # tier E can't state official contacts
            {"org": id_of(c, ORG), "type": "domain", "value": "troweprice-asia.com", "quote": "Website troweprice-asia.com"},
        ]},
    }
    return web


@pytest.mark.parametrize(("page", "verified"), [
    ("Our Malaysian investors log in at troweprice-my-invest.com.", True),
    ("Beware: troweprice-my-invest.com is a fake site, not affiliated with us.", False),
])
def test_official_site_can_confirm_a_second_domain(
    client: TestClient, researched: FakeWeb, page: str, verified: bool
) -> None:
    """Big companies have several domains: the official site mentioning the submitted one, with no warning
    around it, verifies it. The model doesn't have to notice."""
    portal = "https://www.troweprice.com/portals"
    researched.texts[portal] = page
    researched.results["troweprice-my-invest.com @troweprice.com"] = [
        WebResult(title="Careers", url="https://www.troweprice.com/careers", score=0.95),  # doesn't mention it: skipped
        WebResult(title="Portals", url=portal, content="troweprice-my-invest.com", score=0.9),
    ]
    got = investigate(client, text=MESSAGE)
    kinds = {s["kind"] for s in got["signals"]}
    assert ("verified_domain" in kinds) is verified and ("official_domain_mismatch" in kinds) is not verified
    assert researched.extracted[-1] == [portal]


def test_research_finds_checks_and_tiers_evidence(client: TestClient, researched: FakeWeb, llm: ScamLLM) -> None:
    got = investigate(client, text=MESSAGE)
    assert got["status"] == "done"
    # Fixed searches for the company (not for the Securities Commission, a regulator), then the model's.
    assert researched.searched == [
        (ORG, ["sc.com.my"]), (ORG, ["bnm.gov.my"]), (f"{ORG} official website", None),
        ("T Rowe Price Malaysia licence", ["sc.com.my"]),
        ("T Rowe Price Malaysia licence", ["sc.com.my"]),  # follow-up: the regulatory claim had no evidence
        ("troweprice-my-invest.com", ["troweprice.com"]),  # the submitted domain, asked on the official site
    ]
    # Ranked by tier then score; the message's own site and the duplicate are left out.
    assert researched.extracted == [[ALERT, OFFICIAL, FB]]
    rows = {(e["host"], e["tier"], e["direction"], e["official_type"], e["official_value"]) for e in got["evidence"]}
    assert rows == {
        ("bnm.gov.my", "A", "warns", None, None),
        ("troweprice.com", "B", "supports", "domain", "troweprice.com"),  # official domain makes the site tier B
        ("troweprice.com", "B", "supports", "phone", "14103452000"),
    }
    warning = next(e for e in got["evidence"] if e["tier"] == "A")
    assert warning["entity_id"] == next(e["id"] for e in got["entities"] if e["value"] == ORG)
    assert warning["quote"] == "T Rowe Price Group Sdn.Bhd (potential clone entity)"
    steps = [s["text"] for s in got["steps"]]
    assert "Searching the web: T Rowe Price Malaysia licence" in steps
    assert "3 sources found, reading the top 3" in steps
    assert "Checking 3 pages against the claims with Nemotron" in steps
    assert "3 pieces of evidence from 2 pages (1 from regulators)" in steps
    assert llm.kinds() == ["extract", "plan", "check", "check", "check", "plan", "summary"]
    assert "Looking further for 1 claim without evidence" in steps


def test_clone_is_scored_from_its_evidence(client: TestClient, researched: FakeWeb, llm: ScamLLM) -> None:
    """The fixture is scenario B: BNM warns about the name, the official site is troweprice.com with another phone."""
    got = investigate(client, text=MESSAGE)
    signals = {s["kind"]: s for s in got["signals"]}
    assert set(signals) == {"regulatory_warning", "official_domain_mismatch", "contact_mismatch", "payment_pressure"}
    assert (got["score"], got["risk_level"], got["confidence"], got["identity"]) == (95, "CRITICAL", "HIGH", "mismatch")
    evidence = {e["id"]: e for e in got["evidence"]}
    assert evidence[signals["regulatory_warning"]["evidence_id"]]["host"] == "bnm.gov.my"
    assert evidence[signals["official_domain_mismatch"]["evidence_id"]]["official_value"] == "troweprice.com"
    assert signals["payment_pressure"]["input_quote"] == "transfer before Friday"
    claims = {c["category"]: c["verdict"] for c in got["claims"]}
    assert claims == {"investment": "insufficient_evidence", "regulatory": "insufficient_evidence"}
    steps = [s["text"] for s in got["steps"]]
    assert "Risk CRITICAL (score 95), confidence HIGH" in steps and steps[-2:] == ["Writing the summary with Nemotron", "Done"]


def test_summary_findings_must_cite_and_not_accuse(client: TestClient, researched: FakeWeb, llm: ScamLLM) -> None:
    def write(facts: dict[str, Any]) -> dict[str, Any]:
        warning = next(s["id"] for s in facts["signals"] if "regulator" in s["reason"])
        assert facts["risk_level"] == "CRITICAL" and all(e["id"].startswith("v") for e in facts["evidence"])
        return {"findings": [
            {"text": "Bank Negara's alert list names T Rowe Price Group Sdn.Bhd as a potential clone.", "cites": [warning, "v1"]},
            {"text": "They are scammers.", "cites": [warning]},  # accusation: dropped
            {"text": "Signal s1 shows a warning.", "cites": [warning]},  # internal id in the text: dropped
            {"text": "The fund is fake.", "cites": ["v99"]},  # unknown id: dropped
            {"text": "Nothing cited.", "cites": []},  # dropped
        ], "next_steps": ["Don't pay.", "", "Call T. Rowe Price on the number on troweprice.com."]}

    llm.summary = write
    got = investigate(client, text=MESSAGE)
    assert [f["text"] for f in got["findings"]] == ["Bank Negara's alert list names T Rowe Price Group Sdn.Bhd as a potential clone."]
    finding = got["findings"][0]
    warning = next(s for s in got["signals"] if s["kind"] == "regulatory_warning")
    assert finding["signal_ids"] == [warning["id"]] and len(finding["evidence_ids"]) == 1
    assert got["next_steps"] == ["Don't pay.", "Call T. Rowe Price on the number on troweprice.com."]


def test_failed_summary_falls_back_to_the_rules(client: TestClient, researched: FakeWeb, llm: ScamLLM) -> None:
    llm.summary = LLMError("Nebius timed out.")
    got = investigate(client, text=MESSAGE)
    assert got["status"] == "done" and got["risk_level"] == "CRITICAL"
    assert got["findings"][0]["text"] == "A regulator's warning on bnm.gov.my names it"  # strongest signal first
    assert len(got["findings"]) == 4 and all(f["signal_ids"] for f in got["findings"])
    assert got["next_steps"] == inv.CAUTION


def test_follow_up_runs_once_and_skips_pages_already_read(client: TestClient, researched: FakeWeb, llm: ScamLLM) -> None:
    llm.plan = [{"searches": []}, {"searches": [{"query": "official website again"}]}]
    researched.results["official website again"] = [
        WebResult(title="About", url=OFFICIAL, score=0.9),  # read in round 1: skipped
        WebResult(title="News", url="https://www.thestar.com.my/trp", score=0.5),
    ]
    researched.texts["https://www.thestar.com.my/trp"] = "T Rowe Price Group Sdn.Bhd is not approved by the SC."
    investigate(client, text=MESSAGE)
    assert researched.extracted[1] == ["https://www.thestar.com.my/trp"]
    assert len(researched.extracted) == 2 and llm.kinds().count("plan") == 2


def test_no_follow_up_when_material_claims_have_evidence(client: TestClient, researched: FakeWeb, llm: ScamLLM) -> None:
    alert = llm.pages[ALERT]
    llm.pages[ALERT] = lambda c: alert(c) | {"evidence": [
        {"about": id_of(c, "The fund is approved by the Securities Commission."), "direction": "contradicts",
         "quote": "T Rowe Price Group Sdn.Bhd (potential clone entity)"},
    ]}
    got = investigate(client, text=MESSAGE)
    assert llm.kinds().count("plan") == 1
    assert {c["category"]: c["verdict"] for c in got["claims"]}["regulatory"] == "contradicted"
    assert "false_regulatory_claim" in {s["kind"] for s in got["signals"]}


def test_regulator_pages_cant_crowd_out_the_official_site(client: TestClient, researched: FakeWeb) -> None:
    """Seen live: 5 government pages filled every slot, so no company website was ever read."""
    many = [WebResult(title=f"Gov {i}", url=f"https://www.bnm.gov.my/page-{i}", score=0.9) for i in range(6)]
    researched.results[f"{ORG} @bnm.gov.my"] = many
    researched.results[f"{ORG} official website"] = [WebResult(title="About", url=OFFICIAL, score=0.1)]
    investigate(client, text=MESSAGE)
    picked = researched.extracted[0]
    assert len(picked) == inv.MAX_PAGES and picked[:2] == [many[0].url, OFFICIAL]


def test_plan_is_capped_to_the_search_budget(client: TestClient, researched: FakeWeb, llm: ScamLLM) -> None:
    llm.plan = {"searches": [{"query": f"query number {i}"} for i in range(10)]}
    investigate(client, text=MESSAGE)
    # round 1 + follow-up + the domain check on the official site
    assert len(researched.searched) == inv.MAX_SEARCHES + inv.FOLLOW_UP_SEARCHES + 1 == 12


def test_plan_failure_keeps_the_fixed_searches(client: TestClient, researched: FakeWeb, llm: ScamLLM) -> None:
    llm.plan = LLMError("Nebius timed out.")
    got = investigate(client, text=MESSAGE)
    assert got["status"] == "done" and len(researched.searched) == 4  # 3 fixed + the domain check
    assert "Couldn't plan extra searches; using the regulator searches only" in [s["text"] for s in got["steps"]]
    assert {e["tier"] for e in got["evidence"]} == {"A", "B"}


def test_failed_search_is_reported(client: TestClient, researched: FakeWeb) -> None:
    researched.failing = {f"{ORG} official website"}
    got = investigate(client, text=MESSAGE)
    assert "1 source found (1 search failed), reading the top 1" in [s["text"] for s in got["steps"]]
    assert {e["tier"] for e in got["evidence"]} == {"A"}


def test_slow_page_check_runs_out_of_time(
    client: TestClient, researched: FakeWeb, llm: ScamLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(inv, "RESEARCH_SECONDS", 1)
    llm.slow = {OFFICIAL: 3}
    got = investigate(client, text=MESSAGE)
    assert got["status"] == "done"
    steps = [s["text"] for s in got["steps"]]
    assert "1 piece of evidence from 1 page (1 from regulators); ran out of time for 1 page" in steps


def test_no_web_key_skips_research(db_url: str, uploads: Path) -> None:
    def no_web() -> FakeWeb:
        raise TavilyError("Web lookup isn't set up. Add TAVILY_API_KEY to .env (free key at tavily.com).")

    app = create_app(db_url, get_llm=lambda: ScamLLM(EXTRACTION), get_web=no_web)  # type: ignore[arg-type, return-value]
    with TestClient(app) as http:
        made = http.post("/api/investigations", data={"text": MESSAGE}).json()["id"]
        got = http.get(f"/api/investigations/{made}").json()
    assert got["status"] == "done" and got["evidence"] == []
    assert any(s["text"].startswith("Skipped web research: Web lookup isn't set up.") for s in got["steps"])
    assert got["risk_level"] == "GUARDED" and got["confidence"] == "LOW"  # the pressure tactic alone


def test_nothing_to_look_up(client: TestClient, researched: FakeWeb, llm: ScamLLM) -> None:
    llm.replies = [EMPTY]
    got = investigate(client, text="hello")
    assert researched.searched == [] and "Nothing to look up on the web" in [s["text"] for s in got["steps"]]


def test_official_contacts_only_from_ordinary_sites(client: TestClient, researched: FakeWeb, llm: ScamLLM) -> None:
    """Seen live: Nemotron gave bnm.gov.my as an org's website and Bank Negara's hotline as its phone."""
    researched.texts[ALERT] += " To view the updated list, please visit: bnm.gov.my/fca or call 1-300-88-5465."
    llm.pages[ALERT] = lambda c: {"evidence": [], "official": [
        {"org": id_of(c, ORG), "type": "domain", "value": "bnm.gov.my", "quote": "please visit: bnm.gov.my/fca"},
        {"org": id_of(c, ORG), "type": "phone", "value": "1-300-88-5465", "quote": "or call 1-300-88-5465."},
    ]}
    researched.texts[OFFICIAL] += " Partner sites: sc.com.my."
    llm.pages[OFFICIAL] = lambda c: {"evidence": [], "official": [
        {"org": id_of(c, ORG), "type": "domain", "value": "troweprice.com", "quote": "Visit us at troweprice.com."},
        {"org": id_of(c, ORG), "type": "domain", "value": "sc.com.my", "quote": "Partner sites: sc.com.my."},
    ]}
    got = investigate(client, text=MESSAGE)
    assert [(e["official_type"], e["official_value"]) for e in got["evidence"] if e["official_type"]] == [
        ("domain", "troweprice.com"),
    ]


def test_names_any() -> None:
    def entity(kind: EntityType, value: str) -> Entity:
        return Entity(investigation_id=uuid4(), type=kind, value=value, canonical=canonical(kind, value))

    found = [entity(EntityType.org, ORG), entity(EntityType.org, "Securities Commission"),
             entity(EntityType.domain, "troweprice-my-invest.com"), entity(EntityType.phone, "+60 12-000 5678")]
    assert inv.names_any("37. T Rowe Price Group Sdn.Bhd (potential clone entity)", found)
    assert inv.names_any("Beware of TROWEPRICE-MY-INVEST.COM", found)
    assert inv.names_any("Reported number: 012-000 5678", found)
    assert not inv.names_any("Order Granting Approval of a Proposed Rule Change", found)
    assert not inv.names_any("The Securities Commission Malaysia has updated the list", found)  # the regulator itself


def test_relevant_text_matches_other_spellings() -> None:
    page = "Header. " + "x" * 9000 + " | T Rowe Price Group Sdn.Bhd (potential clone entity) | " + "y" * 9000
    assert "T Rowe Price Group Sdn.Bhd (potential clone entity)" in inv.relevant_text(page, [ORG])


def test_relevant_text_keeps_passages_far_down_the_page() -> None:
    page = "Alert list header. " + "x" * 20000 + " | FalconRise Capital | Facebook | 3 Aug 2026 | " + "y" * 20000
    kept = inv.relevant_text(page, ["FalconRise Capital", "ab"])
    assert kept.startswith("Alert list header.") and "| FalconRise Capital | Facebook | 3 Aug 2026 |" in kept
    assert len(kept) <= inv.MAX_CHECKED


@pytest.mark.parametrize(
    ("url", "tier"),
    [
        ("https://www.sc.com.my/investor-alert-list", "A"), ("https://www.bnm.gov.my/x", "A"),
        ("https://www.fbi.gov/x", "A"), ("https://notgov.com", "D"), ("https://www.thestar.com.my/n", "C"),
        ("https://m.facebook.com/p", "E"), ("https://forum.lowyat.net/t", "E"), ("https://www.linkedin.com/company/x", "E"), ("https://example.com", "D"),
    ],
)
def test_tier_of(url: str, tier: str) -> None:
    assert inv.tier_of(inv.host_of(url)) == tier


def test_same_site_needs_a_real_subdomain() -> None:
    assert inv.same_site("invest.troweprice.com", "troweprice.com")
    assert not inv.same_site("troweprice-my.com", "troweprice.com")
    assert not inv.same_site("mytroweprice.com", "troweprice.com")


def test_us_spelling_of_behaviours_is_read() -> None:
    """Nemotron sometimes writes "behaviors"; ignoring it silently lost every pressure tactic."""
    got = ScamExtraction.model_validate({"behaviors": [{"kind": "payment_pressure", "quote": "pay today"}]})
    assert [b.kind for b in got.behaviours] == ["payment_pressure"]


# --- H4: graph, investigate tool, password ---


def test_graph_shows_the_clone_path(client: TestClient, researched: FakeWeb) -> None:
    got = investigate(client, text=MESSAGE)
    nodes = {n["id"]: n for n in got["graph"]["nodes"]}
    edges = {(nodes[e["source"]]["label"], e["kind"], nodes[e["target"]]["label"]) for e in got["graph"]["edges"]}
    assert ("Message", "claims_to_be", ORG) in edges
    assert ("Message", "names", "Securities Commission") in edges  # a regulator the message names, not its identity
    assert (ORG, "official_domain", "troweprice.com") in edges
    assert (ORG, "warned_by", "bnm.gov.my") in edges
    assert next(n for n in nodes.values() if n["label"] == "troweprice-my-invest.com")["flag"] == "mismatch"
    evidence = {e["id"] for e in got["evidence"]}
    assert all(e["evidence_id"] in evidence for e in got["graph"]["edges"] if e["evidence_id"])
    assert all(e["source"] in nodes and e["target"] in nodes for e in got["graph"]["edges"])


def test_risky_check_becomes_a_loop_in_needs_attention(client: TestClient, researched: FakeWeb, llm: ScamLLM) -> None:
    got = investigate(client, text=MESSAGE)
    assert got["risk_level"] == "CRITICAL" and got["loop_title"] == f"Verify {ORG} before paying"
    loop = client.get(f"/api/loops/{got['loop_id']}").json()
    assert (loop["kind"], loop["next_action"]) == ("task", got["next_steps"][0])
    assert loop["source"] | {"id": None, "created_at": None} == {
        "id": None, "kind": "investigation", "text": MESSAGE, "external_id": None,
        "url": f"/investigate.html#{got['id']}", "sender": None, "created_at": None,
    }
    attention = client.get("/api/attention").json()
    assert [(a["loop"]["id"], a["reason"]) for a in attention] == [(got["loop_id"], "critical risk, hold off paying")]
    assert [l["risk_level"] for l in client.get("/api/loops").json()] == ["CRITICAL"]  # the ledger flags it too
    assert [a["by"] for a in client.get(f"/api/activity?loop_id={got['loop_id']}").json()] == ["check"]

    llm.replies.append(EXTRACTION)
    again = investigate(client, text=MESSAGE)  # the same message checked twice: still one loop
    assert again["loop_id"] == got["loop_id"] and len(client.get("/api/loops").json()) == 1


def test_guarded_check_is_tracked_too(client: TestClient) -> None:
    """Only LOW clears a payment: a guarded result still has a risk signal."""
    got = investigate(client, text=MESSAGE)  # no web evidence: the pressure tactic alone, GUARDED
    assert (got["risk_level"], got["loop_title"]) == ("GUARDED", f"Verify {ORG} before paying")
    assert client.get("/api/attention").json()[0]["reason"] == "guarded risk, hold off paying"


def test_low_risk_check_adds_no_loop(session: Session) -> None:
    investigation = Investigation(input_text=MESSAGE, risk_level=RiskLevel.low)
    session.add(investigation)
    session.commit()
    inv._track(session, investigation)
    assert investigation.loop_id is None and eng.list_loops(session) == []


def test_check_started_from_a_loop_stays_with_it(client: TestClient, researched: FakeWeb) -> None:
    with Session(client.app.state.db) as s:  # type: ignore[attr-defined]
        source = eng.add_source(s, "I need to pay the T. Rowe Price deposit by Friday.")
        loop = eng.add_loop(s, source_id=source.id, title="Pay the deposit", summary="Deposit.", kind=LoopKind.task)
        s.commit()
        loop_id = str(loop.id)
    got = investigate(client, text=MESSAGE, loop_id=loop_id)
    assert (got["loop_id"], got["loop_title"]) == (loop_id, "Pay the deposit")
    assert [l["id"] for l in client.get("/api/loops").json()] == [loop_id]  # no "Verify …" loop on top
    assert client.get("/api/attention").json()[0]["reason"] == "critical risk, hold off paying"
    missing = client.post("/api/investigations", data={"text": MESSAGE, "loop_id": str(uuid4())})
    assert missing.status_code == 404 and client.get("/api/investigations").json()[0]["id"] == got["id"]


def test_investigate_tool_answers_with_findings_and_links(
    db_url: str, uploads: Path, llm: ScamLLM, researched: FakeWeb
) -> None:
    from tests.test_mcp_tools import MCP

    app = create_app(db_url, get_llm=lambda: llm, get_web=lambda: researched)  # type: ignore[arg-type, return-value]
    with TestClient(app, base_url="http://127.0.0.1:8000") as http:
        mcp = MCP(http)
        mcp.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "test", "version": "0"}})
        got = mcp.call("investigate", text=MESSAGE)
    reply = got["reply"]
    assert reply.startswith("Risk level: CRITICAL, confidence HIGH")
    assert "Details: http://127.0.0.1:8000/investigate.html#" in reply
    assert f"Source: {ALERT}" in reply and "Source: the message" in reply  # the pressure tactic cites the message
    assert "Next steps:" in reply
    assert f"On your list: Verify {ORG} before paying. It stays under Needs attention" in reply


def test_investigate_tool_still_running_gives_the_link(
    db_url: str, uploads: Path, llm: ScamLLM, researched: FakeWeb, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import mcp_tools
    from tests.test_mcp_tools import MCP

    monkeypatch.setattr(mcp_tools, "INVESTIGATE_WAIT", 0)
    app = create_app(db_url, get_llm=lambda: llm, get_web=lambda: researched)  # type: ignore[arg-type, return-value]
    with TestClient(app, base_url="http://127.0.0.1:8000") as http:
        mcp = MCP(http)
        mcp.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "test", "version": "0"}})
        got = mcp.call("investigate", text=MESSAGE)
        assert got["reply"].startswith("Still checking.") and "investigate.html#" in got["reply"]
        assert mcp.call("investigate", text=" ")["error"].endswith("Paste a message, a link or a screenshot.")
        assert "No loop with id nope" in mcp.call("investigate", text=MESSAGE, loop_id="nope")["error"]
        time.sleep(0.5)  # let the background run finish before the test database goes away


def test_app_password(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "app_password", SecretStr("s3cret"))
    assert client.get("/api/investigations").status_code == 401
    assert client.get("/investigate.html").headers["www-authenticate"].startswith("Basic")
    assert client.get("/api/investigations", auth=("anyone", "wrong")).status_code == 401
    assert client.get("/api/investigations", auth=("anyone", "s3cret")).status_code == 200
    assert client.post("/mcp/", json={}).status_code == 403  # TestClient isn't this machine's loopback
