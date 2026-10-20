"""Email sync (spec §6): linked people are read, each email once, and an email touches only its sender's loops.
Mail from anyone else only proposes loops, for the user's yes."""

from datetime import UTC, datetime, timedelta

import pytest

from sqlmodel import Session, select

from app import engine as eng
from app.config import settings
from app.connectors.google import Email, GoogleError
from app.db import LoopStatus, Source
from app.extraction import EMAIL_NOTE
from tests.conftest import NOW, FakeLLM
from tests.test_activity import demo, history
from tests.test_process_message import loop, process, result

SARAH = "sarah@nvidia.com"
YES, OCT8 = "we'd like to interview you", "interview you on Oct 8"  # quotes from the default email


def email(id: str = "m1", body: str = "Great news, we'd like to interview you on Oct 8.", minutes: int = 0) -> Email:
    return Email(id=id, thread_id=f"t-{id}", sender=f"Sarah <{SARAH}>", subject="Interview", snippet="",
                 body=body, received=NOW.astimezone(UTC) + timedelta(minutes=minutes))


class FakeMailbox:
    """Returns queued emails per sender, and `new` as new mail (anyone), and records each query."""

    def __init__(self, mail: dict[str, list[Email]] | None = None, error: str | None = None,
                 new: list[Email] | None = None) -> None:
        self.mail = mail or {}
        self.error = error
        self.new = new or []
        self.queries: list[tuple[str, datetime | None]] = []
        self.new_queries: list[datetime | None] = []

    def messages_from(self, sender: str, after: datetime | None = None, limit: int = 5) -> list[Email]:
        self.queries.append((sender, after))
        if self.error:
            raise GoogleError(self.error)
        return self.mail.get(sender, [])

    def new_mail(self, after: datetime | None = None, limit: int = 20) -> list[Email]:
        self.new_queries.append(after)
        if self.error:
            raise GoogleError(self.error)
        return self.new


def linked(session: Session):
    wait, portfolio = demo(session)
    eng.set_person_email(session, "Sarah", SARAH)
    return wait, portfolio


def sync(session: Session, llm: FakeLLM, mailbox: FakeMailbox, now: datetime = NOW) -> tuple[list[str], list[str]]:
    return eng.sync_email(session, llm, mailbox, now=now)


def test_an_email_closes_its_loop_with_undo(session: Session) -> None:
    wait, _ = linked(session)
    llm = FakeLLM(result(resolved=[{"id": str(wait.id), "quote": YES}],
                         new=[loop("Prepare for NVIDIA interview", due="2026-10-08", quote=OCT8)]))
    lines, errors = sync(session, llm, FakeMailbox({SARAH: [email()]}))
    assert lines == ['Email from Sarah: closed "Wait for Sarah\'s response"; new "Prepare for NVIDIA interview" (due Thu Oct 8).']
    assert errors == []
    assert eng.get_loop(session, wait.id).loop.status == LoopStatus.resolved
    assert eng.resolved_by(session, wait) == "email"

    prompt = llm.calls[0][1]["content"]
    assert EMAIL_NOTE.format(name="Sarah") in prompt and "Subject: Interview" in prompt
    assert SARAH not in prompt  # the address never goes to the model

    closed = next(a for a in eng.list_activity(session, wait.id) if a.by == "email")
    assert closed.detail == "https://mail.google.com/mail/u/0/#all/t-m1"
    assert eng.undo_activity(session, closed.id).status == LoopStatus.open

    new = next(l for l in eng.list_loops(session) if l.title == "Prepare for NVIDIA interview")
    source = eng.get_loop(session, new.id).source
    assert (source.kind, source.sender, source.external_id) == ("email", "Sarah", "m1")


def test_only_people_with_an_email_and_an_open_loop_are_followed(session: Session) -> None:
    wait, _ = linked(session)
    process(session, result(new=[loop("Get dataset", "waiting", waiting_on="Alex")]), "Alex owes me the dataset.")
    eng.set_person_email(session, "Bob", "bob@x.com")  # no open loop
    mailbox = FakeMailbox()
    sync(session, FakeLLM(), mailbox)
    assert [q[0] for q in mailbox.queries] == [SARAH]  # Alex has no email, Bob has no loop

    eng.resolve_loop(session, wait.id)
    mailbox.queries.clear()
    sync(session, FakeLLM(), mailbox)
    assert mailbox.queries == []


