"""Check the web for watched loops. Prints what waits for the user's yes, or nothing.

Run by hand, or every 10 minutes by a Hermes cron job (spec step 6). Each watched loop is searched
at most once a day, so frequent runs cost nothing extra. Without TAVILY_API_KEY it does nothing.

  python scripts/sync.py
"""

import logging

from sqlmodel import Session

from app import engine as eng
from app.config import settings
from app.connectors.tavily import TavilyClient
from app.db import create_db_engine
from app.llm import LLMClient


def main() -> None:
    logging.basicConfig(level=settings.log_level)
    if not eng.web_enabled():
        return
    web = TavilyClient(settings)
    db = create_db_engine(settings.database_url)
    with Session(db) as s:
        proposals, errors = eng.watch_the_web(s, LLMClient(settings), lambda q: web.search(q, time_range="week").results)
        lines = [f"{eng.describe_action(s, a)} (yes/no)" for a in proposals] + errors
    db.dispose()
    if lines:
        print("\n".join(lines))


if __name__ == "__main__":
    main()
