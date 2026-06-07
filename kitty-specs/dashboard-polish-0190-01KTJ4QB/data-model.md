# Data Model — Dashboard Polish (0.19.0)

No persisted-data changes (read-only). This documents the **additive**
`/api/state` keys and the client-side layout convention. `schema_version`
stays `1`.

## 1. AgentEntry — additive `sent` / `received` (FR-001)

```jsonc
{
  "name": "claude",
  "role": "lead",                 // existing
  "groups": ["devs"],             // existing (absent if none)
  "operator_facing": true,        // existing (absent unless true)
  "last_seen": "...",             // existing (absent if never seen)
  "last_seen_age_seconds": 2.1,   // existing
  "unread": 3,                    // existing
  "sent": 142,                    // NEW: validated msgs where from == name
  "received": 138,                // NEW: validated msgs where to == name
  "composing": [ ... ]            // existing (absent if none)
}
```

`sent`/`received` are **always present integers** (0 is meaningful), counted
from the same validated `msgs` list `_agent_entries` already receives — no
extra scan.

## 2. RootObject — additive `edges` (+ truncation) (FR-002/003)

```jsonc
{
  // ... all existing root keys unchanged ...
  "edges": [                      // NEW: who-talks-to-whom, always present (may be [])
    {"from": "claude", "to": "codex", "count": 87},
    {"from": "codex", "to": "claude", "count": 81}
    // sorted by count desc, then (from,to); top 50
  ],
  "edges_truncated": true,        // NEW: present ONLY when >50 distinct pairs
  "edge_limit": 50                // NEW: present ONLY when truncated
}
```

Edge rules:
- one entry per directed `(from, to)` pair, `count` = number of validated
  messages with that sender→recipient.
- **excludes self-addressed** messages (`from == to`).
- **includes broadcast fan-out copies** (traffic volume, not unique threads).
- sorted `count` desc, deterministic `(from, to)` tiebreak; capped at 50.
- `edges` always present; `edges_truncated`/`edge_limit` absent unless capped.

## 3. Layout convention (client-side, FR-004/005) — NOT in `/api/state`

The renderer classifies each agent into a column from existing fields. No
server `layout_hint`. Case-insensitive substring on `role`; first match wins:

| condition (checked in order) | column |
|---|---|
| `operator_facing === true` OR `role` contains `lead` | top (centered) |
| `role` contains `dev` / `eng` / `impl` | left |
| `role` contains `review` / `qa` / `audit` | right |
| otherwise (including no role) | center |

## 4. Agent card contents (FR-006)

Per card, all from `/api/state`: `name`; `role` + `groups`; last-seen age
(from `last_seen_age_seconds`); `sent`; `received`; **owes** = count of this
root's `threads` whose `next_owner` is this agent (computed client-side, D3);
composing badge when `composing` present.

## 5. Conversation panel (FR-007)

Renders the root's `edges` as directed `from → to (count)` rows (sorted as
received), labelled "showing top 50 of N" when `edges_truncated`. The existing
open-threads table remains.

## 6. Refresh controls (FR-008)

- Refresh `<button>` → one `poll()` on click.
- Auto-refresh `<input type=checkbox>` (default checked) → on check, start the
  ~2 s `setInterval`; on uncheck, `clearInterval`. No page reload; scroll
  preserved.
- Both wired with `addEventListener` (no inline handlers).

## 7. Validation rules

- `schema_version` stays `1`; no existing key removed/renamed (NFR-001).
- `sent`/`received` always present; `edges` always present (≥0 entries).
- `edges_truncated`/`edge_limit` absent unless truncated.
- No `body` key anywhere in `/api/state` (unchanged invariant).
- All bus-derived values rendered via `textContent` (C-005).
- Per-route CSP byte-identical; read-only (no-mutation regression) holds.
