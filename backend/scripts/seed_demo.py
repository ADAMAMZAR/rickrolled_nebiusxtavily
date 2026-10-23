"""Fill an empty database with Troth's demo: a short story for judges (README: A demo for judges) and for UI work.
Dates are relative to today, so "due in 3 days" stays true when it's loaded again each night with --reset.

It refuses a database that holds data and isn't marked as the demo, so it can't touch your real loops.
  DATABASE_URL=sqlite:///./data/seed.db python scripts/seed_demo.py          # macOS/Linux
  $env:DATABASE_URL="sqlite:///./data/seed.db"; python scripts/seed_demo.py  # Windows PowerShell
  python scripts/seed_demo.py --reset    # the demo database: delete everything in it, then load the demo again

The two finished message checks are test samples B and C as recorded for real (tests/fixtures/scam_frozen.jsonl),
scored with today's rules: no Tavily credits. Nemotron writes their findings when NEBIUS_API_KEY is set; otherwise
they're the rules' own words. Prints counts only.
"""

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlmodel import Session, select

from app import engine as eng
from app import investigation as inv
from app.config import settings, setup_logging
from app.db import (
    ActionKind, Activity, ActivityAction, ActivityBy, Claim, Dependency, Entity, Evidence, Goal, Investigation,
    InvestigationStatus, LoopKind, LoopStatus, OpenLoop, PendingAction, Person, RiskSignal, Setting, Source,
    create_db_engine, utcnow,
)
from app.llm import LLMClient, LLMError

ROOT = Path(__file__).resolve().parent.parent
FROZEN = ROOT / "tests" / "fixtures" / "scam_frozen.jsonl"
SAMPLES = ROOT / "tests" / "scenarios"
DEMO = "demo_data"  # Setting key: this database holds the demo, so --reset may empty it


class NoModel:
    """Without NEBIUS_API_KEY: the checks' summary step falls back to the rules' own words."""

    def complete(self, messages: list[dict[str, Any]], json_mode: bool = False, model: str | None = None) -> str:
        raise LLMError("NEBIUS_API_KEY is not set.")


def load(session: Session, llm: Any, reset: bool = False, today: date | None = None) -> str:
    """Load the demo, or refuse with SystemExit and change nothing. Returns what was loaded."""
    marked = session.get(Setting, DEMO) is not None
    if reset:
        if not marked:
            raise SystemExit("This database isn't marked as the demo, and --reset only empties the demo. Nothing changed.")
        try:
            eng.delete_everything(session)
        except eng.InvalidRequest as e:  # a message check is still running
            raise SystemExit(str(e)) from e
    elif any(session.exec(select(model)).first() is not None for _, model in eng.ALL_DATA):
        raise SystemExit("The demo is already loaded: add --reset to load it fresh." if marked else
                         "This database already has data and isn't the demo. Nothing changed: point DATABASE_URL at "
                         "an empty database.")
    if not marked:  # first, so a load that fails half way can still be reset
        session.add(Setting(key=DEMO, value="1"))
        session.commit()
    seed(session, llm, today or eng.local_now().date())
    loops = len(session.exec(select(OpenLoop)).all())
    checks = len(session.exec(select(Investigation)).all())
    return f"Demo loaded: {loops} loops, {checks} message checks, 1 suggestion waiting for a yes."


