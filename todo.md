# Phase 1 TODO

Source: [continuum_phase_1.md](continuum_phase_1.md) §13–14.

- [x] Dev env on this machine: `.venv` + `pip install -e ".[dev]"`, `pytest` green
- [x] **Step 6a — REST API** (§8): `/api/chat`, `/api/goals`, `/api/loops`, `/api/loops/{id}`, resolve, reopen, delete, JSON errors + tests
- [x] **Step 6b — UI** (§9): `app/static/index.html`: chat, loops grouped by goal, friendly due dates, overdue red, highlight changes, detail panel with source, empty state
- [x] `scripts/seed_demo.py` for UI work
- [x] **Step 7 — E2E**: §1 demo through Hermes on Nemotron, restart, memory persists, `pytest -m live` on nebius
- [x] **Step 8 — README** (§13.8) + LICENSE (MIT)
- [x] Final self-review, tick §14 Done Checklist, move "current phase" marker
