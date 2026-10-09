"""Google connector with a fake HTTP transport: no real account is touched."""

import base64
import logging
import json
from collections.abc import Callable
from datetime import datetime
from email import message_from_bytes
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.connectors.google import GoogleClient, GoogleError, connect, disconnect

MESSAGE = {
    "id": "m1", "threadId": "t1", "internalDate": "1790000000000", "snippet": "Great news, we'd like to interview you",
    "payload": {"headers": [{"name": "From", "value": "Sarah <sarah@nvidia.com>"}, {"name": "Subject", "value": "Interview"}]},
}


def google(handler: Callable[[httpx.Request], httpx.Response]) -> GoogleClient:
    return GoogleClient("token-1", timezone="Asia/Kuala_Lumpur", transport=httpx.MockTransport(handler))


def test_messages_from_one_sender() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "m1", "threadId": "t1"}]})
        return httpx.Response(200, json=MESSAGE)

    [email] = google(handler).messages_from("sarah@nvidia.com", after=datetime.fromtimestamp(1789990000))
    assert seen[0].headers["Authorization"] == "Bearer token-1"
    assert seen[0].url.params["q"] == "from:sarah@nvidia.com after:1789990000"
    assert seen[1].url.params["format"] == "full"
    assert (email.sender, email.subject, email.thread_id) == ("Sarah <sarah@nvidia.com>", "Interview", "t1")
    assert email.received.year == 2026 and email.url.endswith("#all/t1")
    assert email.text == "Subject: Interview\n\nGreat news, we'd like to interview you"  # no plain-text part: snippet


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def test_the_body_is_plain_text_without_quotes_or_signature() -> None:
    body = ("Great news, we'd like to interview you on Oct 8 at 10am.\r\n\r\n-- \r\nSarah Chen\r\nRecruiter\r\n\r\n"
            "On Mon, Oct 5, 2026 at 9:00 AM Me <me@x.com>\r\nwrote:\r\n> Any update?\r\n")
    message = {**MESSAGE, "payload": {**MESSAGE["payload"], "mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/html", "body": {"data": b64("<p>ignored</p>")}},
        {"mimeType": "text/plain", "body": {"data": b64(body)}},
    ]}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        return httpx.Response(200, json=message)

    [email] = google(handler).messages_from("sarah@nvidia.com")
    assert email.body == "Great news, we'd like to interview you on Oct 8 at 10am."


def test_long_mail_is_cut() -> None:
    from app.connectors.google import MAX_TEXT, Email
    email = Email(id="m", thread_id="t", sender="s", subject="S", received=datetime.now(), snippet="", body="x" * 9000)
    assert len(email.text) == MAX_TEXT


def test_disconnect_revokes_and_deletes_the_token(tmp_path: Path) -> None:
    token = tmp_path / "google_token.json"
    token.write_text(json.dumps({"refresh_token": "r1", "client_id": "c", "client_secret": "s"}), encoding="utf-8")
    settings = Settings(_env_file=None, google_token_file=str(token))
    revoked: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        revoked.append(request.content)
        return httpx.Response(200)

    disconnect(settings, transport=httpx.MockTransport(handler))
    assert not token.exists() and revoked == [b"token=r1"]
    disconnect(settings)  # already disconnected: nothing to do


def test_disconnect_offline_still_deletes_the_token(tmp_path: Path) -> None:
    token = tmp_path / "google_token.json"
    token.write_text(json.dumps({"refresh_token": "r1", "client_id": "c", "client_secret": "s"}), encoding="utf-8")

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network")

    with pytest.raises(GoogleError, match="myaccount.google.com/permissions"):
        disconnect(Settings(_env_file=None, google_token_file=str(token)), transport=httpx.MockTransport(down))
    assert not token.exists()


def test_no_messages() -> None:
    assert google(lambda r: httpx.Response(200, json={"resultSizeEstimate": 0})).messages_from("a@b.co") == []


def test_draft_is_saved_not_sent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "d1"})

    assert google(handler).create_draft("sarah@nvidia.com", "Thanks", "Thank you, Sarah!", thread_id="t1") == "d1"
    assert seen[0].url.path == "/gmail/v1/users/me/drafts"  # drafts only: there is no send call
    message = json.loads(seen[0].content)["message"]
    mail = message_from_bytes(base64.urlsafe_b64decode(message["raw"]))
    assert (mail["To"], mail["Subject"], message["threadId"]) == ("sarah@nvidia.com", "Thanks", "t1")
    assert mail.get_payload(decode=True).decode().strip() == "Thank you, Sarah!"


def test_a_line_break_cant_add_headers() -> None:
    with pytest.raises(ValueError):
        google(lambda r: httpx.Response(200, json={"id": "d1"})).create_draft("a@b.co", "Hi\nBcc: x@evil.co", "body")


def test_event_uses_the_users_timezone_and_lasts_an_hour() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "e1", "htmlLink": "https://calendar.google.com/event?eid=e1"})

    event = google(handler).create_event("NVIDIA interview", datetime(2026, 10, 8, 10, 0))
    assert (event.id, event.link) == ("e1", "https://calendar.google.com/event?eid=e1")
    assert json.loads(seen[0].content) == {
        "summary": "NVIDIA interview",
        "start": {"dateTime": "2026-10-08T10:00:00", "timeZone": "Asia/Kuala_Lumpur"},
        "end": {"dateTime": "2026-10-08T11:00:00", "timeZone": "Asia/Kuala_Lumpur"},
    }


@pytest.mark.parametrize(("response", "message"), [
    (httpx.Response(401), "rejected the sign-in"),
    (httpx.Response(403, json={"error": {"message": "Gmail API has not been used in project 1"}}),
     "HTTP 403: Gmail API has not been used in project 1"),
    (httpx.Response(500, text="oops"), "HTTP 500"),
])
def test_errors_are_clear(response: httpx.Response, message: str) -> None:
    with pytest.raises(GoogleError, match=message):
        google(lambda r: response).messages_from("a@b.co")


def test_unreachable_is_clear() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network")
    with pytest.raises(GoogleError, match="Couldn't reach Google"):
        google(down).create_draft("a@b.co", "s", "b")


def test_setup_errors_are_clear(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, google_client_secret_file=str(tmp_path / "client_secret.json"),
                        google_token_file=str(tmp_path / "google_token.json"))
    with pytest.raises(GoogleError, match="no OAuth client file"):
        connect(settings)
    with pytest.raises(GoogleError, match="isn't connected"):
        GoogleClient.from_settings(settings)


def test_a_damaged_token_is_clear(tmp_path: Path) -> None:
    token = tmp_path / "google_token.json"
    token.write_text("{}", encoding="utf-8")
    settings = Settings(_env_file=None, google_token_file=str(token))
    with pytest.raises(GoogleError, match="is damaged. Delete it, then connect again"):
        GoogleClient.from_settings(settings)


def test_logs_never_hold_the_gmail_query(caplog: pytest.LogCaptureFixture) -> None:
    """httpx logs every request URL; a Gmail search's ?q= holds the sender's address (privacy, spec §12)."""
    from app.config import setup_logging

    setup_logging("INFO")
    url = httpx.URL("https://gmail.googleapis.com/gmail/v1/users/me/messages", params={"q": "from:sarah@nvidia.com"})
    with caplog.at_level("INFO"):
        logging.getLogger("httpx").info('HTTP Request: %s %s "%s %d %s"', "GET", url, "HTTP/1.1", 200, "OK")
    assert "gmail.googleapis.com/gmail/v1/users/me/messages" in caplog.text
    assert "sarah" not in caplog.text and "q=" not in caplog.text
