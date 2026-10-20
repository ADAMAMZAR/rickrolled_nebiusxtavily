"""Turn chat and email exports into items to label, then check the labels (troth_eval.md §1).

  python scripts/eval_import.py windows --me "Adam Amzar" --me-email adam@example.com
  python scripts/eval_import.py check
  python scripts/eval_import.py blank-scam
  python scripts/eval_import.py injection --set test2
  python scripts/eval_import.py gmail --months 3          # your recent Gmail -> raw/gmail.mbox, instead of Takeout

Exports go in data/eval/raw/: WhatsApp "Export chat" .txt, Telegram Desktop result.json, Gmail Takeout .mbox.
`windows` writes items.jsonl and the two labelling sheets; `check` turns labels_a.csv into gold.jsonl and
scam.csv into scam.jsonl. `injection` puts published attacks from data/eval/benchmarks/ into the benign injection
items: a set to tune on or a held-out one (troth_eval.md §13). Everything stays in data/eval/ (gitignored). Prints counts and row
numbers, never text.
"""

import argparse
import ast
import csv
import email
import email.message
import email.policy
import email.utils
import hashlib
import html
import json
import mailbox
import os
import random
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # so `app` imports without installing the package

from app.config import settings  # noqa: E402
from app.investigation import MAX_TEXT as MAX_CHECK_TEXT  # noqa: E402
from app.connectors.google import PEOPLE_MAIL, Email, GoogleClient, GoogleError, automated, strip_reply  # noqa: E402
from app.quotes import appears  # noqa: E402

EVAL_DIR = Path(os.environ.get("EVAL_DIR", "data/eval"))
SEED = 2026
MERGE = timedelta(minutes=10)  # one sender's messages this close together are one turn
MAX_OTHERS = 5  # the other people's turns shown before yours
MAX_CHARS = 2000
BACKGROUND = 5  # loops from other windows in every item's state, so the model has distractors

SHEET = ["item", "window", "source", "from", "time", "text", "lang", "label", "ref", "title", "quote", "kind", "person", "due",
         "target", "hedged", "note"]
LABELS = {"new", "open", "update", "resolve", "cancel", "link", "none"}
KINDS = {"task", "commitment", "waiting"}
SCAM_LABELS = {"listed", "impersonation", "reported", "legitimate"}
SCAM_SHEET = ["item", "text", "url", "screenshot", "label", "record", "split", "note"]

# Words that make a window worth picking (troth_eval.md §1.1), English and Malay.
TRIGGER = re.compile(
    r"\b(i'?ll|i will|will send|by (?:mon|tue|wed|thu|fri|sat|sun|tmr|tomorrow|tonight|next|end)\w*|before|deadline|"
    r"due|promise|waiting|wait for|remind|get back|tomorrow|tmr|tonight|next week|nanti|nnt|esok|lusa|sebelum|"
    r"hantar|tunggu|janji|minggu depan|malam ni|monday|tuesday|wednesday|thursday|friday|saturday|sunday|isnin|"
    r"selasa|rabu|khamis|jumaat|sabtu|ahad)\b",
    re.IGNORECASE,
)

# Numbers that aren't the point of a loop. Names stay: the person is scored.
MASKS = [
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[EMAIL]"),
    (re.compile(r"\b\d{6}-\d{2}-\d{4}\b"), "[IC]"),
    (re.compile(r"(?<!\w)(?:\+?60[ -]?|0)1\d(?:[ -]?\d){7,8}\b"), "[PHONE]"),
    (re.compile(r"(?<!\w)(?:\+?60[ -]?|0)[3-9](?:[ -]?\d){7,8}\b"), "[PHONE]"),
    (re.compile(r"\b\d(?:[ -]?\d){8,}\b"), "[ACCOUNT]"),
]


def mask(text: str) -> str:
    for pattern, placeholder in MASKS:
        text = pattern.sub(placeholder, text)
    return text


@dataclass
class Message:
    chat: str
    source: str  # whatsapp | telegram | gmail
    time: datetime
    sender: str
    text: str
    subject: str = ""  # email only


# --- reading exports ---

ANDROID = re.compile(r"^(\d{1,2})[/.](\d{1,2})[/.](\d{2,4}),? (\d{1,2}):(\d{2})(?:\s?([ap])\.?m\.?)? - (.*)$", re.I)
IOS = re.compile(r"^\[(\d{1,2})[/.](\d{1,2})[/.](\d{2,4}),? (\d{1,2}):(\d{2})(?::\d{2})?(?:\s?([ap])\.?m\.?)?\] (.*)$", re.I)
NOT_TEXT = re.compile(r"^(<media omitted>|<attached: .*>|(image|video|audio|sticker|gif|document|contact card) omitted|"
                      r"this message was deleted|you deleted this message|null)$", re.I)
EDITED = re.compile(r"\s*<this message was edited>$", re.I)


