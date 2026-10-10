---
name: Continuum
description: A personal AI that remembers what's unfinished and checks who's asking before you pay, kept as a Buku 555 pocket ledger.
colors:
  cover: "#c42127"
  cover-deep: "#9f191e"
  on-cover: "#ffffff"
  on-cover-soft: "#fde3e4"
  paper: "#fbfcfd"
  ground: "#f0f3f7"
  rule: "#d5e2f0"
  rule-strong: "#9db9d8"
  margin: "#eb9a9e"
  ink: "#1b1e24"
  muted: "#545c69"
  red: "#c42127"
  red-wash: "#fcecec"
  blue: "#1f4b99"
  blue-wash: "#e8eef9"
typography:
  numeral:
    fontFamily: "Archivo Variable, Archivo, system-ui, sans-serif"
    fontSize: "clamp(3.25rem, 2.2rem + 3.5vw, 5.25rem)"
    fontWeight: 900
    lineHeight: 0.82
    letterSpacing: "-0.02em"
    fontVariation: "'wdth' 62"
    fontFeature: "'tnum'"
  level:
    fontFamily: "Archivo Variable, Archivo, system-ui, sans-serif"
    fontSize: "clamp(2.75rem, 1.8rem + 3.2vw, 4.5rem)"
    fontWeight: 900
    lineHeight: 0.95
    letterSpacing: "-0.01em"
    fontVariation: "'wdth' 62"
  headline:
    fontFamily: "Archivo Variable, Archivo, system-ui, sans-serif"
    fontSize: "1.5rem"
    fontWeight: 800
    lineHeight: 1.15
    letterSpacing: "-0.01em"
    fontVariation: "'wdth' 92"
  title:
    fontFamily: "Archivo Variable, Archivo, system-ui, sans-serif"
    fontSize: "1.0625rem"
    fontWeight: 650
    lineHeight: 1.35
  body:
    fontFamily: "Archivo Variable, Archivo, system-ui, sans-serif"
    fontSize: "0.9375rem"
    fontWeight: 400
    lineHeight: 1.5
  small:
    fontFamily: "Archivo Variable, Archivo, system-ui, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "Archivo Variable, Archivo, system-ui, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 700
    lineHeight: 1.5
  wordmark:
    fontFamily: "Archivo Variable, Archivo, system-ui, sans-serif"
    fontSize: "1.25rem"
    fontWeight: 750
    letterSpacing: "-0.01em"
    fontVariation: "'wdth' 112"
rounded:
  tag: "2px"
  base: "3px"
spacing:
  s1: "4px"
  s2: "8px"
  s3: "12px"
  s4: "16px"
  s5: "24px"
  s6: "32px"
  s7: "48px"
  row: "48px"
  page: "1280px"
components:
  button:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    typography: "{typography.small}"
    rounded: "{rounded.base}"
    padding: "0 14px"
    height: "36px"
  button-hover:
    backgroundColor: "{colors.ground}"
  button-primary:
    backgroundColor: "{colors.blue}"
    textColor: "{colors.paper}"
    rounded: "{rounded.base}"
    padding: "0 14px"
    height: "36px"
  button-primary-big:
    backgroundColor: "{colors.blue}"
    textColor: "{colors.paper}"
    typography: "{typography.body}"
    rounded: "{rounded.base}"
    padding: "0 22px"
    height: "44px"
  button-danger:
    textColor: "{colors.red}"
    rounded: "{rounded.base}"
    padding: "0 14px"
    height: "36px"
  button-danger-hover:
    backgroundColor: "{colors.red-wash}"
  button-on-cover:
    textColor: "{colors.on-cover}"
    rounded: "{rounded.base}"
    padding: "0 14px"
    height: "36px"
  button-on-cover-hover:
    backgroundColor: "{colors.cover-deep}"
  button-on-cover-solid:
    backgroundColor: "{colors.on-cover}"
    textColor: "{colors.cover}"
    rounded: "{rounded.base}"
  input:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.base}"
    padding: "8px 10px"
    height: "38px"
  sheet:
    backgroundColor: "{colors.paper}"
    rounded: "{rounded.base}"
    padding: "{spacing.s5}"
  band:
    backgroundColor: "{colors.cover}"
    textColor: "{colors.on-cover}"
    padding: "16px 24px"
  entry:
    height: "{spacing.row}"
    padding: "8px 4px"
  chip:
    typography: "{typography.label}"
    rounded: "{rounded.tag}"
    padding: "1px 7px"
  message-user:
    backgroundColor: "{colors.blue}"
    textColor: "{colors.paper}"
    rounded: "{rounded.base}"
    padding: "8px 12px"
  menu:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
    rounded: "{rounded.base}"
    padding: "{spacing.s2}"
