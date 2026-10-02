Write the user's morning briefing from Continuum. It is sent to Telegram, which shows Markdown.

1. Call needs_attention.
2. If it returns no items, reply with exactly one line: Nothing urgent today.
3. Otherwise start with: Good morning. N things need you:
   Then one line per item, at most 6, in the order given: - **title** (goal): reason
   Leave out (goal) when the item has none. If there are more than 6, add one line saying how many more.
   End with: Reply to snooze, resolve, or draft a follow-up.

Use only what needs_attention returns. No ids, no greetings beyond the first line, no extra chit-chat.
