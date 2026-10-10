# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

React + Vite frontend (user's choice, 2026-10-10), built to static files that the FastAPI app serves. Backend unchanged: FastAPI + SQLite, Hermes Agent as the chat brain over MCP, NVIDIA Nemotron on Nebius Token Factory, Tavily for web evidence. Deploy target: one Nebius VM (Phase 4 §11).

## Users

Students and job seekers in Malaysia. They juggle applications, deadlines, promises and things other people owe them, across chat and email. The same people get investment and job-offer messages that ask for money or personal details and aren't from who they claim. They use Continuum on a laptop and on a phone about equally: the dashboard in a browser, plus a Telegram bot.

## Product Purpose

Continuum is a personal AI that remembers what's unfinished and checks who's asking before you pay. It pulls open loops (tasks, promises, things you're waiting on, deadlines) out of normal conversation, keeps them with the words they came from, reminds you before they slip, and closes them when they're done. When a loop asks for money or details, its message check (ScamGraph) researches the sender on the live web and fixed rules score the risk. Success: nothing the user cares about slips, and they don't pay someone who isn't who they claim.

## Positioning

One assistant holds both your commitments and the requests trying to become one. Every risk level comes from fixed rules over cited evidence (regulator alert lists, the company's real site), never from the model's opinion, and every loop links back to the exact words it came from.

## Operating Context

- Capture: dashboard chat, Telegram, email from people linked to open loops, a pasted message, link or screenshot.
- Each loop goes through five steps: capture, remember, check, remind (Needs attention, 8:00 Telegram briefing), close (by the user, an email reply, or web watch).
- Checks take 1–2 minutes and show live progress steps.
- Proposals (calendar events, Gmail drafts, web findings) wait for the user's yes.
- First evaluated in a hackathon: a ≤3-minute demo video and a deployed URL behind a password.

## Capabilities and Constraints

- Two surfaces today: the dashboard (`/`: chat, Needs attention, Pending actions, Open loops with detail, Settings) and the message check (`/investigate.html`: form, live steps, result, evidence graph, claims and evidence).
- The REST API under `/api` is the contract the frontend uses. Chat goes to Hermes through `/api/chat`.
- All message, web and model text shown in the UI is untrusted: render it as text, never HTML; only http(s) links are clickable.
- One user; a server deploy sets `APP_PASSWORD` (HTTP Basic on every page and `/api`).
- Malaysian regulators only (SC, Bank Negara) for now.

## Brand Commitments

- Never-accuse wording (binding): say "risk signals", "warning found", "hold off paying". Never call anyone a scammer, fraudster or criminal, and never show "scam" as a verdict. The risk level is set by fixed rules, not by the model.
- Names in use: Continuum (product), ScamGraph (the message check). Not marked binding.

## Evidence on Hand

- Five labelled test scenarios in `tests/scenarios/` (A–E), plus one screenshot. They're test samples, not real messages, and must be labelled as such on screen.
- No real users, testimonials, metrics or press. Don't invent any.

## Product Principles

1. The user decides. Continuum proposes, cites and reminds; it never sends, pays or reports for them.
2. Evidence over opinion. Every risk claim links to a source or to the user's own message.
3. Keep the original words. Every loop shows what it came from.
4. Missing evidence isn't safety. Only a low-risk result clears a payment loop.
5. Short and direct. Few setup steps, obvious UI, no filler.
