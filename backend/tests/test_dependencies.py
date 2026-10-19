"""Loops that need other loops first, and the "at risk" rules (troth_improvements.md). LLM is faked.

Today is Tue 2026-09-29 (conftest.NOW): Thu = Oct 1, Fri = Oct 2.
"""

from datetime import date, timedelta
from typing import Any

from sqlmodel import Session, select

from app import engine as eng
from app.db import ActivityBy, Dependency, LoopKind, LoopStatus, OpenLoop, Source
from tests.conftest import NOW, FakeLLM
from tests.test_process_message import loop, process, result

TODAY = NOW.date()


def linked(reply: dict[str, Any], *pairs: tuple[str, str]) -> dict[str, Any]:
    """`reply` plus links: (loop, what it needs), each an id or a new loop's title."""
    return {**reply, "links": [{"loop": a, "needs": b} for a, b in pairs]}
STORY = "I promised Sarah my NVIDIA application by Friday. Dr. Lim is writing my reference letter for it, due Thursday."
APP, LETTER = "Send NVIDIA application", "Get reference letter from Dr. Lim"
STORY_RESULT = linked(result(new=[
    loop(APP, "commitment", waiting_on="Sarah", due="2026-10-02"),
    loop(LETTER, "waiting", waiting_on="Dr. Lim", due="2026-10-01"),
]), (APP, LETTER))


def story(session: Session) -> tuple[OpenLoop, OpenLoop]:
    app, letter = process(session, STORY_RESULT, STORY).created_loops
    return app, letter


def email(session: Session, sender: str, reply: dict[str, Any], text: str = "Sorry, next Wednesday.") -> eng.ChangeSet:
    person = next(p for p in eng.list_people(session) if p.name == sender)
    source = Source(kind="email", text=text, external_id=f"m-{len(text)}", url="https://mail.google.com/mail/u/0/#all/t1")
    return eng.process_message(session, FakeLLM(reply), text, NOW, ActivityBy.email, person=person, source=source)


def needs(session: Session) -> set[tuple[str, str]]:
    titles = {l.id: l.title for l in eng.list_loops(session, status=None)}
    return {(titles[d.loop_id], titles[d.needs_id]) for d in session.exec(select(Dependency)).all()}


# --- the §2 story ---


def test_a_stated_link_is_saved_with_who_was_promised(session: Session) -> None:
    app, letter = story(session)
    assert needs(session) == {(APP, LETTER)}
    assert (app.kind, app.waiting_on, app.person_id is not None) == (LoopKind.commitment, "Sarah", True)
    assert [a.detail for a in eng.list_activity(session, app.id) if a.action == "updated"] == [f'needs "{LETTER}"']
    assert eng.dependency_risks(session, TODAY) == {}  # the letter is due before the application


def test_a_delay_by_email_puts_the_promise_at_risk(session: Session) -> None:
    app, letter = story(session)
    changes = email(session, "Dr. Lim", result(updated=[{"id": str(letter.id), "due": "2026-10-07", "quote": "next Wednesday"}]))
    [risk] = changes.at_risk
    assert (risk.loop.id, risk.cause.id) == (app.id, letter.id)
    assert risk.reason == f'at risk: "{LETTER}" is due Wed Oct 7, after this'
    assert risk.options == ["Ask Dr. Lim if Thu Oct 1 works", "Tell Sarah early and offer Wed Oct 7", "Move your date"]
    assert eng.risk_lines(changes) == [
        f'At risk: "{APP}" (due Fri Oct 2): "{LETTER}" is due Wed Oct 7, after this. '
        "Options: Ask Dr. Lim if Thu Oct 1 works · Tell Sarah early and offer Wed Oct 7 · Move your date"
    ]
    [item] = eng.needs_attention(session, TODAY)
    assert (item.loop.id, item.reason, item.cause.id, item.options) == (app.id, risk.reason, letter.id, risk.options)  # type: ignore[union-attr]

    again = email(session, "Dr. Lim", result(updated=[{"id": str(letter.id), "summary": "Still Wednesday.", "quote": "Still Wed"}]), "Still Wed.")
    assert again.at_risk == []  # only new or changed risks are reported


def test_the_promise_is_ready_when_the_letter_arrives(session: Session) -> None:
    app, letter = story(session)
    portfolio = process(session, linked(result(new=[loop("Finish portfolio", due="2026-09-30")]),
                                        (str(app.id), "Finish portfolio")), "Portfolio first.")
    assert portfolio.linked and needs(session) == {(APP, LETTER), (APP, "Finish portfolio")}
    first = email(session, "Dr. Lim", result(resolved=[{"id": str(letter.id), "quote": "Here is your letter"}]),
                  "Here is your letter.")
    assert first.ready == []  # the portfolio is still open
    done = process(session, result(resolved=[str(portfolio.created_loops[0].id)]), "Portfolio done.")
    assert [l.id for l in done.ready] == [app.id]
    assert eng.risk_lines(done) == [f'Ready: "{APP}" (everything it needs is done).']


# --- guards: only stated, valid links ---


def test_bad_links_are_dropped(session: Session) -> None:
    app, letter = story(session)
    changes = process(session, linked(
        result(), (str(letter.id), str(app.id)),  # would be a cycle: the app needs the letter
        (str(app.id), str(app.id)), (str(app.id), LETTER), (str(app.id), "No such loop"), ("No such loop", str(app.id)),
    ), "Mixed up links.")  # self, unknown on either end
    assert changes.linked == [] and changes.source is None  # nothing new, so nothing is stored
    assert needs(session) == {(APP, LETTER)}


