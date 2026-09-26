# 04 · Qwen budget meter (board 3b)

**Status: needs the gateway ledger in the console — not available yet. Ship behind a flag.**

## Anatomy (one bar)
Scale: 0 → max(cap, cutoff). In the example: alert 80 €, cutoff 95 €, cap 100 €.
| Element | Draw | Meaning |
|---|---|---|
| Track | panel2 fill, 1px border, radius h/2 | 0 → scale max |
| Fill | solid; accent < alert ≤ warn < cutoff ≤ bad | **Our ledger** — live, written on every call. The headline number. |
| Projection | dotted (`repeating-linear-gradient 90deg, fillColor@40% 0 3px, transparent 3px 7px`) from 0 to projected | Linear projection to month end at this week's rate. |
| Alert line | 2px **dashed** warn, overhangs 5px | Soft stop — notifies the operator, blocks nothing. |
| Cutoff line | 2px **solid** bad, overhangs 5px | Gateway refuses calls. |
| Cap | 2px fg line at the bar end | The number the operator watches. |
| Bill tick | 3px fg tick, overhangs 4px | Provider bill, **with its own timestamp**. Never summed with the ledger. |

Callout layout (desktop spec board): labels 150px wide, no wrap. Above: ALERT (row 2, centred), HARD CUTOFF (row 1, right-aligned). Below: BILL (right-aligned), LEDGER · LIVE (left-aligned), MONTHLY CAP (row 2, right-aligned). Row pitch 64px so rows never overlap.

## Sizes
Rail: 10px bar + mono 24 value. State cards: 12px. Phone: 10px. Spec board: 28px.

## Six states
| State | Ledger / bill / projection | Colour | Copy |
|---|---|---|---|
| Normal · day 12 | 30.00 / 27.40 / 75 | ok | "Projected 75 € — under the alert level. Nothing to do." |
| Trending over · day 21 | 67.20 / 61.80 / 96 | warn | "Projected past the cutoff around the 29th. The lead asks you once, with no deadline." → creates a SPEND LIMIT needs item |
| Past the alert | 84.10 / 80.30 / — | warn | "Alert sent at 18:02. Nothing is blocked; qwen agents keep working." |
| At the cutoff | 95.00 / 92.60 / — | bad | "Gateway refusing calls. dev-7 and rev-4 are paused; Claude and Codex agents carry on." |
| Bill ahead of ledger | shown 71.30 (bill) | info | "Bill is 4.10 € above our ledger (67.20 €) — calls may be missing. The higher number is shown until they match." |
| Can't see the gateway | 67.20 greyed | dim | "Ledger last written 3h ago. Shown greyed as of 18:22 — never as live." |

## Rules
1. The ledger is the headline; the bill confirms it later.
2. The bill is a tick, not a bar — always with its timestamp; never added.
3. If they disagree, show the higher, and say so.
4. Money is qwen's capacity: rows show € today; percent is only for Claude/Codex windows.
5. Crossing the alert notifies once; approaching the cutoff (projection) raises one SPEND LIMIT item with evidence ("Ledger 67.20 € on day 21 · ≈ 96 € by the 30th · the 95 € cutoff lands around the 29th"), no deadline.
