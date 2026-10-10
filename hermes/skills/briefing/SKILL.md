---
name: briefing
description: What needs the user today, from Continuum, as a short list for Telegram. Used by the 8:00 cron job.
version: 1.0.0
license: MIT
metadata:
  hermes:
    tags: [Continuum, briefing]
---

# Briefing

## When to Use
`/briefing`, or the morning cron job. The reply goes to Telegram, which shows Markdown.

## Procedure
1. Call `needs_attention`.
2. No items → reply with exactly one line: Nothing urgent today.
3. Otherwise start with: N things need you: (one item: 1 thing needs you:)
   Then one line per item, at most 6, in the order given: - **title** (goal): reason
   Leave out (goal) when the item has none. More than 6 → one line saying how many more.
   End with: Reply to snooze, resolve, or draft a follow-up.

## Pitfalls
- Use only what `needs_attention` returns. No ids, no extra chit-chat.
