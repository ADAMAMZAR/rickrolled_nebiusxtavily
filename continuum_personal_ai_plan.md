# Continuum — Plan: one Personal AI, not two apps

**Status:** P0 and P1 built 2026-10-10 (D1–D3 taken as recommended). P2–P4 not started. Open: the Devpost text, "Built during the hackathon", and a live run through Hermes (§8).

**Goal:** Personal AI track judges see one assistant. ScamGraph keeps its name and all its code. It becomes the step Continuum takes before you pay, not a second app.

---

## 1. The problem

- The README, the UI and the demo show two products. Continuum tracks loops. ScamGraph is a separate page with its own name and header, plus a "Back to Continuum" link.
- Nothing connects the two:
  - a check never becomes a loop;
  - a loop can't be checked;
  - Needs attention and the morning briefing never mention a check;
  - `Investigation.loop_id` exists, but nothing sets it ("Track this" is still P1 in [Phase 4 §9](continuum_phase_4.md)).
- So ScamGraph looks like a fraud checker (an entry for the Best Apps track) attached to a personal assistant. A judge will ask why it's here.

## 2. The fix in one line

> **Continuum remembers what you owe and what you're owed, and checks who's asking before you pay.**

A request for your money or details is an open loop too, and it shouldn't close until you know who's asking. Every loop goes through the same five steps:

| Step | What happens | Built? |
|---|---|---|
| **Capture** | Chat, Telegram, email from people you're waiting on, a forwarded message or screenshot | ✓ |
| **Remember** | Goals, loops, people and the exact words they came from, in one SQLite file | ✓ |
| **Check** | A loop asks you to pay or share details → ScamGraph researches who's asking; fixed rules score the risk | ScamGraph ✓, link to loops ✗ |
| **Remind** | Needs attention + the 8:00 Telegram briefing, risky loops first | risky loops ✗ |
| **Close** | Your "done", email replies, web watch; drafts and events wait for your yes | ✓ |

**A day with Continuum** (README story and video, §5):
- 8:00, Telegram: *"2 things need you: Sarah's reply is due tomorrow; your portfolio has had no update in 5 days."*
- 9:12: you forward a "T. Rowe Price" message asking for a deposit by Friday. Continuum saves the loop and asks: *"Want me to check who's asking first?"*
- 9:14: **CRITICAL**. T. Rowe Price's real site is troweprice.com, not the link in the message, and Bank Negara lists T. Rowe Price clones. The loop moves to the top: hold off paying.
- 15:40: Sarah emails back. Her loop closes on its own. The interview time waits as a calendar proposal until you say yes.
- Next morning, the briefing still lists the deposit until you mark it done.

## 3. Track fit today

| The track asks for | Continuum today | Gap → fix |
|---|---|---|
| Always on | Hermes gateway: Telegram, 8:00 briefing, sync every 10 min | Only runs while the laptop is on → P3 |
| Private, your data | One local SQLite file. Hermes' own memory is off. Only Continuum's tools (no terminal, files or browser). Logs hold no content. It never sends anything. Our server never opens a suspicious link. | Only explained at the bottom of the README → P0 |
| Persistent memory | Goals, loops, people, sources, history with undo, checks | Checks don't reach the loops → P1 |
| Reusable skills | Rules live in `SOUL.md`; nothing packaged | → P2 |
| Tools you choose | MCP tools; Google, Tavily and Telegram are each opt-in | Not stated anywhere → P0 |
| Tasks across daily workflows | Drafts, Gmail drafts, calendar events, web watch, email auto-close, all waiting for your yes | ✓ |
| NVIDIA model on Nebius | Nemotron 3 Super for Hermes, extraction and every ScamGraph step (Gemma 3 only reads screenshots) | Show it in the video |
| NemoClaw, OpenShell, Hermes, Nebius Serverless | Hermes Agent | OpenShell: stretch, P4. Serverless: skip (§7) |

---

## 4. Changes, in order

Each tier works on its own, so you can stop at any tier. No schema change: `Investigation.loop_id`, `Source.kind` and `Source.url` already exist.

### P0 Story, copy, submission (no logic, ~half a day)

