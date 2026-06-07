# Research & Decision Records — Dashboard Polish (0.19.0)

All decisions resolve against issue #22, the Codex-accepted proposal
(`pp-62742311`), and the verified current `web.py` (see plan.md "Verified code
facts").

## D1 — `sent`/`received` placement (FR-001)

**Decision**: In `_agent_entries`, build two `collections.Counter`s over the
`msgs` list already passed in — `sent[m.sender] += 1`, `received[m.recipient]
+= 1` — once, before the loop; emit `e["sent"] = sent.get(a, 0)` and
`e["received"] = received.get(a, 0)` for each agent. Always present (a count;
0 is meaningful, not "absent").

**Rationale**: `_agent_entries` already receives the validated `msgs`
(`web.py:763`), so this is zero extra scans (NFR-002). Counts are over the
same validated set `/api/state` renders, so they match what the dashboard
shows. `sent`/`received` as always-present integers is the honest shape (0 is
data); the absent-not-null rule is for optional *presence* fields, not counts.

## D2 — `edges` shape, self/broadcast policy, cap (FR-002/003)

**Decision**: In `_root_state`, `Counter` over `(m.sender, m.recipient)` for
every validated message where `sender != recipient` (exclude self-addressed —
e.g. barrier messages). **Include broadcast fan-out copies** (each copy is a
real from→to message; this panel is traffic volume, not unique threads).
Emit `edges = [{"from", "to", "count"}]` sorted by `count` desc then
`(from, to)` for determinism, capped to the top 50. If the distinct-pair count
> 50, also emit `edges_truncated: true` and `edge_limit: 50` (both absent when
not truncated). `edges` is always present (possibly `[]`).

**Rationale**: Codex constraint — traffic volume incl. fan-out; top-50 sorted
desc; honest truncation signal. Excluding self keeps the panel about
inter-agent communication. Deterministic tiebreak so the list is stable across
polls.

## D3 — "owes" computed client-side (FR-006)

**Decision**: No new server field for "owes". The client already receives each
root's `threads` (each row has `next_owner`); the renderer counts, per agent,
the open threads whose `next_owner` equals that agent (a string equals, or
membership when `next_owner` is a broadcast pending list).

**Rationale**: the data is already on the wire; adding a server `owes` field
would duplicate derivation and risk drift with the threads array. Keeps the
server change minimal (just `sent`/`received`/`edges`).

## D4 — Layout convention, client-side (FR-004/005, C-001)

**Decision**: The role→column classifier lives in `_DASHBOARD_JS`, NOT in
`/api/state` (no server `layout_hint` — Codex constraint). Convention
(case-insensitive substring on the role string; documented + pinned by test):

| condition | column |
|---|---|
| `operator_facing === true` OR role contains `lead` | **top** (centered) |
| role contains `dev`, `eng`, or `impl` | **left** |
| role contains `review`, `qa`, or `audit` | **right** |
| otherwise (incl. no role) | **center** |

First match wins, in that order (operator_facing/lead checked first). Multiple
top agents stack centered; left/right/center are columns of cards.

**Rationale**: layout is presentation; keeping it client-side keeps
`/api/state` semantic/data-only and testable as data. The convention is small,
documented, and pinned so it can't silently drift.

## D5 — Refresh controls + CSP (FR-008, C-004)

**Decision**: `render_dashboard` adds a control bar (a Refresh `<button>` and
an auto-refresh `<input type=checkbox>` + label) to the shell. `_DASHBOARD_JS`
wires both with `addEventListener` (no inline `onclick`/`onchange`). The poll
loop becomes: a single `poll()` fetch function; `setInterval` is started when
the toggle is on and cleared when off; the button calls `poll()` once. Default
toggle = checked (auto-refresh on, ~2 s). The `/dashboard` CSP
(`script-src 'self'; connect-src 'self'`) is unchanged — no inline handlers,
no eval — and every other route's CSP stays byte-identical.

**Rationale**: addEventListener keeps `script-src 'self'` intact (inline
handlers would need `'unsafe-inline'`, which we will not add). Toggling clears/
starts the interval rather than reloading, so scroll position is preserved.

## D6 — Render without innerHTML (C-005)

**Decision**: All new DOM (cards, columns, edge rows, controls) is built with
`document.createElement` + `textContent` + `appendChild`, exactly like the
existing renderer. No `innerHTML` with interpolated bus data anywhere. Agent
names, roles, edge endpoints, subjects all go through `textContent`.

**Rationale**: the dashboard's XSS defense is the textContent-only invariant;
the existing fresh-eyes review verified it. New rendering must hold the same
line.

## D7 — Additivity gate updates (NFR-001)

**Decision**: The existing `tests/test_web.py` assertions that pin the exact
`/api/state` agent/root key sets (and the recursive no-`body` walk) are
EXTENDED to allow `sent`/`received`/`edges`/`edges_truncated`/`edge_limit` —
not rewritten. `schema_version` stays `1` (additive keys don't bump it). A
test asserts no prior key was removed or renamed.

**Rationale**: NFR-001 back-compat. The `body`-absence and no-mutation
invariants are unchanged (these are counts/edges, never message bodies).
