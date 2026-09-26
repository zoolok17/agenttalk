# agenttalk Console Design System

The visual language of the **agenttalk Team Console** — an operator dashboard for observing and steering a live team of coding agents (Claude Code + Codex) collaborating over agenttalk's file-backed message bus. This system was **extracted from the console itself** (`Team Console.dc.html` at the project root); it is the source of truth for building more of the product.

> Source: the console prototype in this project, informed by the agenttalk project — https://github.com/zoolok17/agenttalk (a stdlib-only Python message bus for agent-to-agent collaboration). No external brand assets were provided; there is **no logo** — the wordmark is set in plain type beside a simple gradient square (see Iconography).

---

## Content fundamentals

The console speaks like a **calm operations tool for engineers**, not a consumer app.

- **Voice:** terse, factual, present-tense. Labels are verb-or-noun fragments, not sentences: "Who's doing what", "Needs a human", "Awaiting your decision", "All clear".
- **Person:** addresses the operator as **you** ("Nothing is waiting on you right now"); agents are named in third person by their bus id.
- **Casing:** Title case for view H1s; **UPPERCASE mono** for eyebrow section labels (`STATUS LEGEND`, `AWAITING YOUR DECISION`) and chips (`CLAUDE`, `GATE HOLD`). Message kinds stay lowercase-hyphenated exactly as they appear on the bus (`review-request`, `proposal-response`, `wake`).
- **Identifiers are sacred:** agent names (`claude-lead`), thread ids (`rq-7a1`), work packages (`WP-07`), and paths (`src/agenttalk/lanes.py`) are always mono, never reworded.
- **Numbers over adjectives:** "42/42 green", "resets in 34m", "no progress for 6m" — concrete, scannable. No hype words, no exclamation.
- **Emoji:** none. Ever. Meaning is carried by color + a small stroked icon set.
- **Vibe:** a quiet mission-control readout you can watch all day without fatigue.

## Visual foundations

- **Two themes, one language.** Light (paper `#F1F3F6` canvas, white cards) and dark (graphite `#0E1116` canvas, `#161A21` cards). Every color ships as a **solid + soft (tint) pair** so a status reads identically as a dot, a chip, a meter fill, or a card spine. Toggle with `data-theme="dark"`.
- **Color is functional, not decorative.** The palette is a **status vocabulary**: green=working/GO, blue=quiet-work/question, amber=idle/medium, orange=stuck, red=rate-limited/HOLD/escalate, violet=proposal, teal=wake, gray=unknown. Accent indigo (`#4457E6`, themeable) is reserved for the operator's own actions, active nav, and live highlights. Two CLI identity colors: Claude terracotta, Codex teal.
- **Type:** three families with strict jobs — **Space Grotesk** (headings, 30px stat numbers, wordmark), **Geist** (UI + body), **Geist Mono** (every identifier, count, timer, path). Tight tracking on display (`-.015em` to `-.02em`); wide tracking on uppercase eyebrows (`.08–.09em`).
- **Surfaces & cards:** 12px radius (14px for hero cards), `1px solid --border`, a soft `0 1px 2px` shadow. Hover lifts to `0 4px 16px`. A card's health is shown by a **status spine** — `inset 3px 0 0 <color>` composited with the base shadow — not by tinting the whole card.
- **Chips & meters** are the workhorses: 6px-radius mono pills for every bit of metadata; 4–7px track bars with **threshold fill** (green <60, amber 60–84, red ≥85).
- **Backgrounds:** flat fills only. No gradients (except the tiny 28px wordmark square), no imagery, no texture, no patterns.
- **Motion is minimal and meaningful:** one 1.8s `liveDot` opacity breathe on working/live dots; a 1s `dashmove` on the active-review graph edge; a 0.45s `fadeInUp` for new feed items; 0.5s meter-fill width transitions. Standard `ease`; nothing bounces.
- **Interaction states:** buttons — primary brightens (`filter: brightness(1.06)`), ghost fills to `--hairline`; nav/rows tint to `--hairline`/`--surface-2`; cards raise shadow. No scale/press-shrink.
- **Borders & dividers:** `--border` for card edges, `--hairline` for inner row dividers, `--border-2` for section splits, `--edge` for graph strokes. Hairlines everywhere instead of heavy rules.
- **Density:** desktop-first (~1280px+), tight rhythm (gaps 8–20px, card padding 15–20px). A `compact` density trims card padding. Not designed for mobile.
- **No transparency/blur** as a motif — soft tints are solid-ish rgba on flat surfaces; there is no glassmorphism.

## Iconography

- **Line icons, Lucide-style:** inline SVG, ~1.9–2.2 stroke width, `currentColor`, round caps/joins. Used for nav (grid, chat, alert-triangle, file-text), directional arrows, the checklist/mission mark, the "wrapped/supervised" frame glyph, and the send paper-plane. No icon font, no filled icons, no PNG icons.
- **No emoji, no unicode-glyph icons.**
- **No logo.** There is no brand mark in the source. The wordmark is "agenttalk" (Space Grotesk 600) over "TEAM CONSOLE" (uppercase mono), beside a **28px rounded-square gradient chip** built from the accent color — a neutral placeholder, not a logo. If a real mark arrives, drop it in `assets/` and replace the square. Never invent one.

---

## Index / manifest

- **`styles.css`** — root entry point (import manifest). Link this.
- **`tokens/`** — `colors.css` (light + dark), `typography.css`, `spacing.css`, `effects.css`.
- **`guidelines/`** — foundation specimen cards (Colors, Type, Spacing, Brand groups).
- **`components/core/`** — `Button`, `StatusDot`, `Chip`, `CliBadge`, `Meter`, `Card`.
- **`components/patterns/`** — `AgentCard`, `MessageBubble`, `AttentionItem`, `CapacityPanel`, `RelationshipGraph` (compose the core primitives).
- **`components/navigation/`** — `NavRail` (sidebar view list + status legend).
- **`ui_kits/team-console/`** — Team overview screen recreation + kit readme.
- **`Team Console.dc.html`** (project root) — the full interactive prototype (5 views + lead chat, live sim, theming).
- **`SKILL.md`** — downloadable Agent Skill wrapper.

### Fonts
Space Grotesk, Geist, and Geist Mono are loaded from **Google Fonts** (CDN `@import` in `styles.css`). If you need offline/self-hosted use, drop the font files in `assets/fonts/` and add `@font-face` rules — flag me and I'll wire them.

### Components — intentional scope
This system has **no pre-existing component library**; the inventory was derived from the console's actual recurring UI. It now covers every notable surface:

- **Core primitives:** Button, StatusDot, Chip, CliBadge, Meter, Card.
- **Patterns:** AgentCard, MessageBubble, AttentionItem, CapacityPanel, RelationshipGraph.
- **Navigation:** NavRail.

Component preview cards render **both light and dark** side by side. The remaining console pieces are one-off page layouts (the health-timeline strip, the stat-tile row) composed inline from the above; ask if you want any promoted.