1. **README top.** Replace the first section with:
   - the tagline: *"A personal AI that remembers what's unfinished, and checks who's asking before you pay."*;
   - the five steps and "A day with Continuum" (§2);
   - the §3 table, with links to files;
   - a short "Your data" list, moved up from Privacy.

   The ScamGraph section becomes **Check before you pay (ScamGraph)**, under the Check step, with its five steps unchanged. In the mermaid diagram, draw Engine, ScamGraph, MCP and SQLite inside one "Continuum (one process)" box. That's accurate, and it shows one system.
2. **README sections the submission requires and we don't have yet:**
   - **Where Token Factory helped.** Hermes and our own calls share one OpenAI-compatible endpoint, and a model switch is one env var. That let us time three Nemotron models ([Phase 4 §0.6](continuum_phase_4.md)) and keep Super.
   - **Feedback.**
     - No Nemotron model on Token Factory reads images (Nano, 3.5 Lightning, Super and Ultra all return 400), so screenshots go to Gemma 3.
     - Super takes 11–37 s per call. That's why a check takes 1–2 minutes.
     - JSON mode sometimes wrote `"behaviors"` for `"behaviours"`.
     - Add anything else the team hit.
   - **Built during the hackathon.** Check the submission period. If the repo started before it, list what changed, with dates from the phase specs.
3. **One app shell.** `investigate.html` gets the dashboard's header (loop icon + "Continuum"), the subtitle "Check a message · ScamGraph", nav links "Your loops" and "New check", and the tab title "Check a message · Continuum". The dashboard chat greeting gets one line: *"Got a message asking you to pay? Paste it and ask 'is this legit?'"*
4. **Devpost text:** the same tagline and five steps.

### P1 Connect checks and loops (half a day to a day; this is Phase 4 §9 "Track this")

5. **A risky check saves itself as a loop.** At the end of `run_investigation` ([app/investigation.py](app/investigation.py)), when the level is ELEVATED, HIGH, CRITICAL or INSUFFICIENT_EVIDENCE and `loop_id` is empty:
   - Create a source with `kind="investigation"`, `text=input_text` and `url="/investigate.html#<id>"`.
   - Create a task loop **"Verify <first org, else domain, else the sender> before paying"**, with `next_action` set to the first next step. Skip it if an open loop with that title already exists (re-checks).
   - Set `loop_id` in the same commit as "Done".
   - `_reply` in [app/mcp_tools.py](app/mcp_tools.py) adds one line, built in code: *"On your list: …. It stays under Needs attention until you resolve it."* The result page shows "On your list: …" with a link to the dashboard (the API returns `loop_title`).
   - LOW and GUARDED add nothing. This replaces the spec's "Track this" button (D1).
6. **Risky loops go first in Needs attention.** Add a first rule to `needs_attention` in [app/engine.py](app/engine.py):
   - an open loop whose latest check is ELEVATED or above → e.g. `high risk, hold off paying`;
   - INSUFFICIENT_EVIDENCE → `sender not verified, hold off paying`.

   Read `Investigation` from `app.db`. The engine can't import `investigation.py`, because that would create an import cycle. The 8:00 briefing calls `needs_attention`, so it lists these loops without a prompt change. In `index.html`, show these reasons in red like "overdue", and show `investigation` sources in loop detail as "Checked message · See the check".
7. **Check a loop.** For example, "Pay RM2,000 deposit to Sunway Homes by Fri".
   - `create_investigation(..., loop_id=None)`, and `POST /api/investigations` accepts `loop_id` (404 for an unknown loop). A check started from a loop links to that loop and never adds a new one.
   - A **Check who's asking** button in loop detail posts the loop's source text with its id, then opens `/investigate.html#<id>`.
   - MCP: `investigate(text, loop_id=None)`.
   - New `SOUL.md` rule: *"When `remember` saves a loop about paying money or sharing personal details (bank account, OTP, IC number) with a company, person or website, ask once: 'Want me to check who's asking first?' On yes, call `investigate` with the user's message and that loop's id."* This is prompt-only. Add a flag in code only if live runs show Nemotron skipping the offer.
   - Re-run `setup_hermes.py`.
