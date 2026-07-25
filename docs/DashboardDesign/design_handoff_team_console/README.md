# Handoff: agenttalk Team Console

## Overview
The **Team Console** is a web dashboard for observing a live multi-agent [agenttalk](https://github.com/zoolok17/agenttalk) team at detail level: who each agent is, what it is doing right now, who is talking to whom, what needs a human, and the full transcript of any message thread. It replaces the current "basic web dashboard" with a richer, five-view operator console designed for teams where most agents run **wrapped/supervised** (so heartbeat, capacity, and supervisor state matter).

It is a **read-first monitoring UI** with a few operator actions (dismiss/defer an attention item, restart a stuck agent with context, jump to an agent's transcript). It is calm and legible rather than a dense monitoring wall, and supports **light and dark themes**.

## About the Design Files
The single file in this bundle — `Team Console.dc.html` — is a **design reference created in HTML**. It is a working, self-contained prototype that shows the intended look, layout, and interaction behavior with realistic **sample data**. It is **not production code to copy directly**, and it is **not wired to a real `.agenttalk/` bus**.

Your task is to **recreate this design in agenttalk's own environment**. agenttalk is a stdlib-only Python package with no existing web frontend, so there is no established frontend framework to conform to — **choose the most appropriate stack** for a small, local, read-mostly dashboard that a developer runs alongside the CLI. Two natural options:

- **Zero-dependency static + polling** (recommended to match agenttalk's "just files, no daemon" ethos): a small `agenttalk serve` command using only Python stdlib (`http.server`) that reads `.agenttalk/` and serves JSON; a static HTML/JS frontend that polls. Keeps the "no third-party runtime deps" promise.
- **A small SPA** (React/Vue/Svelte) if the team is comfortable adding a dev toolchain for the frontend only.

Either way, treat the HTML as the **visual + behavioral spec**, and read the data from the real bus (see **Data Sources** below).

> ⚠️ The prototype is a Design Component (`.dc.html`) — it uses a small in-house template runtime and inlines all styling via JavaScript style objects. **Do not try to reuse that runtime.** Read it for structure, measurements, colors, and copy; reimplement cleanly in your chosen stack.

## Fidelity
**High-fidelity (hifi).** Final colors, typography, spacing, radii, shadows, and interactions are all specified below and present in the file. Recreate the UI pixel-closely, then bind it to real data. The sample dataset (10 agents, one spec-kitty mission) is illustrative — the *shape* of the data maps to real agenttalk concepts (see **Data Sources**).

---

## Global Layout

Full-viewport app, `height: 100vh`, `overflow: hidden`, three regions stacked:

```
┌─────────────────────────────────────────────────────────────┐
│ Top bar (height 56px, fixed)                                  │
├──────────┬──────────────────────────────────────────────────┤
│ Sidebar  │  Main view area (scrolls; padding 26px 30px 40px) │
│ (238px)  │                                                    │
│          │                                                    │
└──────────┴──────────────────────────────────────────────────┘
```

- **Body font:** `Geist` (UI/body), `Geist Mono` (all IDs, agent names, counts, timers, code/paths), `Space Grotesk` (headings, section titles, big stat numbers, wordmark). Loaded from Google Fonts.
- **Base font size:** 14px. **Antialiased.**
- Main area is the only vertical scroller. Sidebar and top bar are fixed.

### Top bar (56px)
Left→right, `gap: 18px`, `padding: 0 22px`, background `surface`, `border-bottom: 1px solid border`:
1. **Wordmark**: 28×28 rounded-8px gradient square (`linear-gradient(135deg, accent, accent@70%)`, soft accent shadow) + "agenttalk" (Space Grotesk 600, 16px, letter-spacing −.01em) over "TEAM CONSOLE" (10.5px, uppercase, letter-spacing .04em, `ink3`).
2. **Mission pill**: rounded-8px `surface2` chip, `border`, `padding 6px 12px`, 12px `ink2`: a checklist icon + "spec-kitty · lanes" (500) + "WP 7/12" (mono, `ink3`).
3. Spacer.
4. **Live indicator**: 8px green (`ok`) dot with breathing pulse + "Live" (500) + current clock `HH:MM:SS` (mono, `ink3`).
5. Vertical divider (1px × 24px, `border`).
6. **Operator chip**: 26×26 rounded-7px `accentSoft` avatar w/ `accent` initials ("yo") + "you" (12px 500) over "operator · lead-liaison" (10px, `ink3`).

### Sidebar (238px)
Background `surface`, `border-right: 1px solid border`, `padding 14px 12px`, flex column:
- Section label "VIEWS" (10px 600, uppercase, letter-spacing .09em, `ink4`).
- **Nav items** (4), each: rounded-8px row, `padding 9px 11px`, `gap 11px`, 17px stroked icon + label (13px 500) + optional count badge.
  - **Active** item: `color: accent`, `background: accentSoft`.
  - **Inactive**: `color: ink2`; hover → `background: hairline`, `color: ink`.
  - Items: **Team overview** (grid icon), **Conversations** (chat icon), **Attention** (alert-triangle icon; red count badge = number of open attention items), **Sessions** (file-text icon).
  - "Team overview" is also the active item while an Agent Detail view is open.
- Spacer.
- **Status legend** (top border `border2`): label "STATUS LEGEND" + 4 rows, each a 9px status dot + label (12px `ink2`) + count (mono `ink3`): Working / Idle · waiting / Needs attention / Unknown · offline.

---

## Screens / Views

There are **5 views**, switched by sidebar nav + drill-in. Only one is visible at a time.

### 1. Team overview  (default — "Who's doing what")
**Purpose:** the at-a-glance answer to "what is every agent doing right now."

Layout, top to bottom:
- **Header row:** H1 "Who's doing what" (Space Grotesk 600, 23px, −.015em) + subtitle "`N` agents · 1 mission active · `N` need attention" (13px `ink3`). Right-aligned **filter chips**: All / Working / Idle / Attention, each with a count. Selected chip = filled `accent`, white text; others = `surface` bg, `border`, `ink2`. Clicking filters the agent grid.
- **Stat tiles** (4, equal columns, `gap 14px`): each a `surface` card (`border`, radius 12px, `padding 16px 18px`, subtle shadow) with a status dot + label (12px `ink3`) and a big number (Space Grotesk 600, 30px). Tiles: Working, Idle, Needs attention, Unknown.
- **Two-column body** `grid-template-columns: minmax(0,1fr) 336px; gap 20px`:
  - **Left — Agent grid:** `repeat(auto-fill, minmax(258px, 1fr)); gap 14px`. One **Agent Card** per agent (see component below).
  - **Right — Live activity rail** (sticky): a `surface` card. Header "Live activity" (Space Grotesk 600, 13.5px) + pulsing green dot + "bus messages" (11px `ink4`). Body: a scrolling list of recent **Feed Items**, newest on top; new arrivals animate in (`fadeInUp`, 0.45s) with an accent left-border and `accentSoft` background for one cycle.

#### Component: Agent Card (clickable → Agent Detail)
- `surface` bg, `border`, radius 12px, `padding 15px 16px` (comfortable) / `13px 14px` (compact), flex column, `gap 9px`.
- **Left status bar:** `box-shadow: inset 3px 0 0 <statusColor>, 0 1px 2px shadow`. Hover raises shadow to `0 4px 16px shadowLg`.
- Row 1: **status dot** (10px; the `working_turn` state dot has a breathing `liveDot` animation) + **agent name** (Geist Mono 13px 500, `ink`) + **CLI badge** + spacer + **wrapped icon** (13px, `ink5`) if the agent runs wrapped.
- Row 2: "role · group" (11.5px `ink3`).
- Row 3: current task, 12.5px `ink2`, line-height 1.42, `min-height: 36px` (≈2 lines).
- Row 4: **status chip** (colored pill) + spacer + heartbeat age (mono 11px `ink4`; "no hb" when unknown).
- Row 5 (top border `hairline`, `padding-top 10px`): two mini meters side by side — **RATE** and **CTX** — each a 9.5px mono label + value and a 4px track (`track` bg) with a fill bar colored by threshold (see Meter colors).

#### Component: Feed Item
- `padding 10px 11px`, radius 9px, `surface2` bg (or `accentSoft` + accent left-border if fresh).
- Row 1: `from` (mono 11.5px 500 `ink2`) → arrow icon (`ink5`) → `to` (mono 11.5px `ink3`) + spacer + age (mono 10.5px `ink5`).
- Row 2: **kind chip** + subject (12px `ink2`, ellipsis, single line).

### 2. Conversations  ("Who's talking to whom")
**Purpose:** message flow across the team.

- Header: H1 + subtitle "Message flow across the team · line weight = volume · dashed = active review".
- Two columns `minmax(0,1fr) 340px`:
  - **Left — relationship graph** in a `surface` card (overflow auto). Fixed 640×480 canvas: agents laid out on a **circle** (radius 178 around center 320,240; angle `-90° + i·36°` for 10 nodes). **Edges** are SVG `<line>`s between agents who share a thread; stroke width scales with message volume (`w·1.4 + 0.6`), base stroke `edge` at ~0.55 opacity; the **active-review edge is `accent`, dashed, and animates** (`dashmove`). **Nodes** are HTML: a status-colored dot (3px `surface` ring; `working_turn` pulses) + a mono name pill (`surface` bg, `border`). Clicking a node → Agent Detail.
  - **Right — Active threads** list in a `surface` card. Each row (clickable → Sessions with that thread selected): **kind chip** + spacer + **status chip**; subject (13px 500 `ink`); "a ⇄ b" participants (mono 11px `ink3`) + age.

### 3. Attention  ("Needs a human")
**Purpose:** a single ranked queue of everything blocking or needing an operator.

- Header: H1 "Needs a human" + subtitle "Ranked queue — escalations, gate holds, stuck agents, dead letters" + a red "`N` open" count chip. Constrained to `max-width: 920px`.
- **List** of item cards (`gap 12px`). Each card: `surface`, `border`, radius 12px, `padding 16px 18px`, flex row, and a **severity left bar** (`box-shadow: inset 3px 0 0 <sevColor>`).
  - Left block: row of **source tag** (solid-colored pill, white text — e.g. ESCALATION, GATE HOLD, STUCK, DEAD LETTER, SUPERVISOR) + **severity chip** (HIGH/MED/LOW) + spacer + age. Then title (14.5px 500 `ink`). Then an **agent chip** (mono, `inset` bg) + a detail line (12px `ink3`).
  - Right block: **action buttons** — first is primary (filled `accent`), rest are ghost (`surface`, `border`). Wired behaviors in the prototype: *Restart with context* recovers the stuck agent and removes the item; *Resolve/Arm/Dismiss/Defer* remove (dispose) the item; *Inspect* opens that agent's detail.
- **Empty state:** centered card with a green check badge, "All clear", "Nothing is waiting on you right now."

Item sources & severities in the sample data (map these to real agenttalk concepts — see Data Sources): `escalation` (needs_operator) = high; `gate` HOLD = high; `stuck` = med; `deadletter` = med; `supervisor` (lead-loop unarmed / advisory) = low.

### 4. Sessions  (transcript viewer)
**Purpose:** read the full transcript of any message thread.

- Header: H1 "Sessions" + subtitle.
- Two columns `264px minmax(0,1fr)`:
  - **Left — thread list** in a `surface` card: each row (clickable) = kind chip + status chip; subject (12.5px 500); "a ⇄ b" (mono 10.5px `ink3`). Selected row = `accentSoft` bg + `accent` left border.
  - **Right — transcript** in a `surface` card. Header: subject (Space Grotesk 600, 15px) + "`id` · a ⇄ b" (mono 11px `ink3`) + status chip. Body on `surface2` bg, `padding 22px 26px`, `gap 16px`:
    - **Message bubbles** aligned by sender (thread's first participant = left/`surface`, other = right/`accentSoft`), `max-width 82%`, radius 12px, `border`. Bubble header: CLI badge + sender name (mono 12px 500) + kind chip + spacer + age. Body: 13px, line-height 1.5, `white-space: pre-wrap` (bodies contain markdown-ish `## Goal` sections — render as preformatted text, not parsed markdown). Optional meta footer (mono 10.5px `ink4`, top border) e.g. `status=approved`, `head 7b2d9c1`.
    - **System events** (`wake`, `end`): centered separator — hairline rule + kind chip + sender, no bubble.

### 5. Agent Detail  (drill-in from a card or graph node)
**Purpose:** everything about one agent.

- **Back button** ("← Back to overview", `ink3`, hover `ink`).
- **Header card:** big status dot (16px, 5px soft-color ring; pulses if working) + agent name (Geist Mono 20px 500) + CLI badge; below: status chip · "role · group" · "since `<age>`". Right: **action buttons** — "Restart with context" (primary; only shown when the agent is `stuck_suspected`) and "Open transcript" (ghost).
- **Two columns** `minmax(0,1fr) 306px; gap 18px`:
  - **Left:**
    - **Current work** card: task (15px `ink`), then tag pills "mission · X", "WP-XX", "peer · Y".
    - **Health timeline · last 30m** card: a 26px-tall horizontal segmented bar (`gap 2px`), each segment flex-weighted by duration and colored by state, with a small legend below.
    - **Recent messages** card: rows of kind chip + direction ("→ to" / "← from", mono `ink2`) + subject + age. Empty → "No recent traffic on the bus."
  - **Right:**
    - **Capacity** card: two labeled meters — "5-hour rate limit" and "Context window" — each a 7px track + threshold-colored fill + a note ("Headroom for new work" / "Near cap — steer long work elsewhere"; "Comfortable context budget" / "Compaction risk — avoid heavy context").
    - **Supervisor** card: rows for CLI (badge), Mode ("wrapped · loop" / "manual listen"), Heartbeat ("`<age>` ago" / "missing"), Restartable (yes/no; green if wrapped).
    - **Owned domains** card: per owned domain, a title + mono glob(s).

---

## Interactions & Behavior
- **Navigation:** sidebar switches views; clicking an agent card or graph node opens Agent Detail; clicking a thread (Conversations or its row) opens Sessions with that thread selected; Back returns to overview.
- **Live simulation (prototype only):** a 1s interval advances all "age" timers in real time and, every ~7s, prepends a new synthetic Feed Item (capped at 7, oldest dropped) with a `fadeInUp` entrance. In the real app, replace this with **polling the bus** (e.g., every 1–3s) or file-watching; recompute ages from timestamps client-side each tick so counters feel live. This behavior is gated by a `simulate` flag.
- **Operator actions (prototype):** *Restart with context* flips the stuck agent to a recovered "working" state and clears its attention item; disposition actions remove the attention item; *Inspect* deep-links to the agent. In the real app these map to agenttalk commands (see Data Sources) and should reflect real results.
- **Animations:** `liveDot` (opacity 1→.32→1, 1.8s ease-in-out, infinite) on active/working status dots and the Live indicator; `dashmove` (stroke-dashoffset → −24, 1s linear, infinite) on the active-review graph edge; `fadeInUp` (translateY 7px→0 + fade, 0.45s) on new feed items. Card hover raises box-shadow (.15s). Meter fills transition width (.5s ease).
- **Hover states:** nav rows, filter chips, thread rows (→ `surface2`), buttons (primary → brightness 1.06; ghost → `hairline`), cards (shadow lift).
- **Responsive:** designed for a wide desktop viewport (~1280px+). Graph canvas is fixed-size and scrolls within its card on narrow widths. Not optimized for mobile.

## State Management
Client state needed:
- `view`: one of `overview | flow | attention | sessions | agent`.
- `selectedAgentId`: which agent's detail is open.
- `sessionId`: which thread the Sessions view shows.
- `filter`: overview agent filter (`all | working | idle | attention`).
- `now`: a ticking clock (drives all relative ages).
- `dismissed`: set of dismissed/resolved attention item ids.
- transient recovery state for a restarted agent.
- Tweakable prefs: `theme` (light/dark), `accent`, `simulate` (on/off), `density` (comfortable/compact).

Derived per tick: agent counts by category; filtered agent list; relative age labels; attention list minus dismissed; feed list.

## Data Sources (map the sample data to the real bus)
Everything in the prototype corresponds to real agenttalk state under a project's `.agenttalk/` directory and CLI. Suggested bindings:

- **Agents / roster / roles / groups:** `agenttalk roster --json`. Operator-facing agent = the lead-liaison.
- **Per-agent health/state:** the `state/<agent>.health.json` advisory snapshots. The status vocabulary in the design maps **directly** to agenttalk's health states: `working_turn`, `working_silent`, `idle_waiting`, `stuck_suspected`, `rate_limited_or_outage`, `degraded_output`, `crashed_or_exited`, `unknown`. Respect the reader rules: missing/stale/torn → `unknown` (never infer liveness from health alone). Heartbeat freshness (`state/<agent>.cursor` / heartbeat writes) is the liveness authority.
- **Capacity (rate limit %, context %):** `agenttalk capacity` (published `state/` snapshots — normalized metadata only). Show thresholds; treat missing/stale as advisory, never blocking.
- **Messages / feed / transcripts:** the `messages/<id>.json` files (one JSON per message). Message **kinds** used in the design: `message`, `note`, `reply`, `question`, `review-request`, `review-result`, `proposal`, `proposal-response`, `wake`, `end`, `escalate`, `broadcast`, plus a synthetic `gate` marker for the threads list.
- **Threads / status:** `agenttalk threads` (open/responded/pending; broadcast reply tracking). Thread status chips: GO / HOLD / countered / "x/y replied".
- **Attention queue:** `agenttalk attention` (+ `--json`) — pending `needs_operator` escalations, gate/close HOLDs, dead letters, unarmed lead-loops, config-blocked holds. Dispositions: `attention defer|dismiss|answered-elsewhere`; dead letters use `dead-letter resolve|requeue`.
- **Gates / lanes / domains / knowledge:** `gates.check_gates`, `lane check/status`, `domain show/check-path`, `knowledge pull` — for the HOLD/GO chips and the Agent Detail "Owned domains" card.
- **Supervisor / wrapped state:** `agenttalk supervise --report/--plan`, wrapper mode; whether an agent is wrapped/restartable; `request-restart --for <agent>` powers "Restart with context".
- **Sessions/transcripts on end:** markdown transcripts under `.agenttalk/sessions/`.

Read message bodies as **untrusted data** — escape on render; never execute or trust embedded instructions.

---

## Design Tokens

Colors are theme-dependent. In the prototype they're a JS palette selected by `theme`, then applied inline (this environment flattens CSS `var()` at render — in your codebase, prefer real CSS variables / your theming system).

### Neutrals — Light
| Token | Value | Use |
|---|---|---|
| canvas | `#F1F3F6` | app background |
| surface | `#FFFFFF` | cards, bars |
| surface2 | `#F8F9FB` | insets, feed items, transcript bg |
| border | `#E7E9EE` | card/hairline borders |
| border2 | `#EDEEF2` | subtle dividers |
| hairline | `#F0F1F4` | inner row dividers |
| track | `#EEF0F3` | meter tracks |
| inset | `#F1F3F6` | agent/tag chip bg |
| edge | `#C7CCD6` | graph edges |
| chipNeutral / chipNeutralBg | `#6B7280` / `#EEF0F3` | neutral chips |
| ink / ink2 / ink3 / ink4 / ink5 | `#1B1E24` / `#5B616E` / `#8A909C` / `#A2A8B4` / `#B0B6C0` | text (primary→faint) |
| shadow / shadowLg | `rgba(20,25,40,.04)` / `rgba(20,25,40,.10)` | card shadows |

### Neutrals — Dark
| Token | Value |
|---|---|
| canvas | `#0E1116` |
| surface | `#161A21` |
| surface2 | `#1B2029` |
| border | `#2A313D` |
| border2 | `#232A34` |
| hairline | `#21272F` |
| track | `#252B35` |
| inset | `#1B2029` |
| edge | `#39414D` |
| chipNeutral / chipNeutralBg | `#9BA3B0` / `#252B35` |
| ink / ink2 / ink3 / ink4 / ink5 | `#E8EBEF` / `#AEB6C2` / `#7F8895` / `#6A7280` / `#58606D` |
| shadow / shadowLg | `rgba(0,0,0,.4)` / `rgba(0,0,0,.55)` |

### Semantic status colors (color / soft-background)
| Token | Light color | Light soft | Dark color | Dark soft | Meaning |
|---|---|---|---|---|---|
| ok | `#12996A` | `#E4F5EE` | `#3DBE8B` | `rgba(61,190,139,.16)` | working / GO / healthy |
| info | `#2F7AC7` | `#E7F1FB` | `#5AA0E0` | `rgba(90,160,224,.16)` | working-quiet / question / broadcast |
| warn | `#B4820A` | `#FBF2DA` | `#D9A22E` | `rgba(217,162,46,.16)` | idle / medium |
| attn | `#E4681F` | `#FCEBDE` | `#F0803F` | `rgba(240,128,63,.16)` | stuck / needs attention |
| danger | `#D8443F` | `#FBE7E6` | `#E86560` | `rgba(232,101,96,.18)` | rate-limited / error / HOLD / escalate |
| violet | `#6D4AC0` | `#EEE8FA` | `#9E82E0` | `rgba(158,130,224,.18)` | proposal / countered |
| teal | `#2E9E8F` | `#E1F2EF` | `#42B9A8` | `rgba(66,185,168,.16)` | wake |
| gray | `#98A0AE` | `#EFF1F4` | `#8A929E` | `rgba(138,146,158,.16)` | unknown / offline |

### CLI badge colors (color / bg)
| CLI | Light | Dark |
|---|---|---|
| claude | `#B0532C` / `#F6EBE3` | `#D08A5E` / `rgba(208,138,94,.16)` |
| codex | `#2C7A6B` / `#E4F1EE` | `#4FB3A0` / `rgba(79,179,160,.16)` |

### Accent (theming; user-selectable)
- Default `#4457E6`. Options: `#4457E6`, `#2F7D6B`, `#B0532C`, `#6D4AC0`, `#2563C9`.
- `accentSoft` = accent at 12% alpha (light) / 22% alpha (dark).

### Meter fill colors (rate & context)
- `< 60%` → `ok`; `60–84%` → `warn`; `≥ 85%` → `danger`.

### Typography scale (px)
- H1 (view title): Space Grotesk 600, 23, letter-spacing −.015em.
- Stat number: Space Grotesk 600, 30, −.02em.
- Card/section title: Space Grotesk 600, 13–15.
- Body: Geist 400/500, 12.5–15, line-height ~1.42–1.5.
- Agent name: Geist Mono 500, 13 (card) / 20 (detail).
- Chips / labels: Geist Mono 500, 10 (chips) / 9.5–11 (meta), uppercase section labels 10–11 with .08–.09em tracking.

### Spacing / radius / shadow
- Grid/section gaps: 12–20px. Card padding: 15–22px (13–14px compact). Page padding: 26px 30px 40px.
- Radius: chips 5–6px; buttons/rows 8px; cards 12px; header card 14px; dots/pills 50%/999px.
- Shadow: cards `0 1px 2px shadow`; hover `0 4px 16px shadowLg`; status left-bar via `inset 3px 0 0 <color>`.

### Tweakable options (expose in the real app)
- **theme:** light / dark.
- **accent:** the 5 options above.
- **simulate / live updates:** on / off (in prod: polling interval).
- **density:** comfortable / compact (affects card padding).

## Assets
- **Fonts:** Google Fonts — `Space Grotesk` (500/600/700), `Geist` (400/500/600), `Geist Mono` (400/500). Swap for locally hosted files if offline use is required.
- **Icons:** inline SVG, Lucide-style stroked paths (grid, chat, alert-triangle, file-text, arrow, back-arrow, checklist, check, wrapped-frame). No icon font. `stroke-width` ~1.9–2.2, `currentColor`.
- **Images:** none. No raster assets, no logos beyond the CSS gradient wordmark square.

## Files
- `Team Console.dc.html` — the full design reference (all five views, both themes, sample data, and interactions) in one self-contained file. Open it in a browser to explore; read the JS `renderVals()` for exact per-element style objects and the sample data shapes.
