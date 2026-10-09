"""Continuity engine: all domain logic lives here.

`add_*` helpers don't commit, so callers can group them into one transaction.
User actions (resolve, reopen, delete) commit themselves.
Every loop change writes an Activity row in the same transaction.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, TypeAdapter, field_validator, model_validator
from sqlmodel import Session, col, delete, func, or_, select, update

from app.config import settings
from app.db import (
    ActionKind, ActionStatus, Activity, ActivityAction, ActivityBy, Goal, GoalStatus, Investigation, LoopKind, LoopStatus,
    OpenLoop, PendingAction, Person, RiskLevel, Setting, Source, utcnow,
)
from app.connectors import google
from app.connectors.google import Email, GoogleClient, GoogleError
from app.connectors.tavily import NOT_SET_UP, TavilyError, WebResult
from app.extraction import EMAIL_NOTE, WEB_NOTE, Completer, EventCandidate, ExtractionError, extract
from app.llm import LLMError

log = logging.getLogger("continuum.engine")


class NotFound(Exception):
    pass


class InvalidRequest(Exception):
    pass


@dataclass
class ChangeSet:
    source: Source | None  # None when the message changed nothing (then it isn't stored)
    created_goals: list[Goal] = field(default_factory=list)
    created_loops: list[OpenLoop] = field(default_factory=list)
    updated_loops: list[OpenLoop] = field(default_factory=list)
    resolved_loops: list[OpenLoop] = field(default_factory=list)
    events: list[EventCandidate] = field(default_factory=list)  # from an email, to propose for the calendar


@dataclass
class GoalSummary:
    goal: Goal
    open_loops: int


@dataclass
class LoopDetail:
    loop: OpenLoop
    goal: Goal | None
    source: Source


@dataclass
class Attention:
    loop: OpenLoop
    reason: str  # "overdue by 2 days", "due today", "waiting 5 days, no reply", ...


# --- process a message ---


def process_message(
    session: Session, llm: Completer, text: str, now: datetime | None = None, by: ActivityBy = ActivityBy.chat,
    person: Person | None = None, source: Source | None = None,
) -> ChangeSet:
    """Extract goals and loops from one message, then save them in one transaction.

    The LLM runs before any write, so a failed extraction writes nothing. A message that
    changes nothing isn't stored either (privacy: keep only what the system needs).
    `person` + `source`: an email from that person. It may only update or resolve their loops.
    """
    now = now or local_now()
    goals = [s.goal for s in list_goals(session)]
    open_loops = list_loops(session)
    in_scope = [loop for loop in open_loops if person is None or loop.person_id == person.id]
    goal_titles = {g.id: g.title for g in goals}
    people = {normalize(p.name): p for p in list_people(session)}
    try:
        result = extract(
            llm,
            text,
            now,
            goals=[{"title": g.title} for g in goals],
            loops=[_loop_context(loop, goal_titles) for loop in in_scope],
            note=EMAIL_NOTE.format(name=person.name) if person else None,
            people=[p.name for p in people.values()],
        )
    except Exception as e:
        log.warning("extraction_failed error=%s", type(e).__name__)
        raise

    known = {str(loop.id): loop for loop in in_scope}
    goal_by_name = {normalize(g.title): g for g in goals}
    seen = {_loop_key(loop.title, loop.waiting_on) for loop in open_loops}
    try:
        if source is None:
            source = add_source(session, text)
        else:
            session.add(source)
        link = source.url or ""  # an email's Gmail thread, shown as the change's source
        changes = ChangeSet(source=source, events=result.events if person else [])
        for candidate in result.goals:
            _find_or_add_goal(session, candidate.title, candidate.description, goal_by_name, changes)

        for candidate in result.new_loops:
            key = _loop_key(candidate.title, candidate.waiting_on)
            if key in seen:
                log.info("loop_duplicate_skipped")
                continue
            seen.add(key)
            goal = _find_or_add_goal(session, candidate.goal, None, goal_by_name, changes) if candidate.goal else None
            created = add_loop(
                session,
                source_id=source.id,
                goal_id=goal.id if goal else None,
                title=candidate.title,
                summary=candidate.summary,
                kind=LoopKind(candidate.kind),
                waiting_on=candidate.waiting_on,
                due=candidate.due,
                next_action=candidate.next_action,
            )
            if created.kind == LoopKind.waiting and normalize(created.waiting_on or ""):
                created.person_id = _find_or_add_person(session, created.waiting_on, people).id
            changes.created_loops.append(created)
            _log(session, created, ActivityAction.created, by, link)

        stamp = utcnow()
        for update in result.updated_loops:
            loop = known.get(update.id)
            fields = update.model_dump(exclude={"id"}, exclude_none=True)
            if loop is not None:  # the model often repeats current values; those aren't changes
                fields = {name: value for name, value in fields.items() if getattr(loop, name) != value}
            if loop is None or not fields:
                log.info("loop_update_dropped known=%s", loop is not None)
                continue
            undo = loop.model_dump(mode="json", include=set(fields))
            for name, value in fields.items():
                setattr(loop, name, value)
            loop.updated_at = stamp
            changes.updated_loops.append(loop)
            _log(session, loop, ActivityAction.updated, by, link or ", ".join(fields), undo)

        for loop_id in dict.fromkeys(result.resolved_loop_ids):  # each id once
            loop = known.get(loop_id)
            if loop is None:  # the LLM may only resolve loops we showed it
                log.info("loop_resolve_dropped")
                continue
            loop.status, loop.resolved_at, loop.updated_at = LoopStatus.resolved, stamp, stamp
            changes.resolved_loops.append(loop)
            _log(session, loop, ActivityAction.resolved, by, link, {"status": LoopStatus.open.value, "resolved_at": None})
        changes.updated_loops = [l for l in changes.updated_loops if l not in changes.resolved_loops]

        if not (changes.created_goals or changes.created_loops or changes.updated_loops or changes.resolved_loops):
            session.rollback()
            changes.source = None
            log.info("message_processed nothing_to_save")
            return changes
        session.commit()
    except Exception:
        session.rollback()
        raise
    log.info(
        "message_processed goals=%d created=%d updated=%d resolved=%d",
        len(changes.created_goals), len(changes.created_loops),
        len(changes.updated_loops), len(changes.resolved_loops),
    )
    for loop in changes.created_loops:
        log.info("loop_created id=%s", loop.id)
    for loop in changes.resolved_loops:
        log.info("loop_resolved id=%s", loop.id)
    return changes


def local_now() -> datetime:
    """Now in the user's timezone (TIMEZONE). Relative dates like "Friday" depend on it."""
    return datetime.now(ZoneInfo(settings.timezone))