def test_an_email_can_only_touch_its_senders_loops(session: Session) -> None:
    wait, portfolio = linked(session)
    llm = FakeLLM(result(resolved=[str(portfolio.id)], updated=[{"id": str(portfolio.id), "due": "2026-10-20"}]))
    assert sync(session, llm, FakeMailbox({SARAH: [email()]})) == ([], [])
    assert eng.get_loop(session, portfolio.id).loop.status == LoopStatus.open
    prompt = llm.calls[0][1]["content"]
    assert str(wait.id) in prompt and str(portfolio.id) not in prompt


def test_each_email_is_processed_once_and_last_sync_advances(session: Session) -> None:
    wait, _ = linked(session)
    mailbox = FakeMailbox({SARAH: [email()]})
    sync(session, FakeLLM(result(updated=[{"id": str(wait.id), "due": "2026-10-09", "quote": OCT8}])), mailbox)
    assert mailbox.queries[0][1] == NOW.astimezone(UTC) - timedelta(days=7)  # first run: SYNC_LOOKBACK_DAYS
    later = NOW + timedelta(minutes=10)
    llm = FakeLLM()  # no replies queued: any LLM call would fail the test
    assert sync(session, llm, mailbox, now=later) == ([], [])
    assert llm.calls == [] and mailbox.queries[1][1] == NOW.astimezone(UTC)
    assert len(session.exec(select(Source).where(Source.external_id == "m1")).all()) == 1


def test_an_email_that_changes_nothing_isnt_stored(session: Session) -> None:
    linked(session)
    assert sync(session, FakeLLM(result()), FakeMailbox({SARAH: [email(body="Thanks for applying!")]})) == ([], [])
    assert session.exec(select(Source).where(Source.kind == "email")).all() == []


def test_a_google_error_writes_nothing_and_is_reported_once(session: Session) -> None:
    linked(session)
    broken = FakeMailbox(error="Google disconnected (access expired or was revoked).")
    assert sync(session, FakeLLM(), broken) == ([], ["Google disconnected (access expired or was revoked)."])
    assert sync(session, FakeLLM(), broken) == ([], [])  # same error next run: no repeat message
    assert eng.get_setting(session, eng.LAST_EMAIL_SYNC) is None
    sync(session, FakeLLM(), FakeMailbox())  # fixed: the error is cleared
    assert sync(session, FakeLLM(), broken)[1] != []


def test_an_unreadable_email_is_read_again_next_run(session: Session) -> None:
    wait, _ = linked(session)
    mailbox = FakeMailbox({SARAH: [email()]})
    lines, errors = sync(session, FakeLLM("nope", "still nope"), mailbox)
    assert lines == [] and errors[0].startswith("Couldn't read an email from Sarah")
    assert eng.get_setting(session, eng.LAST_EMAIL_SYNC) is None
    lines, _ = sync(session, FakeLLM(result(resolved=[{"id": str(wait.id), "quote": YES}])), mailbox)
    assert len(lines) == 1


def test_later_emails_win(session: Session) -> None:
    wait, _ = linked(session)
    newest_first = [email("m2", "Actually, Oct 12.", minutes=5), email("m1", "Interview Oct 9.")]
    llm = FakeLLM(result(updated=[{"id": str(wait.id), "due": "2026-10-09", "quote": "Interview Oct 9"}]),
                  result(updated=[{"id": str(wait.id), "due": "2026-10-12", "quote": "Actually, Oct 12"}]))
    sync(session, llm, FakeMailbox({SARAH: newest_first}))
    assert str(eng.get_loop(session, wait.id).loop.due) == "2026-10-12"
    assert "Oct 9" in llm.calls[0][1]["content"]


def test_forget_email_data(session: Session) -> None:
    wait, _ = linked(session)
    reply = result(resolved=[{"id": str(wait.id), "quote": YES}], new=[loop("Prepare for interview", quote=OCT8)])
    sync(session, FakeLLM(reply), FakeMailbox({SARAH: [email()]}))
    assert eng.forget_email_data(session) == 1
    new = next(l for l in eng.list_loops(session) if l.title == "Prepare for interview")
    source = eng.get_loop(session, new.id).source
    assert (source.text, source.sender, source.url, source.external_id) == ("", None, None, "m1")
    assert all(a.detail == "" and a.quote is None for a in eng.list_activity(session) if a.by == "email")
    assert ("resolved", "email", True) in history(session, wait)
    eng.reopen_loop(session, wait.id)  # so Sarah is read again
    llm, mailbox = FakeLLM(), FakeMailbox({SARAH: [email()]})
    sync(session, llm, mailbox, now=NOW + timedelta(minutes=10))
    assert mailbox.queries and llm.calls == []  # a forgotten email is never read again


