import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.connectors.tavily import ExtractedPage, TavilyError
from app.db import EntityType
from app.extraction import SCAM_INVALID
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
    """Nemotron's JSON for extraction; plain text for the vision model."""

    def __init__(self, *replies: dict[str, Any] | str | Exception, screenshot_text: str = "") -> None:
        self.replies = list(replies)
        self.screenshot_text = screenshot_text
        self.calls: list[tuple[str | None, list[dict[str, Any]]]] = []

    def complete(self, messages: list[dict[str, Any]], json_mode: bool = False, model: str | None = None) -> str:
        self.calls.append((model, messages))
        if model == settings.nebius_vision_model:
            return self.screenshot_text
        reply = self.replies.pop(0) if self.replies else EMPTY
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)


class FakeWeb:
    def __init__(self, pages: list[ExtractedPage] | None = None, error: str | None = None) -> None:
        self.pages, self.error = pages or [], error
        self.extracted: list[list[str]] = []

    def extract(self, urls: list[str]) -> list[ExtractedPage]:
        self.extracted.append(urls)
        if self.error:
            raise TavilyError(self.error)
        return self.pages


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
    return FakeWeb([ExtractedPage(url="https://troweprice-my-invest.com", raw_content="Welcome to T Rowe Price Asia. Deposit now.")])


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
    assert "ScamGraph" in client.get("/investigate.html").text
    assert 'href="/investigate.html"' in client.get("/").text


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
        "Found 4 names and contacts, 2 claims, 1 pressure tactic", "Done",
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
    (vision, messages), (nemotron, _) = llm.calls
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