def normalize(text: str) -> str:
    """Casefold, drop punctuation, collapse spaces. Used for dedup matching."""
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


def _loop_key(title: str, waiting_on: str | None) -> tuple[str, str]:
    return normalize(title), normalize(waiting_on or "")


def _loop_context(loop: OpenLoop, goal_titles: dict[UUID, str]) -> dict[str, Any]:
    return {
        "id": str(loop.id),
        "title": loop.title,
        "kind": loop.kind.value,
        "waiting_on": loop.waiting_on,
        "due": loop.due.isoformat() if loop.due else None,
        "goal": goal_titles.get(loop.goal_id) if loop.goal_id else None,
    }


def _find_or_add_goal(
    session: Session, title: str, description: str | None, goal_by_name: dict[str, Goal], changes: ChangeSet
) -> Goal:
    goal = goal_by_name.get(normalize(title))
    if goal is None:
        goal = add_goal(session, title, description)
        goal_by_name[normalize(title)] = goal
        changes.created_goals.append(goal)
    return goal


# --- create (no commit) ---


def add_source(session: Session, text: str, kind: str = "chat") -> Source:
    source = Source(text=text, kind=kind)
    session.add(source)
    return source


def add_goal(session: Session, title: str, description: str | None = None) -> Goal:
    goal = Goal(title=title, description=description)
    session.add(goal)
    return goal


def add_loop(
    session: Session,
    *,
    source_id: UUID,
    title: str,
    summary: str,
    kind: LoopKind,
    goal_id: UUID | None = None,
    waiting_on: str | None = None,
    due: date | None = None,
    next_action: str | None = None,
) -> OpenLoop:
    loop = OpenLoop(
        source_id=source_id,
        goal_id=goal_id,
        title=title,
        summary=summary,
        kind=kind,
        waiting_on=waiting_on,
        due=due,
        next_action=next_action,
    )
    session.add(loop)
    return loop


# --- read ---


def list_goals(session: Session) -> list[GoalSummary]:
    """Active goals with their open loop counts, newest first."""
    open_count = (
        select(func.count(col(OpenLoop.id)))
        .where(OpenLoop.goal_id == Goal.id, OpenLoop.status == LoopStatus.open)
        .scalar_subquery()
    )
    rows = session.exec(
        select(Goal, open_count)
        .where(Goal.status == GoalStatus.active)
        .order_by(col(Goal.created_at).desc())
    ).all()
    return [GoalSummary(goal=goal, open_loops=count) for goal, count in rows]


def list_loops(
    session: Session,
    status: LoopStatus | None = LoopStatus.open,
    goal_id: UUID | None = None,
    include_snoozed: bool = True,
    today: date | None = None,
) -> list[OpenLoop]:
    """Loops sorted by due date (undated last), then oldest first. status=None means all.
    include_snoozed=False hides loops snoozed past `today` (default: the user's today).
    Extraction keeps snoozed loops, so chat can still update or resolve them."""
    query = select(OpenLoop)
    if status is not None:
        query = query.where(OpenLoop.status == status)
    if goal_id is not None:
        query = query.where(OpenLoop.goal_id == goal_id)
    if not include_snoozed:
        today = today or local_now().date()
        query = query.where(or_(col(OpenLoop.snoozed_until).is_(None), col(OpenLoop.snoozed_until) <= today))
    query = query.order_by(
        col(OpenLoop.due).is_(None), col(OpenLoop.due), col(OpenLoop.created_at)
    )
    return list(session.exec(query).all())


