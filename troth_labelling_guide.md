# Troth labelling guide

You mark what a careful assistant should take from each message: what's unfinished, who it involves, and when it's due. Label on your own, and don't look at what Troth answered. Labeller B never sees A's file.

## Files

- **A** labels `sheet_a.csv` (every item). **B** labels `sheet_b.csv` (the windows you both do). Both are in `backend/data/eval/`.
- Open the file in Excel or LibreOffice, not Google Sheets, which would upload the messages. Save it as `labels_a.csv` or `labels_b.csv`, type **CSV UTF-8**.
- Each item already has a row. Read the rows in order: a **window** is one stretch of one chat, or one person's emails.
- Fill in `lang` once per item: `en`, `ms` or `mixed`.
- If an item needs more than one label, insert a row under it and copy the item id into it.
- When you're done, run `python scripts/eval_import.py check` from `backend/`. It lists any problems by row number. Fix them and run it again.

## Labels

| label | When | Fill in |
|---|---|---|
| `new` | The item starts something unfinished | quote, kind, person, due, hedged; ref and title if useful |
| `open` | First item of a window only: a loop you already had before it (e.g. you were waiting for Sarah's reply) | title, kind, person, due |
| `update` | It moves the due date of an open loop | target, due, quote |
| `resolve` | It says an open loop is done or has arrived | target, quote |
| `cancel` | It calls an open loop off | target, quote |
| `link` | One loop has to wait for another | ref (the loop that waits), target (what it needs) |
| `none` | Nothing to track | nothing else |

## Kinds (Troth's own)

- **task**: you need to do it. Examples: "I need to renew my passport", or a request to you that you haven't answered.
- **commitment**: you promised someone. Example: "ok nnt I send by Wednesday k" after Aisyah asks. The person is who you promised.
- **waiting**: someone owes you. Example: "Dr Lim said he'll send it Friday". The person is who owes it.

## Fields

- **quote**:
  - the words that state it, copied exactly from this item's text (not from earlier items);
  - under 20 words;
  - it may run across the `Name:` lines.
- **person**: as the message names them ("Aisyah", "Dr Lim"). For an email, copy the `from` column exactly: Troth links the email to that person.
- **due**: only a specific day, as `YYYY-MM-DD`, counted from the item's `time`:
  - a weekday ("Friday", "by Fri", "Jumaat", "Sat"): the first one after the message's day. "Before Friday" is that Friday.
  - "tomorrow", "tmr", "esok": the next day. "Lusa": the day after that. "Tonight", "malam ni", "today", "hari ni": that day.
  - a date ("15/10", "15hb Okt"): its next occurrence. "End of the month": that month's last day.
  - vague ("next week", "soon", "nanti", "later"): leave it blank.
- **ref**: a short name for a new loop, so later rows can point to it (e.g. `slides`). If blank, it becomes `item.1`, `item.2`, and so on.
- **target**: the ref of the loop an update, resolve, cancel or link is about. It must come from an earlier item in the same window, or from an `open` row.
- **hedged**: `y` when the promise is soft: "I'll try", "insyaAllah", "tengok dulu", "maybe", "if I have time".
- **title** (optional): 2–6 words, like Troth's ("Send slides to Aisyah"). If blank, the quote is used.

## Hard cases

| Situation | Label it as |
|---|---|
| "ok" / "can" / "boleh" to a request | a commitment to whoever asked |
| A request to you, not answered yet | a task |
| A step you say comes first ("need to fix the charts first") | its own task, plus a link to the loop that waits for it |
| Someone's promise to you | waiting on them |
| Someone's promise to someone else in a group | none |
| A plan with a day ("jom makan Sat" + "can") | a commitment, due that day |
| Done in the same message ("dah hantar") | resolve, if it closes an earlier loop; otherwise none |
| Called off ("tak jadi", "cancel", "no need already") | cancel |
| Past facts, small talk, thanks, jokes, ads, OTPs, newsletters | none |

If you're torn between two labels, pick one and write why in `note`.

## Message checks (`scam.csv`)

Columns: `item`, `text` (or `url`, or `screenshot`: a file inside `data/eval`), `label`, `record`, `split` (leave blank), `note`.

| label | Meaning | record |
|---|---|---|
| `listed` | A name, number or account in it is on BNM's Financial Consumer Alert List, SC's Investor Alert List, or in Semak Mule | The entry's URL |
| `impersonation` | The real organisation says the message isn't from them | Their warning page |
| `reported` | Reported publicly (news, forum), but on no list | The article or post |
| `legitimate` | Confirmed genuine through the organisation's own website, app or phone line | That channel |

Mask numbers that aren't the point. Keep the ones that are, such as the phone or account the message tells you to use.

Labeller B runs `python scripts/eval_import.py blank-scam`, labels `scam_sheet_b.csv` from the records, and saves it as `scam_b.csv`.

## Privacy

Everything stays in `backend/data/eval/` and is never committed. Only use chats whose people agreed, and tell them where the messages go: Nebius (the eval runs) and Anthropic (Claude reads the failures).