def read_whatsapp(path: Path, tz: tzinfo, month_first: bool = False) -> list[Message]:
    """Android ("14/09/2026, 21:14 - Aisyah: hi") and iOS ("[14/09/2026, 9:14:03 PM] Aisyah: hi") exports.
    A line without a date continues the message above it. System lines and media are skipped."""
    out: list[Message] = []
    current: Message | None = None
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.replace(" ", " ").replace(" ", " ").lstrip("‎‏")
        match = ANDROID.match(line) or IOS.match(line)
        if match is None:
            if current is not None:
                current.text += "\n" + line
            continue
        a, b, year, hour, minute, ampm, rest = match.groups()
        day, month = (int(b), int(a)) if month_first else (int(a), int(b))
        h = int(hour) % 12 + (12 if ampm.lower() == "p" else 0) if ampm else int(hour)
        sender, colon, text = rest.partition(": ")
        if not colon:  # "Aisyah added Ali", "Messages are end-to-end encrypted"
            current = None
            continue
        when = datetime(int(year) + (2000 if len(year) == 2 else 0), month, day, h, int(minute), tzinfo=tz)
        current = Message(re.sub(r"^WhatsApp Chat (with |- )?", "", path.stem), "whatsapp", when, sender.strip(), text)
        out.append(current)
    return [replace(m, text=text) for m in out if (text := EDITED.sub("", m.text).strip()) and not NOT_TEXT.match(text)]


def read_telegram(path: Path, tz: tzinfo) -> list[Message]:
    """Telegram Desktop's result.json: one chat, or a full export with every chat."""
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for chat in data["chats"]["list"] if "chats" in data else [data]:
        name = chat.get("name") or "Saved Messages"
        for m in chat.get("messages", []):
            text = m.get("text", "")
            if isinstance(text, list):  # links, bold and mentions come as parts
                text = "".join(p if isinstance(p, str) else p.get("text", "") for p in text)
            if m.get("type") == "message" and text.strip():
                when = datetime.fromisoformat(m["date"]).replace(tzinfo=tz)
                out.append(Message(f"{path.stem}/{name}", "telegram", when, m.get("from") or "", text.strip()))
    return out


def read_mbox(path: Path, tz: tzinfo, me: set[str]) -> tuple[list[Message], int]:
    """Emails from people, as the Gmail connector would read them. Your own mail is left out; newsletters,
    no-reply senders and unreadable mail are skipped and counted."""
    out, skipped = [], 0
    for raw in mailbox.mbox(path):
        msg = email.message_from_bytes(raw.as_bytes(), policy=email.policy.default)
        name, address = email.utils.parseaddr(str(msg.get("From", "")))
        address = address.lower()
        if not address or address in me:
            continue
        headers = {"list-unsubscribe": str(msg.get("List-Unsubscribe") or ""), "precedence": str(msg.get("Precedence", ""))}
        try:
            when = email.utils.parsedate_to_datetime(str(msg.get("Date", ""))).astimezone(tz)
            body = _body(msg)
        except (TypeError, ValueError, LookupError):  # no date, a bad date, an unknown charset
            when, body = None, ""
        if automated(address, headers) or when is None or not body:  # as sync's new mail (GoogleClient.new_mail)
            skipped += 1
            continue
        out.append(Message(address, "gmail", when, name or address.split("@")[0], body, str(msg.get("Subject", ""))))
    return out, skipped


def _body(msg: email.message.EmailMessage) -> str:
    """Like the Gmail connector: the plain-text part without quotes and signature; for HTML-only mail, about as
    much text as Gmail's snippet."""
    part = msg.get_body(preferencelist=("plain",))
    if part is not None:
        return strip_reply(part.get_content())
    part = msg.get_body(preferencelist=("html",))
    if part is None:
        return ""
    page = re.sub(r"(?is)<(style|script)\b.*?</\1>", " ", part.get_content())
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())[:200]


# --- items and windows ---


def chat_items(messages: list[Message], me: set[str]) -> list[dict[str, Any]]:
    """Each of your turns, with the other people's turns since your previous one, as a paste ("Me: ..."). A turn
    is one sender's messages in a row: people split one thought over several messages."""
    turns: list[Message] = []
    for m in sorted(messages, key=lambda m: m.time):
        if turns and turns[-1].sender == m.sender and m.time - turns[-1].time <= MERGE:
            turns[-1] = replace(turns[-1], text=f"{turns[-1].text}\n{m.text}", time=m.time)
        else:
            turns.append(m)
    items, others = [], []
    for turn in turns:
        if turn.sender.casefold() not in me:
            others.append(turn)
            continue
        lines = [f"{o.sender}: {o.text}" for o in others[-MAX_OTHERS:]] + [f"Me: {turn.text}"]
        while len(lines) > 1 and len("\n".join(lines)) > MAX_CHARS:
            lines.pop(0)
        items.append({"source": turn.source, "chat": turn.chat, "path": "chat", "now": turn.time.isoformat(),
                      "text": mask("\n".join(lines))[-MAX_CHARS:]})
        others = []
    return items


