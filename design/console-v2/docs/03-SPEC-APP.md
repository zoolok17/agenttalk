# 03 · The app (board 3a)

Frame: 1240×780 in the prototype; in production, full viewport, min width 1024 (below that, use the phone layout in 05).
Grid: `columns minmax(0,1fr) 340px`, `rows 58px minmax(0,1fr) 32px`. Theme tokens from `tokens/themes.json` (keys: bg panel panel2 border fg dim accent accentInk ok warn bad info).
Fonts: Geist (UI), Geist Mono (names, numbers, times, evidence), Instrument Serif (greeting and item titles). Terminal theme: all three → JetBrains Mono; all radii → 0; serif sizes drop (greeting 40→28, item title 22→17).

## 1. Header (58px, spans both columns, bottom border)
Left → right:
- Logo square 22px (radius 9, accent) + "agenttalk" 15/600.
- **Team switcher** — segmented control (3px padding, panel bg, border). One chip per team: freshness dot 7px (ok + pulse 2s when live; warn, no pulse when offline) · label 12/500 · machine (mono 10.5, 65% opacity) · needs-you badge (mono 10.5/700, warn bg, #111 text, pill, hidden at 0). Active chip: fg bg, bg text.
- spacer
- (prototype only) "DEMO" + scenario segments: Busy / Quiet day / Can't see team.
- Theme segments: Midnight / Paper / Synthwave / Terminal.
- "?" key button (opens the keyboard overlay).

No mission progress, no clock-as-decoration, no "spec-kitty".

## 2. Stream (left column)
Padding 22/26/14, gap 14, scrolls; composer pinned below.

**Offline banner** (only when the team's source is stale beyond threshold — see 08): 14px top margin, warn border 50% / warn bg 10%, radius 16. "CAN'T SEE THE TEAM" (mono 10.5/600, warn) · message 13px · "Retry now" button (warn outline) → after a failed retry: "Still unreachable · tried HH:MM".

**Greeting** (serif 40/1.1) + sub (13.5, dim, max 620):
| State | Greeting | Sub |
|---|---|---|
| Busy, n open | "Three things need you." (word number, singular/plural) | "Stuck agents first, then oldest. A deadline only shows if someone set one." |
| Busy, all answered | "That's everything." | "The lead will come back only when something needs a human." |
| Quiet | "All quiet." | "Nothing needs you. 8 of 10 agents are idle — that's their resting state; they wake when the lead messages them." |
| Offline | "Can't see the main team." | "Everything below is greyed and stamped. Nothing is live, and nothing you press can reach the lead until win-ws01 is back." |

**Lead's latest message**: 30px avatar (2px accent ring, "LE") + bubble (panel, border, radius 4/16/16/16, 14/1.5) + "lead · just now" (mono 10.5 dim).

**Needs-you cards** (max 700 wide): panel bg; border = tone at 50% (accent when selected); radius 16; padding 15/18; gap 9.
- Row 1: KIND (mono 10.5/600, .1em, tone color) · spacer · age (mono 11 dim): "14m" or "no deadline · waiting 2d" or "decide by Fri 18:00" when a deadline exists.
- Title: serif 22/1.2.
- Evidence row: "EVIDENCE" label (mono 10 dim) + text (mono 12/1.5). **Required** for every card.
- Actions: options as pills (first = primary: accent bg / accentInk; others outline), then "Later" (text button, dim), then key hint when selected ("enter · l later").
- Answered: actions replaced by "✓ Sent to the lead: <option>" (ok), card at 65% opacity.
- Selected (keyboard): accent border + 3px accent ring at 18%.
- Kinds and tones: LOOKS STUCK → bad · SPEND LIMIT → warn · GATE HOLD → warn · DECISION → info.
- Locked options (need actions, or offline) render at 50% opacity with a reason label, e.g. "Waive · CLI only".
- Order: stuck first, then oldest waiting.

**Deferred line** (if any): "1 deferred · still open, not dismissed · show" (mono 11.5 dim, click restores).

**Side block** (dashed rows, radius 9):
- Busy: "ALSO HAPPENING · NOT FOR YOU" — e.g. "dev-5 is quiet, not stuck — Tests running 6m · output 40s ago · no card unless output stops for 10 min"; "rev-2 is capped — Codex 5-hour window full · resets 23:40 · the lead routes reviews to rev-1".
- Quiet: "SINCE YOU LAST LOOKED · YESTERDAY 22:10" — 3 lines (delivered work, lesson published, spend delta).

**Chat thread**: your bubbles right (accent, radius 16/4/16/16), lead left. Auto-scroll to bottom on new message only.

**Composer**: panel, border, radius 16, 6/6/6/14 padding; input 14.5; send button 40px accent. Placeholder "Message the lead   ( / )". Offline: disabled, 50%, "Paused — the lead can't receive while win-ws01 is offline".

## 3. Rail (340px, panel bg, left border, padding 18, gap 20, scrolls)
**Qwen gateway · <month>** — Q badge 18px (qwen color) · title 12.5/600 · stamp ("live · ledger" / "as of 18:22"). Value: mono 24/600 "67.20 €" + "of 100 € cap". Bar 10px (see 04). Legend grid (mono key colored / dim value): alert · cutoff · bill ▮ · projected.

**USAGE WINDOWS** — per runtime × window (Claude 5-hour, Claude weekly, Codex 5-hour, Codex weekly): runtime badge 16px + name + window + % (mono 12/600, threshold color); 4px bar; "resets 23:40" right-aligned. Thresholds: <60 ok, 60–84 warn, ≥85 bad.

**TEAM · 10** + summary ("8 idle · that's normal" or "frozen · as of 18:22"). Rows (gap 11): avatar 32px (silhouette per theme, contain; runtime badge 15px bottom-right with 2px panel border; Terminal: 32px box with the runtime letter) · short name (mono 12/500; full name in title/tooltip) · status line (11px, state color, ellipsis) · right cap (mono 10.5: "€3.20 today" in qwen color, "resets 23:40" in bad).
State colors: working ok · busy (silent but evidenced) info · idle dim · looks stuck warn · capped bad.

## 4. Footer (32px, top border, mono 11 dim)
Key hints: `j k` move · `enter` answer · `l` later · `/` lead · `1 2` team · `t` theme · `?` keys. Right: "click the board to use keys" (prototype only).

## 5. Keyboard overlay
Scrim bg at 80%; 580px panel, radius 16; "Keyboard" serif 28; 2-column list of all keys (see 08); footnote "Same keys in every theme…". Esc or ? closes; click outside closes.

## 6. Offline treatment (applies everywhere)
Everything data-bearing at 50% opacity; all timestamps become "as of HH:MM"; live dots stop pulsing and turn warn; bars turn dim color; actions disabled with reason; composer disabled. Never hide the last-known data — grey it.

## 7. Inherited surfaces (from Turn 2, restyled into this frame)
Gates & close, lanes & ownership, risk register, tasks, lessons, onboarding unknowns — see `reference/TURN-1-2-REFERENCE.md` (2a–2d). In the app they appear as lead-stream items and a detail drawer, not as separate dashboards.
