"""CI guard for ScamGraph's rules (troth_eval.md §3). Checks of test scenarios A-F were run once against real Nemotron
and Tavily and stored whole (fixtures/scam_frozen.jsonl). Here they're scored again with today's rules, offline: a
rule change may not turn one into a miss or a false alarm that it wasn't when recorded.

Re-record after a change to the research steps (not needed for rule changes):
  EVAL_DIR=tests/scenarios/eval_sample EVAL_RUNS=1 EVAL_ONLY=scam pytest -m eval
then copy the new results file over fixtures/scam_frozen.jsonl.
"""

from pathlib import Path
from typing import Any

import pytest

from scripts import eval_report as rep

FROZEN = Path(__file__).parent / "fixtures" / "scam_frozen.jsonl"
BAD = {"miss", "false_alarm"}


def frozen() -> list[dict[str, Any]]:
    return [r for r in rep.load(FROZEN) if r["status"] == "done"]


def test_every_scenario_is_frozen() -> None:
    assert {r["item"] for r in frozen()} == set("abcdef")


def test_rules_dont_turn_a_frozen_check_into_a_miss_or_false_alarm() -> None:
    for r in frozen():
        now = rep.outcome(r["label"], rep.rescore(r["frozen"])[1])
        assert not (now in BAD and rep.outcome(r["label"], r["level"]) not in BAD), (r["item"], r["level"], now)


def test_rescoring_matches_the_pipeline_while_the_rules_are_the_same() -> None:
    same = [r for r in frozen() if r["rules"] == rep.rules_version()]
    if not same:
        pytest.skip("The rules changed since the checks were recorded; the guard above still runs.")
    assert [rep.rescore(r["frozen"]) for r in same] == [(r["score"], r["level"]) for r in same]