---

# Design System: Continuum

## Overview

**Creative North Star: "The Buku 555 Pocket Ledger"**

Continuum is kept like the kedai runcit's 555 book: a red cover band on top, cool white ruled sheets underneath, graphite handwriting-weight ink. What you owe, what's owed to you, and requests that want your money are entries on lines, not cards on a board. The system refuses the SaaS task board (sidebar, card grid, blue-accent chrome) and the dark threat console.

Two inks carry meaning and nothing else gets colour. Red is Continuum's warning: risk signals, overdue, payment asks not yet checked. Ballpoint blue is settled and yours: resolve, soon, supports, your own chat messages. The ledger's furniture (pale-blue rules, the double red margin, the cover band) stays the same in every state, so meaning comes only from which ink an entry is written in.

Density is a working notebook: 48px rows, small type, tabular numerals, no decorative imagery. The one flourish is the strike: settling an entry draws a single ballpoint line through it, in place. This is an Operate-mode product; controls, inputs and menus stay standard and legible.

**Key Characteristics:**
- Red 555 cover band with the day's count in extra-condensed black numerals.
- Paper sheets on a grey-white ground, pale-blue rules between entries and behind running text.
- Two meaning-bearing inks: red warns, blue settles.
- Archivo Variable throughout, width axis doing the expressive work.
- Authored one-stroke line icons.
- One animation that matters: the ballpoint strike.

## Colors

A cool paper-and-graphite palette with two inks, red and ballpoint blue, each reserved for one meaning.

