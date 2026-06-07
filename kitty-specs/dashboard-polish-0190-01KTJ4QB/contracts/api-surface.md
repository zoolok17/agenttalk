# Contract: API + client surface — Dashboard Polish (0.19.0)

All changes are **additive** or client-only. No route added/removed/renamed;
no exit code, CSP, or existing JSON key changed.

## `/api/state` (additive only — `schema_version` stays 1)

| Addition | Location | Shape | Presence |
|---|---|---|---|
| `sent` | each agent | int | always |
| `received` | each agent | int | always |
| `edges` | each root | `[{from,to,count}]` top-50, sorted count desc | always (may be `[]`) |
| `edges_truncated` | each root | `true` | only when >50 pairs |
| `edge_limit` | each root | `50` | only when truncated |

Unchanged: every existing agent/root/thread key; `schema_version == 1`; no
`body` anywhere; per-root errors-as-data; the validated-message scan count
(one per root per request).

## Routes (all unchanged)

`/`, `/messages/<id>`, `/api/status`, `/api/messages`, `/api/messages/<id>`,
`/favicon.ico`, `/api/state`, `/dashboard`, `/static/dashboard.js` — same
methods, same allowlist, same per-route CSP (script-capable only on
`/dashboard`). `/dashboard` HTML shell and `/static/dashboard.js` content
change (richer renderer + controls); their **headers/CSP do not**.

## `/dashboard` client behavior (FR-004–008)

- Hierarchical roster: liaison/lead top, dev-ish left, review-ish right, else
  center (client-side classification, D4).
- Agent cards: name, role/groups, last-seen, sent, received, owes, composing.
- Conversation panel: `edges` as `from → to (count)`, truncation-labelled.
- Refresh button (one fetch) + auto-refresh toggle (default on, ~2 s), both
  `addEventListener`; no reload, scroll preserved.
- All DOM via `createElement`/`textContent`; no `innerHTML` with bus data.

## Invariants (tested)

1. `schema_version == 1`; no prior `/api/state` key removed/renamed.
2. `sent`/`received` present and correct; `edges` correct, capped, sorted,
   self-excluded, fan-out-included; truncation signal when >50.
3. `/messages/<id>` + all non-dashboard route CSP byte-identical.
4. read-only full-tree-hash regression passes after dashboard traffic.
5. no `body` key at any depth in `/api/state`.
6. the JS renderer uses no inline handlers and no `innerHTML` with data.