def seed(session: Session, llm: Any, today: date) -> None:
    """The story. Written as Troth would have saved it, with each loop's words and history."""
    def day(n: int) -> date:
        return today + timedelta(days=n)

    def ago(days: float) -> datetime:
        return utcnow() - timedelta(days=days)

    def said(text: str, days: float, kind: str = "chat", sender: str | None = None) -> Source:
        source = Source(text=text, kind=kind, sender=sender, created_at=ago(days),
                        external_id=f"demo-{len(text)}-{days}" if kind == "email" else None)
        session.add(source)
        return source

    def loop(source: Source, quote: str, **fields: Any) -> OpenLoop:
        made = OpenLoop(source_id=source.id, summary=fields.pop("summary", fields["title"]),
                        created_at=source.created_at, updated_at=source.created_at, **fields)
        session.add(made)
        session.add(Activity(loop_id=made.id, action=ActivityAction.created, by=ActivityBy.chat, source_id=source.id,
                             quote=quote, created_at=source.created_at))
        return made

    people = {name: Person(name=name, email=email) for name, email in
              [("Sarah", "sarah@nvidia.example"), ("Dr. Lim", "lim@university.example"), ("Aina", None),
               ("Registrar Office", "registrar@university.example")]}
    session.add_all(people.values())
    goal = Goal(title="Secure NVIDIA internship", description="Land the summer internship at NVIDIA.", created_at=ago(6))
    session.add(goal)

    # The promise and what it needs: Dr. Lim's email moves the letter after the promise, so it's at risk.
    app_due, first_letter, letter_due = day(3), day(2), day(7)
    story = said(f"I promised Sarah I'd send my NVIDIA application by {app_due:%A}, but I can only send it once "
                 f"Dr. Lim sends my reference letter. He said it'll be ready by {first_letter:%A}.", 3)
    app = loop(story, f"I promised Sarah I'd send my NVIDIA application by {app_due:%A}",
               title="Send NVIDIA application", kind=LoopKind.commitment, waiting_on="Sarah", due=app_due,
               goal_id=goal.id, person_id=people["Sarah"].id)
    letter = loop(story, f"He said it'll be ready by {first_letter:%A}", title="Get reference letter from Dr. Lim",
                  kind=LoopKind.waiting, waiting_on="Dr. Lim", due=first_letter, goal_id=goal.id,
                  person_id=people["Dr. Lim"].id)
    session.add(Dependency(loop_id=app.id, needs_id=letter.id, source_id=story.id, created_at=story.created_at))
    session.add(Activity(loop_id=app.id, action=ActivityAction.updated, by=ActivityBy.chat,
                         detail=f'needs "{letter.title}"', source_id=story.id, created_at=story.created_at))
    delay = f"The letter will only be ready on {letter_due:%A, %d %B}"
    mail = said(f"Subject: Reference letter\n\nHi, sorry for the delay. {delay}.\n\nDr. Lim", 1, "email", "Dr. Lim")
    letter.due, letter.updated_at = letter_due, mail.created_at
    session.add(Activity(loop_id=letter.id, action=ActivityAction.updated, by=ActivityBy.email, detail="due",
                         undo={"due": first_letter.isoformat()}, source_id=mail.id, quote=delay, created_at=mail.created_at))

    sarah = said(f"Sarah from NVIDIA said she'll tell me my interview slot by {day(-1):%A}.", 5)
    loop(sarah, f"she'll tell me my interview slot by {day(-1):%A}", title="Wait for Sarah's interview slot",
         kind=LoopKind.waiting, waiting_on="Sarah", due=day(-1), goal_id=goal.id, person_id=people["Sarah"].id)
    portfolio = said("I need to finish my portfolio before the interview.", 6)
    loop(portfolio, "I need to finish my portfolio", title="Finish portfolio", kind=LoopKind.task, goal_id=goal.id)
    charger = said(f"I promised Aina I'd return her charger on {day(1):%A}.", 2)
    loop(charger, f"I promised Aina I'd return her charger on {day(1):%A}", title="Return Aina's charger",
         kind=LoopKind.commitment, waiting_on="Aina", due=day(1), person_id=people["Aina"].id)

    # Closed by email, with undo in its history.
    waiting = said("Still waiting for the registrar to send my official transcript.", 4)
    transcript = loop(waiting, "waiting for the registrar to send my official transcript",
                      title="Get transcript from registrar", kind=LoopKind.waiting, waiting_on="Registrar Office",
                      person_id=people["Registrar Office"].id)
    arrived = "your official transcript is attached"
    reply = said(f"Subject: Your transcript\n\nDear student, {arrived}.\n\nRegistrar Office", 2, "email",
                 "Registrar Office")
    transcript.status, transcript.resolved_at, transcript.updated_at = LoopStatus.resolved, reply.created_at, reply.created_at
    session.add(Activity(loop_id=transcript.id, action=ActivityAction.resolved, by=ActivityBy.email,
                         undo={"status": LoopStatus.open.value, "resolved_at": None}, source_id=reply.id,
                         quote=arrived, created_at=reply.created_at))

    # Someone new: their task waits for Add or Ignore.
    ask = f"Please complete the online assessment by {day(5):%A, %d %B}"
    invite = said(f"Subject: Online assessment\n\nHi, thanks for applying to Mahsuri Labs. {ask}.\n\nMahsuri Labs "
                  "Talent Team", 0.1, "email", "Mahsuri Labs Talent")
    held = eng.HeldLoop(title="Complete Mahsuri Labs online assessment", summary="Complete the online assessment.",
                        kind=LoopKind.task, due=day(5), quote=ask, source_id=invite.id, sender="Mahsuri Labs Talent",
                        sender_email="talent@mahsurilabs.example", why=eng.NEW_SENDER)
    session.add(PendingAction(kind=ActionKind.loop_update, payload=held.model_dump(mode="json"), created_at=ago(0.1)))

    # A payment ask, checked: test sample B, a clone of a real fund company.
    transfer = said(f"I need to transfer RM5,000 to T. Rowe Price by {app_due:%A}. Their agent sent me the details.", 1)
    pay = loop(transfer, f"I need to transfer RM5,000 to T. Rowe Price by {app_due:%A}",
               title="Transfer RM5,000 to T. Rowe Price", kind=LoopKind.task, due=app_due)
    session.commit()
    _replay_check(session, llm, "b", "b_clone_company.txt", ago(0.9), pay.id)
    _replay_check(session, llm, "c", "c_legitimate.txt", ago(2), None)  # a real bank: low risk


