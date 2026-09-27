# Turn 1 & 2 reference (exploration boards 1a–1d, 2a–2d)

These boards are the exploration that led to the chosen direction. Build from `docs/03-SPEC-APP.md`; use this file for detail on surfaces the app inherits (gates, lanes, ownership, risk, tasks, lessons, onboarding) and for the other three directions.

## Canvas layout
One scrolling canvas (`#141519` background, 64px side padding), newest turn at the top:
- **Turn 2 header**: eyebrow, title and description, plus two mode chips that only *display* the actions state.
- **Gap grid**: 4 columns, 12 cards. Each card has a source tag (`REPO VIEW` / `REPO CLI` in teal `#7CE7C4`, `PROPOSAL …` in amber `#F5B83D`), a title, the reason it was missing, and a link to the board that now covers it.
- **Boards 2a–2d**, then **Turn 1 header**, a "Switch all at once" theme row, and **boards 1a–1d**.
- Every board is **1240×780**. Each has an id badge (`1a` etc.: Geist Mono 13/600, `#0B0C0F` on `#E8E9ED`, radius 6) with a title (Space Grotesk 21/600) and a one-line description (13.5, `#8B8F98`).

The canvas itself is presentation only. In the product, each direction becomes the whole app.

## Shared data model (all boards)
Agent: `{id, ini, cli: claude|codex, role, state, task, rate, ctx, peer, hb}`
- `state` is one of `working | quiet | idle | stuck | limited`. It maps to the repo's health states: `working_turn`, `working_silent`, `idle_waiting`, `stuck_suspected`, `rate_limited_or_outage`.
- `hb` is the heartbeat age in seconds, displayed as `8s`, `6m 12s`, etc.
- Threshold coloring for rate and ctx: **< 60 ok, 60–84 warn, ≥ 85 bad**.

Other entities (full examples in `fixtures.json`):
- **Attention item**: `{id, sev: bad|warn, src, title, meta, actLabel, act}`. Sources: ESCALATION, GATE HOLD, STUCK.
- **Bus message**: `{from, to, kind, subj, t}`. Kinds: review-request, review-result, wake, note, message, reply, broadcast, escalate. A new message is highlighted for one tick.
- **Gate**: `{id, name, scope, state: GO|HOLD|WAIVED, evidence, verifier, expires}`.
- **Close verdict**: HOLD if any gate is HOLD, otherwise GO. Exit code 3 or 0, matching `agenttalk close check`.
- **Lane**: `{id, owner, domain, stage 0..3 (assigned→checked→delivered→cleaned), hist: [R|F|A], cycle, rev, hold?}`.
- **Domain**: `{name, globs, owner, rev, shared?}`, from `domains.json`.
- **Risk**: `{id, sev: HIGH|MED, src, title, owner, age}`. "Accepted" risks expire after 7 days.
- **Program stages** (roadmap): Assessed, Plan approved, Reimplemented, Parity verified, Acceptance-ready, Cutover-ready, Decommissioned. Each stage is done, partial or not yet, and shows one line of proof.
- **Lead stream item** types: `lead`, `you`, `event`, `task` (status accepted / done / declined / pending), `decision` (title, impact, deadline, options), `lesson` (body, domain, who), `unknown` (onboarding blocker, answerable).
- **Project**: `{label, id}`. The switcher changes the whole view scope (repo `project_id`).
- **Roster scope**: `active` (the current run) or `all`, which adds retired agents as dashed, 45%-opacity "retired · tombstoned" rows.

### Cross-board behavior (one shared store)
- **Sending to the lead**: shows "claude-lead is typing" for 1.3 s, then the reply arrives. Both the message and the reply are added to the bus feed.
- **Commands** (sent as normal messages to the lead):
  - `/status`
  - `/approve m-4.2`: stays HOLD; the lead offers to request a waiver.
  - `/restart claude-arch`: the agent goes back to working, its heartbeat resets, and its STUCK item is removed.
  - `/reassign WP-11`, or "Yes, reassign…": removes the escalation.
  - `/evidence m-4.2`
