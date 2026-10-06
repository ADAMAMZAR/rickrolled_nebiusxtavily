"""Google: read mail from one sender, save Gmail drafts, create Calendar events. Never sends email.

Uses an OAuth "Desktop app" client (README: Google setup). The token is saved to GOOGLE_TOKEN_FILE.
"""

import base64
import re
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError
from oauthlib.oauth2 import OAuth2Error
from pydantic import BaseModel

from app.config import Settings

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",  # for drafts; Continuum never calls send
    "https://www.googleapis.com/auth/calendar.events",
]
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
EVENTS = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
REVOKE = "https://oauth2.googleapis.com/revoke"
RECONNECT = "Connect again in the dashboard (Settings)."
MAX_TEXT = 4000  # characters of an email that go to the LLM


class GoogleError(Exception):
    pass


class Email(BaseModel):
    id: str
    thread_id: str
    sender: str
    subject: str
    received: datetime
    snippet: str
    body: str = ""  # plain text, quotes and signature removed

    @property
    def url(self) -> str:
        return f"https://mail.google.com/mail/u/0/#all/{self.thread_id}"

    @property
    def text(self) -> str:
        """What extraction reads: subject + body (or Gmail's snippet if there's no plain-text part), cut to MAX_TEXT."""
        return f"Subject: {self.subject}\n\n{self.body or self.snippet}"[:MAX_TEXT]


class Event(BaseModel):
    id: str
    link: str


def connect(settings: Settings) -> None:
    """Open Google's consent screen in the browser, then save the token."""
    secret = Path(settings.google_client_secret_file)
    if not secret.exists():
        raise GoogleError(f"Google isn't set up: no OAuth client file at {secret}. See README: Google setup.")
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    try:  # consent: always returns a refresh token
        credentials = flow.run_local_server(port=0, prompt="consent", timeout_seconds=300)
    except WSGITimeoutError as e:
        raise GoogleError("Google sign-in timed out after 5 minutes. Try again.") from e
    except OAuth2Error as e:  # e.g. the user clicked Cancel
        raise GoogleError(f"Google sign-in didn't finish: {e.description or e.error}") from e
    _save(settings, credentials)


def connected(settings: Settings) -> bool:
    return Path(settings.google_token_file).exists()


