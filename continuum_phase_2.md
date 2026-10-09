# Continuum — Phase 2 Spec: Proactive

**Needs:** every box in Phase 1 §14 checked.

> Phase 1 remembers what's unfinished. Phase 2 **tells you before it slips.**

Most of this phase uses features Hermes already has (cron, Telegram). We add a little code: rules for what needs attention, and snoozing.

---

## 1. Success Story (the demo)

1. Phase 1 state: "Wait for Sarah's response" is due Fri Oct 2.
2. The morning of Oct 2, a Telegram message from Continuum:
   > Good morning. 2 things need you:
   > • **Sarah's reply (NVIDIA)**: due today
   > • **Finish portfolio**: no update in 5 days
3. User replies in Telegram: *"Draft a follow-up to Sarah."* → Hermes writes a short, polite email draft. The user copies and sends it themselves.
4. User: *"Snooze the portfolio till Monday."* → it's hidden until Monday.
5. User, on the go: *"Alex promised me the slides by Thursday."* → a new loop, saved from Telegram.
6. The dashboard shows a **Needs attention** section at the top with the same items. Each has **Snooze** and **Draft follow-up** buttons.

---

## 2. Architecture Changes

```mermaid
flowchart LR
    CRON[Hermes cron<br/>daily 08:00] --> H[Hermes Agent]
    TG[Telegram] <--> H
    UI[Dashboard] --> API[Continuum FastAPI]
    API --> H
    H -->|MCP| E[Continuity Engine]
    API --> E
    E --> DB[(SQLite)]
```

