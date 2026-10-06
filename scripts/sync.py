"""Check email and the web for open loops. Prints what changed and what waits for the user's yes, or nothing.

Run by hand, or every 10 minutes by a Hermes cron job (spec step 6). Each watched loop is searched
at most once a day, so frequent runs cost nothing extra. Email is read only from people linked to
open loops. Without Google connected or TAVILY_API_KEY, that part is skipped.

  python scripts/sync.py
"""

import logging

from sqlmodel import Session

from app import engine as eng
from app.config import settings
from app.connectors import google
from app.connectors.google import GoogleClient, GoogleError
from app.connectors.tavily import TavilyClient
from app.db import create_db_engine
from app.llm import LLMClient


def main() -> None:
    logging.basicConfig(level=settings.log_level)
    if not (google.connected(settings) or eng.web_enabled()):
        return
    db = create_db_engine(settings.database_url)
    llm = LLMClient(settings)
    lines: list[str] = []
    with Session(db) as s:
        if google.connected(settings):
            try:
                changed, errors = eng.sync_email(s, llm, GoogleClient.from_settings(settings))
            except GoogleError as e:  # expired or revoked token
                changed, errors = [], eng.report_sync_errors(s, [str(e)])
            lines += changed + errors
        if eng.web_enabled():
            web = TavilyClient(settings)
            proposals, errors = eng.watch_the_web(s, llm, lambda q: web.search(q, time_range="week").results)
            lines += [f"{eng.describe_action(s, a)} (yes/no)" for a in proposals] + errors
    db.dispose()
    if lines:
        print("\n".join(lines))


if __name__ == "__main__":
    main()
