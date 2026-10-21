# Troth — Plan: see what your promises depend on

**Status:** C1–C3 plus a "Needs: …" line in loop detail built 2026-10-19 (decisions in §7, results in §6 and §10). Still to check live: Telegram and Hermes. C4's chain editor, loop editing (C4), export and delete-everything (C5) wait. D3 is already built.

**Source:** a moat-strategy note (retired 2026-10-20), reviewed and mapped onto Troth. The product keeps the name Troth (renamed from Continuum on 2026-10-11), its users and its stack.

**Goal:** Troth links each promise to what it depends on. When an upstream item slips, Troth flags the promise at risk, says why, and offers a fix, before the user lets someone down.

---

## 1. Review of the strategy doc

**What it gets right, and Troth lacks today:**
- **Dependencies.** Loops are isolated: one goal, one person, nothing else. "Send the application" can't know it needs "Dr. Lim's reference letter".
- **Change propagation.** A new fact already updates the loop it's about (Dr. Lim's letter now due Wed). Nothing tells the promise that depends on it (application due Fri).
- **Prevention, not reminders.** Needs attention says "due tomorrow". It doesn't say "this will slip because X moved", and it offers no recovery.
- **Correcting memory.** Today you can undo, delete, resolve or snooze a loop. You can't edit its title, date or person.
- **Portable memory.** There's no export and no "delete everything".

**Already in Troth (no work needed):** Nemotron extraction with source words, persistence, approval before any external action, an audit log (Activity and PendingAction with statuses), proactive delivery (8:00 briefing, sync every 10 min to Telegram), and asking back when something is unclear (P2b in the plan addendum).

**What doesn't fit, and why we skip it:**

| Doc suggests | Troth keeps | Why |
|---|---|---|
| Next.js + Tailwind | React + Vite | Already built (PRODUCT.md) |
| PostgreSQL + pgvector | SQLite, in-context lists | Phase 3 rules out vector search; under ~500 loops it isn't needed |
| APScheduler / Nebius Serverless | Hermes cron | Already schedules the briefing and sync (personal AI plan §7) |
| Numeric risk score | Reasons from fixed rules | An uncalibrated score is a claim we can't back. The doc says so too (§5) |
| Personal reliability model | Plain counts, stretch (§5 S1) | Needs weeks of real history; seeded history would be the "simulated" result the doc warns against |
| `UserPolicy` entity | `platform_toolsets` + approval gate; OpenShell is P4 | Already enforced in config and code |
| Freelancers as primary users | Students and job seekers in Malaysia | PRODUCT.md; the same story works with applications and reference letters |
| "Don't be a broad agent" | Keep ScamGraph | One story covers both: nothing you owe slips, not a promise and not a payment to a fake |

The "moats" are a pitch line, not code. Judges score the demo and track fit. This plan builds the one moat that shows in a demo: the commitment graph with prevention.

---

## 2. The story (demo and acceptance test)

1. Telegram: *"I promised Sarah my NVIDIA application by Friday. Dr. Lim is writing my reference letter, and I still need to finish my portfolio."*
   → commitment "Send NVIDIA application" (promised to Sarah, due Fri), which needs "Get reference letter from Dr. Lim" (waiting) and "Finish portfolio" (task).
2. Dr. Lim emails: *"Sorry, I can only send the letter next Wednesday."*
   → sync updates his loop's due date to Wed. Code, not the model, sees that the application now can't make Friday.
3. Telegram, unprompted, from the sync job:
   *At risk: "Send NVIDIA application" (Fri Oct 16): Dr. Lim's reference letter is now due Wed Oct 21.*
   *Options: ask Dr. Lim if Thursday works · tell Sarah early and offer Wed · move your date.*
4. "Tell Sarah" → Hermes writes the email → Gmail draft proposal in Sarah's thread → "yes" → saved in Drafts, not sent.
5. Dashboard: the application tops Needs attention with that reason. Its detail shows the chain, and the history quotes Dr. Lim's email.
6. Dr. Lim's letter arrives → *Ready: "Send NVIDIA application" (the reference letter arrived).*

---

## 3. Design

### 3.1 Data