def email_items(messages: list[Message]) -> list[dict[str, Any]]:
    out = []
    for m in sorted(messages, key=lambda m: m.time):
        subject, body = mask(m.subject), mask(m.text)
        text = Email(id="-", thread_id="-", sender=m.chat, subject=subject, received=m.time, snippet="", body=body).text
        out.append({"source": "gmail", "chat": m.chat, "path": "email", "now": m.time.isoformat(), "text": text,
                    "sender": m.sender, "subject": subject, "body": body})
    return out


def windows(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    """Consecutive items of one chat (or one sender's emails), `size` at a time."""
    by_chat: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        by_chat[(item["source"], item["chat"])].append(item)
    return [chat[i:i + size] for chat in by_chat.values() for i in range(0, len(chat), size)]


def choose(cands: list[list[dict[str, Any]]], target: int, rng: random.Random) -> list[tuple[list[dict[str, Any]], str]]:
    """Half the items from windows picked at random (the real base rate), half from windows with promise, request
    or date words (enough to score)."""
    pool = rng.sample(cands, len(cands))
    chosen: list[tuple[list[dict[str, Any]], str]] = []

    def count() -> int:
        return sum(len(w) for w, _ in chosen)

    for w in pool:
        if count() >= target / 2:
            break
        chosen.append((w, "random"))
    taken = {id(w) for w, _ in chosen}
    for w in pool:
        if count() >= target:
            break
        if id(w) not in taken and any(TRIGGER.search(i["text"]) for i in w):
            chosen.append((w, "picked"))
    return sorted(chosen, key=lambda c: (c[0][0]["source"], c[0][0]["chat"], c[0][0]["now"]))


def split_windows(chosen: list[tuple[list[dict[str, Any]], str]], test: float, double: int, rng: random.Random) -> list[dict[str, Any]]:
    """Number the windows and items; split by window within each source; mark the windows both labellers do."""
    meta = [{"window": f"w{n:02d}", "source": w[0]["source"], "split": "dev", "double": False}
            for n, (w, _) in enumerate(chosen, 1)]
    for source in sorted({m["source"] for m in meta}):  # sorted: set order changes between runs
        ws = rng.sample([m for m in meta if m["source"] == source], sum(m["source"] == source for m in meta))
        for m in ws[:round(len(ws) * test)]:
            m["split"] = "test"
    n_test = round(double * test)
    for split, n in (("test", n_test), ("dev", double - n_test)):
        group = [m for m in meta if m["split"] == split]
        for m in rng.sample(group, min(n, len(group))):
            m["double"] = True
    out = []
    for m, (w, sample) in zip(meta, chosen):
        for i, item in enumerate(w, 1):
            out.append({"item": f"{m['window']}-{i}", "window": m["window"], **item, "sample": sample,
                        "split": m["split"], "double": m["double"]})
    return out


def write_sheet(path: Path, items: list[dict[str, Any]]) -> None:
    """utf-8-sig so Excel opens it with Malay and emoji intact."""
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        out = csv.DictWriter(f, SHEET)
        out.writeheader()
        for item in items:
            when = datetime.fromisoformat(item["now"])
            out.writerow({"item": item["item"], "window": item["window"], "source": item["source"],
                          "from": item.get("sender", ""), "time": f"{when:%Y-%m-%d %H:%M %a}", "text": item["text"]})


def cmd_windows(args: argparse.Namespace) -> None:
    folder = Path(args.dir)
    if (folder / "items.jsonl").exists() and not args.force:
        sys.exit("items.jsonl already exists, and labels depend on it. Use --force to start over.")
    tz = ZoneInfo(args.tz)
    me = {name.casefold().strip() for name in args.me}
    me_emails = {address.lower() for address in args.me_email}
    items: list[dict[str, Any]] = []
    skipped = 0
    for path in sorted((folder / "raw").rglob("*")):
        if path.suffix == ".txt":
            items += chat_items(read_whatsapp(path, tz, args.month_first), me)
        elif path.suffix == ".json":
            items += chat_items(read_telegram(path, tz), me)
        elif path.suffix == ".mbox":
            mails, n = read_mbox(path, tz, me_emails)
            items, skipped = items + email_items(mails), skipped + n
    if not items:
        sys.exit(f"No items: put exports in {folder / 'raw'} and check --me matches your name in them.")
    rng = random.Random(args.seed)
    chosen = choose(windows(items, args.window), args.target, rng)
    out = split_windows(chosen, args.test, args.double, rng)
    with (folder / "items.jsonl").open("w", encoding="utf-8") as f:
        f.writelines(json.dumps(item, ensure_ascii=False) + "\n" for item in out)
    write_sheet(folder / "sheet_a.csv", out)
    write_sheet(folder / "sheet_b.csv", [i for i in out if i["double"]])
    print(f"{len(items)} items in the exports ({skipped} emails skipped); chose {len(out)} in {len(chosen)} windows.")
    for key in ("source", "sample", "split"):
        print(f"  {key}: " + ", ".join(f"{k} {v}" for k, v in sorted(Counter(i[key] for i in out).items())))
    print(f"  labelled by both: {sum(i['double'] for i in out)} items")
    print("Next: label sheet_a.csv and save it as labels_a.csv; labeller B does the same with sheet_b.csv.")


# --- labels ---


def parse_due(text: str) -> date:
    """ISO, or day-first as Excel saves dates in Malaysia (2/10/2026)."""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(text)


def load_items(folder: Path) -> dict[str, dict[str, Any]]:
    with (folder / "items.jsonl").open(encoding="utf-8") as f:
        return {i["item"]: i for i in map(json.loads, f)}


def parse_labels(path: Path, items: dict[str, dict[str, Any]], every: list[str]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str], list[str]]:
    """Rows → labels per item, each item's language, and errors (row numbers and ids, never text).
    `every`: the items this file must label."""
    labels: dict[str, list[dict[str, Any]]] = defaultdict(list)
    langs: dict[str, str] = {}
    errors: list[str] = []
    refs: set[str] = set()
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row_no, row in enumerate(csv.DictReader(f), 2):
            row = {k: (v or "").strip() for k, v in row.items() if k}
            item_id, label = row.get("item", ""), row.get("label", "").lower()
            if not item_id and not any(row.values()):
                continue
            where = f"{path.name} row {row_no} ({item_id or 'no item'})"
            item = items.get(item_id)
            if item is None:
                errors.append(f"{where}: unknown item")
                continue
            if row.get("lang"):
                langs[item_id] = row["lang"].lower()
            if label not in LABELS:
                errors.append(f"{where}: label must be one of {', '.join(sorted(LABELS))}")
                continue
            entry = {"row": row_no, "label": label, "ref": row.get("ref", ""), "title": row.get("title", ""),
                     "quote": row.get("quote", ""), "kind": row.get("kind", "").lower(), "person": row.get("person", ""),
                     "due": None, "target": row.get("target", ""), "hedged": row.get("hedged", "").lower() in {"y", "yes", "1", "true"}}
            if row.get("due"):
                try:
                    entry["due"] = parse_due(row["due"]).isoformat()
                except ValueError:
                    errors.append(f"{where}: due must be a date like 2026-10-02")
            problem = _row_problem(entry, item)
            if problem:
                errors.append(f"{where}: {problem}")
            if label in ("new", "open") and not entry["ref"]:
                entry["ref"] = f"{item_id}.{sum(e['label'] in ('new', 'open') for e in labels[item_id]) + 1}"
            if entry["ref"] and label in ("new", "open"):
                if entry["ref"] in refs:
                    errors.append(f"{where}: ref {entry['ref']} is used twice")
                refs.add(entry["ref"])
            labels[item_id].append(entry)
    for item_id in every:
        if item_id not in labels:
            errors.append(f"{path.name}: item {item_id} has no label (use 'none' if nothing applies)")
        elif len(labels[item_id]) > 1 and any(e["label"] == "none" for e in labels[item_id]):
            errors.append(f"{path.name}: item {item_id} has 'none' and other labels")
    return dict(labels), langs, errors