def needs_attention(session: Session, today: date, stale_days: int | None = None) -> list[Attention]:
    """Open, unsnoozed loops that need the user now. Plain rules, no LLM. `today` is the user's local date.

    Order: loops whose latest message check found risk (oldest first), overdue (most overdue first),
    due today/tomorrow, then undated loops with no update for at least `stale_days` days (longest first).
    """
    stale_days = settings.stale_days if stale_days is None else stale_days
    tz = ZoneInfo(settings.timezone)
    loops = list_loops(session, include_snoozed=False, today=today)
    risk = check_levels(session, [loop.id for loop in loops])
    found: list[tuple[int, Any, Attention]] = []  # (rule, sort key, item)
    for loop in loops:
        if risk.get(loop.id) in RISKY:
            found.append((0, loop.created_at, Attention(loop, risk_reason(risk[loop.id]))))
            continue
        if loop.due is not None:
            late = (today - loop.due).days
            if late > 0:
                found.append((1, loop.due, Attention(loop, f"overdue by {_days(late)}")))
            elif late >= -1:
                found.append((2, loop.due, Attention(loop, "due today" if late == 0 else "due tomorrow")))
            continue
        idle = (today - loop.updated_at.astimezone(tz).date()).days
        if idle >= stale_days:
            reason = f"waiting {_days(idle)}, no reply" if loop.kind == LoopKind.waiting else f"no update in {_days(idle)}"
            found.append((3, loop.updated_at, Attention(loop, reason)))
    found.sort(key=lambda f: (f[0], f[1]))  # stable: ties keep list_loops order
    return [item for _, _, item in found]


def _days(n: int) -> str:
    return "1 day" if n == 1 else f"{n} days"


# --- message checks (ScamGraph, app/investigation.py) on loops ---

# Levels that keep a loop at the top of Needs attention. Not enough evidence counts: the sender isn't verified.
RISKY = (RiskLevel.elevated, RiskLevel.high, RiskLevel.critical, RiskLevel.insufficient)


def risk_reason(level: RiskLevel) -> str:
    if level == RiskLevel.insufficient:
        return "sender not verified, hold off paying"
    return f"{level.value.lower()} risk, hold off paying"


def check_levels(session: Session, loop_ids: list[UUID]) -> dict[UUID, RiskLevel]:
    """Each loop's latest finished check. A newer check replaces an older one's level."""
    query = (
        select(Investigation.loop_id, Investigation.risk_level)
        .where(col(Investigation.loop_id).in_(loop_ids), col(Investigation.risk_level).is_not(None))
        .order_by(col(Investigation.created_at))
    )
    return {loop_id: level for loop_id, level in session.exec(query).all()}  # type: ignore[misc]


def track_check(session: Session, title: str, summary: str, next_action: str | None, text: str, link: str) -> OpenLoop:
    """The loop a risky check leaves, so it stays in Needs attention and the briefing until the user closes it
    (no commit). An open loop with the same title is reused: checking a message twice doesn't add a second one."""
    key = normalize(title)
    for loop in list_loops(session):
        if normalize(loop.title) == key:
            return loop
    source = add_source(session, text, kind="investigation")
    source.url = link
    loop = add_loop(session, source_id=source.id, title=title, summary=summary, kind=LoopKind.task, next_action=next_action)
    _log(session, loop, ActivityAction.created, ActivityBy.check)
    log.info("loop_created id=%s by=check", loop.id)
    return loop


def get_loop(session: Session, loop_id: UUID) -> LoopDetail:
    loop = _get(session, loop_id)
    source = session.get(Source, loop.source_id)
    if source is None:  # source_id is required; missing means a corrupt DB
        raise RuntimeError(f"Loop {loop_id} points to missing source {loop.source_id}")
    goal = session.get(Goal, loop.goal_id) if loop.goal_id else None
    return LoopDetail(loop=loop, goal=goal, source=source)


# --- user actions (commit) ---


def resolve_loop(session: Session, loop_id: UUID, by: ActivityBy = ActivityBy.user) -> OpenLoop:
    """Resolving a resolved loop changes nothing."""
    loop = _get(session, loop_id)
    if loop.status == LoopStatus.resolved:
        return loop
    undo = loop.model_dump(mode="json", include={"status", "resolved_at"})
    now = utcnow()
    loop.status = LoopStatus.resolved
    loop.resolved_at = now
    loop.updated_at = now
    _log(session, loop, ActivityAction.resolved, by, undo=undo)
    session.commit()
    session.refresh(loop)
    log.info("loop_resolved id=%s", loop_id)
    return loop


def reopen_loop(session: Session, loop_id: UUID) -> OpenLoop:
    """Reopen a loop. If its goal was marked done, the goal becomes active again."""
    loop = _get(session, loop_id)
    if loop.status == LoopStatus.open:
        return loop
    loop.status = LoopStatus.open
    loop.resolved_at = None
    loop.updated_at = utcnow()
    _reactivate_goal(session, loop)
    _log(session, loop, ActivityAction.reopened, ActivityBy.user)
    session.commit()
    session.refresh(loop)
    log.info("loop_reopened id=%s", loop_id)
    return loop