```python
class Dependency(SQLModel, table=True):
    """`loop_id` can't be finished before `needs_id`. Only stated links: from a message, an email or the user."""
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    loop_id: UUID = Field(foreign_key="openloop.id", index=True)
    needs_id: UUID = Field(foreign_key="openloop.id", index=True)
    source_id: UUID | None = Field(default=None, foreign_key="source.id")  # None = added in the dashboard
    created_at: datetime = Field(default_factory=utcnow)
```

- **A new table, not a column.** `create_all` adds it to existing DBs, so no reset is needed. It also allows several upstream loops ("application needs letter and portfolio").
- **`Activity.source_id`** (the message behind a change) lets the history quote the words that moved a date. Email changes already store the Gmail link. Chat changes store only field names. This is a new column on an existing table, so old `.db` files must be deleted (D3).
- **"Promised to" on commitments:** reuse `waiting_on` and `person_id` for commitment loops. The UI labels it "Promised to" for commitments. No new column (D4). This lets recovery drafts reach the right person, and lets that person's emails reach the loop.
- `delete_loop` also deletes the loop's dependencies. Dependency changes are logged as `updated` with detail `needs "<title>"`, without undo (removing the link is the undo).

### 3.2 Extraction (`app/extraction.py`)

- JSON shape: a top-level `links: [{"loop": …, "needs": …}]`, each end an existing loop's id or the exact title of a loop in `new_loops`.
  *Changed 2026-10-19 from `new_loops[].needs`: live, the model only filled `needs` when it happened to write the needed loop first (5 of 8 runs). A list written after every loop doesn't depend on order.*
- New rule 9: links only when the message says one thing has to happen before another ("once", "after", "first", "needs", "depends on", "can't … until"), or that something is *for* another loop ("my reference letter for it"). Never from a shared topic, goal or person.
- `waiting_on` now covers both kinds: who they wait on (waiting) or who they promised (commitment), with the known-people rule for both. *(Written only for commitments at first, it made the model drop known full names, e.g. "Sarah" for "Sarah Chen": 3 of 12 live runs failed vs 0 of 12 before; shared, 16 of 16 passed.)*
- An existing loop can also be named by its exact title (the model sometimes does), but only within the message's scope.
- **Code guard** in `process_message`, same transaction: resolve each ref, then drop unknown refs, self-links, duplicates and anything that would make a cycle. Log `dependency_dropped reason=…`. An email may link only its in-scope loops and its new loops.
- Still one LLM call per message. Risk detection adds no LLM call.

### 3.3 Risk rules (`app/engine.py`, plain code)

`dependency_risks(session, today)` walks the links over **all** open loops (snoozed upstream loops still block):

- `ready_by(L)` = the latest due date across L's open upstream chain.
- **blocked:** an upstream loop in the chain is overdue → *"blocked: Dr. Lim's reference letter is overdue"*.
- **at risk (date):** `ready_by(L) > L.due` → *"at risk: Dr. Lim's reference letter is due Wed Oct 21, after this"*. Add *"(via X)"* when the cause is two or more steps up.
- **at risk (no date):** an upstream loop has no due date and L is due within 3 days → *"at risk: Dr. Lim's reference letter has no date yet"*.
- A resolved upstream loop counts as done. Rules use dates and stated links only. Silence never means "not done" (doc §5 rule).

`needs_attention` order becomes: 0 risky check, 1 overdue, **2 dependency**, 3 due soon, 4 stale. A loop that is both overdue and blocked gets both reasons, joined with "; ". `Attention` gains `cause: OpenLoop | None` and `options: list[str]`.

**Recovery options are built in code**, like ScamGraph's next steps, so they are fixed and explainable:
- The cause is a `waiting` loop with a person → "Ask {person} if {L.due weekday} works".
- L is a commitment with a person → "Tell {person} early and offer {ready_by}".
- Always → "Move your date", "Block time on {day}".

**Propagation:** `process_message` computes `dependency_risks` before and after the write. `ChangeSet` gains `at_risk` (new or changed reasons) and `ready` (loops whose last open upstream loop just closed).

### 3.4 Surfaces