8. **Tests:**
   - [tests/test_investigation.py](tests/test_investigation.py): HIGH saves one linked loop; LOW, a re-check and a check started from a loop save none.
   - [tests/test_attention.py](tests/test_attention.py): the new rule and its order.
   - `loop_id` in the API and the MCP tool.
   - Then run `pytest`, and `pytest -m live -k "scam_live or hermes"` for the reply and SOUL changes.

### P2 Reusable skills (~2 h, gate first)

9. **Gate (30 min).** Put a test `SKILL.md` in the continuum profile's `skills/` folder, restart the gateway and send `/<name>` on Telegram. It passes if the skill loads while `platform_toolsets` stays as it is (Continuum's tools only). If it fails, skip P2: `SOUL.md` already covers this.
10. **Ship two skills:**
    - `hermes/skills/check/SKILL.md`: `/check <message>` calls `investigate` with the message word for word and sends `reply` unchanged.
    - `hermes/skills/briefing/SKILL.md`: today's `briefing_prompt.md`, which then gets deleted.

    `setup_hermes.py` copies `hermes/skills/` into the profile, the same way it copies `SOUL.md`. The cron job becomes:

    ```
    hermes -p continuum cron create "0 8 * * *" "Send my morning briefing." --skill briefing --deliver telegram --name briefing
    ```

    If the profile got Hermes' bundled skill catalog, opt out so the Telegram menu shows only Continuum's skills. `SOUL.md` keeps its short "is this legit?" rule: the skills toolset stays off, so the model can't load a skill by itself.

### P3 Always on ([issue #4](https://github.com/ADAMAMZAR/rickrolled_nebiusxtavily/issues/4), Phase 4 §11)

11. Run the planned Nebius VM: uvicorn and `hermes -p continuum gateway run` as services, Telegram on, the briefing and sync cron jobs, `APP_PASSWORD`, `PUBLIC_URL`, a fresh DB and Google off. Then remove the README limit "only work while Hermes' gateway is running on your machine". Do this before recording, so the video can say "running on Nebius".

### P4 Stretch: Hermes inside OpenShell (only after the video)

12. NemoClaw's Hermes path ([quickstart](https://docs.nvidia.com/nemoclaw/latest/get-started/quickstart-hermes.html)) runs Hermes in an OpenShell sandbox with a network policy.
    - **Why it fits:** Continuum reads hostile text on purpose. If the sandbox only lets Hermes reach Nebius, Telegram and Continuum's MCP, even a successful prompt injection can't get anywhere else.
    - **Unknowns from the docs:**
      - Nebius isn't a listed provider. The generic OpenAI-compatible "custom" provider should work.
      - The docs don't cover adding MCP servers or reaching a service on the host.
      - Config lives in `/sandbox/.hermes`, but our setup script writes a profile.
      - It needs a Linux host with Docker or Podman.
    - **Time box:** half a day. If Hermes can't call Continuum's MCP from the sandbox, stop and write that up in the README feedback.

---

## 5. Demo video (≤ 3 min, one story)