- **Attention "Later"**: defers the item and hides it for the session.
- **Themes**: one theme per direction (`th1..th4`). The "Switch all at once" row sets all four to index 0, 1 or 2.

---

## Turn 1

### 1a Terminal Garden — keyboard-first text console
- **Frame**: radius 14, JetBrains Mono 12.5 throughout, text-shadow `0 0 6px {soft}`. Two overlays on top: scanlines (`repeating-linear-gradient(0deg, rgba(0,0,0,.22) 0 1px, transparent 1px 3px)`, which the `crt` prop turns off) and a vignette (`inset 0 0 140px rgba(0,0,0,.6)`).
- **Top bar (44px)**: `agenttalk://team` in hot + glow, `mission:lanes`, `[▮▮▮▮▮▮▮▯▯▯▯▯] 7/12`, theme chips, and `● LIVE hh:mm:ss`.
- **Body grid**: `minmax(0,1fr) 360px`, gap 18, padding 22/18/16. Panels have a 1px `{line}` border and a title set into the top-left of the border (`top:-9px`, padded on `{bg}`).
- **TEAM table**: columns `22px 108px 76px minmax(0,1fr) 84px 84px` (ST, AGENT, STATE, TASK, RATE, CTX).
  - Glyphs: ● working (pulses), ◐ quiet, ○ idle, ▲ stuck, ■ capped.
  - Bars are five characters (`▮▮▮▯▯ 58`), colored by threshold.
  - The selected row is inverted (`{fg}` background, `{selInk}` text, no glow).
- **INSPECT panel**: a key/value grid (110px keys) showing state, task, peer, runtime and heartbeat. Footer hints: `[m] message [t] transcript [r] restart [esc] back`.
- **BUS TAIL**: newest message at the bottom, followed by a blinking `█`. Grid `64px 1fr`: time, then `from → to kind subj`, with kind colored.
- **NEEDS YOU [n]**: `[!]` in bad or warn, title and meta, and a key action `[1] reassign` that sends the action.
- **Command line**: `lead>` prompt with an input. Enter sends. The shortcut legend shows the last lead line on the right.
- **Themes**: phosphor, amber, ice (`themes.json`).

### 1b Orbital — the team as a star system
- **Frame**: radius 22, Sora. Background is a radial `{glow}` at the center over `{bg}`. Panels are glass: `{glass}` fill, 1px `{border}`, `backdrop-filter: blur(16px)`, radius 18–22.
- **Header (68px)**: ring logo (24px, 2px accent border, glow), a segmented control (Orbit / Threads / Sessions; active item uses `{fg}` background with `{bg}` text), theme segments and clock.
- **Orbit** (600×600 stage at left 320, top 68):
  - Two rings: 500px solid and 340px dashed, both in `{ring}`.
  - Core (176px): conic progress ring `accent 0–210°` (7 of 12), with `LANES / 7/12 / work packages` inside.
  - Agents sit at radius 250, starting at −90° and spaced 45° apart. Each orb is 60px (70px when selected) with a 2px state-colored border. Working agents get a glow of `0 0 28px` at 55% of the state color; the selected agent gets an extra `0 0 7px` accent ring at 22%.
  - Edges: the active review pair is drawn in `accent2`, dashed `4 8` and animated `dash 1s linear infinite`. The others are accent at 35% opacity.
- **IN FOCUS panel** (left, 290 wide): name (Geist Mono 19), state and CLI pills, task, rate and context bars (5px, gradient accent→accent2), peer and heartbeat. Buttons: **Ask lead** (pre-fills the lead input) and **Transcript**.
- **SIGNALS**: the four latest bus messages, each with a glowing kind dot.
- **NEEDS YOU** (right): glass cards with a primary action and **Later**. When empty: "Clear skies — nothing needs you."
- **Composer**: a 640-wide pill fixed to the bottom. Inside it: the LE avatar, a one-line preview of the lead's last reply (ellipsized) above the input, and a 48px round send button.
- **Themes**: nebula, solar, void.

