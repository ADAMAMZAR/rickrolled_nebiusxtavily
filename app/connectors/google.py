"""Google: read mail from one sender, save Gmail drafts, create Calendar events. Never sends email.

Uses an OAuth "Desktop app" client (README: Google setup). The token is saved to GOOGLE_TOKEN_FILE.
"""

import base64
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from pydantic import BaseModel

from app.config import Settings

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",  # for drafts; Continuum never calls send
    "https://www.googleapis.com/auth/calendar.events",
]
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
EVENTS = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
RECONNECT = "Connect again: python scripts/google_gate.py <email>"


class GoogleError(Exception):
    pass


class Email(BaseModel):
    id: str
    thread_id: str
    sender: str
    subject: str
    received: datetime
    snippet: str

    @property
    def url(self) -> str:
        return f"https://mail.google.com/mail/u/0/#all/{self.thread_id}"


class Event(BaseModel):
    id: str
    link: str


def connect(settings: Settings) -> None:
    """Open Google's consent screen in the browser, then save the token."""
    secret = Path(settings.google_client_secret_file)
    if not secret.exists():
        raise GoogleError(f"Google isn't set up: no OAuth client file at {secret}. See README: Google setup.")
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    _save(settings, flow.run_local_server(port=0, prompt="consent"))  # consent: always returns a refresh token


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
        """Newest first. Reads the From/Subject headers and Gmail's snippet, not the body."""
        query = f"from:{sender}" + (f" after:{int(after.timestamp())}" if after else "")
        found = self._call("GET", f"{GMAIL}/messages", params={"q": query, "maxResults": limit})
        return [self._email(m["id"]) for m in found.get("messages", [])]

    def _email(self, message_id: str) -> Email:
        m = self._call("GET", f"{GMAIL}/messages/{message_id}",
                       params={"format": "metadata", "metadataHeaders": ["From", "Subject"]})
        headers = {h["name"].lower(): h["value"] for h in m["payload"].get("headers", [])}
        return Email(id=m["id"], thread_id=m["threadId"], sender=headers.get("from", ""),
                     subject=headers.get("subject", ""), snippet=m.get("snippet", ""),
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


def _reason(response: httpx.Response) -> str:
    """Google's own error text, e.g. "Gmail API has not been used in project ...", which says what to fix."""
    try:
        return response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return response.reason_phrase
