"""Check email and the web for open loops. Prints what changed and what waits for the user's yes, or nothing.

Run by hand, or every 10 minutes by a Hermes cron job (spec step 6). Each watched loop is searched
at most once a day, so frequent runs cost nothing extra. Email is read only from people linked to
open loops. Without Google connected or TAVILY_API_KEY, that part is skipped.

  python scripts/sync.py
"""

from sqlmodel import Session

from app import engine as eng
from app.config import settings, setup_logging
from app.connectors import google
from app.connectors.google import GoogleClient
from app.connectors.tavily import TavilyClient
from app.db import create_db_engine
from app.llm import LLMClient


def main() -> None:
    setup_logging(settings.log_level)
    if not (google.connected(settings) or eng.web_enabled()):
        return
    db = create_db_engine(settings.database_url)
    web = TavilyClient(settings) if eng.web_enabled() else None
    with Session(db) as s:
        lines = eng.run_sync(s, LLMClient(settings),
                             (lambda: GoogleClient.from_settings(settings)) if google.connected(settings) else None,
                             (lambda q: web.search(q, time_range="week").results) if web else None)
    db.dispose()
    if lines:
        print("\n".join(lines))


if __name__ == "__main__":
    main()