def snooze_loop(
    session: Session, loop_id: UUID, until: date, today: date | None = None, by: ActivityBy = ActivityBy.user
) -> OpenLoop:
    """Hide a loop from lists and attention until `until`. It shows again on that date."""
    if until <= (today or local_now().date()):
        raise InvalidRequest("Snooze date must be after today.")
    loop = _get(session, loop_id)
    undo = loop.model_dump(mode="json", include={"snoozed_until"})
    loop.snoozed_until, loop.updated_at = until, utcnow()
    _log(session, loop, ActivityAction.updated, by, f"snoozed until {until.isoformat()}", undo)
    session.commit()
    session.refresh(loop)
    log.info("loop_snoozed id=%s", loop_id)
    return loop


def unsnooze_loop(session: Session, loop_id: UUID) -> OpenLoop:
    loop = _get(session, loop_id)
    loop.snoozed_until, loop.updated_at = None, utcnow()
    _log(session, loop, ActivityAction.updated, ActivityBy.user, "unsnoozed")
    session.commit()
    session.refresh(loop)
    log.info("loop_unsnoozed id=%s", loop_id)
    return loop


def complete_goal(session: Session, goal_id: UUID) -> Goal:
    """Mark a goal done (achieved or dropped) and resolve its open loops.
    Done goals aren't sent to the LLM anymore. Reopening one of its loops makes it active again."""
    goal = session.get(Goal, goal_id)
    if goal is None:
        raise NotFound(f"Goal {goal_id} not found")
    now = utcnow()
    goal.status, goal.updated_at = GoalStatus.done, now
    for loop in list_loops(session, goal_id=goal_id):
        loop.status, loop.resolved_at, loop.updated_at = LoopStatus.resolved, now, now
        _log(session, loop, ActivityAction.resolved, ActivityBy.user, "goal done")
    session.commit()
    session.refresh(goal)
    log.info("goal_completed id=%s", goal_id)
    return goal


def delete_loop(session: Session, loop_id: UUID) -> None:
    """Delete a wrong loop with its history and proposals. Its source stays, since other loops may share it."""
    loop = _get(session, loop_id)
    session.exec(delete(Activity).where(col(Activity.loop_id) == loop_id))
    session.exec(delete(PendingAction).where(col(PendingAction.loop_id) == loop_id))
    session.delete(loop)
    session.commit()
    log.info("loop_deleted id=%s", loop_id)


def _get(session: Session, loop_id: UUID) -> OpenLoop:
    loop = session.get(OpenLoop, loop_id)
    if loop is None:
        raise NotFound(f"Loop {loop_id} not found")
    return loop


def _reactivate_goal(session: Session, loop: OpenLoop) -> None:
    """An open loop's goal must be active, or the LLM no longer sees it (no commit)."""
    goal = session.get(Goal, loop.goal_id) if loop.goal_id else None
    if goal and goal.status == GoalStatus.done:
        goal.status, goal.updated_at = GoalStatus.active, loop.updated_at
        log.info("goal_reopened id=%s", goal.id)


# --- activity: what changed a loop, and undo ---


def _log(
    session: Session, loop: OpenLoop, action: ActivityAction, by: ActivityBy,
    detail: str = "", undo: dict[str, Any] | None = None,
) -> None:
    """Record a change (no commit). User changes get no undo: the dashboard has Reopen/Unsnooze for them."""
    session.add(Activity(loop_id=loop.id, action=action, by=by, detail=detail,
                         undo=None if by == ActivityBy.user else undo))


def _restore(loop: OpenLoop, values: dict[str, Any]) -> None:
    """Set fields from JSON values (e.g. "2026-10-02" back to a date)."""
    for name, value in values.items():
        setattr(loop, name, TypeAdapter(OpenLoop.model_fields[name].annotation).validate_python(value))


def list_activity(session: Session, loop_id: UUID | None = None) -> list[Activity]:
    """Newest first, at most 100."""
    query = select(Activity).order_by(col(Activity.created_at).desc()).limit(100)
    if loop_id is not None:
        query = query.where(Activity.loop_id == loop_id)
    return list(session.exec(query).all())


def undo_activity(session: Session, activity_id: UUID) -> OpenLoop:
    """Put a loop back the way it was before an automatic change. Each change can be undone once."""
    activity = session.get(Activity, activity_id)
    if activity is None:
        raise NotFound(f"Change {activity_id} not found")
    if activity.undo is None or activity.loop_id is None:
        raise InvalidRequest("This change can't be undone.")
    loop = _get(session, activity.loop_id)
    reopens = activity.undo.get("status") == LoopStatus.open and loop.status == LoopStatus.resolved
    _restore(loop, activity.undo)
    loop.updated_at = utcnow()
    if reopens:
        _reactivate_goal(session, loop)
    activity.undo = None
    _log(session, loop, ActivityAction.reopened if reopens else ActivityAction.updated, ActivityBy.user,
         f"undid {activity.action.value} by {activity.by.value}")
    session.commit()
    session.refresh(loop)
    log.info("activity_undone id=%s", activity_id)
    return loop


# --- proposals: run only after the user's yes ---

WEB_URL = r"^https?://\S+$"
EMAIL = r"[^@\s]+@[^@\s]+\.[^@\s]+"
GOOGLE_NOT_CONNECTED = "Google isn't connected. Connect it in the dashboard (Settings)."


