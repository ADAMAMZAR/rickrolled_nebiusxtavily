"""Turn one user message into structured continuity data. All LLM prompts live here."""

import json
import logging
from datetime import date, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

log = logging.getLogger("continuum.extraction")

SYSTEM_PROMPT = """\
You are Continuum's extraction engine, not a chat assistant. Read one user message and return JSON only.

Extract:
- goals: outcomes the user is working toward, e.g. "Secure NVIDIA internship".
- new_loops: concrete unresolved items:
  - kind "task": something the user needs to do.
  - kind "commitment": something the user promised someone.
  - kind "waiting": something the user is waiting on from someone else. Set waiting_on to who they wait on, as a
    short name (e.g. "Sarah", "NVIDIA"). If it's someone in the known people list, use that name exactly.
- updated_loops: changes to an EXISTING open loop (by id): a new due date, next action or summary.
- resolved_loop_ids: ids of EXISTING open loops that the message clearly says are finished or received.

Rules:
1. Extract only what the message states. Never invent people, dates or commitments. Unknown = null.
2. Ignore facts with no future action, e.g. "I like pizza" or "The meeting went well". Return empty lists.
3. Dates: convert relative dates to YYYY-MM-DD using the current date given. "Friday", "next Friday",
   "by Friday" and "before Friday" all mean the nearest upcoming Friday. If unsure, use null.
4. If the message is about an existing open loop, use updated_loops or resolved_loop_ids. Never add it again to new_loops.
5. Resolve a loop only when the message clearly says it is done or has arrived.
6. Set each new loop's "goal" to a goal's exact title (an existing goal or one in your goals list) only if the message
   mentions what that goal is about (e.g. the same company, project or event). Otherwise null, even if the user
   has only one goal.
7. Don't repeat an existing goal in goals.
8. Titles are short (about 2-6 words), e.g. "Wait for Sarah's response", "Finish portfolio". Summaries are one sentence.
   Goal titles start with a verb and name the outcome, e.g. "Secure NVIDIA internship", not "NVIDIA internship".

Return exactly this JSON shape:
{"goals": [{"title": "...", "description": "..."}],
 "new_loops": [{"title": "...", "summary": "...", "kind": "task", "waiting_on": null, "due": null, "next_action": null, "goal": null}],
 "updated_loops": [{"id": "...", "summary": null, "due": null, "next_action": null}],
 "resolved_loop_ids": []}
"""

INVALID_MESSAGE = "The message could not be safely converted into structured continuity data."

WEB_NOTE = "These are web search results about this open loop. Only report a change a result clearly states."

EMAIL_NOTE = ("This is an email to the user from {name}. The existing open loops are the ones involving {name}. "
              "Only report what the email clearly states.")


class ExtractionError(Exception):
    pass


class Completer(Protocol):
    def complete(self, messages: list[dict[str, str]], json_mode: bool = False) -> str: ...


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _blank_to_none(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {k: (None if v == "" else v) for k, v in data.items()}
        return data


class GoalCandidate(_Model):
    title: str
    description: str | None = None


class LoopCandidate(_Model):
    title: str
    summary: str
    kind: Literal["task", "commitment", "waiting"]
    waiting_on: str | None = None
    due: date | None = None
    next_action: str | None = None
    goal: str | None = None


class LoopUpdate(_Model):
    id: str
    summary: str | None = None
    due: date | None = None
    next_action: str | None = None


class ExtractionResult(_Model):
    goals: list[GoalCandidate] = []
    new_loops: list[LoopCandidate] = []
    updated_loops: list[LoopUpdate] = []
    resolved_loop_ids: list[str] = []


def build_messages(
    text: str,
    now: datetime,
    goals: list[dict[str, Any]],
    loops: list[dict[str, Any]],
    note: str | None = None,
    people: list[str] | None = None,
) -> list[dict[str, str]]:
    """`note` says where text that isn't the user's own message came from, e.g. WEB_NOTE.
    `people` are names only: their emails never go to the model."""
    context = (
        f"Current date: {now:%A %Y-%m-%d} ({now.tzinfo})\n\n"
        f"Existing active goals: {json.dumps(goals) if goals else 'none'}\n\n"
        f"Known people: {json.dumps(people) if people else 'none'}\n\n"
        f"Existing open loops: {json.dumps(loops) if loops else 'none'}\n\n"
        + (f"{note}\n\nText" if note else "User message")
        + f':\n"""\n{text}\n"""'
    )
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": context}]


def extract(
    llm: Completer,
    text: str,
    now: datetime,
    goals: list[dict[str, Any]],
    loops: list[dict[str, Any]],
    note: str | None = None,
    people: list[str] | None = None,
) -> ExtractionResult:
    """Call the LLM and validate its JSON. One retry with the error attached, then fail."""
    messages = build_messages(text, now, goals, loops, note, people)
    for attempt in (1, 2):
        raw = llm.complete(messages, json_mode=True)
        try:
            result = ExtractionResult.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError) as e:
            log.warning("extraction_invalid attempt=%d error=%s", attempt, type(e).__name__)
            messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"That JSON was invalid: {e}. Return only the corrected JSON."},
            ]
            continue
        log.info(
            "extraction_ok goals=%d new=%d updated=%d resolved=%d",
            len(result.goals), len(result.new_loops), len(result.updated_loops), len(result.resolved_loop_ids),
        )
        return result
    raise ExtractionError(INVALID_MESSAGE)