def test_a_link_alone_is_saved(session: Session) -> None:
    app = process(session, result(new=[loop(APP, "commitment", waiting_on="Sarah")]), "App for Sarah.").created_loops[0]
    letter = process(session, result(new=[loop(LETTER, "waiting", waiting_on="Dr. Lim")]), "Letter.").created_loops[0]
    changes = process(session, linked(result(), (str(app.id), str(letter.id))), "The app needs the letter.")
    assert changes.source is not None and [(a.id, b.id) for a, b in changes.linked] == [(app.id, letter.id)]


def test_an_existing_loop_can_be_named_by_title(session: Session) -> None:
    portfolio = process(session, result(new=[loop("Finish portfolio")]), "Portfolio.").created_loops[0]
    changes = process(session, linked(result(new=[loop("Send Google application")]),
                                      ("Send Google application", "finish portfolio")), "Google app once the portfolio's done.")
    assert [(a.title, b.id) for a, b in changes.linked] == [("Send Google application", portfolio.id)]


def test_an_email_links_only_its_senders_loops(session: Session) -> None:
    app, letter = story(session)
    other = process(session, result(new=[loop("Book the flight")]), "Book the flight.").created_loops[0]
    changes = email(session, "Dr. Lim", linked(result(), (str(letter.id), str(other.id))), "Letter after your flight.")
    assert changes.linked == [] and needs(session) == {(APP, LETTER)}  # the flight isn't Dr. Lim's loop


def test_a_repeated_loop_can_still_get_a_link(session: Session) -> None:
    app, letter = story(session)
    portfolio = process(session, result(new=[loop("Finish portfolio")]), "Portfolio.").created_loops[0]
    changes = process(session, linked(result(new=[loop(APP, "commitment", waiting_on="Sarah")]), (APP, str(portfolio.id))),
                      "The application needs my portfolio too.")
    assert changes.created_loops == [] and (APP, "Finish portfolio") in needs(session)


def test_delete_removes_its_links(session: Session) -> None:
    app, letter = story(session)
    eng.delete_loop(session, letter.id)
    assert session.exec(select(Dependency)).all() == [] and eng.get_loop(session, app.id).loop.status == LoopStatus.open


# --- the rules ---


def chain(session: Session, *dues: date | None, kinds: tuple[LoopKind, ...] = ()) -> list[OpenLoop]:
    """Loops where each one needs the next: chain(a, b, c) = A needs B needs C."""
    source = eng.add_source(session, "test")
    loops = [eng.add_loop(session, source_id=source.id, title=f"L{i}", summary="s", due=due,
                          kind=kinds[i] if i < len(kinds) else LoopKind.task) for i, due in enumerate(dues)]
    session.flush()
    for a, b in zip(loops, loops[1:]):
        session.add(Dependency(loop_id=a.id, needs_id=b.id))
    session.commit()
    return loops


def test_an_overdue_need_blocks_up_the_chain(session: Session) -> None:
    top, middle, bottom = chain(session, TODAY + timedelta(days=10), TODAY + timedelta(days=5), TODAY - timedelta(days=1))
    risks = eng.dependency_risks(session, TODAY)
    assert risks[top.id].reason == 'blocked: "L2" is overdue (via "L1")'
    assert risks[middle.id].reason == 'blocked: "L2" is overdue'
    assert bottom.id not in risks


def test_a_resolved_need_counts_as_done(session: Session) -> None:
    top, middle, _ = chain(session, TODAY + timedelta(days=2), TODAY + timedelta(days=5), TODAY - timedelta(days=3))
    eng.resolve_loop(session, middle.id)
    assert eng.dependency_risks(session, TODAY) == {}  # what the done loop needed doesn't matter anymore


def test_a_snoozed_need_still_counts(session: Session) -> None:
    top, need = chain(session, TODAY + timedelta(days=2), TODAY + timedelta(days=6))
    eng.snooze_loop(session, need.id, TODAY + timedelta(days=4), today=TODAY)
    assert eng.dependency_risks(session, TODAY)[top.id].reason.startswith('at risk: "L1" is due')


def test_no_date_matters_only_close_to_the_deadline(session: Session) -> None:
    soon, _ = chain(session, TODAY + timedelta(days=3), None)
    later, _ = chain(session, TODAY + timedelta(days=4), None)
    undated, _ = chain(session, None, None)
    risks = eng.dependency_risks(session, TODAY)
    assert risks[soon.id].reason == 'at risk: "L1" has no date yet' and risks[soon.id].options == ["Move your date"]
    assert later.id not in risks and undated.id not in risks


def test_overdue_and_blocked_show_both_reasons(session: Session) -> None:
    late, need = chain(session, TODAY - timedelta(days=2), TODAY - timedelta(days=5))
    due_soon, _ = chain(session, TODAY + timedelta(days=1), TODAY + timedelta(days=9))
    items = {a.loop.id: a for a in eng.needs_attention(session, TODAY)}
    assert items[late.id].reason == 'overdue by 2 days; blocked: "L1" is overdue'
    assert items[due_soon.id].reason == 'due tomorrow; at risk: "L1" is due Thu Oct 8, after this'
    order = [a.loop.id for a in eng.needs_attention(session, TODAY)]
    assert order.index(late.id) < order.index(due_soon.id)
