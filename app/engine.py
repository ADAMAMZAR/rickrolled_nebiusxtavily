"""Continuity engine: all domain logic lives here.

`add_*` helpers don't commit, so callers can group them into one transaction.
User actions (resolve, reopen, delete) commit themselves.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlmodel import Session, col, func, or_, select

from app.config import settings
from app.db import Goal, GoalStatus, LoopKind, LoopStatus, OpenLoop, Source, utcnow
from app.extraction import Completer, extract

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


def process_message(session: Session, llm: Completer, text: str, now: datetime | None = None) -> ChangeSet:
    """Extract goals and loops from one message, then save them in one transaction.

    The LLM runs before any write, so a failed extraction writes nothing. A message that
    changes nothing isn't stored either (privacy: keep only what the system needs).
    """
    now = now or local_now()
    goals = [s.goal for s in list_goals(session)]
    open_loops = list_loops(session)
    goal_titles = {g.id: g.title for g in goals}
    try:
        result = extract(
            llm,
            text,
            now,
            goals=[{"title": g.title} for g in goals],
            loops=[_loop_context(loop, goal_titles) for loop in open_loops],
        )
    except Exception as e:
        log.warning("extraction_failed error=%s", type(e).__name__)
        raise

    known = {str(loop.id): loop for loop in open_loops}
    goal_by_name = {normalize(g.title): g for g in goals}
    seen = {_loop_key(loop.title, loop.waiting_on) for loop in open_loops}
    try:
        source = add_source(session, text)
        changes = ChangeSet(source=source)
        for candidate in result.goals:
            _find_or_add_goal(session, candidate.title, candidate.description, goal_by_name, changes)

        for candidate in result.new_loops:
            key = _loop_key(candidate.title, candidate.waiting_on)
            if key in seen:
                log.info("loop_duplicate_skipped")
                continue
            seen.add(key)
            goal = _find_or_add_goal(session, candidate.goal, None, goal_by_name, changes) if candidate.goal else None
            changes.created_loops.append(
                add_loop(
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
            )

        stamp = utcnow()
        for update in result.updated_loops:
            loop = known.get(update.id)
            fields = update.model_dump(exclude={"id"}, exclude_none=True)
            if loop is None or not fields:
                log.info("loop_update_dropped known=%s", loop is not None)
                continue
            for name, value in fields.items():
                setattr(loop, name, value)
            loop.updated_at = stamp
            changes.updated_loops.append(loop)

        for loop_id in result.resolved_loop_ids:
            loop = known.get(loop_id)
            if loop is None:  # the LLM may only resolve loops we showed it
                log.info("loop_resolve_dropped")
                continue
            loop.status, loop.resolved_at, loop.updated_at = LoopStatus.resolved, stamp, stamp
            changes.resolved_loops.append(loop)
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

    Order: overdue (most overdue first), due today/tomorrow, then undated loops with no update
    for at least `stale_days` days (longest first).
    """
    stale_days = settings.stale_days if stale_days is None else stale_days
    tz = ZoneInfo(settings.timezone)
    found: list[tuple[int, Any, Attention]] = []  # (rule, sort key, item)
    for loop in list_loops(session, include_snoozed=False, today=today):
        if loop.due is not None:
            late = (today - loop.due).days
            if late > 0:
                found.append((0, loop.due, Attention(loop, f"overdue by {_days(late)}")))
            elif late >= -1:
                found.append((1, loop.due, Attention(loop, "due today" if late == 0 else "due tomorrow")))
            continue
        idle = (today - loop.updated_at.astimezone(tz).date()).days
        if idle >= stale_days:
            reason = f"waiting {_days(idle)}, no reply" if loop.kind == LoopKind.waiting else f"no update in {_days(idle)}"
            found.append((2, loop.updated_at, Attention(loop, reason)))
    found.sort(key=lambda f: (f[0], f[1]))  # stable: ties keep list_loops order
    return [item for _, _, item in found]


def _days(n: int) -> str:
    return "1 day" if n == 1 else f"{n} days"


def get_loop(session: Session, loop_id: UUID) -> LoopDetail:
    loop = _get(session, loop_id)
    source = session.get(Source, loop.source_id)
    if source is None:  # source_id is required; missing means a corrupt DB
        raise RuntimeError(f"Loop {loop_id} points to missing source {loop.source_id}")
    goal = session.get(Goal, loop.goal_id) if loop.goal_id else None
    return LoopDetail(loop=loop, goal=goal, source=source)


# --- user actions (commit) ---


def resolve_loop(session: Session, loop_id: UUID) -> OpenLoop:
    loop = _get(session, loop_id)
    now = utcnow()
    loop.status = LoopStatus.resolved
    loop.resolved_at = now
    loop.updated_at = now
    session.commit()
    session.refresh(loop)
    log.info("loop_resolved id=%s", loop_id)
    return loop


def reopen_loop(session: Session, loop_id: UUID) -> OpenLoop:
    """Reopen a loop. If its goal was marked done, the goal becomes active again."""
    loop = _get(session, loop_id)
    loop.status = LoopStatus.open
    loop.resolved_at = None
    loop.updated_at = utcnow()
    goal = session.get(Goal, loop.goal_id) if loop.goal_id else None
    if goal and goal.status == GoalStatus.done:
        goal.status, goal.updated_at = GoalStatus.active, loop.updated_at
        log.info("goal_reopened id=%s", goal.id)
    session.commit()
    session.refresh(loop)
    log.info("loop_reopened id=%s", loop_id)
    return loop


def snooze_loop(session: Session, loop_id: UUID, until: date, today: date | None = None) -> OpenLoop:
    """Hide a loop from lists and attention until `until`. It shows again on that date."""
    if until <= (today or local_now().date()):
        raise InvalidRequest("Snooze date must be after today.")
    loop = _get(session, loop_id)
    loop.snoozed_until, loop.updated_at = until, utcnow()
    session.commit()
    session.refresh(loop)
    log.info("loop_snoozed id=%s", loop_id)
    return loop


def unsnooze_loop(session: Session, loop_id: UUID) -> OpenLoop:
    loop = _get(session, loop_id)
    loop.snoozed_until, loop.updated_at = None, utcnow()
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
    session.commit()
    session.refresh(goal)
    log.info("goal_completed id=%s", goal_id)
    return goal


def delete_loop(session: Session, loop_id: UUID) -> None:
    """Delete a wrong loop. Its source stays, since other loops may share it."""
    session.delete(_get(session, loop_id))
    session.commit()
    log.info("loop_deleted id=%s", loop_id)


def _get(session: Session, loop_id: UUID) -> OpenLoop:
    loop = session.get(OpenLoop, loop_id)
    if loop is None:
        raise NotFound(f"Loop {loop_id} not found")
    return loop
