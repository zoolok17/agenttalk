# 08 · Interactions

## Keyboard (same in every theme; ignored while typing in an input)
| Key | Action |
|---|---|
| j / ↓ | next needs item |
| k / ↑ | previous needs item |
| enter | answer the selected item with its first (primary) option — skipped if that option is locked |
| l | later (defer; never dismiss) |
| / | focus the composer |
| esc | close overlay; blur composer |
| 1 / 2 | switch team |
| t | next theme |
| ? | show / hide the keyboard overlay |

## Needs item lifecycle
open → **answered** (option sent to the lead as a normal message; card shows "✓ Sent to the lead: …") · open → **deferred** (hidden, counted in "n deferred · still open, not dismissed · show"; restore on click). Answers on any device update the same item.
Locked options (require actions; or team offline) are visible but disabled with the reason in the label.

## Offline
Triggered per team by stale/unreachable source. Banner + greying + "as of" stamps + stopped pulses + disabled composer/actions. "Retry now" polls immediately; failure shows "Still unreachable · tried HH:MM". The other team stays live and switchable.

## Lead replies (prototype keyword rules — production uses the real lead)
status → summary · restart → "Restarting dev-6 with its last context…" · raise/54 → "Noted: 54 € for the main gateway. Changing the limit is an operator step on win-ws01, from the command line…" (the console never changes limits) · keep → "Keeping it…" · wait → "Waiting…" · else → "Noted. I'll fold that in and only come back if it needs you."

## Motion
Live dot pulse 2s ease-in-out (opacity 1→.35); nothing else animates. `prefers-reduced-motion`: no pulse.