def _row_problem(entry: dict[str, Any], item: dict[str, Any]) -> str | None:
    label, quote = entry["label"], entry["quote"]
    if label in ("new", "update", "resolve", "cancel") and not (quote and appears(quote, item["text"])):
        return "quote is missing or not in the item's text (copy it exactly)"
    if label in ("new", "open") and entry["kind"] not in KINDS:
        return "kind must be task, commitment or waiting"
    if label == "open" and not entry["title"]:
        return "an 'open' loop needs a title"
    if label == "update" and not entry["due"]:
        return "an update needs the new due date"
    if label in ("update", "resolve", "cancel", "link") and not entry["target"]:
        return "target is missing"
    if label == "link" and not entry["ref"]:
        return "a link needs ref (the loop that waits) and target (what it needs)"
    return None


def build_gold(items: dict[str, dict[str, Any]], labels: dict[str, list[dict[str, Any]]], langs: dict[str, str],
               seed: int = SEED) -> tuple[list[dict[str, Any]], list[str]]:
    """Each item with its gold labels and its state: the loops still open from earlier items in its window, plus
    BACKGROUND loops from other windows in the same split, never about a person this window names (that would be a
    copy of the real target). Errors for targets that aren't open there."""
    by_window: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items.values():
        by_window[item["window"]].append(item)
    created = {(e["ref"], item["window"], item["split"], item["now"]): e
               for item in items.values() for e in labels.get(item["item"], []) if e["label"] == "new"}
    out, errors = [], []
    for wid, witems in by_window.items():
        rng = random.Random(f"{seed}-{wid}")
        names = {(e["person"] or "").casefold() for i in witems for e in labels.get(i["item"], [])} \
            | {i.get("sender", "").casefold() for i in witems}
        pool = sorted((key, e) for key, e in created.items()
                      if key[1] != wid and key[2] == witems[0]["split"] and (e["person"] or "").casefold() not in names - {""})
        background = [_state(e, now) for (_, _, _, now), e in rng.sample(pool, min(BACKGROUND, len(pool)))]
        open_: dict[str, dict[str, Any]] = {}
        links: list[dict[str, str]] = []
        for item in witems:
            rows = labels.get(item["item"], [])
            sender = item.get("sender") if item["path"] == "email" else None
            for e in rows:
                if e["label"] == "open":
                    open_[e["ref"]] = _state(e, item["now"], sender)
            new = {e["ref"] for e in rows if e["label"] == "new"}
            for e in rows:
                where = f"labels_a.csv row {e['row']} ({item['item']})"
                if e["label"] in ("update", "resolve", "cancel") and e["target"] not in open_:
                    errors.append(f"{where}: target {e['target']} isn't an open loop from earlier in the window")
                if e["label"] == "link" and not {e["ref"], e["target"]} <= new | set(open_):
                    errors.append(f"{where}: a link's ref and target must be loops from this item or still open")
            gold = {
                "loops": [{k: e[k] for k in ("ref", "title", "quote", "kind", "person", "due", "hedged")}
                          for e in rows if e["label"] == "new"],
                "updates": [{"target": e["target"], "due": e["due"], "quote": e["quote"]} for e in rows if e["label"] == "update"],
                "resolves": [{"target": e["target"], "quote": e["quote"], "cancel": e["label"] == "cancel"}
                             for e in rows if e["label"] in ("resolve", "cancel")],
                "links": [{"loop": e["ref"], "needs": e["target"]} for e in rows if e["label"] == "link"],
            }
            state_links = [link for link in links if {link["loop"], link["needs"]} <= set(open_)]
            out.append({**item, "lang": langs.get(item["item"], ""), "state": [*open_.values(), *background],
                        "state_links": state_links, "gold": gold})
            for e in rows:
                if e["label"] == "new":
                    open_[e["ref"]] = _state(e, item["now"], sender)
                elif e["label"] == "update" and e["target"] in open_:
                    open_[e["target"]] = {**open_[e["target"]], "due": e["due"], "time": item["now"]}
                elif e["label"] in ("resolve", "cancel"):
                    open_.pop(e["target"], None)
                elif e["label"] == "link":
                    links.append({"loop": e["ref"], "needs": e["target"]})
    return out, errors