class LoopUpdate(BaseModel):
    """A change found on the web. Applied only when the user approves it."""

    resolve: bool = False
    due: date | None = None
    source_url: str = Field(pattern=WEB_URL)  # shown as a link, so web pages only
    source_title: str | None = None

    @model_validator(mode="after")
    def _changes_something(self) -> "LoopUpdate":
        if not self.resolve and self.due is None:
            raise ValueError("A loop update needs resolve or a new due date.")
        return self


def propose_loop_update(session: Session, loop_id: UUID, change: LoopUpdate) -> PendingAction:
    """Save a proposal. If the same change already waits for an answer, return that one instead."""
    if _get(session, loop_id).status != LoopStatus.open:
        raise InvalidRequest("That loop is already resolved.")
    payload = change.model_dump(mode="json")
    for pending in list_actions(session):
        if (pending.loop_id, pending.kind) == (loop_id, ActionKind.loop_update) and (
            (pending.payload["resolve"], pending.payload["due"]) == (payload["resolve"], payload["due"])
        ):
            return pending
    return _add_action(session, loop_id, ActionKind.loop_update, payload)


class CalendarEvent(BaseModel):
    """Times are in the user's TIMEZONE. No end = one hour."""

    title: str = Field(min_length=1, max_length=200)
    start: datetime
    end: datetime | None = None

    @field_validator("start", "end")
    @classmethod
    def _local(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(ZoneInfo(settings.timezone)).replace(tzinfo=None)
        return value

    @model_validator(mode="after")
    def _ends_after_start(self) -> "CalendarEvent":
        if self.end is not None and self.end <= self.start:
            raise ValueError("The event must end after it starts.")
        return self


class GmailDraft(BaseModel):
    """Saved in Gmail's Drafts for the user to send. Continuum never sends it."""

    to: str = Field(pattern=f"^{EMAIL}$")
    subject: str = Field(min_length=1, max_length=300, pattern=r"^[^\r\n]*$")
    body: str = Field(min_length=1, max_length=20000)
    thread_id: str | None = None  # reply in this Gmail thread


def propose_google(session: Session, loop_id: UUID, change: CalendarEvent | GmailDraft) -> PendingAction:
    """Propose a calendar event or a Gmail draft for a loop (open or resolved: a thank-you often follows
    a closed loop). A draft to the loop's person replies in their latest email thread."""
    if not google.connected(settings):
        raise InvalidRequest(GOOGLE_NOT_CONNECTED)
    loop = _get(session, loop_id)
    if isinstance(change, GmailDraft) and change.thread_id is None:
        person = session.get(Person, loop.person_id) if loop.person_id else None
        if person and person.email == change.to.lower():
            change = change.model_copy(update={"thread_id": email_thread(session, loop)})
    kind = ActionKind.calendar_event if isinstance(change, CalendarEvent) else ActionKind.gmail_draft
    payload = change.model_dump(mode="json")
    for pending in list_actions(session):
        if (pending.loop_id, pending.kind, pending.payload) == (loop_id, kind, payload):
            return pending
    return _add_action(session, loop_id, kind, payload)


def _add_action(session: Session, loop_id: UUID, kind: ActionKind, payload: dict[str, Any]) -> PendingAction:
    action = PendingAction(loop_id=loop_id, kind=kind, payload=payload)
    session.add(action)
    session.commit()
    session.refresh(action)
    log.info("action_proposed id=%s kind=%s", action.id, action.kind.value)
    return action


def list_actions(session: Session, status: ActionStatus | None = ActionStatus.proposed) -> list[PendingAction]:
    """Oldest first. status=None means all."""
    query = select(PendingAction).order_by(col(PendingAction.created_at))
    if status is not None:
        query = query.where(PendingAction.status == status)
    return list(session.exec(query).all())


def describe_action(session: Session, action: PendingAction) -> str:
    """One line for the dashboard, chat and Telegram."""
    loop = session.get(OpenLoop, action.loop_id) if action.loop_id else None
    title = f'"{loop.title}"' if loop else "a deleted loop"
    if action.kind == ActionKind.loop_update:
        change = LoopUpdate.model_validate(action.payload)
        what = f"Resolve {title}" if change.resolve else f"Set {title} due {change.due:%a %b} {change.due.day}"
        site = urlparse(change.source_url).netloc.removeprefix("www.")
        page = f"{change.source_title} ({site})" if change.source_title else change.source_url
        return f"{what}? Found on the web: {page}"
    if action.kind == ActionKind.calendar_event:
        event = CalendarEvent.model_validate(action.payload)
        return f'Add "{event.title}" to your calendar, {event.start:%a %b} {event.start.day} {event.start:%H:%M}?'
    draft = GmailDraft.model_validate(action.payload)
    return f'Save a Gmail draft to {draft.to}: "{draft.subject}"? It won\'t be sent.'


def approve_action(
    session: Session, action_id: UUID, get_google: Callable[[], GoogleClient] | None = None
) -> PendingAction:
    """Run a proposed action once. Only call this after the user said yes to it.
    Calendar events and drafts use `get_google` (default: the saved Google token)."""
    action = _decide(session, action_id, ActionStatus.done)
    session.commit()  # claimed first, so a second approve fails even while Google is slow
    try:
        if action.kind == ActionKind.loop_update:
            _apply_loop_update(session, action)
        else:
            _run_google(session, action, (get_google or (lambda: GoogleClient.from_settings(settings)))())
        session.commit()
    except Exception as e:
        session.rollback()
        action.status, action.updated_at = ActionStatus.failed, utcnow()
        session.commit()
        log.warning("action_failed id=%s error=%s", action_id, type(e).__name__)
        raise InvalidRequest(f"Couldn't do it: {e}") from e
    session.refresh(action)
    log.info("action_done id=%s", action_id)
    return action


def reject_action(session: Session, action_id: UUID) -> PendingAction:
    action = _decide(session, action_id, ActionStatus.rejected)
    session.commit()
    session.refresh(action)
    log.info("action_rejected id=%s", action_id)
    return action


def _decide(session: Session, action_id: UUID, status: ActionStatus) -> PendingAction:
    """Move a proposal out of "proposed" (no commit). The conditional UPDATE lets only one of two
    racing requests (a double click, dashboard + Telegram) through."""
    action = _get_action(session, action_id)
    claimed = session.exec(
        update(PendingAction)
        .where(col(PendingAction.id) == action_id, col(PendingAction.status) == ActionStatus.proposed)
        .values(status=status, updated_at=utcnow())
        .execution_options(synchronize_session=False)
    ).rowcount
    session.refresh(action)
    if not claimed:
        raise InvalidRequest(f"This was already {action.status.value}.")
    return action


def _apply_loop_update(session: Session, action: PendingAction) -> None:
    change = LoopUpdate.model_validate(action.payload)
    loop = _get(session, action.loop_id)  # type: ignore[arg-type]  # a loop_update always has a loop
    if loop.status != LoopStatus.open:
        raise InvalidRequest("That loop is already resolved.")
    # Only what this change touches, so undoing a due date can't reopen a loop resolved since.
    touched = ({"status", "resolved_at"} if change.resolve else set()) | ({"due"} if change.due else set())
    undo = loop.model_dump(mode="json", include=touched)
    now = utcnow()
    if change.due:
        loop.due = change.due
    if change.resolve:
        loop.status, loop.resolved_at = LoopStatus.resolved, now
    loop.updated_at = now
    _log(session, loop, ActivityAction.resolved if change.resolve else ActivityAction.updated,
         ActivityBy.web, change.source_url, undo)


def _run_google(session: Session, action: PendingAction, client: GoogleClient) -> None:
    if action.kind == ActionKind.calendar_event:
        event = CalendarEvent.model_validate(action.payload)
        created = client.create_event(event.title, event.start, event.end)
        action.external_id, detail = created.id, created.link or f'calendar event "{event.title}"'
    else:
        draft = GmailDraft.model_validate(action.payload)
        action.external_id = client.create_draft(draft.to, draft.subject, draft.body, draft.thread_id)
        detail = "Gmail draft saved, not sent"
    loop = session.get(OpenLoop, action.loop_id) if action.loop_id else None
    if loop is not None:
        _log(session, loop, ActivityAction.action_done, ActivityBy.user, detail)


def _get_action(session: Session, action_id: UUID) -> PendingAction:
    action = session.get(PendingAction, action_id)
    if action is None:
        raise NotFound(f"Action {action_id} not found")
    return action


# --- web watch: findings only become proposals ---


def web_enabled() -> bool:
    return bool(settings.tavily_api_key.get_secret_value())


def watch_loop(session: Session, loop_id: UUID, query: str | None, by: ActivityBy = ActivityBy.user) -> OpenLoop:
    """Search the web for news about one open loop once a day, or stop (empty query).
    Only open loops are searched, so resolving a loop stops its watch (reopening resumes it)."""
    loop = _get(session, loop_id)
    query = " ".join((query or "").split()) or None
    if query is not None:
        if not web_enabled():
            raise InvalidRequest(NOT_SET_UP)
        if loop.status != LoopStatus.open:
            raise InvalidRequest("That loop is already resolved.")
        if len(query) > 400:
            raise InvalidRequest("Keep the search under 400 characters.")
    if query == loop.watch_query:
        return loop
    undo = loop.model_dump(mode="json", include={"watch_query", "watch_checked_on"})
    loop.watch_query, loop.watch_checked_on, loop.updated_at = query, None, utcnow()
    _log(session, loop, ActivityAction.updated, by, f"watching the web: {query}" if query else "stopped watching the web", undo)
    session.commit()
    session.refresh(loop)
    log.info("loop_watch id=%s on=%s", loop_id, query is not None)
    return loop


def watch_the_web(
    session: Session, llm: Completer, search: Callable[[str], list[WebResult]], now: datetime | None = None
) -> tuple[list[PendingAction], list[str]]:
    """Search each watched open loop at most once a day. Returns the new proposals, and one line per
    loop whose search failed (it's tried again tomorrow)."""
    now = now or local_now()
    before = {a.id for a in list_actions(session)}
    errors: list[str] = []
    for loop in list_loops(session):
        if loop.watch_query is None or (loop.watch_checked_on or date.min) >= now.date():
            continue
        loop.watch_checked_on = now.date()  # one search a day, even if it fails
        session.commit()
        try:
            results = search(loop.watch_query)
        except TavilyError as e:
            log.warning("web_watch_failed id=%s error=%s", loop.id, e)
            errors.append(f"Couldn't check the web for \"{loop.title}\": {e}")
            continue
        log.info("web_watch_searched id=%s results=%d", loop.id, len(results))
        _review_web_results(session, llm, loop, results, now)
    return [a for a in list_actions(session) if a.id not in before], errors


def _review_web_results(session: Session, llm: Completer, loop: OpenLoop, results: list[WebResult], now: datetime) -> None:
    """Each new result goes through extraction alone, so a finding keeps its page. Only this loop's
    resolve or due date count; anything else the model returns is dropped."""
    # ponytail: a page counts as seen once it made a proposal, so pages with no finding are re-read
    # (one LLM call each) while they stay in the week's results. Store seen URLs if that costs too much.
    seen = {a.payload.get("source_url") for a in list_actions(session, None) if a.loop_id == loop.id}
    goal = session.get(Goal, loop.goal_id) if loop.goal_id else None
    context = [_loop_context(loop, {goal.id: goal.title} if goal else {})]
    for page in results:
        if page.url in seen or not re.match(WEB_URL, page.url):
            continue
        seen.add(page.url)
        try:
            found = extract(llm, f"{page.title}\n{page.url}\n\n{page.content}", now, goals=[], loops=context, note=WEB_NOTE)
        except (ExtractionError, LLMError) as e:
            log.warning("web_result_unreadable id=%s error=%s", loop.id, type(e).__name__)
            continue
        due = next((u.due for u in found.updated_loops if u.id == str(loop.id) and u.due and u.due != loop.due), None)
        if str(loop.id) in found.resolved_loop_ids:
            change = LoopUpdate(resolve=True, source_url=page.url, source_title=page.title or None)
        elif due:
            change = LoopUpdate(due=due, source_url=page.url, source_title=page.title or None)
        else:
            continue
        propose_loop_update(session, loop.id, change)


# --- people: who the user waits on ---


def list_people(session: Session) -> list[Person]:
    return list(session.exec(select(Person).order_by(col(Person.name))).all())


def get_person(session: Session, person_id: UUID) -> Person:
    person = session.get(Person, person_id)
    if person is None:
        raise NotFound(f"Person {person_id} not found")
    return person


def person_loops(session: Session, person_id: UUID) -> list[OpenLoop]:
    """Open loops waiting on this person."""
    return [loop for loop in list_loops(session) if loop.person_id == person_id]


def set_person_email(session: Session, name: str, email: str | None) -> Person:
    """Save the email the user gave for someone (empty clears it). Creates the person if new, and links
    open waiting loops on that name that have no person yet. Emails are never guessed."""
    name = " ".join(name.split())
    if not normalize(name):
        raise InvalidRequest("Say whose email this is.")
    email = (email or "").strip().lower() or None
    if email and not re.fullmatch(EMAIL, email):
        raise InvalidRequest(f"{email} isn't an email address.")
    person = _find_or_add_person(session, name, {normalize(p.name): p for p in list_people(session)})
    person.email = email
    for loop in list_loops(session):
        if loop.person_id is None and loop.kind == LoopKind.waiting and normalize(loop.waiting_on or "") == normalize(name):
            loop.person_id = person.id
    session.commit()
    session.refresh(person)
    log.info("person_email_set id=%s has_email=%s", person.id, email is not None)
    return person


def _find_or_add_person(session: Session, name: str, people: dict[str, Person]) -> Person:
    """Match by name, ignoring case and punctuation (no commit)."""
    key = normalize(name)
    if key not in people:
        people[key] = Person(name=name)
        session.add(people[key])
    return people[key]


# --- email: replies from people linked to open loops ---

LAST_EMAIL_SYNC = "email_last_sync"  # Setting keys
LAST_SYNC_ERROR = "email_sync_error"


class Mailbox(Protocol):
    def messages_from(self, sender: str, after: datetime | None = None, limit: int = 5) -> list[Email]: ...


def get_setting(session: Session, key: str) -> str | None:
    row = session.get(Setting, key)
    return row.value if row else None


def _set_setting(session: Session, key: str, value: str | None) -> None:
    """No commit. None deletes the key."""
    row = session.get(Setting, key)
    if value is None:
        if row:
            session.delete(row)
    elif row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))


