# 04 · Qwen budget (board 3b)

**Status: needs gateway spend in the console — not available yet. Ship behind a flag.**

## Two levels (never mix them)
| Level | What it shows | Limits on it |
|---|---|---|
| **Team** (one per machine gateway) | That gateway's ledger, projection | Its own **alert** and **cutoff** (main: 39 / 44 €; shopfront: 35 / 39 €). No cap. Label: "of 44 € cutoff". |
| **Account** | Both ledgers stacked (sum = live account figure), provider **bill** tick | The **100 € monthly cap** (bar end). |

The gateways' limits are sized so both together stay under the cap. A team bar that says "of 100 € cap" would count the cap twice — don't.

## Team bar
Scale: 0 → max(cutoff, projection) × 1.04, so a projection past the cutoff is visible.
- Fill: ledger (accent < alert ≤ warn < cutoff ≤ bad). Radius h/2.
- Projection: dotted `repeating-linear-gradient(90deg, fill@40% 0 3px, transparent 3px 7px)`.
- Alert: 2px **dashed** warn line, overhangs 5px.
- Cutoff: 2px **solid** bad line, overhangs 5px.
Legend: alert (warn) "39 € · notifies you, blocks nothing" · cutoff (bad) "44 € · this gateway refuses calls" · projected "≈ 52.60 € by the 30th · past the cutoff ~25th" (bad when past the cutoff; crossing day = day × cutoff / ledger).

## Account bar
Scale 0 → cap (100 €).
- Segment 1: main ledger (accent). Segment 2: shopfront ledger (accent 45%). Together = sum of ledgers.
- Bill tick: 3px fg, overhangs 4px, **always with its own timestamp**.
- Cap: 2px fg line at the bar end.
Line under it: "main 36.80 € · shopfront 30.40 € · bill ▮ 61.80 € as of 14:00". A stale ledger is marked "(stale)" in this line and greyed in its team bar.

## Where it appears
- **Rail (3a):** "Qwen · <machine> gateway" block (mono 24 ledger + "of 44 € cutoff", 10px team bar, legend), then a divider and "Account · September" (mono 13 sum + "of 100 € cap", 8px account bar, line).
- **Phone (3c):** team ledger "36.80 € / 44 € cutoff", 10px team bar, line "alert 39 · account 67.20 € of 100 € · bill 61.80 € (14:00)".
- **Roster:** qwen agents show "€ today".

## Example values (September, day 21)
| | Ledger | Alert | Cutoff | Projected |
|---|---|---|---|---|
| Main (win-ws01) | 36.80 € | 39 € | 44 € | 52.60 € (crosses cutoff ~25th) |
| Shopfront (linux-02) | 30.40 € | 35 € | 39 € | 37.10 € |
| **Account** | **67.20 €** (sum) · bill 61.80 € @14:00 | — | — | cap **100 €** |
Quiet day: main 24.10, shopfront 20.00, sum 44.10, bill 41.00.

## Six states
| State | Level | Values | Colour | Copy |
|---|---|---|---|---|
| Main · normal · day 12 | team | 15.00, proj 37.50 | ok | "Projected 37.50 € — under this gateway's 39 € alert. Nothing to do." |
| Main · trending over · day 21 | team | 36.80, proj 52.60 | warn | "Projected past the 44 € cutoff around the 25th. The lead asks you once, with no deadline." → SPEND LIMIT item |
| Main · past the alert | team | 40.20 | warn | "Alert sent at 18:02. Nothing is blocked; qwen agents keep working." |
| Main · at the cutoff | team | 44.00 | bad | "Main gateway refusing calls: dev-7 and q.rev-1 are paused. Shopfront's gateway is separate and keeps working." |
| Account · bill ahead | account | 36.80 + 30.40, bill 71.30 | info | "Bill 71.30 € is 4.10 € above both ledgers (67.20 €) — calls may be missing. The account shows the higher number until they match." |
| Can't see the main gateway | team | 36.80 greyed | dim | "Main ledger last written 3h ago. Greyed as of 18:22; the account sum marks main as stale." |

## The SPEND LIMIT item
- Title: "Raise the main gateway's cutoff from 44 € to 54 €?"
- Evidence: "Main ledger 36.80 € on day 21 · ≈ 52.60 € by the 30th · the 44 € cutoff lands around the 25th · both gateways would total 93 € of the 100 € cap"
- Age: "no deadline · waiting 2d". Options: "Raise to 54 €" (primary), "Keep 44 €".
- Answering **only sends the answer to the lead.** Confirmation: "✓ Sent to the lead: Raise to 54 € · the limit itself changes on win-ws01, from the command line". Lead reply: "Noted: 54 € for the main gateway. Changing the limit is an operator step on win-ws01, from the command line. I'll confirm once its ledger reports the new cutoff."
- The console never changes gateway limits, even with `--enable-actions`.

## Rules
1. Cap and bill are account-level; only the account bar shows them.
2. Each gateway has its own limits; a team bar never says "of 100 €".
3. Ledgers are live, the bill is late: account figure = sum of ledgers; bill is a timestamped tick; if the bill is newer and higher, show it and say so.
4. Limits change on the machine, never in the console.