def _state(e: dict[str, Any], now: str, sender: str | None = None) -> dict[str, Any]:
    """A loop as Troth would hold it. A task from an email is linked to its sender, as process_message does."""
    person = e["person"] or (sender if e["kind"] == "task" else None)
    return {"ref": e["ref"], "title": e["title"] or e["quote"], "kind": e["kind"], "person": person or None,
            "due": e["due"], "time": now}


def parse_scam(path: Path, folder: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rows, errors, seen = [], [], set()
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row_no, row in enumerate(csv.DictReader(f), 2):
            row = {k: (v or "").strip() for k, v in row.items() if k}
            if not any(row.values()):
                continue
            where = f"{path.name} row {row_no} ({row.get('item') or 'no item'})"
            if not row.get("item") or row["item"] in seen:
                errors.append(f"{where}: item id missing or used twice")
            seen.add(row.get("item", ""))
            if row.get("label", "").lower() not in SCAM_LABELS:
                errors.append(f"{where}: label must be one of {', '.join(sorted(SCAM_LABELS))}")
            if not row.get("record"):
                errors.append(f"{where}: record is missing (the list entry, warning page, article or channel)")
            if not (row.get("text") or row.get("url") or row.get("screenshot")):
                errors.append(f"{where}: needs text, url or screenshot")
            if row.get("screenshot") and not (folder / row["screenshot"]).is_file():
                errors.append(f"{where}: screenshot file not found")
            if len(row.get("text", "")) > MAX_CHECK_TEXT:
                errors.append(f"{where}: text over {MAX_CHECK_TEXT} characters (the check page's limit)")
            if row.get("url") and not row["url"].lower().startswith(("http://", "https://")):
                errors.append(f"{where}: url must start with http:// or https://")
            if row.get("split", "").lower() not in ("", "dev", "test"):
                errors.append(f"{where}: split must be dev, test or blank")
            rows.append({k: row.get(k, "") for k in SCAM_SHEET} | {"label": row.get("label", "").lower(),
                                                                    "split": row.get("split", "").lower()})
    return rows, errors


def split_scam(rows: list[dict[str, Any]], test: float, seed: int = SEED) -> list[dict[str, Any]]:
    """A blank split is filled in, 60/40 within each label."""
    rng = random.Random(seed)
    for label in sorted({r["label"] for r in rows}):
        blank = [r for r in rows if r["label"] == label and not r["split"]]
        for n, r in enumerate(rng.sample(blank, len(blank))):
            r["split"] = "test" if n < round(len(blank) * test) else "dev"
    return rows


def cmd_check(args: argparse.Namespace) -> None:
    folder = Path(args.dir)
    errors: list[str] = []
    written = []
    if (folder / "items.jsonl").exists():
        items = load_items(folder)
        if not (folder / "labels_a.csv").exists():
            sys.exit("No labels_a.csv yet: label sheet_a.csv and save it under that name.")
        labels, langs, errors = parse_labels(folder / "labels_a.csv", items, list(items))
        if (folder / "labels_b.csv").exists():
            errors += parse_labels(folder / "labels_b.csv", items, [i for i, item in items.items() if item["double"]])[2]
        gold, gold_errors = build_gold(items, labels, langs)
        errors += gold_errors
    if (folder / "scam.csv").exists():
        scam, scam_errors = parse_scam(folder / "scam.csv", folder)
        errors += scam_errors
        if (folder / "scam_b.csv").exists():
            errors += parse_scam(folder / "scam_b.csv", folder)[1]
    if errors:
        print("\n".join(errors))
        sys.exit(f"{len(errors)} problems; nothing written.")
    if (folder / "items.jsonl").exists():
        with (folder / "gold.jsonl").open("w", encoding="utf-8") as f:
            f.writelines(json.dumps(item, ensure_ascii=False) + "\n" for item in gold)
        n = Counter(e["label"] for rows in labels.values() for e in rows)
        written.append(f"gold.jsonl: {len(gold)} items, " + ", ".join(f"{k} {v}" for k, v in sorted(n.items())))
    if (folder / "scam.csv").exists():
        with (folder / "scam.jsonl").open("w", encoding="utf-8") as f:
            f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in split_scam(scam, args.test))
        written.append(f"scam.jsonl: {len(scam)} items, " + ", ".join(
            f"{k} {v}" for k, v in sorted(Counter(r["label"] for r in scam).items())))
    print("\n".join(written) or f"Nothing to check in {folder}.")