def sync_email(
    session: Session, llm: Completer, mailbox: Mailbox, now: datetime | None = None
) -> tuple[list[str], list[str]]:
    """Read new mail from people who have an email and an open loop, nobody else. Returns one line per
    email that changed something, and error lines (each error is reported once, not every run)."""
    now = now or local_now()
    started = now.astimezone(UTC)
    last = get_setting(session, LAST_EMAIL_SYNC)
    after = datetime.fromisoformat(last) if last else started - timedelta(days=settings.sync_lookback_days)
    people = [p for p in list_people(session) if p.email and person_loops(session, p.id)]
    try:  # fetch everything first: a Google error writes nothing
        # ponytail: newest 10 per person per run; raise the limit if someone sends more than that in 10 minutes
        inbox = [(p, e) for p in people for e in mailbox.messages_from(p.email, after=after, limit=10)]  # type: ignore[arg-type]
    except GoogleError as e:
        log.warning("email_sync_failed error=%s", type(e).__name__)
        return [], report_sync_errors(session, [str(e)])
    lines, errors = [], []
    for person, email in sorted(inbox, key=lambda pe: pe[1].received):  # oldest first, so a later email wins
        if session.exec(select(Source).where(Source.external_id == email.id)).first():
            continue
        try:
            changes = ingest_email(session, llm, person, email, now)
        except (ExtractionError, LLMError) as e:
            log.warning("email_unreadable id=%s error=%s", email.id, type(e).__name__)
            errors.append(f"Couldn't read an email from {person.name}: {e}")
            continue
        if changes.source is not None:
            lines.append(describe_email(person, changes))
        lines += [f"{describe_action(session, a)} (yes/no)" for a in _propose_events(session, person, changes, now)]
    if not errors:  # otherwise the same window is read again next run
        _set_setting(session, LAST_EMAIL_SYNC, started.isoformat())
    log.info("email_synced people=%d messages=%d changed=%d", len(people), len(inbox), len(lines))
    return lines, report_sync_errors(session, errors)