# --- calendar events in an email become proposals (spec §1 step 3) ---


@pytest.fixture
def google_on(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    token = tmp_path / "google_token.json"
    token.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(settings, "google_token_file", str(token))


@pytest.mark.usefixtures("google_on")
def test_an_event_in_an_email_becomes_a_calendar_proposal(session: Session) -> None:
    wait, _ = linked(session)
    reply = result(resolved=[{"id": str(wait.id), "quote": YES}],
                   new=[loop("Prepare for NVIDIA interview", due="2026-10-08", quote=OCT8)])
    reply["events"] = [{"title": "NVIDIA interview", "start": "2026-10-08T10:00", "end": None}]
    llm = FakeLLM(reply)
    lines, _ = sync(session, llm, FakeMailbox({SARAH: [email()]}))
    assert lines[1] == 'Add "NVIDIA interview" to your calendar, Thu Oct 8 10:00? (yes/no)'
    [action] = eng.list_actions(session)
    prepare = next(l for l in eng.list_loops(session) if l.title == "Prepare for NVIDIA interview")
    assert action.loop_id == prepare.id  # on the loop the email created
    assert '"events"' in llm.calls[0][1]["content"]


@pytest.mark.usefixtures("google_on")
def test_past_and_malformed_events_are_skipped(session: Session) -> None:
    linked(session)
    reply = result()
    reply["events"] = [
        {"title": "Yesterday's call", "start": "2026-09-28T10:00"},
        {"title": "No time", "start": "next week"},
        {"title": "Ends before it starts", "start": "2026-10-08T10:00", "end": "2026-10-08T09:00"},
    ]
    assert sync(session, FakeLLM(reply), FakeMailbox({SARAH: [email()]})) == ([], [])
    assert eng.list_actions(session) == []


def test_chat_messages_never_propose_events(session: Session) -> None:
    reply = result(new=[loop("Interview", due="2026-10-08")])
    reply["events"] = [{"title": "Interview", "start": "2026-10-08T10:00"}]
    assert process(session, reply, "Interview on Oct 8 at 10.").events == []


def test_an_email_task_that_asks_for_money_waits_for_a_yes(session: Session) -> None:
    """Published attacks get in as plain requests in an email; the harmful ones name money, a link or an address
    (troth_eval.md §13.2). Those wait for a yes; the rest of the email applies as before."""
    linked(session)
    fee = "please pay the RM250 admin fee today"
    reply = result(new=[loop("Pay RM250 admin fee", quote=fee), loop("Prepare for NVIDIA interview", quote=OCT8)])
    lines, _ = sync(session, FakeLLM(reply), FakeMailbox({SARAH: [email(body=f"Great news, {OCT8}. Also {fee}.")]}))
    assert lines == ['Email from Sarah: new "Prepare for NVIDIA interview".',
                     'Add "Pay RM250 admin fee" from Sarah\'s email? It asks for money or details, so check who\'s asking'
                     ' first. (yes/no)']
    assert "Pay RM250 admin fee" not in [l.title for l in eng.list_loops(session)]

    action = eng.list_actions(session)[0]
    eng.approve_action(session, action.id)
    made = next(l for l in eng.list_loops(session) if l.title == "Pay RM250 admin fee")
    assert eng.get_loop(session, made.id).source.external_id == "m1"
    created = eng.list_activity(session, made.id)[0]
    assert (created.by, created.quote, created.detail) == ("email", fee, "https://mail.google.com/mail/u/0/#all/t-m1")
    with pytest.raises(eng.InvalidRequest):  # once only
        eng.approve_action(session, action.id)


def test_a_rejected_email_task_keeps_nothing_of_the_email(session: Session) -> None:
    linked(session)
    words = "confirm your account at www.nvidia-careers.co today"
    reply = result(new=[loop("Confirm account", quote=words)])
    lines, _ = sync(session, FakeLLM(reply), FakeMailbox({SARAH: [email(body=f"Hi Adam, please {words}.")]}))
    assert lines == ['Add "Confirm account" from Sarah\'s email? It has a link, so check who\'s asking first. (yes/no)']
    rejected = eng.reject_action(session, eng.list_actions(session)[0].id)
    source = session.exec(select(Source).where(Source.external_id == "m1")).one()
    assert (source.text, source.sender) == ("", None)  # the Gmail id stays, so it's never read again
    assert (rejected.payload["quote"], rejected.payload["sender_email"], rejected.payload["source_url"]) == ("", None, None)
    assert eng.hold_reason("Send the report to ben@example.com") == "has an email address"
    assert eng.hold_reason("Reply with the security code") == eng.hold_reason("Add a donation appeal") == (
        "asks for money or details")
    assert eng.hold_reason("Send the report by Friday") is None


def test_forgetting_email_data_drops_the_tasks_it_proposed(session: Session) -> None:
    linked(session)
    fee = "please pay the RM250 admin fee today"
    sync(session, FakeLLM(result(new=[loop("Pay RM250 admin fee", quote=fee)])), FakeMailbox({SARAH: [email(body=fee)]}))
    assert len(eng.list_actions(session)) == 1
    eng.forget_email_data(session)
    assert eng.list_actions(session, None) == []


def test_an_email_change_needs_words_from_the_email(session: Session) -> None:
    """Email changes apply without the user, so one the model can't quote from the email isn't made (audit item 4)."""
    wait, _ = linked(session)
    reply = result(resolved=[{"id": str(wait.id), "quote": "We are pleased to offer you the role"}],  # not in the email
                   new=[loop("Wire the deposit"), loop("Prepare for NVIDIA interview", quote=OCT8)])  # no quote; real
    lines, _ = sync(session, FakeLLM(reply), FakeMailbox({SARAH: [email()]}))
    assert lines == ['Email from Sarah: new "Prepare for NVIDIA interview".']
    assert eng.get_loop(session, wait.id).loop.status == LoopStatus.open
    [made] = [a for a in eng.list_activity(session) if a.by == "email"]
    assert made.quote == OCT8 and eng.get_loop(session, made.loop_id).quote == OCT8


def test_one_sync_reports_a_google_error_once_and_keeps_it_for_settings(session: Session) -> None:
    """scripts/sync.py and AUTO_SYNC_MINUTES share run_sync. Without Telegram, Settings shows the error."""
    wait, _ = linked(session)
    llm = FakeLLM(result(resolved=[{"id": str(wait.id), "quote": YES}]))
    assert eng.run_sync(session, llm, lambda: FakeMailbox(error="Token has been expired or revoked."), None) == [
        "Token has been expired or revoked."]
    assert eng.run_sync(session, llm, lambda: FakeMailbox(error="Token has been expired or revoked."), None) == []
    assert eng.get_setting(session, eng.LAST_SYNC_ERROR) == "Token has been expired or revoked."
    assert eng.run_sync(session, llm, lambda: FakeMailbox({SARAH: [email()]}), None) == [
        'Email from Sarah: closed "Wait for Sarah\'s response".']
    assert eng.get_setting(session, eng.LAST_SYNC_ERROR) is None  # a good check clears it


def test_an_email_task_is_linked_to_its_sender_so_their_next_email_can_close_it(session: Session) -> None:
    """On real mail, "you've passed the assessment" couldn't close the assessment task, and a reminder made a second
    one: an email's task had no person, so the sender's next email never saw it (troth_eval.md §15)."""
    linked(session)
    task = "please complete the online assessment by Sunday"
    sync(session, FakeLLM(result(new=[loop("Complete online assessment", quote=task)])),
         FakeMailbox({SARAH: [email(body=f"Hi Adam, {task}.")]}))
    made = next(l for l in eng.list_loops(session) if l.title == "Complete online assessment")
    sarah = next(p for p in eng.list_people(session) if p.name == "Sarah")
    assert made.person_id == sarah.id and made.waiting_on is None  # still a task, not a "waiting on"
    llm = FakeLLM(result(resolved=[{"id": str(made.id), "quote": "you passed the assessment"}]))
    lines, _ = sync(session, llm, FakeMailbox({SARAH: [email(id="m2", body="Hi Adam, you passed the assessment!", minutes=5)]}),
                    now=NOW + timedelta(minutes=10))
    assert lines == ['Email from Sarah: closed "Complete online assessment".']
    assert str(made.id) in llm.calls[0][1]["content"]  # the model was shown it


# --- mail from someone new: proposals only ---

PEARL = "pearl@revolab.com"
SIGN = "please sign and return the contract by Friday"


def pearl(id: str = "p1", body: str = f"Hi Adam, {SIGN}.", minutes: int = 0) -> Email:
    return Email(id=id, thread_id=f"t-{id}", sender=f"Pearl Li <{PEARL}>", subject="Offer", snippet="",
                 body=body, received=NOW.astimezone(UTC) + timedelta(minutes=minutes))


def test_mail_from_someone_new_only_proposes_and_a_yes_follows_them(session: Session) -> None:
    """Sync read only people linked to an open loop, so a new recruiter's request never reached Troth. Their mail
    now proposes loops and changes none; a yes saves their address, so their replies are followed."""
    wait, _ = linked(session)
    reply = result(goals=[{"title": "Join Revolab"}], resolved=[{"id": str(wait.id), "quote": SIGN}],
                   new=[loop("Sign Revolab contract", due="2026-10-02", goal="Join Revolab", quote=SIGN)])
    llm, mailbox = FakeLLM(reply), FakeMailbox(new=[pearl()])
    lines, _ = sync(session, llm, mailbox)
    assert lines == ['Add "Sign Revolab contract" (due Fri Oct 2) from Pearl Li\'s email? They\'re new: on a yes, '
                     'Troth follows their replies. (yes/no)']
    assert str(wait.id) not in llm.calls[0][1]["content"] and PEARL not in llm.calls[0][1]["content"]
    assert eng.get_loop(session, wait.id).loop.status == LoopStatus.open
    assert [s.goal.title for s in eng.list_goals(session)] == ["Secure NVIDIA internship"]  # no goal without a yes
    assert "Pearl Li" not in [p.name for p in eng.list_people(session)]  # nobody is kept for a proposal

    eng.approve_action(session, eng.list_actions(session)[0].id)
    made = next(l for l in eng.list_loops(session) if l.title == "Sign Revolab contract")
    person = next(p for p in eng.list_people(session) if p.name == "Pearl Li")
    assert (made.person_id, person.email, made.goal_id is not None) == (person.id, PEARL, True)
    sync(session, FakeLLM(), mailbox, now=NOW + timedelta(minutes=10))
    assert PEARL in [q[0] for q in mailbox.queries]  # followed: their next email can update or close it


def test_a_risky_ask_from_someone_new_says_why_and_a_reminder_doesnt_ask_twice(session: Session) -> None:
    words = "pay the RM50 deposit at www.revolab-pay.co"
    reply = result(new=[loop("Pay Revolab deposit", quote=words)])
    lines, _ = sync(session, FakeLLM(reply, reply),
                    FakeMailbox(new=[pearl("p2", f"Reminder: {words}.", minutes=5), pearl("p1", f"Hi, {words}.")]))
    assert lines == ['Add "Pay Revolab deposit" from Pearl Li\'s email? It has a link, so check who\'s asking first. (yes/no)']
    assert len(eng.list_actions(session)) == 1


def test_someone_saved_without_an_open_loop_is_new_too(session: Session) -> None:
    eng.set_person_email(session, "Bob", "bob@x.com")
    ask = "send the slides by Friday"
    mail = Email(id="b1", thread_id="t-b1", sender="Robert <bob@x.com>", subject="Slides", snippet="",
                 body=f"Can you {ask}?", received=NOW.astimezone(UTC))
    sync(session, FakeLLM(result(new=[loop("Send slides to Bob", quote=ask)])), FakeMailbox(new=[mail]))
    eng.approve_action(session, eng.list_actions(session)[0].id)
    [bob] = eng.list_people(session)
    assert (bob.name, bob.email) == ("Bob", "bob@x.com")
    assert next(l for l in eng.list_loops(session) if l.title == "Send slides to Bob").person_id == bob.id


def test_new_mail_from_someone_followed_is_read_once_as_theirs(session: Session) -> None:
    wait, _ = linked(session)
    llm = FakeLLM(result(resolved=[{"id": str(wait.id), "quote": YES}]))
    lines, _ = sync(session, llm, FakeMailbox({SARAH: [email()]}, new=[email()]))
    assert lines == ['Email from Sarah: closed "Wait for Sarah\'s response".'] and len(llm.calls) == 1


def test_new_senders_can_be_turned_off(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "email_new_senders", False)
    mailbox = FakeMailbox(new=[pearl()])
    assert sync(session, FakeLLM(), mailbox) == ([], [])
    assert mailbox.new_queries == []
