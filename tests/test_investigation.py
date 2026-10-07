from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import create_app

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
MESSAGE = "ABC Capital Bhd here. Guaranteed 8% monthly returns. Transfer today to secure your slot."


@pytest.fixture
def uploads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "uploads"
    monkeypatch.setattr(settings, "uploads_dir", str(folder))
    return folder


@pytest.fixture
def client(db_url: str, uploads: Path) -> Iterator[TestClient]:
    with TestClient(create_app(db_url), base_url="http://127.0.0.1:8000") as http:
        yield http


def test_create_and_get(client: TestClient) -> None:
    response = client.post("/api/investigations", data={"text": MESSAGE, "url": "https://abc-capital-invest.com"})
    assert response.status_code == 201
    got = client.get(f"/api/investigations/{response.json()['id']}").json()
    assert got["status"] == "running" and got["input_text"] == MESSAGE
    assert got["input_url"] == "https://abc-capital-invest.com"
    assert [s["text"] for s in got["steps"]] == ["Received"]
    assert got["entities"] == got["claims"] == got["evidence"] == got["signals"] == []
    assert got["has_screenshot"] is False and "screenshot_path" not in got


def test_list_newest_first(client: TestClient) -> None:
    first = client.post("/api/investigations", data={"text": "one"}).json()["id"]
    second = client.post("/api/investigations", data={"text": "two"}).json()["id"]
    assert [i["id"] for i in client.get("/api/investigations").json()] == [second, first]


def test_persists_across_restart(db_url: str, uploads: Path) -> None:
    """H0 gate: an investigation survives an app restart."""
    with TestClient(create_app(db_url)) as http:
        made = http.post("/api/investigations", data={"text": MESSAGE}).json()["id"]
    with TestClient(create_app(db_url)) as http:
        assert http.get(f"/api/investigations/{made}").json()["input_text"] == MESSAGE


def test_screenshot_saved_outside_static(client: TestClient, uploads: Path) -> None:
    made = client.post("/api/investigations", files={"screenshot": ("shot.png", PNG, "image/png")}).json()["id"]
    assert (uploads / f"{made}.png").read_bytes() == PNG
    assert client.get(f"/api/investigations/{made}").json()["has_screenshot"] is True
    assert client.get(f"/{made}.png").status_code == 404


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
def test_invalid_input_rejected(client: TestClient, uploads: Path, data: dict, files: dict | None, message: str) -> None:
    response = client.post("/api/investigations", data=data, files=files)
    assert response.status_code == 422
    assert message in response.json()["message"]
    assert client.get("/api/investigations").json() == []
    assert not uploads.exists() or not any(uploads.iterdir())


def test_unknown_investigation_404(client: TestClient) -> None:
    response = client.get("/api/investigations/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404 and response.json()["error"] == "not_found"


def test_page_served(client: TestClient) -> None:
    assert "ScamGraph" in client.get("/investigate.html").text
    assert 'href="/investigate.html"' in client.get("/").text