### 1c Loud — neo-brutalist zine
- **Frame**: square corners, Archivo (900 for headlines), Space Mono for metadata.
- **Hard shadows** everywhere: `5px 5px 0 {ink}`. On hover, cards move by `translate(-2px,-2px)` and the shadow grows to 8px (0.12 s).
- **Header**: the `AGENTTALK` sticker (hl background, 2px ink border, rotated −2°) and theme chips (2px border with a 3px hard shadow).
- **Headline** (Archivo 900, 60/0.95, −0.04em, uppercase): "8 agents. 5 busy. **3 need you.**" The last phrase sits on `{hot}`. Next to it, a rotated LIVE badge (88px circle).
- **Tile grid**: 4×2. Each tile shows name, a CLI pill (rotated −4°; claude pills are filled), and the status word at 30px/900:
  - BUSY: `inset 0 -11px 0 {hl}` underline
  - THINKING: italic
  - WAITING: muted
  - STUCK / CAPPED: `{hot}` block
  - Below the status word: the task, then RATE and CTX bars (12px, 2px bordered).
- **NEEDS YOU** panel: `{ink}` background with a `{hl}` hard shadow, scrolls if it overflows. Each item has its source in hl, the title, and a primary button plus **LATER**.
- **LEAD SAYS →** box with an input ("TELL THE LEAD…").
- **Ticker** (46px): hl strip, `marquee 38s linear infinite`, with the bus list duplicated so the loop is seamless.
- **Themes**: acid, bubblegum, riso.

### 1d The Conversation — lead-first
- **Frame**: radius 18. Grid `minmax(0,1fr) 320px`. Geist, with Instrument Serif for display.
- **Header (58px)**: logo, project, theme segments, clock.
- **Presence strip**: 44px avatars with a 2px state-colored border. Working avatars get a 4px ring at 20% and a slow pulse. Short names (`cl-dev`, `cx-rev`); the lead's name is shown in accent.
- **Stream**: starts with a serif greeting (36/1.12): "Evening. WP-07 just cleared review — *a few things need you.*" (the italic part in accent).
  - Lead bubble: panel background, radius `4 16 16 16`, with optional quick-reply pills (the first pill is filled).
  - Your bubble: aligned right, accent background, radius `16 4 16 16`.
  - Event row: 30px tone icon (✓ ok, ‖ bad, ! warn), title and meta, plus action pills.
  - The stream auto-scrolls to the bottom when new items arrive.
- **Command chips**: `/status`, `/approve m-4.2`, `/restart claude-arch`, `/reassign WP-11`. The composer has a `/` hint.
- **Rail**: Mission `lanes 7/12` (12 segments: done in ok, current in accent with a pulse, the rest in border color), then "Right now", listing each agent with a dot, name, state, task and a 3px rate bar.
- **Themes**: midnight, paper, synthwave.

---

## Turn 2 (each board reuses its direction's frame and theme)

### 2a Terminal Garden · Gates & close
- **Header**: `agenttalk://gates`, project switcher, `snapshot:current|stale`, a mode badge (`ACTIONS · loopback operator` in warn when on, `READ-ONLY · act via CLI` otherwise), and theme chips.
- **CLOSE panel**: a 44px verdict with glow (HOLD in bad, GO in fg), "N blocking gates without evidence or waiver", review and waiver counts, and `exit 3|0 · agenttalk close check m-4.2`. The panel border takes the verdict color.
- **GATES table**: columns `22px 130px 70px 72px 1fr 96px` (ST, GATE, SCOPE, STATE, EVIDENCE, VERIFIER). Glyphs: ● GO, ■ HOLD, ~ WAIVED. The selected row is inverted.
- **EVIDENCE panel** for the selected gate: state, evidence, "verifier · independent of author", expiry.
  - `[w] waive as operator (7d)` works only when actions are on **and** the gate is HOLD. It sets the gate to WAIVED with a 7-day expiry, and the close verdict recomputes.
  - Otherwise the control is struck through, with the CLI hint.
