"""scripts/correction_rate.py on a scratch database: edits, undos and (from the log) deletes within 48 hours."""

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from sqlmodel import Session

from app import engine as eng
from app.db import utcnow
from scripts.correction_rate import corrections
from tests.conftest import NOW, FakeLLM
from tests.test_process_message import loop, result


def test_corrections_within_48_hours(session: Session, tmp_path: Path) -> None:
    a, b, c = eng.process_message(session, FakeLLM(result(new=[loop("A"), loop("B"), loop("C")])), "A, B, C.",
                                  now=NOW).created_loops
    eng.edit_loop(session, a.id, {"title": "A, fixed", "due": None, "next_action": "x"})
    eng.process_message(session, FakeLLM(result(updated=[{"id": str(c.id), "due": "2026-10-09"}])), "C moves.", now=NOW)
    eng.undo_activity(session, next(x for x in eng.list_activity(session, c.id) if x.action == "updated").id)

    later = utcnow() + timedelta(days=3)  # all three were made over 48 h before this
    got = corrections(session, later)
    assert (got["made"], got["made_by_chat"], got["edited"], got["undone"], got["corrected"]) == (3, 3, 1, 1, 2)
    assert got["field:title"] == got["field:next action"] == 1 and "log" not in got

    gone, kept, check = uuid4(), uuid4(), uuid4()  # deleted after 1 h; deleted after 3 days; a message check's loop
    t = utcnow()
    log = tmp_path / "troth.log"
    log.write_text(
        f"{t.isoformat()} vm troth[1]: INFO:continuum.engine:loop_created id={gone}\n"
        f"{(t + timedelta(hours=1)).astimezone().strftime('%Y-%m-%dT%H:%M:%S%z')} vm troth[1]: INFO:continuum.engine:loop_deleted id={gone}\n"
        f"{t.isoformat()} vm troth[1]: INFO:continuum.engine:loop_created id={kept}\n"
        f"{(t + timedelta(days=3)).isoformat()} vm troth[1]: INFO:continuum.engine:loop_deleted id={kept}\n"
        f"{t.isoformat()} vm troth[1]: INFO:continuum.engine:loop_created id={check} by=check\n"
        f"INFO:continuum.engine:loop_created id={uuid4()}\n",  # no timestamp: can't be placed
        encoding="utf-8")
    got = corrections(session, later + timedelta(days=1), log)
    assert (got["made"], got["made_gone"], got["deleted"], got["corrected"]) == (5, 2, 1, 3)


def test_email_suggestions_added_and_ignored(session: Session) -> None:
    """Of the loops email suggested, how many the user added (troth_eval.md §16): says if new senders are too noisy."""
    from scripts.correction_rate import suggestions
    from tests.test_email import FakeMailbox, pearl, sync

    words = "pay the RM50 deposit at www.revolab-pay.co"
    sync(session, FakeLLM(result(new=[loop("Sign contract", quote="please sign and return the contract by Friday")]),
                          result(new=[loop("Pay deposit", quote=words)]), result(new=[loop("Book room", quote="book a room")])),
         FakeMailbox(new=[pearl("p1"), pearl("p2", f"Hi, {words}.", 1), pearl("p3", "Hi, book a room.", 2)]))
    first, second, third = eng.list_actions(session)
    eng.approve_action(session, first.id)
    eng.reject_action(session, second.id)
    assert suggestions(session) == {"new:done": 1, "risky:rejected": 1, "new:proposed": 1}