def disconnect(settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
    """Revoke Continuum's access at Google and delete the saved token. The token is deleted even if
    Google can't be reached; then the error says where to remove access by hand."""
    path = Path(settings.google_token_file)
    if not path.exists():
        return
    try:
        token = Credentials.from_authorized_user_file(str(path), SCOPES).refresh_token
    except ValueError:  # damaged: nothing to revoke
        token = None
    path.unlink()
    if token:
        try:  # 400 = already invalid, which is fine
            with httpx.Client(timeout=15, transport=transport) as http:
                http.post(REVOKE, data={"token": token})
        except httpx.HTTPError as e:
            raise GoogleError("Disconnected here, but couldn't reach Google to revoke access. "
                              "Remove Continuum at https://myaccount.google.com/permissions") from e


def _save(settings: Settings, credentials: Credentials) -> None:
    path = Path(settings.google_token_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(credentials.to_json(), encoding="utf-8")


class GoogleClient:
    def __init__(self, token: str, timezone: str = "UTC", transport: httpx.BaseTransport | None = None) -> None:
        self.timezone = timezone
        self._http = httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=30, transport=transport)

    @classmethod
    def from_settings(cls, settings: Settings) -> "GoogleClient":
        """Load the saved token, refreshing it if it expired."""
        path = Path(settings.google_token_file)
        if not path.exists():
            raise GoogleError(f"Google isn't connected. {RECONNECT}")
        try:
            credentials = Credentials.from_authorized_user_file(str(path), SCOPES)
        except ValueError as e:  # not JSON, or missing the refresh token
            raise GoogleError(f"The saved Google token at {path} is damaged. Delete it, then {RECONNECT[0].lower()}{RECONNECT[1:]}") from e
        if not credentials.valid:
            try:
                credentials.refresh(Request())
            except RefreshError as e:
                raise GoogleError(f"Google disconnected (access expired or was revoked). {RECONNECT}") from e
            except TransportError as e:
                raise GoogleError("Couldn't reach Google to renew access.") from e
            _save(settings, credentials)
        return cls(credentials.token, settings.timezone)

    def messages_from(self, sender: str, after: datetime | None = None, limit: int = 5) -> list[Email]:
        """Newest first. Attachments aren't downloaded."""
        query = f"from:{sender}" + (f" after:{int(after.timestamp())}" if after else "")
        found = self._call("GET", f"{GMAIL}/messages", params={"q": query, "maxResults": limit})
        return [self._email(m["id"]) for m in found.get("messages", [])]

    def _email(self, message_id: str) -> Email:
        m = self._call("GET", f"{GMAIL}/messages/{message_id}", params={"format": "full"})
        headers = {h["name"].lower(): h["value"] for h in m["payload"].get("headers", [])}
        return Email(id=m["id"], thread_id=m["threadId"], sender=headers.get("from", ""),
                     subject=headers.get("subject", ""), snippet=m.get("snippet", ""),
                     body=strip_reply(_plain_text(m["payload"])),
                     received=datetime.fromtimestamp(int(m["internalDate"]) / 1000, UTC))

    def create_draft(self, to: str, subject: str, body: str, thread_id: str | None = None) -> str:
        """Save a draft in Gmail for the user to send (or not). Returns the draft id."""
        mail = EmailMessage()
        mail["To"], mail["Subject"] = to, subject  # raises ValueError on a line break, so no header injection
        mail.set_content(body)
        message: dict[str, str] = {"raw": base64.urlsafe_b64encode(mail.as_bytes()).decode()}
        if thread_id:
            message["threadId"] = thread_id
        return self._call("POST", f"{GMAIL}/drafts", json={"message": message})["id"]

    def create_event(self, title: str, start: datetime, end: datetime | None = None) -> Event:
        """Times without a timezone are in the user's TIMEZONE. Ends after an hour unless `end` is given."""
        end = end or start + timedelta(hours=1)
        body = {
            "summary": title,
            "start": {"dateTime": start.isoformat(), "timeZone": self.timezone},
            "end": {"dateTime": end.isoformat(), "timeZone": self.timezone},
        }
        event = self._call("POST", EVENTS, json=body)
        return Event(id=event["id"], link=event.get("htmlLink", ""))

    def _call(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._http.request(method, url, **kwargs)
        except httpx.HTTPError as e:
            raise GoogleError(f"Couldn't reach Google ({type(e).__name__}).") from e
        if response.status_code == 401:
            raise GoogleError(f"Google rejected the sign-in. {RECONNECT}")
        if response.is_error:
            raise GoogleError(f"Google returned HTTP {response.status_code}: {_reason(response)}")
        return response.json()


def _plain_text(part: dict[str, Any]) -> str:
    """The first text/plain part of a Gmail payload, or "" (HTML-only mail falls back to the snippet)."""
    if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
    return next((text for p in part.get("parts", []) if (text := _plain_text(p))), "")


# "On Tue, Oct 6, 2026 at 9:00 AM Sarah <sarah@x.com> wrote:" (often wrapped over two lines)
_QUOTE_HEADER = re.compile(r"^On\b.{0,300}?\bwrote:[ \t]*$", re.MULTILINE | re.DOTALL)


def strip_reply(body: str) -> str:
    """Drop the quoted earlier messages and the signature: only the new text matters, and is stored."""
    body = body.replace("\r\n", "\n")
    if match := _QUOTE_HEADER.search(body):
        body = body[: match.start()]
    body = body.split("\n-- \n")[0]  # standard signature separator
    lines = [line for line in body.split("\n") if not line.startswith(">")]
    return "\n".join(lines).strip()


def _reason(response: httpx.Response) -> str:
    """Google's own error text, e.g. "Gmail API has not been used in project ...", which says what to fix."""
    try:
        return response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return response.reason_phrase