- **WAIVERS**: gate, "by operator", reason, and an expiry bar `[▮▮▮▮▯▯▯]` that turns warn at 4 days or less.
- **SIGN-OFFS by risk class**: ✓ approved, · pending, – n/a. Footnote: "one-shot reviewers count as evidence, never as sign-offs".
- **BREAKERS & DEAD LETTERS**: commit-gate breaker state per agent, poison count, retry ceiling.

### 2b Orbital · Lanes & ownership
- **Lane ledger** (glass panel): columns `90 150 1fr 150 96` (LANE, OWNER, the four-stage track, REVIEW ROUNDS, CYCLE).
  - Track: nodes are 11px, or 16px with glow for the current stage, joined by 44px connectors. Blocked lanes use warn and show a `HOLD` tag.
  - Review history: 24px circles, R in bad, F in warn, A in ok.
  - The selected row gets an accent 12% background and an accent 35% border.
- **LANE CHECK**: 22px verdict (GO in ok, HOLD in warn) and reason, plus domain, worktree `.agenttalk/worktrees/<id>`, `base → lanes/<id>` and reviewer.
  - **Deliver** works only when actions are on, the lane isn't blocked, and its stage is below 2. It advances the lane to delivered.
  - Otherwise the button reads "Deliver · CLI only", "Blocked" or "Delivered".
- **OWNERSHIP MAP** (from `domains.json`): domain name, globs (mono 10.5, dim), and `owner · rev reviewer` in accent. Shared paths get a `SHARED` tag in warn.

### 2c Loud · Program & risk
- **Headline**: "Parity verified. **Not cutover-ready.**"
- **Seven stage boxes**:
  - done: paper background, hard shadow, "✓ PROVEN"
  - partial: hl background, "◐ PARTIAL"
  - not yet: transparent, 70% opacity, "✗ NOT YET" in hot
  - Each box also shows its one line of proof.
- **RISK REGISTER**: a 2-column grid of cards. Each card has a severity chip (HIGH on hot, MED on hl), source, age, title, and `OWNER x · OPEN`. The button reads "ACCEPT RISK · 7D" when actions are on; accepting drops the card to 60% opacity and changes the state to "ACCEPTED · EXPIRES 7D".
- **THE PROOF**: four big numbers (41, 39, 6, 1) with labels.
- **Export**: **EXPORT SIGNED BRIEFING →** changes to "✓ BRIEFING-2026-09-25.HTML". The note under it: a static, timestamped file with no remote access to the live console.

### 2d The Conversation · Tasks, decisions, memory
- **Stream item types**:
  - **TASK**: accent outline tag, `claude-lead → agent · work order`, status pill (accepted in info, done in ok, declined in bad, pending in warn).
  - **DECISION CENTER**: warn border until decided, "decide by Fri 18:00 · owner you", serif 24 title, impact text, option pills. Picking an option locks the card, marks the choice with ✓, and sends it. "Rehearse first" also adds a new pending TASK.
  - **LESSON**: dashed border, ok tag, `knowledge publish · domain · taught to`.
  - **BLOCKING UNKNOWN**: bad tag, onboarding ledger segment, an **Answer** button that sends an answer and changes to "✓ answered".
- **Composer placeholder**: "Message the lead — it can issue tasks, not you". The operator doesn't dispatch work directly.
- **Rail**:
  - Snapshot freshness dot (ok with pulse, or warn) and snapshot age.
  - Roster toggle: `Active run · 8` / `All roster · 10`.
  - Agent rows with a capacity-confidence value: fresh (fg), `93%~` stale (warn), `?` unknown (dim).
  - Legend line explaining the three confidence states.

---

## Design tokens
- **Per-direction palettes**: `themes.json` has all 12 themes, every key with exact hex/rgba values.
  - Keys for 1a: `bg line fg dim hot warn bad info glow soft selInk`
  - Keys for 1b: `bg glow glass border ring fg dim accent accent2 track ok info warn bad node`
  - Keys for 1c: `bg paper ink hl hlInk hot hotInk mute`
  - Keys for 1d: `bg panel panel2 border fg dim accent accentInk ok warn bad info serif`