def ingest_email(session: Session, llm: Completer, person: Person, email: Email, now: datetime | None = None) -> ChangeSet:
    """One email from a linked person. It may close or update only their loops, and add new ones.
    Every change is logged as by "email" with undo, linked to the Gmail thread."""
    source = Source(kind="email", text=email.text, external_id=email.id, url=email.url, sender=person.name)
    return process_message(session, llm, email.text, now, ActivityBy.email, person=person, source=source)


def _propose_events(session: Session, person: Person, changes: ChangeSet, now: datetime) -> list[PendingAction]:
    """Each future event in the email becomes a calendar proposal on the loop the email was about."""
    loops = changes.created_loops + changes.updated_loops + changes.resolved_loops + person_loops(session, person.id)
    proposed = []
    for candidate in changes.events if loops else []:
        try:
            event = CalendarEvent(title=candidate.title, start=candidate.start, end=candidate.end)
            if event.start <= now.astimezone(ZoneInfo(settings.timezone)).replace(tzinfo=None):
                continue  # already past
            proposed.append(propose_google(session, loops[0].id, event))
        except (ValueError, InvalidRequest) as e:  # pydantic's ValidationError is a ValueError
            log.info("event_not_proposed error=%s", type(e).__name__)
    return proposed


def describe_email(person: Person, changes: ChangeSet) -> str:
    """One line for Telegram, e.g. 'Email from Sarah: closed "Wait for Sarah's response"; new "Prepare for interview" (due Thu Oct 8).'"""
    def due(loop: OpenLoop) -> str:
        return f" (due {loop.due:%a %b} {loop.due.day})" if loop.due else ""
    parts = [f'closed "{l.title}"' for l in changes.resolved_loops]
    parts += [f'updated "{l.title}"{due(l)}' for l in changes.updated_loops]
    parts += [f'new "{l.title}"{due(l)}' for l in changes.created_loops]
    parts += [f'new goal "{g.title}"' for g in changes.created_goals]
    return f"Email from {person.name}: {'; '.join(parts)}."