| Where | Change |
|---|---|
| `remember` (MCP) | Returns `at_risk: [{title, reason, options}]` and `ready: [titles]` |
| `needs_attention` (MCP) | Items carry `options` when a dependency caused them |
| `inspect_loop` (MCP) | `needs` and `needed_by`: title, due, status |
| `scripts/sync.py` | `describe_email` adds "At risk: …" and "Ready: …" lines, so they reach Telegram unprompted |
| `SOUL.md` | `at_risk` → one line per item with its reason, then its options; draft or propose only after the user picks one. Say "at risk", never "will fail". "What's blocking X?" → `inspect_loop`. "X needs Y first" → `remember`. |
| Briefing skill | No change: the reason text carries the cause |
| `/api/attention`, `/api/loops/{id}` | `cause`, `options`, `needs`, `needed_by` |
| New API | `PATCH /api/loops/{id}` (title, due, kind, person; logged with undo), `PUT` / `DELETE /api/loops/{id}/needs/{needs_id}`, `GET /api/export`, `POST /api/data/forget` |
| Dashboard | Loop detail: "Needs" and "Needed by" rows as a small inline chain (entries with due dates, arrows, no graph lib), "+ Needs…" picker, × to remove, Edit form (`<input type="date">`). Attention: the cause title opens that loop. Settings: Export my data, Delete everything (confirm). Follow DESIGN.md and `.impeccable/surfaces/`, then rebuild `app/static`. |

No new MCP tool: `remember` creates links from chat. `tools.include` stays the same. Re-run `setup_hermes.py` after the `SOUL.md` and docstring changes.

### 3.5 Memory controls

- **Export:** one JSON file, `troth-export-YYYY-MM-DD.json`, with goals, loops, dependencies, people, sources, activity, actions and check results. Settings rows stay out. The Google token is a file and never leaves the machine.
- **Delete everything:** all rows and uploaded screenshots. Google stays connected until you disconnect it (the dialog says so). Built next to "Forget email data".

---

## 4. Build order

| Step | What | Effort |
|---|---|---|
| C0 | Settle §7, add this plan to AGENTS.md's spec list | 15 min |
| C1 | `Dependency` table, `Activity.source_id`, promised-to, extraction `needs` + guard, delete cascade. Offline tests. | ½ day |
| C2 | `dependency_risks`, `needs_attention` order and reasons, recovery options, `ChangeSet.at_risk/ready`, sync lines. Offline tests. | ½ day |
| C3 | MCP outputs, `SOUL.md` rules, setup script. Live check through Hermes on a scratch DB (§2 steps 1–4 by chat, then by email). | ½ day |
| C4 | API + dashboard: chain, links, edit, attention cause. `npm run build`, checked in a browser. | 1 day |
| C5 | Export + delete everything (API, Settings, tests) | 2 h |
| C6 | Eval scenarios + live test, numbers recorded here (§6) | ½ day |
| C7 | README "What it depends on" section, demo script (§8), Devpost text | ½ day |

