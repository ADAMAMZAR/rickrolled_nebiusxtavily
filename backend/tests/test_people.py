"""Person linking (spec §5): waiting loops link to a Person by name; emails only come from the user."""

import pytest
from sqlmodel import Session, select

from app import engine as eng
from app.db import Person
from tests.conftest import NOW, FakeLLM
from tests.test_activity import demo
from tests.test_process_message import loop, process, result


def people(session: Session) -> dict[str, str | None]:
    return {p.name: p.email for p in session.exec(select(Person)).all()}


def test_a_waiting_loop_links_to_its_person(session: Session) -> None:
    wait, portfolio = demo(session)
    assert people(session) == {"Sarah": None}
    assert wait.person_id is not None and portfolio.person_id is None  # tasks have no person


def test_the_same_name_is_one_person(session: Session) -> None:
    wait, _ = demo(session)
    again = process(session, result(new=[loop("Get the offer letter", "waiting", waiting_on="sarah")]), "Sarah will send the offer.")
    assert again.created_loops[0].person_id == wait.person_id
    assert list(people(session)) == ["Sarah"]


def test_the_model_sees_known_names_but_not_emails(session: Session) -> None:
    demo(session)
    eng.set_person_email(session, "Sarah", "sarah@nvidia.com")
    llm = FakeLLM(result())
    eng.process_message(session, llm, "Any news?", now=NOW)
    prompt = llm.calls[0][1]["content"]
    assert 'Known people: ["Sarah"]' in prompt and "sarah@nvidia.com" not in prompt


def test_set_person_email(session: Session) -> None:
    wait, _ = demo(session)
    person = eng.set_person_email(session, " sarah ", " Sarah@NVIDIA.com ")
    assert (person.name, person.email, person.id) == ("Sarah", "sarah@nvidia.com", wait.person_id)
    assert eng.set_person_email(session, "Sarah", "").email is None  # empty clears it
    with pytest.raises(eng.InvalidRequest, match="isn't an email address"):
        eng.set_person_email(session, "Sarah", "sarah at nvidia")
    with pytest.raises(eng.InvalidRequest, match="whose email"):
        eng.set_person_email(session, "  ", "a@b.co")


def test_one_address_belongs_to_one_person(session: Session) -> None:
    """Sync gives each email to one sender, so the same address on two people left one of them without it."""
    demo(session)
    eng.set_person_email(session, "Sarah", "sarah@nvidia.com")
    with pytest.raises(eng.InvalidRequest, match="already Sarah's email"):
        eng.set_person_email(session, "Ben", "Sarah@nvidia.com")
    assert eng.set_person_email(session, "sarah", "sarah@nvidia.com").email == "sarah@nvidia.com"  # saving it again is fine
    eng.set_person_email(session, "Sarah", None)
    assert eng.set_person_email(session, "Ben", "sarah@nvidia.com").email == "sarah@nvidia.com"  # once it's free


def test_a_new_name_creates_the_person_and_links_matching_loops(session: Session) -> None:
    """Loops saved before their person existed (or without one) get linked by their waiting_on name."""
    changes = process(session, result(new=[loop("Get dataset", "waiting", waiting_on="Alex")]), "Alex owes me the dataset.")
    alex_loop = changes.created_loops[0]
    alex_loop.person_id = None  # e.g. saved before person linking
    session.commit()
    person = eng.set_person_email(session, "alex", "alex@uni.edu")
    assert eng.get_loop(session, alex_loop.id).loop.person_id == person.id
    assert [l.title for l in eng.person_loops(session, person.id)] == ["Get dataset"]