### Primary
- **555 Cover Red** (#c42127): the cover band on every page, the message node in the evidence graph. The wordmark, tally and band actions sit on it in white.
- **Cover Deep** (#9f191e): hover fill for outline buttons on the band only.

### Secondary
- **Warning Red** (#c42127, `red`): Continuum's warning ink. Risk flags in the margin column, overdue dates, "warns/contradicts" verdicts and graph edges, the flagged-phrase underline, error lines. Same hex as the cover in light mode; it splits to a lighter #ff858a in dark so it stays legible on dark paper.
- **Red Wash** (#fcecec): hover fill behind the danger button. Never a section fill.

### Tertiary
- **Ballpoint Blue** (#1f4b99): settled and yours. The primary button, the strike, "soon" dates, "supports/verified" verdicts, links, focus rings, the caret, your own chat bubbles.
- **Blue Wash** (#e8eef9): selection, row hover (mixed at 45%), the "updated" row highlight and the evidence flash.

### Neutral
- **Paper** (#fbfcfd): every sheet, input, menu and button face.
- **Ground** (#f0f3f7): the page behind the sheets; also the hover fill of neutral buttons and menu items.
- **Pale Rule** (#d5e2f0): lines between entries, ruled text backgrounds, sheet borders, graph edges at rest.
- **Strong Rule** (#9db9d8): control borders, sub-section underlines, the "in the message" legend line.
- **Margin Pink** (#eb9a9e): the double margin rule on flagged lists.
- **Graphite Ink** (#1b1e24): text, 2px section-head rules, A/B evidence-tier tags.
- **Pencil Grey** (#545c69): meta text, dates, kind icons, settled entries.
- **On-Cover White** (#ffffff) and **On-Cover Blush** (#fde3e4): text on the band; blush for the tally label.

Dark theme (via `prefers-color-scheme`) keeps the same roles: paper #1b1e24 on ground #101215, rules #2a3442 / #43546c, ink #e8ebf0, muted #a6afbc, red #ff858a, blue #93b6ff, cover #b11e24. Full map in `.impeccable/design.json`.

### Named Rules
**The Two Inks Rule.** Red means Continuum is warning you; blue means it's settled or it's yours. Never use either as decoration, and never swap them.

**The No Grey Box Rule.** Content lives on paper sheets with rules, never in grey or tinted filled boxes. Washes and the ground appear only as hover, selection or a brief flash.

## Typography

**Display Font:** Archivo Variable (self-hosted via @fontsource-variable/archivo, standard axes: wght 100–900, wdth 62–125%), falling back to Archivo, system-ui, sans-serif.
**Body Font:** the same family at normal width.

**Character:** one grotesque doing everything; the width axis carries the voice. Extra-condensed black (wdth 62%, 900) for numerals and the check level, slightly condensed (92%) heavy heads, extended (112%) for the wordmark, normal width for all UI.

### Hierarchy
- **Numeral** (900, wdth 62%, clamp 3.25–5.25rem, line-height 0.82, tabular): the band's day count only. 3rem on phones.
- **Level** (900, wdth 62%, clamp 2.75–4.5rem, uppercase, line-height 0.95): the message check's risk level, in red or blue, over a 2px ink rule.
- **Headline** (800, wdth 92%, 1.5rem, line-height 1.15): sheet heads and the band title.
- **Title** (650, 1.0625rem): entry titles, sub-heads (750), evidence claims.
- **Body** (400, 0.9375rem, line-height 1.5; 1.7 on ruled text): running text, messages, quotes.
- **Small** (400–650, 0.8125rem): meta, dates, buttons, form labels.
- **Label** (700, 0.75rem): chips, tier tags, legends, captions.

### Named Rules
**The Width Not Size Rule.** Emphasis comes from Archivo's width and weight axes, not from new families or ever-larger sizes. Condensed 62% is reserved for numerals and the check level.

**The Tabular Rule.** Counts, dates and step numbers use tabular numerals, like an amount column.

## Layout

A centred page up to 1280px with 24px gutters (12px on phones). Spacing steps are 4/8/12/16/24/32/48; sections stack 48px apart, sheets pad 24px (16px on phones).

Dashboard: Needs attention spans full width; below it the ledger (minmax 0, 1fr) sits beside a sticky chat sheet (320–400px). At ≤1080px it stacks attention, chat, ledger, and the ledger's two sides (Owed to you, You owe) go side by side; at ≤720px everything is one column and entries reflow to title over due date.

Message check: form beside a 260–340px "how it works" column; verdict beside the checked message (280–400px). Both stack at ≤960px. The evidence graph is 460px tall (520px on phones).

Entries are 48px-min rows on a grid: kind icon or flag column, title, due date (the amount column), chevron. Band actions take the full width as a third row on phones.

## Elevation & Depth

Flat paper on a ground. Sheets lift only a hair; depth comes from paper against ground and from rules, not from shadow. The single real shadow belongs to popover menus.

### Shadow Vocabulary
- **Sheet lift** (`box-shadow: 0 1px 2px rgb(27 30 36 / 0.05)`): every paper sheet.
- **Popover** (`box-shadow: 0 6px 24px rgb(27 30 36 / 0.14), 0 1px 3px rgb(27 30 36 / 0.1)`): Snooze and Settings menus only. Darker in dark mode.

### Named Rules
**The Paper Not Plastic Rule.** If it isn't a floating menu, it doesn't cast a visible shadow.

## Shapes

Nearly square: 3px on sheets, buttons, inputs, menus and chat bubbles; 2px on chips, tier tags and menu items. Borders are 1px rules; section heads close with a 2px ink rule. The double red margin (two 1px lines 3px apart, 44px in) runs down flagged lists. Graph nodes keep the same vocabulary: rounded-rectangle message in cover red, circles for people and organisations, squares for sources.

## Components

### Buttons
Standard, quiet, legible: a ledger's pen, not a toy.
- **Shape:** nearly square (3px), 36px tall, 650 small type, optional 16px icon.
- **Default:** paper face, strong-rule border, ink text; hover to ground fill and pencil-grey border (120ms).
- **Primary:** ballpoint blue fill, paper text; hover mixes 14% ink in. The 44px "big" size is for the one main action on a page (Check).
- **Danger:** transparent, red text with a 45% red border; hover red wash. Delete sits apart from Resolve.
- **On cover:** transparent with a 55% white border on the band; solid variant is white with cover-red text.
- **Link button:** blue underlined text, no box.
- **Focus:** 2px blue outline, 2px offset (white on the band).

### Chips
- **Style:** 1px border in currentColor, no fill, 2px radius, 700 label type. Colour comes from the verdict ink: red for contradicted/mismatch/suspicious, blue for supported/verified, muted for unverified.
- **Tier tags:** filled 2px tags, ink fill for A/B sources, pale rule fill for the rest.

### Cards / Containers
- **Sheet:** paper, 1px pale-rule border, 3px radius, 24px padding, sheet lift. Each sheet opens with a head: headline, muted tabular count, tools right, closed by a 2px ink rule.

### Inputs / Fields
- **Style:** paper, 1px strong-rule border, 3px radius, 38px min height, 8px 10px padding. Checkboxes use blue accent.
- **Focus:** 2px blue outline at 0 offset and a blue border. Hover darkens the border to pencil grey.
- **File picker:** its button matches the default button.

### Navigation
- **The cover band:** cover red, full width, on every page. Wordmark (ledger icon + "Continuum", 750, wdth 112%) left; on the dashboard the day's count and "need you today" in on-cover blush; actions right. On the check page a band title replaces the tally.

### Ruled Rows
Pale-blue 1px rules between entries, the same furniture in every state. A row is a full-width button that opens its detail in place; hover is a faint blue wash, the chevron rotates 90° (160ms). A freshly updated row gets the blue wash and a small blue "updated" tag.

### Margin and Flag Column
Lists that can carry warnings (Needs attention, the ledger) draw the double margin rule. Continuum's flag sits in the column left of it in red: a filled-dot risk icon for risk signals, a question icon for payment asks not yet checked. Entry text starts 10px after the rule.

### The Strike (signature)
Settling an entry draws one ballpoint-blue stroke (2.2px, round caps, a slightly wavering path) through its title, in place, over 420ms on `cubic-bezier(0.2, 0.7, 0.2, 1)`. It draws once; resolved entries render it static. Reduced motion snaps it drawn. Settled titles and meta go pencil grey.

### Ruled Text
Running source text (quotes, the checked message) sits on pale rules at a 1.7em line height, like writing on the page. Flagged phrases in a checked message get a 2px red underline, 4px offset, no fill.

### Chat
A sheet that sizes to content, then becomes a fixed scrolling sheet (max 640px). Your messages are blue bubbles with paper text, right-aligned; Continuum's are outlined in pale rule; errors are outlined in red.

### Evidence Graph
Cytoscape on paper inside a ruled border, in the same inks: red edges and borders for warnings, blue for support, pale rule for neutral; ink for strong sources. Edge labels appear on hover only. A legend of short coloured lines sits below.

### Icons
Authored set on a 24px grid, one 1.8 stroke, round caps and joins, in currentColor: ledger, task, ask, waiting, commitment, done, risk, send, search, gear, chevron.

## Do's and Don'ts

### Do:
- **Do** put every page under the red cover band, with the wordmark at left.
- **Do** write warnings in red and settled or user-owned things in blue, and nothing else in either.
- **Do** separate entries with 1px pale-blue rules and close sheet heads with a 2px ink rule.
- **Do** use the double margin rule and flag column for any list where Continuum may warn.
- **Do** use Archivo's width axis for emphasis: 62% black for numerals and the check level only.
- **Do** keep motion to small state transitions (120–160ms colour and rotate, a 400ms evidence flash) plus the strike; honour reduced motion.
- **Do** word every risk as "risk signals", "warning found", "hold off paying". The level shown is the fixed-rule result.
- **Do** keep Operate controls standard: native inputs, details-based menus, visible focus rings.

### Don't:
- **Don't** fill sections, cards or callouts with grey or tinted boxes; sheets are paper.
- **Don't** show "scam", "scammer" or "fraud" as a verdict, label or chip.
- **Don't** add a second display family, or use the 62% condensed width for body text or section heads.
- **Don't** animate entrances, lists or numbers; the strike is the only drawn motion.
- **Don't** use shadows beyond the sheet hairline and the popover.
- **Don't** use icon fonts, emoji or text glyphs as icons; draw them in the authored line set.
- **Don't** rebuild the SaaS task board: no sidebar, no card grid, no blue as a generic brand accent.