| Time | Show | Track point |
|---|---|---|
| 0:00 | Hook: *"Two things slip: what you promised, and the message pretending to be someone you trust."* Phone (Telegram) next to the dashboard. | — |
| 0:15 | Dashboard chat: the NVIDIA internship message → goal + 2 loops; click one to show the exact sentence. Telegram: "What am I waiting on?" → the same answer. | Memory, Hermes + Nemotron |
| 0:50 | Telegram, in your own words: *"I need to transfer RM5,000 to T. Rowe Price Group Sdn. Bhd. at troweprice-my-invest.com by Friday for their 12% monthly fund."* (scenario B's entities, [b_clone_company.txt](tests/scenarios/b_clone_company.txt)) → loop saved, due Friday → *"Want me to check who's asking first?"* → "yes" → (cut the 40–60 s wait) CRITICAL, official troweprice.com, BNM clone warning, next steps. Dashboard: the loop tops Needs attention. Open the check: live steps, graph, click the BNM edge → quote. | Works for you, tools, fixed rules |
| 1:50 | Briefing on Telegram (`cron run`): the deposit loop first, then Sarah's reply due tomorrow. Reply "snooze the portfolio till Monday". | Always on, proactive |
| 2:20 | (Google set up) Sarah's email closes her loop; a calendar proposal waits; approve it. | Tasks, your yes |
| 2:40 | Architecture: Hermes ↔ MCP ↔ Continuum; Nemotron 3 Super on Nebius Token Factory for every reasoning step; Tavily for evidence; one SQLite file; nothing sent. 2 s of the Token Factory usage page. | NVIDIA + Nebius |

- **Use your own words, not a pasted message:** Hermes checks a forwarded money message right away, without saving the loop first or offering (seen live 2026-10-10). That path still saves a "Verify …" loop, but it has no due date.
- **If the offer doesn't come:** forward the message with "is this legit?". Step 5 saves the loop anyway.
- **Takes:** B finds the official domain about 2 runs in 3. Record until it does, or use scenario A (HIGH every run). No hand-edited data.
- **Labels:** mark every sample on screen as a test sample (Phase 4 §1).

## 6. Decisions for you

- **D1. Saving risky checks.** *Recommended:* save them as loops automatically (step 5). It's less code, and there's no button to forget to press. The cost is a loop you didn't ask for, closed with one click. *Alternative:* the spec's "Track this" button, plus a new MCP tool for chat.
- **D2. Offering checks.** *Recommended:* Hermes offers a check when a new loop involves paying someone. It's prompt-only and the most "personal AI" moment in the demo.
- **D3. Where this plan lives.** *Recommended:* as an addendum to Phase 4. The video has to show this work, and Phase 4's video box is still open. A Phase 5 couldn't start until that box is checked (AGENTS.md), so it would arrive after the video.
- **D4. OpenShell.** P4 only if P0–P3 are done.

## 7. Skipped on purpose

- **Nebius Serverless:** Hermes cron already schedules the jobs. A second scheduler adds a moving part just to show another logo.
- **Auto-checking synced email:** Phase 4 rules out inbox scanning. Checks run only when you ask or say yes.
- **Watching tracked orgs for new warnings** (Phase 4 P1): costs Tavily credits every day for each loop. Add after the demo if wanted.
- **Remembering verified contacts across checks** (e.g. Maybank's real domain): a real memory win, but it touches scoring and the live scenario set. After the hackathon.
- **A check history page:** the risky checks are already listed as loops.

## 8. Done checklist

- [x] README opens with the tagline, the five steps, the track table and "Your data"; ScamGraph sits under Check; Token Factory and Feedback sections added
  *Done 2026-10-10. Still open: "Built during the hackathon" (check the submission period first) and the Devpost text.*
- [x] Check page uses Continuum's header; tab title "Check a message · Continuum"
- [x] A risky check saves one linked loop; LOW doesn't; re-checks don't duplicate it
  *Done 2026-10-10: `tests/test_investigation.py` (`test_risky_check_becomes_a_loop_in_needs_attention`, `test_low_risk_check_adds_no_loop`).*
- [x] Risky loops top Needs attention and the 8:00 briefing
  *Done 2026-10-10: `tests/test_attention.py::test_risky_checks_go_first`. The briefing reads `needs_attention`, so it follows; not yet seen on Telegram.*
- [ ] A loop can be checked from its detail panel and from chat; Hermes offers the check for payment loops (live)
  *Code and offline tests done 2026-10-10 (`test_check_started_from_a_loop_stays_with_it`).*
  *Live 2026-10-10 through Hermes:*
  - *"Is this legit?" + scenario A → HIGH, "Verify FalconRise Capital before paying" at the top of Needs attention.*
  - *Own-words payment loop → saved due Friday, check run with its `loop_id`, no second loop.*
  - *Still open: Tavily answered HTTP 432 (out of credits?), so that check had no evidence and scored GUARDED. Re-run with credits.*
- [ ] (P2) `/check` and `/briefing` work on Telegram; the briefing cron uses the skill
- [ ] Runs on the Nebius VM; README limits updated
- [ ] Video follows §5; Devpost uses §2
- [ ] `pytest` passes; `pytest -m live` re-run after the MCP and SOUL changes