- **Canvas chrome**: `#141519` background, cards `#1B1C21` with `#2A2C33` border, text `#F4F5F7` / `#9A9EA8` / `#8B8F98`. Links `#9AA6FF`, hover `#C4CBFF`.
- **Existing console design system**: `design-system/` (tokens, components, guidelines, readme). This is the calm baseline the current console uses. Keep its status semantics (ok / info / warn / attn / danger / violet / teal / gray) consistent across every new theme.
- **Type**:
  - JetBrains Mono (1a)
  - Sora (1b)
  - Archivo 500/700/900 plus Space Mono (1c)
  - Geist, Geist Mono and Instrument Serif (1d)
  - Space Grotesk (canvas titles)
  - All from Google Fonts. Self-host them in production; the repo is offline-first.
- **Motion**:
  - `pulse`: opacity 1 → 0.35, 1.4–2.4 s ease-in-out
  - `blink`: 1 s, `steps(1)`
  - `dash`: stroke-dashoffset −24, 1 s linear
  - `marquee`: 38 s linear
  - Lane transitions: 0.25 s
  - Hover lift: 0.12 s
  - Respect `prefers-reduced-motion` and disable all of the above.
- **Radii by direction**: 1a 14 frame, square inside · 1b 22 frame, 18–22 panels, 999 pills · 1c 0 · 1d 18 frame, 14–16 cards, 10–12 controls.

## Assets
- `prototype/avatars/`: round avatars for the 10 agents (`claude-lead.png` etc.), from the earlier round.
- `avatars-shaped/`: 60 transparent shaped avatars in 6 silhouettes (hexagon, triangle, star, rounded-square, oval-vivid, oval-muted), plus `contact-sheet.png`.
  - Do **not** crop these into circles. Each avatar's shape is its alpha channel.
  - The prototypes currently use two-letter initials. Swapping avatars into the orbs (1b), presence strip (1d) and roster rails is an optional enhancement; pick one silhouette per theme.
- **Icons**: inline Lucide-style strokes (arrow, send). No icon font, no emoji.

## Files
- `prototype/Console Futures.dc.html`: all 8 boards, fully interactive. The props at the top of the logic class are `actions`, `live` and `crt`.
- `prototype/Team Console.dc.html`: the current calm console redesign (5 views plus lead chat, light and dark). Use it as the functional baseline and for view structure.
- `prototype/support.js`: runtime needed to open the `.dc.html` files. Not for production.
- `themes.json`: the 12 palettes, exact values.
- `fixtures.json`: mock agents, attention items, gates, lanes, domains, risks, retired agents and projects. Use them to seed tests and dev snapshots.
- `design-system/`: tokens (`colors.css` light and dark, `typography.css`, `spacing.css`, `effects.css`), reference components (Button, Chip, StatusDot, CliBadge, Meter, Card, AgentCard, MessageBubble, nav and more; each with `.jsx`, `.d.ts` and prompt notes), guideline cards and the design readme.
- `github.md`: source repo, branch, and a map from each board to the repo files it was derived from.

## Suggested implementation order
1. **Snapshot adapter.** Map the existing `/api` snapshot to the data model above, including freshness and capacity confidence. Add roster scope and `project_id`.
2. **Theme engine.** Load `themes.json`, set CSS variables per direction and theme, and persist the choice in localStorage.
3. **Direction shell.** Build the chosen direction's frame and board 1x (roster, bus, attention, lead chat).
4. **Turn 2 surfaces for that direction.** Gates and close, lanes and ownership, program and risk, tasks and decisions. The roadmap items (program stages, risk register, Decision Center, signed export) have **no backend yet**; ship them behind a feature flag and read-only until the server supports them.
5. **Action gating.** Hook every action to `--enable-actions` and the operator principal, keep the read-only CLI hints, and add server-side re-checks.
6. **Accessibility.**
   - Every state is shown by color *and* glyph or label (already the case in the designs).
   - Keep contrast at 4.5:1 or better. Check 1c bubblegum and riso, and every `dim` text color.
   - Full keyboard navigation (1a's shortcuts should work in every direction).
   - `prefers-reduced-motion`.
