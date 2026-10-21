"""How often you correct the loops Troth makes (troth_eval.md §5). Of the loops made from chat or email at least
48 hours ago: the share you edited, deleted, or whose automatic change you undid, within 48 hours of it being made.
Also: of the loops email suggested (troth_eval.md §16), how many you added and how many you ignored.
Reads DATABASE_URL and changes nothing. Prints counts, never text.

  python scripts/correction_rate.py
  journalctl -u troth -o short-iso > troth.log && python scripts/correction_rate.py --log troth.log

Deleting a loop also deletes its history, so only the log shows deleted loops. Without --log they aren't counted.
"""

import argparse
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # so `app` and `scripts` import without installing the package

from sqlmodel import Session, select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import (  # noqa: E402
    ActionStatus, Activity, ActivityAction, ActivityBy, OpenLoop, PendingAction, create_db_engine, utcnow,
)
from app.engine import NEW_SENDER  # noqa: E402
from scripts.eval_report import rate_row  # noqa: E402

WINDOW = timedelta(hours=48)
# journalctl -o short-iso: "2026-10-19T08:00:00+0800 host troth[12]: INFO:continuum.engine:loop_created id=..."
LOG_LINE = re.compile(r"^(\S+) .*\bloop_(created|deleted) id=([0-9a-f-]{36})(?: by=(\w+))?")


def read_log(path: Path) -> tuple[dict[UUID, datetime], dict[UUID, datetime]]:
    """When each loop was made (by chat or email; a message check's loops are left out) and deleted."""
    made: dict[UUID, datetime] = {}
    deleted: dict[UUID, datetime] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = LOG_LINE.match(line)
        if match is None or match[4] == "check":
            continue
        try:
            when = datetime.fromisoformat(match[1])
        except ValueError:  # a line without a timestamp can't be placed in the window
            continue
        (made if match[2] == "created" else deleted)[UUID(match[3])] = when
    return made, deleted


def corrections(session: Session, now: datetime, log: Path | None = None) -> Counter[str]:
    history: dict[UUID, list[Activity]] = defaultdict(list)
    for a in session.exec(select(Activity)).all():
        history[a.loop_id].append(a)  # type: ignore[index]
    made = [a for rows in history.values() for a in rows
            if a.action == ActivityAction.created and a.by in (ActivityBy.chat, ActivityBy.email)]
    c: Counter[str] = Counter()
    for a in made:
        if now - a.created_at < WINDOW:
            c["too_new"] += 1
            continue
        c["made"] += 1
        c[f"made_by_{a.by.value}"] += 1
        later = [x for x in history[a.loop_id] if x.by == ActivityBy.user and a.created_at < x.created_at <= a.created_at + WINDOW]  # type: ignore[index]
        edits = [x for x in later if x.detail.startswith("edited ")]
        undone = any(x.detail.startswith("undid ") for x in later)
        for field in {f.strip() for x in edits for f in x.detail.removeprefix("edited ").split(",")}:
            c[f"field:{field}"] += 1
        c["edited"] += bool(edits)
        c["undone"] += undone
        c["corrected"] += bool(edits) or undone
    if log is not None:
        c["log"] = 1
        alive = set(session.exec(select(OpenLoop.id)).all())
        log_made, log_deleted = read_log(log)
        for loop_id, when in log_made.items():
            if loop_id in alive:
                continue  # counted above, from its history
            if now - when < WINDOW:
                c["too_new"] += 1
                continue
            c["made"] += 1
            c["made_gone"] += 1
            if loop_id in log_deleted and log_deleted[loop_id] - when <= WINDOW:
                c["deleted"] += 1
                c["corrected"] += 1
    return c


def suggestions(session: Session) -> Counter[str]:
    """Loops an email would add that waited for a yes, by why ("new": someone Troth didn't follow yet; "risky": a link,
    an address or a money/details ask) and by what you did: done = Add, rejected = Ignore."""
    c: Counter[str] = Counter()
    for a in session.exec(select(PendingAction)).all():
        if a.payload.get("new_loop"):
            c[f"{'new' if a.payload.get('why') == NEW_SENDER else 'risky'}:{a.status.value}"] += 1
    return c


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--log", type=Path, help="Troth's log with timestamps, e.g. from journalctl -o short-iso")
    args = parser.parse_args()
    path = settings.database_url.removeprefix("sqlite:///")
    if settings.database_url.startswith("sqlite:///") and not Path(path).exists():
        sys.exit(f"No database at {path}: set DATABASE_URL to Troth's database.")  # opening it would create one
    db = create_db_engine(settings.database_url)
    with Session(db) as session:
        c = corrections(session, utcnow(), args.log)
        s = suggestions(session)
    print(f"Loops Troth made from chat or email at least 48 h ago: {c['made']} (chat {c['made_by_chat']}, "
          f"email {c['made_by_email']}" + (f", deleted since {c['made_gone']}" if c["log"] else "") + ").\n")
    print("| Corrected within 48 h | Rate | 95% interval | n |\n|---|---|---|---|")
    print(rate_row("Edited", c["edited"], c["made"], "loops"))
    print(rate_row("Automatic change undone", c["undone"], c["made"], "loops"))
    print(rate_row("Deleted", c["deleted"], c["made"], "loops") if c["log"] else "| Deleted | not counted: no --log | - | - |")
    print(rate_row("Any of these", c["corrected"], c["made"], "loops"))
    fields = sorted((k.removeprefix("field:"), v) for k, v in c.items() if k.startswith("field:"))
    if fields:
        print("\nFields edited: " + ", ".join(f"{name} x{n}" for name, n in fields))
    print(f"\nMade in the last 48 h, not counted yet: {c['too_new']}.")
    print("\n| Email suggestions you answered | Added | 95% interval | n |\n|---|---|---|---|")
    for why, label in (("new", "From someone new"), ("risky", "Link, address or money ask")):
        added, ignored = s[f"{why}:{ActionStatus.done.value}"], s[f"{why}:{ActionStatus.rejected.value}"]
        print(rate_row(label, added, added + ignored, "answered"))
    waiting = sum(n for k, n in s.items() if k.endswith((ActionStatus.proposed.value, ActionStatus.failed.value)))
    print(f"\nStill waiting for an answer: {waiting}.")


if __name__ == "__main__":
    main()
