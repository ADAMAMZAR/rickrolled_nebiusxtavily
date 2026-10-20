"""Troth on the labelled set (troth_eval.md §3), with real Nemotron (and Tavily for message checks). Each item runs on
its own scratch database, so data/continuum.db is never touched. Every run becomes one JSON line for
scripts/eval_report.py; nothing is scored here.

  pytest -m eval                                   # dev split, 5 runs each: loops, message checks, injection
  EVAL_PILOT=1 pytest -m eval                      # 5 loop items and 2 checks once: cost per item, before a full run
  EVAL_SPLIT=test pytest -m eval                   # the test split: twice in total, before and after the fixes
  EVAL_ONLY=loops EVAL_DROP_RULE=6 pytest -m eval  # ablation: the extraction prompt without rule 6

Data: <EVAL_DIR>/gold.jsonl and scam.jsonl (scripts/eval_import.py check); EVAL_DIR defaults to data/eval.
Injection items: tests/scenarios/injection.jsonl. Results go to <EVAL_DIR>/results/.
Other settings: EVAL_RUNS (5), EVAL_WORKERS (4 for loops; checks use half), EVAL_FORCE=1 (a third test run).
"""

import hashlib
import json
import logging
import math
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from sqlmodel import Session, col, select

from app import engine as eng
from app import extraction
from app import investigation as inv
from app.config import settings
from app.connectors.google import Email, strip_reply
from app.connectors.tavily import TavilyClient, WebResult
from app.db import Activity, ActivityAction, Dependency, Goal, LoopKind, OpenLoop, Person, create_db_engine
from app.extraction import ExtractionError, ExtractionResult
from app.llm import LLMClient, LLMError
from scripts.eval_report import rules_version

EVAL_DIR = Path(os.environ.get("EVAL_DIR", "data/eval"))
SPLIT = os.environ.get("EVAL_SPLIT", "dev")
RUNS = int(os.environ.get("EVAL_RUNS", "5"))
ONLY = os.environ.get("EVAL_ONLY", "")
DROP_RULE = int(os.environ["EVAL_DROP_RULE"]) if os.environ.get("EVAL_DROP_RULE") else None
PILOT = os.environ.get("EVAL_PILOT") == "1"
WORKERS = int(os.environ.get("EVAL_WORKERS", "4"))
FORCE = os.environ.get("EVAL_FORCE") == "1"
INJECTION = Path(__file__).parent / "scenarios" / "injection.jsonl"
DROPS = re.compile(r"dropped|skipped|invalid")  # the guards' log lines; none of them carries message text


def drop_rule(prompt: str, n: int) -> str:
    """The extraction prompt without rule `n`: its numbered line and the indented lines under it."""
    out, skip, found = [], False, False
    for line in prompt.splitlines(keepends=True):
        if match := re.match(r"(\d+)\. ", line):
            skip = int(match[1]) == n
            found |= skip
        elif not line.startswith("   "):
            skip = False
        if not skip:
            out.append(line)
    if not found:
        raise ValueError(f"The extraction prompt has no rule {n}.")
    return "".join(out)


def test_drop_rule_removes_one_rule_and_nothing_else() -> None:
    without = drop_rule(extraction.SYSTEM_PROMPT, 6)
    assert "\n6. " not in without and "\n5. " in without and "\n7. " in without
    assert "would happen anyway" not in without  # rule 6's indented lines go too
    assert len(without) < len(extraction.SYSTEM_PROMPT) and "Return exactly this JSON shape" in without
    with pytest.raises(ValueError):
        drop_rule(extraction.SYSTEM_PROMPT, 99)


# --- recording ---


def version(kind: str) -> dict[str, Any]:
    """Which code, prompts and model made a run."""
    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return ""

    if kind == "scam":
        parts = [extraction.SCAM_EXTRACT_PROMPT, extraction.PLAN_PROMPT, extraction.CLASSIFY_PROMPT,
                 extraction.SUMMARY_PROMPT, extraction.SCREENSHOT_PROMPT]
    else:
        parts = [extraction.SYSTEM_PROMPT, extraction.EMAIL_NOTE, extraction.WEB_NOTE]
    return {"commit": git("rev-parse", "--short", "HEAD") or "unknown", "dirty": bool(git("status", "--porcelain")),
            "prompt": hashlib.sha256("\n".join(parts).encode()).hexdigest()[:8], "model": settings.nebius_model}


