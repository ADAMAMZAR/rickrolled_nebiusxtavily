# AGENTS.md — Continuum

Personal AI that tracks **open loops** (unfinished tasks, promises, things you're waiting on) extracted from chat.
Hackathon: Nebius x NVIDIA, Personal AI track. **Hermes Agent + NVIDIA Nemotron via Nebius are required and must be real, not mocked, at runtime.**

**Specs (source of truth):** read the current phase before any work. If reality differs (APIs, SDKs), update the spec as well.
- [continuum_phase_1.md](continuum_phase_1.md): Core: chat → open loops → dashboard. *(done 2026-10-01)*
- [continuum_phase_2.md](continuum_phase_2.md): Proactive: attention rules, snooze, Hermes cron briefing, Telegram **← current phase**
- [continuum_phase_3.md](continuum_phase_3.md): Connected: Gmail/Calendar, auto-resolve from email, approval-gated actions

Start a phase only after every box in the previous phase's Done Checklist is checked. When a phase is finished, move the "current phase" marker.

## Principles (from the user)
1. **Good but not complex.** Choose the simplest thing that works. No speculative abstractions, layers, or deps.
2. **Easy for the user.** Few setup steps, obvious UI, clear errors.
3. **Short and direct.** Code comments, docs, UI text, and replies to the user: no filler.

## Architecture (short)
- `app/engine.py` owns all domain logic. FastAPI routes and MCP tools are thin wrappers around it.
- Hermes = chat brain. It calls Continuum through MCP tools at `/mcp`. The UI chat proxies to the Hermes API server.
- All LLM prompts live in `app/extraction.py`. All LLM calls go through `app/llm.py`.
- `LLM_PROVIDER=deepseek` is a **dev stand-in only**, used while there's no Nebius key. The final demo and submission must use `nebius` (Nemotron): re-run `pytest -m live` with it before submitting. Keep prompts provider-neutral.
- Hermes runs in its own `continuum` profile (`%LOCALAPPDATA%\hermes\profiles\continuum`), built from `hermes/` by `scripts/setup_hermes.py`. Never edit the user's default Hermes profile. New MCP tools must be added to `tools.include` in `hermes/config.example.yaml`, then re-run the setup script.
- MCP SDK is 2.x: `from mcp.server.mcpserver import MCPServer` (not `FastMCP`). Raise `ToolError` for expected failures; other exceptions reach the model only as "Error executing tool".
- Tool docstrings (`app/mcp_tools.py`) and `hermes/SOUL.md` steer Hermes' tool choice. After editing either, re-run the setup script.
- Testing writes through Hermes: run Continuum with `DATABASE_URL=sqlite:///<scratch path>` so the user's real `data/continuum.db` stays clean.
- Extract first, then write everything in one DB transaction. A failure writes nothing.

## Commands
```bash
python -m venv .venv && .venv\Scripts\activate   # Windows (source .venv/bin/activate elsewhere)
pip install -r requirements.txt -e .  # pinned, tested versions + the app itself (scripts import `app`)
python scripts/setup_hermes.py    # once, and after changing LLM_PROVIDER or keys
uvicorn app.main:app --reload     # Continuum on :8000 (MCP at /mcp/)
hermes -p continuum gateway run   # Hermes API server on :8642
pytest                            # offline, LLM mocked
pytest -m live                    # real LLM + Hermes (needs keys and both servers running)
```

## Rules
- Python 3.11+, type hints, Pydantic for anything from the LLM or HTTP.
- Tests mock the LLM. Only `-m live` tests hit the network.
- Never commit `.env`, `*.db`, bot tokens, or Google tokens/client secrets. Never log API keys or message content.
- Don't swallow exceptions.
- Stay in the current phase's scope. Ask before adding anything from its "Out" list.
- Continuum never sends anything on the user's behalf. Drafts and approved calendar events only.
- Schema changes go through `db.migrate()` (from Phase 2 on). Old DBs must upgrade without data loss.
- Windows dev machine: keep commands cross-platform or give both variants.
- Before saying something is done: run `pytest` and, for LLM/Hermes changes, check the real path.