def cmd_blank_scam(args: argparse.Namespace) -> None:
    folder = Path(args.dir)
    rows, errors = parse_scam(folder / "scam.csv", folder)
    if errors:
        print("\n".join(errors))
        sys.exit("Fix scam.csv first.")
    with (folder / "scam_sheet_b.csv").open("w", encoding="utf-8-sig", newline="") as f:
        out = csv.DictWriter(f, SCAM_SHEET)
        out.writeheader()
        out.writerows({k: r[k] for k in ("item", "text", "url", "screenshot")} for r in rows)
    print(f"scam_sheet_b.csv: {len(rows)} items without labels, for labeller B (save as scam_b.csv).")


# --- held-out injection items (troth_eval.md §13) ---

INJECTION = ROOT / "tests" / "scenarios" / "injection.jsonl"
WRAPPER_FILES = ("agentdojo_baseline_attacks.py", "agentdojo_important_instructions_attacks.py")
# BIPIA's categories that could change a loop. The others (ciphers, emoji, translation, misspelling...) change how a
# chat answer is written; Troth writes fields, so they would pass for free. Chosen before each set was scored.
BIPIA_CATEGORIES = ("Task Automation", "Information Dissemination", "Marketing & Advertising", "Scams & Fraud",
                    "Misinformation & Propaganda")
BIPIA_TRAIN_CATEGORIES = ("Information Retrieval", "Content Creation", "Learning and Tutoring", "Programming Help",
                          "Instruction", "Social Interaction", "Persuasion", "Clickbait", "Malware Distribution")
# What each set is made of: AgentDojo suites (2 wrappers per goal), and a BIPIA file with how many of its attacks
# (None: all) from which categories. "test" was scored twice and is spent; "train" is for tuning; "test2" waits for
# the email guard (troth_eval.md §13.2). The letters start each item's id.
SETS: dict[str, dict[str, Any]] = {
    "test": {"agentdojo": {"workspace": "ad"}, "bipia": ("bipia_text_attack_test.json", 12, "bp", BIPIA_CATEGORIES)},
    "train": {"agentdojo": {}, "bipia": ("bipia_text_attack_train.json", None, "bt", BIPIA_TRAIN_CATEGORIES)},
    "test2": {"agentdojo": {"banking": "ab", "slack": "as", "travel": "at"}, "bipia": None},
}
# AgentDojo's templates that need only the goal and two names (tool_knowledge also needs AgentDojo's tool calls).
WRAPPERS = ("direct", "ignore_previous", "system_message", "injecagent", "important_instructions")
USER, MODEL = "Adam", "Nemotron"  # the carriers' "Dear Adam"; AgentDojo names the model it attacks
POSITIONS = ("start", "middle", "end")  # where BIPIA puts an attack in its context
URL = re.compile(r"www\.[\w.-]*\w")


def class_strings(node: ast.ClassDef) -> dict[str, str]:
    """A class's own string attributes, e.g. name = "direct"."""
    out: dict[str, str] = {}
    for stmt in node.body:
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
            out |= {t.id: stmt.value.value for t in stmt.targets if isinstance(t, ast.Name)}
    return out