def _replay_check(session: Session, llm: Any, item: str, sample: str, when: datetime, loop_id: UUID | None) -> None:
    """A check recorded for real, run again from its stored rows: verdicts, signals and level come from today's rules."""
    rows = next(r for r in map(json.loads, FROZEN.read_text(encoding="utf-8").splitlines())
                if r["item"] == item and r["status"] == "done")["frozen"]
    check = Investigation(id=UUID(rows["entities"][0]["investigation_id"]), input_text=(SAMPLES / sample).read_text(
        encoding="utf-8").strip(), loop_id=loop_id, created_at=when, updated_at=when)
    inv.add_step(check, "Replaying a check of a test message, recorded with Tavily and Nemotron")
    session.add(check)
    session.add_all([Entity.model_validate(e) for e in rows["entities"]] + [Claim.model_validate(c) for c in rows["claims"]]
                    + [Evidence.model_validate(e) for e in rows["evidence"]])
    for s in rows["signals"]:
        if s.get("input_quote"):  # behaviour signals from the message; the rest come from the evidence (_score)
            session.add(RiskSignal.model_validate(s | {"weight": inv.WEIGHTS[s["kind"]]}))
    session.commit()
    inv._verify(session, check)
    inv._score(session, check)
    inv._summarize(session, check, llm)
    inv._track(session, check)
    check.status = InvestigationStatus.done
    inv._step(session, check, "Done")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--reset", action="store_true", help="the demo database: delete everything, then load again")
    args = parser.parse_args()
    setup_logging(settings.log_level)
    llm = LLMClient(settings) if settings.nebius_api_key.get_secret_value() else NoModel()
    with Session(create_db_engine(settings.database_url)) as session:
        print(load(session, llm, args.reset))


if __name__ == "__main__":
    main()
