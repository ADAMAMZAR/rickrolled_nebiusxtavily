"""Google gate: connect, list the last 5 emails from one address, save one draft, create one event.

Usage: python scripts/google_gate.py someone@example.com

The first run opens your browser to connect Google (README: Google setup). Nothing is sent: the
draft (to that address) waits in Gmail's Drafts, and the event is tomorrow 10:00. Delete both after.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import settings
from app.connectors.google import GoogleClient, GoogleError, connect


def main() -> None:
    if len(sys.argv) != 2 or "@" not in sys.argv[1]:
        sys.exit(__doc__)
    address = sys.argv[1]
    try:
        if not Path(settings.google_token_file).exists():
            print("Opening your browser to connect Google...")
            connect(settings)
        google = GoogleClient.from_settings(settings)

        emails = google.messages_from(address, limit=5)
        print(f"1. Last {len(emails)} emails from {address}:")
        for email in emails:
            print(f"   {email.received.astimezone(ZoneInfo(settings.timezone)):%Y-%m-%d %H:%M}  {email.subject or '(no subject)'}")

        google.create_draft(address, "Continuum test draft", "Created by Continuum's Google check. Not sent. Delete it.")
        print("2. Draft saved, not sent: https://mail.google.com/mail/u/0/#drafts")

        tomorrow = datetime.now(ZoneInfo(settings.timezone)) + timedelta(days=1)
        start = tomorrow.replace(hour=10, minute=0, second=0, microsecond=0, tzinfo=None)
        event = google.create_event("Continuum test event", start)
        print(f"3. Event created for {start:%a %b %d} 10:00: {event.link}")
    except GoogleError as e:
        sys.exit(f"Google check failed: {e}")
    print("Google gate passed. Delete the test draft and event.")


if __name__ == "__main__":
    main()
