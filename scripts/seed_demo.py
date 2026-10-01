"""Fill a database with sample goals and loops for UI work. Not the real demo (that goes through chat).

Usage (use a scratch DB, not your real one):
  DATABASE_URL=sqlite:///./data/seed.db python scripts/seed_demo.py          # macOS/Linux
  $env:DATABASE_URL="sqlite:///./data/seed.db"; python scripts/seed_demo.py  # Windows PowerShell
"""

from datetime import timedelta

from sqlmodel import Session

from app import engine as eng
from app.config import settings
from app.db import LoopKind, create_db_engine


def main() -> None:
    today = eng.local_now().date()
    db = create_db_engine(settings.database_url)
    with Session(db) as s:
        src = eng.add_source(
            s,
            "I'm applying for an NVIDIA internship. I submitted my application yesterday. Sarah said she'll "
            "get back to me next Friday. I also need to finish my portfolio before the interview.",
        )
        goal = eng.add_goal(s, "Secure NVIDIA internship", "Land the summer internship at NVIDIA.")
        eng.add_loop(s, source_id=src.id, goal_id=goal.id, title="Wait for Sarah's response", kind=LoopKind.waiting,
                     summary="Sarah will reply about the application.", waiting_on="Sarah", due=today + timedelta(days=2))
        eng.add_loop(s, source_id=src.id, goal_id=goal.id, title="Finish portfolio", kind=LoopKind.task,
                     summary="Finish the portfolio before the interview.")

        src2 = eng.add_source(s, "Alex said he'll send me the dataset yesterday, still nothing. I promised Mia the slides by tomorrow.")
        hack = eng.add_goal(s, "Win the datathon")
        eng.add_loop(s, source_id=src2.id, goal_id=hack.id, title="Get dataset from Alex", kind=LoopKind.waiting,
                     summary="Alex owes the dataset.", waiting_on="Alex", due=today - timedelta(days=1),
                     next_action="Ping Alex")
        eng.add_loop(s, source_id=src2.id, goal_id=hack.id, title="Send Mia the slides", kind=LoopKind.commitment,
                     summary="Promised Mia the slides.", due=today + timedelta(days=1))

        src3 = eng.add_source(s, "I need to renew my passport.")
        eng.add_loop(s, source_id=src3.id, title="Renew passport", kind=LoopKind.task, summary="Passport renewal.")
        s.commit()
        done = eng.add_loop(s, source_id=src3.id, title="Book dentist", kind=LoopKind.task, summary="Dentist booking.")
        s.commit()
        eng.resolve_loop(s, done.id)
    print(f"Seeded {settings.database_url}")


if __name__ == "__main__":
    main()