About 4 days in total. That leaves room for P3 deploy (issue #4) and the video before the deadline (third-party listings give Oct 30; confirm on Devpost).

---

## 5. Stretch (after the video)

- **S1 Track record:** per person, how many of their waiting loops closed on time and how late the rest were ("Dr. Lim: 2 of 3 late"). For the user: an on-time rate per kind. Shown as facts in the person panel and `inspect_loop`, never as a probability. Real history only.
- **S2 Person notes:** "Sarah prefers short, formal emails" → `set_person_note`; drafts read it. This is the doc's relationship-aware follow-ups.
- **S3 Graph view:** reuse the check page's Cytoscape graph for goals → loops → dependencies.
- **S4 Proposal outcomes:** count approved vs rejected proposals by kind in Settings. Learning from them comes after the hackathon.

---

## 6. Evaluation

*Measured 2026-10-19, final prompt, extraction only against real Nemotron, 8 runs each (synthetic, labelled test messages):*
| Message | Linked | Right direction |
|---|---|---|
| "…Dr. Lim is writing my reference letter for it" | 7/8 | 7/7 |
| "…I can only send it once Dr. Lim sends my reference letter" | 8/8 | 8/8 |
| Same-topic control: NVIDIA application + NVIDIA hackathon | 0/8 (no false link) | — |

*`tests/test_dependency_live.py` (story + delay email, link to an existing loop, same-topic control): 3/3 in each of 3 runs. Live acceptance + email tests: 11/11 twice.*

*Real email, 2026-10-20 ([troth_eval.md](troth_eval.md) §15): 46 of your emails, labelled, 18 held out; before → after the two fixes it found (email tasks linked to their sender, bulk mail isn't tasks):*

| Measure (5 runs each) | Tuning, 28 emails | Held out, 18 emails |
|---|---|---|
| Emails handled fully right: before → after | 48% → 67% | 19% (7–42%) → **52% (31–73%)** |
| Right in most runs (McNemar) | 14 → 20 (p = 0.03) | 3 → 10 (p = 0.016) |
| Loops found (recall) | 76% → 69% | 48% → 55% |
| Troth's loops that are in the answer key (precision) | 34% → 47% | 12% → 22% |
| Nothing-to-track emails where Troth made a loop | 8.6 → 4.4 of 18 | 8.4 → 3.8 of 11 |
| Closes found | 0 → 1 of 1 | 0 → 1 of 1 |
| Due date right, on loops found | 98% → 100% | 95% → 100% |

Synthetic scenarios, clearly labelled. Numbers go here and in the README only after a run.

| Measure | How |
|---|---|
| Link precision / recall / direction | `tests/test_commitment_live.py` (`-m live`), 5 scripts × 3 runs: reference letter; two upstream loops (transcript + essay before a scholarship); **same topic, no link** (NVIDIA application vs NVIDIA hackathon); delay with no date; upstream done → ready |
| Deadline handling | The delay email moves the upstream due date and nothing else; "no date" invents none |
| Risk detection | Offline: every rule, chains, cycles, snoozed upstream, ordering. Live: §2 step 3 appears |
| Intervention usefulness | Real approved / rejected counts only (S4); no made-up ratings |
| Permission compliance | Existing tests: nothing sent, proposals need a yes; add one for the recovery draft |
| Memory persistence | Phase 1's restart check still applies; add tests for export, then delete everything |

---

## 7. Decisions for you

*Settled 2026-10-19, all as recommended:*
- *D1: a Phase 4 addendum (listed in AGENTS.md).*
- *D2: students and job seekers, the §2 reference-letter story.*
- *D3: built 2026-10-19.*
- *D4: reuse `waiting_on` / `person_id` for commitments, labelled "Promised to". Known effect: email sync then also reads mail from people the user promised something to, since it reads every person linked to an open loop.*
- *D5: in scope, scoped size: C1–C3 plus one "Needs: …" line in loop detail. The link editor and chain view (C4), loop editing, export and delete-everything (C5) wait.*

- **D1 Where this plan lives.** *Recommended:* an addendum to Phase 4, like the personal AI plan, so the video can show it. A Phase 5 couldn't start until Phase 4's deploy and video boxes are checked (AGENTS.md), so it would land after submission.
- **D2 Users and story.** *Recommended:* keep students and job seekers (PRODUCT.md) and §2's reference-letter story. It continues the Sarah/NVIDIA demo. The doc's freelancer release story would need new demo data and a new audience.
- **D3 `Activity.source_id`.** *Built 2026-10-19 (the 2026-10-19 audit, item 3).* *Recommended:* add it. The history then quotes the words that moved a date. Cost: delete your local `.db` once (the deploy starts fresh anyway). Without it, only email changes show their source (a Gmail link).
- **D4 Promised-to person.** *Recommended:* reuse `waiting_on`/`person_id` for commitments, labelled "Promised to". A new column is cleaner, but it touches the UI, prompts and tests for no demo gain.
- **D5 Out-list check.** Phase 1 deferred "knowledge graph" and "auto follow-ups". Phase 2 deferred "learned habits". Phase 4 rules out "Neo4j or any graph DB". This plan uses one SQLite table, sends nothing, and leaves learning to S1, so it stays inside those lines. Confirm.

---

## 8. Demo video change

Replace the 2:20 segment ("Sarah's email closes her loop; approve a calendar proposal") with §2 steps 2–5. It shows email, proactive Telegram, fixed rules, a draft and approval in the same 25 s, and it's the doc's demo moment: a new fact reaches an old promise. To set it up, the 0:15 message becomes §2 step 1, so the chain exists from the start.

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| Nemotron links the wrong way round ("A needs B" read as B needs A) | Examples in rule 9, a live direction measure, one-click remove/add in the dashboard |
| Links from topic alone (false alarms) | "Only when stated" rule, the same-topic control scenario, cycle and unknown-ref guard |
| Alert noise on every sync | Report only new or changed reasons (before/after diff), not every run |
| Wrong weekday in options | Dates come from code (`ready_by`, `due`), never from the model |
| DB reset (D3) wipes local test data | Use a scratch DB for tests (AGENTS.md); export first (C5 lands before you need it) |

---

## 10. Done checklist

- [x] §7 decisions settled and recorded here; plan added to AGENTS.md
  *Done 2026-10-19.*
- [x] A message with "once / after / needs" creates linked loops; unstated links aren't created; cycles are dropped
  *Done 2026-10-19: `tests/test_dependencies.py` (guards), `tests/test_dependency_live.py`; live link rates in §6.*
- [x] An upstream delay (chat or email) flags the dependent promise with a reason naming the cause and its date
  *Done 2026-10-19, offline and live (an email from Dr. Lim moving the letter to Oct 7 → the application at risk).*
- [x] The sync job posts "At risk" and "Ready" lines ~~to Telegram~~ unprompted
  *Code done 2026-10-19 (`sync_email` adds `risk_lines`), tested offline. Re-scoped 2026-10-21: the user doesn't use Telegram. `AUTO_SYNC_MINUTES` runs the same sync inside Troth, and Needs attention shows "at risk" with its options and "ready" for 3 days (`ready_loops`), unprompted; tested offline (`test_dependencies.py`, `test_google_actions.py`).*
- [ ] Recovery options come from code; a chosen option becomes a draft or proposal that waits for a yes
  *Options from code: done (`_options`). Picking one goes through Hermes' existing draft/proposal tools via the SOUL rule; not checked live through Hermes yet.*
- [x] Dashboard shows the chain, lets you add/remove links and edit a loop (with undo)
  *Done 2026-10-21: loop detail "What it depends on" (Needs first → this → Then), × removes a link, "+ Needs…" adds one (a circle is refused), entries and Needs attention's "Because of" open that loop. `PUT` / `DELETE /api/loops/{id}/needs/{needs_id}` (`engine.add_need`, `remove_need`), logged in History. A removed link's undo is adding it back; edits keep their Undo. Checked in headless Chrome on a scratch DB.*
- [x] History quotes the words that moved a date (D3)
  *Done 2026-10-19 (audit item 3).*
- [x] Export downloads all your data; Delete everything empties it
  *Done 2026-10-21: `GET /api/export`, `POST /api/data/forget` (`engine.export_data`, `delete_everything`; refuses while a check runs; keeps the last email sync time so old mail isn't re-read), Settings → All your data, `tests/test_data.py`.*
- [x] `pytest` passes; `pytest -m live` scenario numbers recorded in §6
  *Live link and acceptance numbers in §6 (2026-10-19); real-email numbers in §6 and [troth_eval.md](troth_eval.md) §15. A full `pytest -m live` needs Hermes, so it runs on the server or the home PC before submitting.*
- [x] §2 checked live through Hermes ~~and Telegram~~ on a scratch DB
  *Hermes done 2026-10-19 on the deployed VM (2c54414), dashboard-chat endpoint, own conversation: the link was saved; "the letter will only be ready on October 28" got at risk with the three options from code; "what's blocking", "what's urgent" and "snooze till Monday" (Mon Oct 26) all right. The two test loops were deleted after. The email step (Dr. Lim's delay email → at risk) is checked live in `tests/test_dependency_live.py`: sync is Troth's code, not Hermes. Telegram dropped 2026-10-21 (the user doesn't use it).*
- [ ] README, demo script and Devpost text updated