def report_sync_errors(session: Session, errors: list[str]) -> list[str]:
    """Sync runs every 10 minutes: repeat an error only when it changes (commits)."""
    text = "\n".join(errors) or None
    repeat = text == get_setting(session, LAST_SYNC_ERROR)
    _set_setting(session, LAST_SYNC_ERROR, text)
    session.commit()
    return [] if repeat else errors


def email_thread(session: Session, loop: OpenLoop) -> str | None:
    """The Gmail thread id of the latest email about this loop, or None."""
    urls = [a.detail for a in list_activity(session, loop.id) if a.by == ActivityBy.email and a.detail]
    source = session.get(Source, loop.source_id)
    if source is not None and source.url:
        urls.append(source.url)
    return urls[0].rsplit("/", 1)[-1] if urls else None  # urls end in /<thread id> (Email.url)


def resolved_by(session: Session, loop: OpenLoop) -> ActivityBy | None:
    """Who closed a resolved loop, for the "closed by email" tag."""
    if loop.status != LoopStatus.resolved:
        return None
    last = session.exec(
        select(Activity).where(Activity.loop_id == loop.id, Activity.action == ActivityAction.resolved)
        .order_by(col(Activity.created_at).desc())
    ).first()
    return last.by if last else None


def forget_email_data(session: Session) -> int:
    """Delete the text, sender and links of every email Continuum read. Loops made from one stay, with
    "source deleted". Gmail message ids stay, so sync never reads those emails again. Returns the count."""
    sources = session.exec(select(Source).where(Source.kind == "email")).all()
    for source in sources:
        source.text, source.sender, source.url = "", None, None
    for activity in session.exec(select(Activity).where(Activity.by == ActivityBy.email)).all():
        activity.detail = ""
    session.commit()
    log.info("email_data_forgotten sources=%d", len(sources))
    return len(sources)
