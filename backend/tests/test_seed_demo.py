"""scripts/seed_demo.py: the judges' demo tells its story, and can't touch a database that isn't the demo."""

import pytest
from sqlmodel import Session, select

from app import engine as eng
from app.db import Investigation, LoopStatus, OpenLoop, RiskLevel
from scripts.seed_demo import NoModel, load
from tests.test_process_message import loop, process, result


def test_the_demo_tells_its_story(session: Session) -> None:
    assert load(session, NoModel()) == "Demo loaded: 7 loops, 2 message checks, 1 suggestion waiting for a yes."
    today = eng.local_now().date()
    reasons = {a.loop.title: a for a in eng.needs_attention(session, today)}
    at_risk = reasons["Send NVIDIA application"]
    assert at_risk.reason.startswith('at risk: "Get reference letter from Dr. Lim" is due')
    assert len(at_risk.options) == 3 and at_risk.cause.title == "Get reference letter from Dr. Lim"
    assert reasons["Transfer RM5,000 to T. Rowe Price"].reason.endswith("hold off paying")  # its check found risk
    assert reasons["Wait for Sarah's interview slot"].reason == "overdue by 1 day"
    assert reasons["Return Aina's charger"].reason == "due tomorrow"
    assert reasons["Finish portfolio"].reason == "no update in 6 days"

    [suggestion] = eng.list_actions(session)
    assert suggestion.payload["why"] == eng.NEW_SENDER and suggestion.payload["sender_email"]
    closed = next(l for l in eng.list_loops(session, None) if l.status == LoopStatus.resolved)
    assert eng.resolved_by(session, closed) == "email"

    checks = session.exec(select(Investigation)).all()
    assert len(checks) == 2 and all(c.status == "done" and c.findings and c.next_steps for c in checks)
    risky = next(c for c in checks if c.loop_id is not None)
    assert risky.risk_level in eng.RISKY and session.get(OpenLoop, risky.loop_id).title.startswith("Transfer")
    assert eng.latest_check(session, risky.loop_id) == risky.id  # the loop links to its evidence ("See the check")
    assert RiskLevel.low in {c.risk_level for c in checks}  # the real bank


def test_it_refuses_a_database_that_isnt_the_demo(session: Session) -> None:
    process(session, result(new=[loop("My real loop")]), "My real loop.")
    with pytest.raises(SystemExit, match="isn't the demo"):
        load(session, NoModel())
    with pytest.raises(SystemExit, match="only empties the demo"):
        load(session, NoModel(), reset=True)
    assert [l.title for l in eng.list_loops(session)] == ["My real loop"]


def test_reset_loads_the_demo_fresh(session: Session) -> None:
    load(session, NoModel())
    with pytest.raises(SystemExit, match="already loaded"):
        load(session, NoModel())
    eng.delete_loop(session, eng.list_loops(session)[0].id)  # a judge deletes things
    load(session, NoModel(), reset=True)
    assert len(session.exec(select(OpenLoop)).all()) == 7 and len(eng.list_actions(session)) == 1