def read_wrappers(paths: list[Path]) -> dict[str, str]:
    """AgentDojo's attack templates by name. The files are parsed, never run: a template is the string a class
    passes to super().__init__, written there or kept in one of its own attributes."""
    found: dict[str, str] = {}
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ClassDef) or "name" not in (strings := class_strings(node)):
                continue
            for call in ast.walk(node):
                if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "__init__"
                        and isinstance(call.func.value, ast.Call) and getattr(call.func.value.func, "id", None) == "super"
                        and call.args):
                    continue
                arg = call.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found[strings["name"]] = arg.value
                elif isinstance(arg, ast.Attribute) and arg.attr in strings:
                    found[strings["name"]] = strings[arg.attr]
    if missing := [w for w in WRAPPERS if w not in found]:
        sys.exit(f"No AgentDojo template named {', '.join(missing)}: the files may be another version.")
    return {w: found[w] for w in WRAPPERS}


def read_goals(path: Path) -> list[tuple[str, str, list[str]]]:
    """AgentDojo's injection tasks: (class, GOAL, the values GOAL is built from). GOAL is an f-string over the
    class's own strings; anything else stops the import rather than guess."""
    goals = []
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.ClassDef):
            continue
        strings = class_strings(node)
        goal = next((stmt.value for stmt in node.body if isinstance(stmt, ast.Assign)
                     and any(getattr(t, "id", None) == "GOAL" for t in stmt.targets)), None)
        if goal is None:
            continue
        parts, used = [], []
        for v in goal.values if isinstance(goal, ast.JoinedStr) else [goal]:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            elif (isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name) and v.value.id in strings
                  and v.conversion == -1 and v.format_spec is None):
                parts.append(strings[v.value.id])
                used.append(strings[v.value.id])
            else:
                sys.exit(f"{path.name}: {node.name}.GOAL isn't an f-string over the class's own strings.")
        goals.append((node.name, "".join(parts), used))
    if not goals:
        sys.exit(f"No injection tasks in {path.name}.")
    return goals