class Results:
    """One JSON line per run, written as each run finishes, so a stopped run keeps what it did."""

    def __init__(self, kind: str) -> None:
        folder = EVAL_DIR / "results"
        folder.mkdir(parents=True, exist_ok=True)
        v = version(kind)
        drop = DROP_RULE if kind == "loops" else None
        self.meta = {"kind": kind, "split": "pilot" if PILOT else SPLIT, "drop_rule": drop, **v}
        name = f"{datetime.now():%Y%m%d-%H%M%S}-{kind}-{self.meta['split']}-{v['commit']}-{v['prompt']}"
        self.path = folder / (name + (f"-drop{drop}" if drop else "") + ".jsonl")
        self._lock = threading.Lock()

    def add(self, record: dict[str, Any]) -> None:
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(self.meta | record, ensure_ascii=False) + "\n")


class Recorder:
    """Nemotron, keeping every raw reply: the model's own JSON, before any guard."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm
        self.replies: list[str] = []

    def complete(self, messages: list[dict[str, Any]], json_mode: bool = False, model: str | None = None) -> str:
        reply = self.llm.complete(messages, json_mode, model)
        self.replies.append(reply)
        return reply


class CountingWeb:
    """Tavily, counting estimated credits: a basic search is 1, a basic extract 1 per 5 pages."""

    def __init__(self, web: TavilyClient) -> None:
        self.web, self.credits, self._lock = web, 0, threading.Lock()

    def _add(self, n: int) -> None:
        with self._lock:
            self.credits += n

    def search(self, query: str, **kwargs: Any) -> Any:
        self._add(1)
        return self.web.search(query, **kwargs)

    def extract(self, urls: list[str], cached: bool = False) -> Any:
        self._add(math.ceil(len(urls) / 5))
        return self.web.extract(urls, cached=cached)


class DropLog(logging.Handler):
    """Collects the guards' drop lines per worker thread: each item-run runs in one thread."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.local = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        lines = getattr(self.local, "lines", None)
        if lines is not None and DROPS.search(message := record.getMessage()):
            lines.append(message)


@pytest.fixture
def drops() -> Iterator[DropLog]:
    handler, logger = DropLog(), logging.getLogger("continuum")
    level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)
    logger.setLevel(level)


def run_all(kind: str, tasks: list[tuple[dict[str, Any], int]], one: Callable[[dict[str, Any], int], dict[str, Any]],
            workers: int, capsys: pytest.CaptureFixture[str]) -> Path:
    """Every (item, run) in a thread pool. An unexpected error is a bug: it stops the rest and fails the test."""
    results = Results(kind)
    pool = ThreadPoolExecutor(max(1, workers))
    try:
        futures = [pool.submit(one, item, run) for item, run in tasks]
        for n, future in enumerate(as_completed(futures), 1):
            results.add(future.result())
            with capsys.disabled():
                print(f"\r{kind}: {n}/{len(tasks)} runs", end="", flush=True)
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown()
    with capsys.disabled():
        print(f"\n{kind}: {results.path}\n  python scripts/eval_report.py {kind} {results.path}")
    return results.path