- **No background worker in Continuum.** Hermes cron does the scheduling.
- **Telegram** runs through the Hermes gateway, so it's a second chat interface to the same MCP tools. We write no Telegram code.
- **Follow-up drafts** come from Hermes itself (it's an LLM, and `inspect_loop` gives it the context). No new prompt or endpoint. The dashboard button just sends a chat message.
- **Continuum never sends messages on the user's behalf.** Drafts only.

---

## 3. Scope

**In:** attention rules, daily briefing via Hermes cron → Telegram, snooze, follow-up drafts, Telegram chat, Needs-attention UI.

**Out (Phase 3+):** Gmail/Calendar, sending anything automatically, learned habits, vector search, multi-user.

---

## 4. Data Model Changes

```python
OpenLoop:
    + snoozed_until: date | None
```

That's the only schema change.

Engine: `snooze_loop(id, until)` (`until` must be after today, else `InvalidRequest`) and `unsnooze_loop(id)`. Both bump `updated_at`. `list_loops(include_snoozed=False)` hides loops with `snoozed_until > today`. Extraction still sees snoozed loops, so chat can update or resolve them.

**No migration.** There are no old databases to keep. `create_all` doesn't add columns to an existing table, so delete `data/continuum.db` (and any scratch DBs) after pulling this change.

---

## 5. Attention Rules (`engine.needs_attention(today)`)

Plain rules, no LLM. Open loops only. Skip a loop if `snoozed_until > today`.

| Rule | Condition | Reason text |
|---|---|---|
| Overdue | `due < today` | "overdue by N days" |
| Due soon | `today ≤ due ≤ today+1` | "due today" / "due tomorrow" |
| Stale wait | `kind=waiting`, no `due`, `updated_at` older than `STALE_DAYS` | "waiting N days, no reply" |
| Stale task | `kind in (task, commitment)`, no `due`, `updated_at` older than `STALE_DAYS` | "no update in N days" |

- "Older than `STALE_DAYS`" = at least `STALE_DAYS` days since `updated_at`, by the user's local date.
- Sort: overdue → due soon → stale. Within each group, oldest first (earliest due date; for stale, longest without an update).
- Return `[{loop, reason}]`.
- `today` uses `TIMEZONE`. The function takes `today` as a parameter so tests can fix the date.
- New env: `STALE_DAYS=4`.
- `updated_at` changes whenever a loop is updated (by chat, snooze, or reopen), so mentioning a loop again resets the stale timer. That's intended.

---

## 6. MCP Tools (added)

| Tool | Does |
|---|---|
| `needs_attention()` | Runs the rules for today |
| `snooze_loop(id, until)` | Sets `snoozed_until`. Hermes turns "till Monday" into a date |

Add both to the Hermes `tools.include` list.

**Tool descriptions matter.** Write them so Hermes knows: use `needs_attention` for "what should I do / what's urgent", and use `list_open_loops` for "what am I waiting on / show everything".

---

## 7. Daily Briefing (Hermes cron)

Set up once. Document the exact command in the README:

```bash
hermes cron create "every day at 08:00" "<briefing prompt>" --deliver telegram
```

Keep the briefing prompt in `hermes/briefing_prompt.md`:
- Call `needs_attention`.
- If it's empty, send one line: "Nothing urgent today."
- Otherwise send a short list (≤ 6 items): bold title, (goal), reason. End with: "Reply to snooze, resolve, or draft a follow-up."
- No extra chit-chat.

**Check in Hermes docs:** exact schedule syntax, the timezone the cron uses, and whether the cron platform needs MCP tools enabled (`hermes tools` → cron). Update this section with what works.

What the Hermes 0.21 docs say (not yet run live):
- Command: `hermes -p continuum cron create "0 8 * * *" "<prompt>" --deliver telegram --name briefing`. The README passes `hermes/briefing_prompt.md` as the prompt (bash `$(cat …)`, PowerShell `(Get-Content … -Raw)`). The prompt has no double quotes, because PowerShell 5.1 drops them in arguments; a test enforces it.
- Timezone: top-level `timezone:` in the profile config. `setup_hermes.py` sets it from `TIMEZONE`, so Hermes' clock, cron and Continuum agree.
- Tools: the cron agent gets `platform_toolsets.cron` (default: the full CLI toolset, no MCP). Set to `[continuum]`.
- Delivery: `--deliver telegram` goes to `TELEGRAM_HOME_CHANNEL`; `setup_hermes.py` sets it to the first allowed user id (a DM's chat id).
- Replies: by default a delivered brief isn't part of the Telegram chat, so "snooze the second one" lacks context. `cron.mirror_delivery: true` fixes that.
- The gateway runs the scheduler (checks every 60 s); it must be running. `hermes -p continuum cron run <id>` fires a job on the next check.

---

## 8. Telegram

- Set up the Hermes Telegram platform (bot token from @BotFather). Allow only the user's own chat id.
- Telegram and the dashboard share the same MCP tools, so they always show the same state.
- Put setup steps in the README. Token goes in Hermes config/env, **never** in this repo.

What works (Hermes 0.21 docs):
- The user sets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USERS` (numeric ids, from @userinfobot) in Continuum's gitignored `.env`. `setup_hermes.py` validates them and copies them into the profile `.env`. Unlisted users are denied by default.
- `gateway.standalone: true` runs the profile's own adapters, so `hermes -p continuum gateway run` serves the API server and Telegram together. Polling: no public URL.
- **`platform_toolsets.telegram: [continuum]` is required.** Without it, Telegram gets Hermes' default toolset (terminal, files, web) and no MCP tools. A test checks every platform in `hermes/config.example.yaml` is `[continuum]` only.
- One bot token can't be polled by two running gateways: use a bot just for Continuum.
- Telegram keeps one long session (until `/new`), so `SOUL.md` tells Hermes to call the tools again instead of answering from earlier messages.

---

## 9. REST API (added)

```http
GET  /api/attention               → [{loop, reason}]
POST /api/loops/{id}/snooze       {until: "2026-10-05"}
POST /api/loops/{id}/unsnooze
```

`GET /api/loops` hides snoozed loops by default. `?include_snoozed=true` shows them. A snooze date that isn't after today → 422 `invalid_request`.

---

## 10. UI Changes

```text
┌─ Needs attention (2) ─────────────────────────────────────┐
│ ⚠ Sarah's reply · NVIDIA · due today   [Draft] [Snooze ▾] │
│ • Finish portfolio · no update 5 days  [Draft] [Snooze ▾] │
└───────────────────────────────────────────────────────────┘
  ... Phase 1 layout below ...
```

- Snooze menu: Tomorrow / In 3 days / Next Monday / Pick date.
- **Draft** sends `Draft a follow-up for loop <id>` to `/api/chat`. The reply shows in the chat panel with a **Copy** button.
- Snoozed loops show "💤 until Mon" when "show snoozed" is on.
- Hide the section when it's empty.

---

## 11. Tests

- **Attention rules**, with a fixed `today`: each rule fires, the boundaries (`due == today`, exactly `STALE_DAYS`) behave, snoozed loops are skipped, resolved loops never appear, and the sort order is right.
- **Snooze:** set/unsnooze, `updated_at` bumps, the list filter hides snoozed loops.
- **API:** new endpoints.
- **Manual:** trigger the cron job now (`hermes cron run <id>` or the docs equivalent) → the Telegram message arrives.

---

## 12. Build Order

1. `snoozed_until` + tests.
   *Done 2026-10-02: column, `snooze_loop`/`unsnooze_loop`, `list_loops(include_snoozed=False)`. Delete old `.db` files (no migration). Startup stops with a clear message if the `.db` is outdated.*
2. `needs_attention()` + tests.
   *Done 2026-10-02: the 4 rules in §5, `STALE_DAYS` setting. Tests use a fixed `today`, pass under any `TIMEZONE`. Not exposed yet (REST in step 3, MCP in step 4).*
3. REST endpoints + UI section.
   *Done 2026-10-02: §9 routes. UI: Needs attention section, Snooze menu (also in loop detail, plus Unsnooze), Show snoozed toggle, Draft with a Copy button. Draft needs Hermes to give a reply.*
4. MCP tools → check in Hermes chat: "what's urgent?" / "snooze X till Monday".
   *Code done 2026-10-02: `needs_attention`, `snooze_loop` (+ `list_open_loops(include_snoozed)`), in `tools.include`, SOUL.md rules for urgent / snooze / drafts. Offline MCP tests pass. Live check done 2026-10-02 through real Hermes + Nemotron: capture → `remember`, "what's urgent?" → `needs_attention`, "snooze the portfolio till Monday" → `snooze_loop` (Mon Oct 5), "draft a follow-up to Sarah" → `inspect_loop` and nothing saved, "Sarah got back to me!" → `resolve_loop`. It first failed because the Hermes profile was still Phase 1's: re-run `scripts/setup_hermes.py` after pulling tool or SOUL.md changes.*
5. **Telegram gate:** chatting with Continuum through Telegram works.
   *Config done 2026-10-02 (§8). Live check done 2026-10-02: capture → `remember`, urgent → `needs_attention`, snooze → `snooze_loop`, draft → `inspect_loop` with no writes. The first message was blocked because `TELEGRAM_ALLOWED_USERS` held the bot's own id (the start of the token); setup now rejects that.*
6. Cron briefing → trigger it by hand, check delivery. Then schedule it.
   *Code done 2026-10-02: `hermes/briefing_prompt.md`, cron toolset, timezone, home channel, README command (§7). Triggered by hand 2026-10-02, both delivered to Telegram: empty day → "Nothing urgent today.", 2 items → the §7 list. Scheduled run seen 2026-10-03 (a one-off job at 22:30): fired on its own and was delivered with the right item. It only fires while the gateway runs and the PC is awake.*
7. Run the §1 demo. Add a Phase 2 section to the README.

---

## 13. Done Checklist

- [x] Briefing arrives on Telegram at the scheduled time, with correct items
- [x] Empty day → "Nothing urgent today."
- [x] Snooze via Telegram and dashboard; snoozed loops come back after the date
  *Telegram checked live (step 5); come-back tested offline (`test_attention.py`). Dashboard checked live 2026-10-09: Snooze → Tomorrow hides the loop from both lists and saves the date.*
- [x] "Draft a follow-up" works in both, and nothing is sent automatically
  *Telegram checked live (step 5). Dashboard checked live 2026-10-09 with real Hermes: Draft shows a reply, Copy copies it, nothing saved or proposed.*
- [x] New loops can be captured from Telegram
- [x] `pytest` passes offline
- [x] No bot token or chat id in git
