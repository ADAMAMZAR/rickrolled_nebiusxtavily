from datetime import date

import pytest

from app.extraction import ExtractionError, extract
from tests.conftest import NOW, FakeLLM

VALID = {
    "goals": [{"title": "Secure NVIDIA internship", "description": ""}],
    "new_loops": [
        {"title": "Wait for Sarah's response", "summary": "Sarah will reply.", "kind": "waiting",
         "waiting_on": "Sarah", "due": "2026-10-02", "next_action": None, "goal": "Secure NVIDIA internship",
         "extra_field": "ignored"},
    ],
    "updated_loops": [],
    "resolved_loop_ids": [],
}


def test_valid_json_is_parsed() -> None:
    result = extract(FakeLLM(VALID), "msg", NOW, goals=[], loops=[])
    loop = result.new_loops[0]
    assert loop.kind == "waiting"
    assert loop.due == date(2026, 10, 2)
    assert result.goals[0].description is None  # "" becomes None


def test_empty_result_is_valid() -> None:
    empty = {"goals": [], "new_loops": [], "updated_loops": [], "resolved_loop_ids": []}
    result = extract(FakeLLM(empty), "I like pizza.", NOW, goals=[], loops=[])
    assert result.new_loops == [] and result.goals == []


def test_prompt_has_date_and_existing_state() -> None:
    llm = FakeLLM(VALID)
    loops = [{"id": "abc-123", "title": "Finish portfolio", "kind": "task", "waiting_on": None, "due": None, "goal": None}]
    extract(llm, "Sarah replied!", NOW, goals=[{"title": "Secure NVIDIA internship"}], loops=loops)
    user = llm.calls[0][1]["content"]
    assert "Tuesday 2026-09-29" in user
    assert "abc-123" in user and "Secure NVIDIA internship" in user
    assert "Sarah replied!" in user


def test_invalid_then_valid_retries_once_with_error() -> None:
    llm = FakeLLM("not json", VALID)
    result = extract(llm, "msg", NOW, goals=[], loops=[])
    assert len(result.new_loops) == 1
    assert "invalid" in llm.calls[1][-1]["content"]


def test_bad_kind_twice_fails() -> None:
    bad = {**VALID, "new_loops": [{**VALID["new_loops"][0], "kind": "deadline"}]}
    with pytest.raises(ExtractionError):
        extract(FakeLLM(bad, bad), "msg", NOW, goals=[], loops=[])