def load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def pick(items: list[dict[str, Any]], pilot: int) -> list[dict[str, Any]]:
    """The split's items; a pilot takes a few dev items spread over the file (so over windows and sources)."""
    chosen = [i for i in items if i["split"] == ("dev" if PILOT else SPLIT)]
    return chosen[::max(1, len(chosen) // pilot)][:pilot] if PILOT else chosen


def guard_test(kind: str) -> None:
    """The test split is scored twice in total: before and after the fixes. More would tune on it."""
    if SPLIT == "test" and not PILOT and not FORCE:
        done = list((EVAL_DIR / "results").glob(f"*-{kind}-test-*.jsonl"))
        if len(done) >= 2:
            pytest.skip(f"The {kind} test split was already scored {len(done)} times. EVAL_FORCE=1 to run it anyway.")


def runs() -> int:
    return 1 if PILOT else RUNS


# --- one item-run on a scratch database ---


def _person(session: Session, people: dict[str, Person], name: str) -> Person:
    key = eng.normalize(name)
    if key not in people:
        people[key] = Person(name=name)
        session.add(people[key])
    return people[key]


def seed(session: Session, item: dict[str, Any]) -> tuple[dict[str, OpenLoop], dict[str, Person], set[Any]]:
    """The item's state as real rows: open loops as they were at their time, their people and links."""
    source = eng.add_source(session, "(earlier messages)")
    loops: dict[str, OpenLoop] = {}
    people: dict[str, Person] = {}
    for s in item["state"]:
        loop = eng.add_loop(session, source_id=source.id, title=s["title"], summary=s["title"], kind=LoopKind(s["kind"]),
                            waiting_on=s["person"], due=date.fromisoformat(s["due"]) if s["due"] else None)
        loop.created_at = loop.updated_at = datetime.fromisoformat(s["time"])
        if s["person"] and loop.kind != LoopKind.task:  # the same rule as a new loop from chat
            loop.person_id = _person(session, people, s["person"]).id
        loops[s["ref"]] = loop
    for link in item.get("state_links", []):
        session.add(Dependency(loop_id=loops[link["loop"]].id, needs_id=loops[link["needs"]].id))
    session.commit()
    return loops, people, {d.id for d in session.exec(select(Dependency)).all()}


FIELDS = ("title", "summary", "kind", "waiting_on", "due", "next_action", "status")


def _snapshot(loop: OpenLoop) -> dict[str, Any]:
    return {name: getattr(loop, name).isoformat() if isinstance(getattr(loop, name), date) else
            str(getattr(loop, name)) if getattr(loop, name) is not None else None for name in FIELDS}


def _parsed(replies: list[str]) -> ExtractionResult | None:
    """The reply the run used: the last one that validates."""
    for reply in reversed(replies):
        try:
            return ExtractionResult.model_validate(json.loads(reply))
        except ValueError:
            continue
    return None


def describe(session: Session, loops: dict[str, OpenLoop], before: dict[str, dict[str, Any]], seeded: set[Any],
             replies: list[str]) -> dict[str, Any]:
    """What a run changed. State loops are named by their ref, new ones new:0, new:1, ..."""
    ref_of = {loop.id: ref for ref, loop in loops.items()}
    new = [l for l in session.exec(select(OpenLoop).order_by(col(OpenLoop.created_at))).all() if l.id not in ref_of]
    ref_of |= {loop.id: f"new:{n}" for n, loop in enumerate(new)}
    kept = {a.loop_id: a.quote for a in session.exec(select(Activity).where(Activity.action == ActivityAction.created)).all()}
    raw = _parsed(replies)
    said = {c.title: c.quote for c in raw.new_loops} if raw else {}
    changed, updates, resolves = [], [], []
    for ref, loop in loops.items():
        session.refresh(loop)
        after = _snapshot(loop)
        fields = {k: after[k] for k in FIELDS if k != "status" and after[k] != before[ref][k]}
        if fields:
            changed.append({"target": ref, "fields": fields})
        if "due" in fields:
            updates.append({"target": ref, "due": fields["due"]})
        if before[ref]["status"] == "open" and after["status"] == "resolved":
            resolves.append({"target": ref})
    return {
        "loops": [{"ref": ref_of[l.id], **{k: v for k, v in _snapshot(l).items() if k != "status"},
                   "quote": kept.get(l.id), "raw_quote": said.get(l.title)} for l in new],
        "updates": updates, "resolves": resolves, "changed": changed,
        "links": [{"loop": ref_of[d.loop_id], "needs": ref_of[d.needs_id]}
                  for d in session.exec(select(Dependency)).all() if d.id not in seeded],
        "goals": [g.title for g in session.exec(select(Goal)).all()],
        "proposals": [{"kind": a.kind.value, "target": ref_of.get(a.loop_id), "payload": a.payload}
                      for a in eng.list_actions(session, None)],
    }


def run_item(item: dict[str, Any], run: int, drops: DropLog) -> dict[str, Any]:
    """One chat message, email or web page through Troth's real path. A model failure (no valid JSON, Nebius
    unreachable) is recorded; the report scores the first as an empty answer and leaves the second out."""
    llm = LLMClient(settings)
    rec = Recorder(llm)
    lines: list[str] = []
    drops.local.lines = lines
    start = time.perf_counter()
    with TemporaryDirectory() as tmp:
        db = create_db_engine(f"sqlite:///{Path(tmp) / 'eval.db'}")
        try:
            with Session(db) as s:
                loops, people, seeded = seed(s, item)
                before = {ref: _snapshot(loop) for ref, loop in loops.items()}
                now = datetime.fromisoformat(item["now"])
                error = None
                try:
                    if item["path"] == "email":
                        sender = _person(s, people, item["sender"])
                        s.commit()
                        body = strip_reply(item["body"]) if item.get("raw_body") else item["body"]
                        mail = Email(id=item["item"], thread_id=item["item"], sender=item["sender"],
                                     subject=item["subject"], received=now, snippet="", body=body)
                        eng.ingest_email(s, rec, sender, mail, now)
                    elif item["path"] == "web":
                        page = WebResult(title=item["title"], url=item["url"], content=item["content"])
                        eng._review_web_results(s, rec, loops[item["watch"]], [page], now)
                        s.commit()
                    else:
                        eng.process_message(s, rec, item["text"], now=now)
                except (ExtractionError, LLMError) as e:
                    error = type(e).__name__
                predicted = describe(s, loops, before, seeded, rec.replies)
        finally:
            db.dispose()
    drops.local.lines = None
    return {"item": item["item"], "run": run, "error": error, "ms": int((time.perf_counter() - start) * 1000),
            "calls": llm.calls, "tokens_in": llm.tokens_in, "tokens_out": llm.tokens_out, "raw": rec.replies,
            "drops": lines, "predicted": predicted}


def run_check(item: dict[str, Any], run: int) -> dict[str, Any]:
    """One message check, end to end. Its rows are kept whole ("frozen"), so the report can score them again
    with other weights, offline (troth_eval.md §3)."""
    llm, web = LLMClient(settings), CountingWeb(TavilyClient(settings))
    start = time.perf_counter()
    with TemporaryDirectory() as tmp:
        db = create_db_engine(f"sqlite:///{Path(tmp) / 'eval.db'}")
        try:
            with Session(db) as s:
                shot = (EVAL_DIR / item["screenshot"]).read_bytes() if item["screenshot"] else None
                made = inv.create_investigation(s, text=item["text"] or None, url=item["url"] or None, screenshot=shot).id
            inv.run_investigation(db, made, lambda: llm, lambda: web)  # type: ignore[arg-type, return-value]
            with Session(db) as s:
                d = inv.get_investigation(s, made)
                i = d.investigation
                result = {
                    "status": i.status.value, "error": i.error, "level": i.risk_level.value if i.risk_level else None,
                    "score": i.score, "confidence": i.confidence.value if i.confidence else None,
                    "identity": i.identity.value if i.identity else None,
                    "frozen": {name: [row.model_dump(mode="json") for row in rows] for name, rows in
                               (("entities", d.entities), ("claims", d.claims), ("evidence", d.evidence), ("signals", d.signals))},
                }
        finally:
            db.dispose()
    return {"item": item["item"], "run": run, "label": item["label"], "rules": rules_version(), **result,
            "ms": int((time.perf_counter() - start) * 1000), "calls": llm.calls, "tokens_in": llm.tokens_in,
            "tokens_out": llm.tokens_out, "credits": web.credits}


# --- the three runs ---


@pytest.mark.eval
def test_loops(drops: DropLog, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    if ONLY not in ("", "loops"):
        pytest.skip("EVAL_ONLY")
    items = pick(load(EVAL_DIR / "gold.jsonl"), 5)
    if not items:
        pytest.skip(f"No {SPLIT} items in {EVAL_DIR / 'gold.jsonl'}: run scripts/eval_import.py check first.")
    guard_test("loops")
    if DROP_RULE:
        monkeypatch.setattr(extraction, "SYSTEM_PROMPT", drop_rule(extraction.SYSTEM_PROMPT, DROP_RULE))
    run_all("loops", [(i, r) for i in items for r in range(runs())], lambda i, r: run_item(i, r, drops), WORKERS, capsys)


@pytest.mark.eval
def test_scam(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    if ONLY not in ("", "scam"):
        pytest.skip("EVAL_ONLY")
    items = pick(load(EVAL_DIR / "scam.jsonl"), 2)
    if not items:
        pytest.skip(f"No {SPLIT} items in {EVAL_DIR / 'scam.jsonl'}: run scripts/eval_import.py check first.")
    guard_test("scam")
    monkeypatch.setattr(settings, "uploads_dir", str(tmp_path / "uploads"))
    run_all("scam", [(i, r) for i in items for r in range(runs())], run_check, WORKERS // 2, capsys)


@pytest.mark.eval
def test_injection(drops: DropLog, capsys: pytest.CaptureFixture[str]) -> None:
    """Emails and web pages that try to steer the model (troth_eval.md §1.3). Synthetic, so no split."""
    if ONLY not in ("", "injection") or PILOT:
        pytest.skip("EVAL_ONLY or EVAL_PILOT")
    items = load(INJECTION)
    run_all("injection", [(i, r) for i in items for r in range(runs())], lambda i, r: run_item(i, r, drops), WORKERS, capsys)
