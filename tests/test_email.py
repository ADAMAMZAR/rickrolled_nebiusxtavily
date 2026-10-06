"""Email sync (spec §6): only linked people are read, each email once, and an email touches only its sender's loops."""

from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from app import engine as eng
from app.connectors.google import Email, GoogleError
from app.db import LoopStatus, Source
from app.extraction import EMAIL_NOTE
from tests.conftest import NOW, FakeLLM
from tests.test_activity import demo, history
from tests.test_process_message import loop, process, result

SARAH = "sarah@nvidia.com"


def email(id: str = "m1", body: str = "Great news, we'd like to interview you on Oct 8.", minutes: int = 0) -> Email:
    return Email(id=id, thread_id=f"t-{id}", sender=f"Sarah <{SARAH}>", subject="Interview", snippet="",
                 body=body, received=NOW.astimezone(UTC) + timedelta(minutes=minutes))


class FakeMailbox:
    """Returns queued emails per sender and records each query."""

    def __init__(self, mail: dict[str, list[Email]] | None = None, error: str | None = None) -> None:
        self.mail = mail or {}
        self.error = error
        self.queries: list[tuple[str, datetime | None]] = []

    def messages_from(self, sender: str, after: datetime | None = None, limit: int = 5) -> list[Email]:
        self.queries.append((sender, after))
        if self.error:
            raise GoogleError(self.error)
        return self.mail.get(sender, [])


def linked(session: Session):
    wait, portfolio = demo(session)
    eng.set_person_email(session, "Sarah", SARAH)
    return wait, portfolio


def sync(session: Session, llm: FakeLLM, mailbox: FakeMailbox, now: datetime = NOW) -> tuple[list[str], list[str]]:
    return eng.sync_email(session, llm, mailbox, now=now)


def test_an_email_closes_its_loop_with_undo(session: Session) -> None:
    wait, _ = linked(session)
    llm = FakeLLM(result(resolved=[str(wait.id)], new=[loop("Prepare for NVIDIA interview", due="2026-10-08")]))
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


def test_only_people_with_an_email_and_an_open_loop_are_read(session: Session) -> None:
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
    sync(session, FakeLLM(result(updated=[{"id": str(wait.id), "due": "2026-10-09"}])), mailbox)
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
    lines, _ = sync(session, FakeLLM(result(resolved=[str(wait.id)])), mailbox)
    assert len(lines) == 1


def test_later_emails_win(session: Session) -> None:
    wait, _ = linked(session)
    newest_first = [email("m2", "Actually, Oct 12.", minutes=5), email("m1", "Interview Oct 9.")]
    llm = FakeLLM(result(updated=[{"id": str(wait.id), "due": "2026-10-09"}]),
                  result(updated=[{"id": str(wait.id), "due": "2026-10-12"}]))
    sync(session, llm, FakeMailbox({SARAH: newest_first}))
    assert str(eng.get_loop(session, wait.id).loop.due) == "2026-10-12"
    assert "Oct 9" in llm.calls[0][1]["content"]


def test_forget_email_data(session: Session) -> None:
    wait, _ = linked(session)
    sync(session, FakeLLM(result(resolved=[str(wait.id)], new=[loop("Prepare for interview")])), FakeMailbox({SARAH: [email()]}))
    assert eng.forget_email_data(session) == 1
    new = next(l for l in eng.list_loops(session) if l.title == "Prepare for interview")
    source = eng.get_loop(session, new.id).source
    assert (source.text, source.sender, source.url, source.external_id) == ("", None, None, "m1")
    assert all(a.detail == "" for a in eng.list_activity(session) if a.by == "email")
    assert ("resolved", "email", True) in history(session, wait)
    eng.reopen_loop(session, wait.id)  # so Sarah is read again
    llm, mailbox = FakeLLM(), FakeMailbox({SARAH: [email()]})
    sync(session, llm, mailbox, now=NOW + timedelta(minutes=10))
    assert mailbox.queries and llm.calls == []  # a forgotten email is never read again