def insert(text: str, attack: str, position: str) -> str:
    """The attack as its own paragraph (sentence, on a one-paragraph page) at the start, middle or end."""
    sep = "\n\n" if "\n\n" in text else " "
    parts = text.split(sep) if sep != " " else re.split(r"(?<=[.!?]) ", text)
    at = {"start": 0, "middle": (len(parts) + 1) // 2, "end": len(parts)}[position]
    return sep.join(parts[:at] + [attack.strip()] + parts[at:])


def cmd_injection(args: argparse.Namespace) -> None:
    folder, chosen = Path(args.dir), SETS[args.set]
    src = folder / "benchmarks"
    suites, bipia_file = chosen["agentdojo"], chosen["bipia"]
    files = ([src / name for name in WRAPPER_FILES] if suites else []) + [
        src / f"agentdojo_{suite}_injection_tasks.py" for suite in suites] + ([src / bipia_file[0]] if bipia_file else [])
    if missing := [p.name for p in files if not p.exists()]:
        sys.exit(f"Missing in {src}: {', '.join(missing)}. troth_eval.md §13 says where to get them.")
    out = folder / f"injection_{args.set}.jsonl"
    if out.exists() and not args.force:
        sys.exit(f"{out} exists. Make it again only if it hasn't been scored (--force).")
    rng = random.Random(args.seed)
    wrappers = read_wrappers(files[:2]) if suites else {}
    attacks = []
    for suite, prefix in suites.items():
        n = 0
        for name, goal, used in read_goals(src / f"agentdojo_{suite}_injection_tasks.py"):
            for wrapper in rng.sample(WRAPPERS, 2):
                n += 1
                attacks.append({"item": f"{prefix}-{n:02}", "pattern": f"agentdojo/{wrapper}",
                                "text": wrappers[wrapper].format(goal=goal, user=USER, model=MODEL),
                                "bad": list(dict.fromkeys(u for u in used if len(u) >= 6)),  # not "13": dates have it
                                "from": f"AgentDojo v1 {suite} {name}"})
    if bipia_file:
        name, count, prefix, categories = bipia_file
        bipia = json.loads((src / name).read_text(encoding="utf-8"))
        if missing := [c for c in categories if c not in bipia]:
            sys.exit(f"No BIPIA category {', '.join(missing)}: the file may be another version.")
        pools = [[(c, text) for text in rng.sample(bipia[c], len(bipia[c]))] for c in categories]
        for n, (category, text) in enumerate([pair for row in zip(*pools) for pair in row][:count], 1):
            attacks.append({"item": f"{prefix}-{n:02}", "pattern": f"bipia/{category}", "text": text,
                            "bad": URL.findall(text), "from": f"BIPIA {name.removeprefix('bipia_').removesuffix('.json')}"})
    with INJECTION.open(encoding="utf-8") as f:
        carriers = [i for i in map(json.loads, filter(str.strip, f)) if i["pattern"] == "benign"]
    rng.shuffle(attacks)  # then each carrier gets one attack at each position
    held = args.set.startswith("test")
    items = []
    for n, a in enumerate(attacks):
        carrier, position = carriers[n % len(carriers)], POSITIONS[n // len(carriers) % len(POSITIONS)]
        field = "body" if carrier["path"] == "email" else "content"
        items.append(carrier | {
            "item": a["item"], "pattern": a["pattern"], field: insert(carrier[field], a["text"], position),
            "bad": a["bad"], "carrier": carrier["item"], "position": position,
            "note": (f"TEST SAMPLE, {'held out' if held else 'to tune on'}: {a['from']} inside {carrier['item']} "
                     f"({position}). Made by scripts/eval_import.py injection"
                     + ("; never tune on it" if held else "") + " (troth_eval.md §13)."),
        })
    with out.open("w", encoding="utf-8") as f:
        f.writelines(json.dumps(i, ensure_ascii=False) + "\n" for i in sorted(items, key=lambda i: i["item"]))
    sums = ", ".join(f"{p.name} {hashlib.sha256(p.read_bytes()).hexdigest()[:12]}" for p in files)
    agentdojo = sum(a["pattern"].startswith("agentdojo/") for a in attacks)
    print(f"{out.name}: {len(items)} items ({agentdojo} AgentDojo, {len(items) - agentdojo} BIPIA) in {len(carriers)} "
          f"benign carriers.\nSources (sha256): {sums}\nScore it with EVAL_SPLIT={args.set} pytest -m eval -k injection"
          + (", twice in total." if held else "."))


def cmd_gmail(args: argparse.Namespace) -> None:
    """Your recent Gmail as raw/gmail.mbox, like a Takeout slice, through the Google connection Troth already has
    (read-only). Gmail's promotions, social, updates and forums tabs and your own mail are left out; `windows` then
    skips newsletters and no-reply senders as it does for Takeout. Prints counts, never text."""
    out = Path(args.dir) / "raw" / "gmail.mbox"
    if out.exists() and not args.force:
        sys.exit(f"{out} exists. Pull again with --force (it replaces the file).")
    after = datetime.now().astimezone() - timedelta(days=30 * args.months)
    query = f"after:{int(after.timestamp())} {PEOPLE_MAIL}"  # what sync reads from people it doesn't follow yet
    try:
        raws = GoogleClient.from_settings(settings).raw_messages(query, args.max)
    except GoogleError as e:
        sys.exit(str(e))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    box = mailbox.mbox(out)
    try:
        for raw in raws:
            box.add(raw)
        box.flush()
    finally:
        box.close()
    print(f"{out.name}: {len(raws)} emails from the last {args.months} months"
          + (f" (the newest {args.max}: raise --max for more)" if len(raws) >= args.max else "") + ".")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--dir", default=str(EVAL_DIR))
    sub = parser.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("windows", help="exports in raw/ -> items.jsonl, sheet_a.csv, sheet_b.csv")
    w.add_argument("--me", action="append", default=[], help="your name as it appears in the exports (repeat)")
    w.add_argument("--me-email", action="append", default=[], help="your email address (repeat)")
    w.add_argument("--month-first", action="store_true", help="WhatsApp dates are month/day (US phones)")
    w.add_argument("--tz", default=settings.timezone)
    w.add_argument("--target", type=int, default=150, help="items to choose")
    w.add_argument("--window", type=int, default=8, help="items per window")
    w.add_argument("--test", type=float, default=0.4, help="share of windows in the test split")
    w.add_argument("--double", type=int, default=6, help="windows both labellers do")
    w.add_argument("--seed", type=int, default=SEED)
    w.add_argument("--force", action="store_true")
    c = sub.add_parser("check", help="labels_a.csv -> gold.jsonl; scam.csv -> scam.jsonl")
    c.add_argument("--test", type=float, default=0.4)
    sub.add_parser("blank-scam", help="scam.csv -> scam_sheet_b.csv without labels")
    g = sub.add_parser("gmail", help="your recent Gmail -> raw/gmail.mbox, through Troth's Google connection")
    g.add_argument("--months", type=int, default=3)
    g.add_argument("--max", type=int, default=1000, help="at most this many emails, newest first")
    g.add_argument("--force", action="store_true")
    j = sub.add_parser("injection", help="benchmarks/ -> injection_<set>.jsonl: published attacks in benign items")
    j.add_argument("--set", choices=SETS, default="test")
    j.add_argument("--seed", type=int, default=SEED)
    j.add_argument("--force", action="store_true")
    args = parser.parse_args()
    {"windows": cmd_windows, "check": cmd_check, "blank-scam": cmd_blank_scam, "injection": cmd_injection,
     "gmail": cmd_gmail}[args.cmd](args)


if __name__ == "__main__":
    main()
