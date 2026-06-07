# Feature Specification: Dashboard Polish (agenttalk 0.19.0)

**Mission**: `dashboard-polish-0190-01KTJ4QB`
**Created**: 2026-06-07
**Status**: Draft
**Source**: GitHub issue #22 — operator feedback on the 0.17.0 dashboard; design accepted by Codex (bus proposal `pp-62742311`).

## Overview

The 0.17.0 obligation dashboard is correct but reads like a raw developer
view. This release adds presentation polish: a **hierarchical team view**
(operator-facing liaison / lead on top; developers grouped to the left,
reviewers to the right), **per-agent stats**, a **who-is-talking-to-whom**
conversation panel below the roster, a **manual refresh button**, and an
**auto-refresh toggle**.

It is layered on the existing read-only `serve` / `web.py` surface. The work
is mostly client-side (the `_DASHBOARD_JS` renderer + the `/dashboard` HTML
shell) plus small **additive** server aggregation on `/api/state`. No new
routes, no CSP widening, no mutation, no spec-kitty dependency. The operator
chose **bus-native stats only** and the **liaison/lead-on-top role-grouped
layout**.

## User Scenarios & Testing

### Scenario 1 — read the team at a glance
The operator opens `/dashboard`. The operator-facing liaison (or a lead-ish
role) sits centered on top. Below, agents are arranged in role-grouped
columns: developer-ish roles to the left, reviewer-ish roles to the right,
anything else center. Each agent is a card showing name, role/groups,
last-seen age, messages sent, messages received, how many obligations it owes,
and a composing badge when drafting. The operator can tell who the team is and
what each member is doing without reading raw JSON.

### Scenario 2 — see who is talking to whom
Below the roster, a conversation panel lists message traffic as directed pairs
(sender → recipient) with volume, plus the existing open-threads table. The
operator can see the communication pattern of the team. When traffic exceeds
the display cap, the panel says so honestly rather than silently hiding pairs.

### Scenario 3 — control refresh
The page auto-refreshes about every 2 seconds by default. The operator can
toggle auto-refresh off (the page then holds still) and press a manual refresh
button to pull the latest state on demand. Toggling and the button never
reload the page or lose scroll position.

### Scenario 4 — automation still works
A script polling `GET /api/state` still parses successfully: the new
per-agent counts and the conversation edges are additive keys; every existing
key keeps its shape and meaning, and `schema_version` is still `1`.

### Testing expectations
pytest, extending `tests/test_web.py`. The new `/api/state` fields are pinned
(presence, additive-not-null, schema_version still 1, no existing key
removed/renamed); the edge cap + truncation signal is pinned; the read-only
full-tree-hash regression still passes; the per-route CSP is unchanged
(script-capable only on `/dashboard`); the JS renders the new controls and
cards without message-derived HTML injection (textContent only). The
client-side role→layout convention is pinned by a JS-visible classification
test or an equivalent server-data-driven assertion.

## Requirements

### Functional Requirements

| ID | Requirement | Status |
|----|-------------|--------|
| FR-001 | Each agent entry in `/api/state` gains additive `sent` (count of validated messages where the agent is the sender) and `received` (count where the agent is the recipient), derived from the same single per-root validated-message scan `build_state` already performs — no extra filesystem walks. | Proposed |
| FR-002 | Each root in `/api/state` gains an additive `edges` array of `{from, to, count}` objects: validated **non-self** message sender→recipient pair counts, **including broadcast fan-out copies** (traffic volume, not unique-thread semantics), sorted by `count` descending and capped to the top 50 pairs. | Proposed |
| FR-003 | When the distinct-pair count exceeds the cap, the root carries an additive truncation signal (e.g. `edges_truncated: true` and/or `edge_limit`) so the client can label the panel honestly; absent when not truncated. | Proposed |
| FR-004 | The `/dashboard` view renders a hierarchical roster: the operator-facing liaison (or a lead-classified role) is placed on top; remaining agents are grouped into columns below — developer-classified roles to the left, reviewer-classified roles to the right, unclassified roles center. The classification is performed **client-side** from the existing `operator_facing`, `role`, and `groups` fields; no server-provided layout hint is added to `/api/state`. | Proposed |
| FR-005 | The role→column convention is: operator-facing OR a role matching lead-like terms → top; a role matching developer-like terms (developer/dev/eng/impl) → left; a role matching reviewer-like terms (review/reviewer/qa/audit) → right; otherwise → center. Matching is case-insensitive substring on the role string. The convention is documented and pinned by test. | Proposed |
| FR-006 | Each agent renders as a card showing: name, role and groups, last-seen age, `sent`, `received`, the count of obligations it owes (open threads whose next owner is that agent), and a composing badge when a composing marker is present. All values come from `/api/state`. | Proposed |
| FR-007 | Below the roster, a conversation panel renders the `edges` as a who-talks-to-whom view (directed pairs with counts), labelled as truncated when FR-003 signals it, alongside the existing open-threads table. | Proposed |
| FR-008 | The page provides a manual **Refresh** button that fetches `/api/state` once on click, and an **auto-refresh toggle** (default on, ~2 s interval) that enables/disables the polling loop. Both are wired with `addEventListener` (no inline event handlers). Toggling or clicking never reloads the page or resets scroll position. | Proposed |

### Non-Functional Requirements

| ID | Requirement | Status |
|----|-------------|--------|
| NFR-001 | Backward compatibility: `schema_version` stays `1`; no existing `/api/state` key is removed or renamed; all new keys are additive and absent-not-null when unused. Existing exact-key tests pass after being extended for the additive keys, not rewritten. | Proposed |
| NFR-002 | The new server aggregation adds no extra store scan: `sent`/`received`/`edges` are computed from the same validated-message list `build_state` already iterates. `/api/state` for a 1,000-message store stays under 2 seconds on CI hardware. | Proposed |
| NFR-003 | The full test suite passes on the CI matrix (Python 3.10–3.13 × Ubuntu/Windows/macOS) before any release tag. | Proposed |

### Constraints

| ID | Constraint | Status |
|----|-----------|--------|
| C-001 | Stdlib-only runtime: no new third-party imports; the client renderer remains the embedded `_DASHBOARD_JS` string in `web.py` (no standalone static file, no packaging/package-data migration). | Mandatory |
| C-002 | Additive and backward-compatible: existing commands, routes, exit codes, and JSON shapes preserved; the only changes are additive `/api/state` keys and the `/dashboard` client renderer. | Mandatory |
| C-003 | Read-only: no write path is reachable from request handling; the full-tree content-hash no-mutation regression still passes. | Mandatory |
| C-004 | Security posture unchanged: loopback-only, GET/HEAD-only, per-request peer gate, route allowlist, and **per-route CSP all byte-identical** — the script-capable policy stays only on `/dashboard`; hostile-body routes keep the stricter policy. Refresh controls use `addEventListener` only (no inline handlers, no eval), so `script-src 'self'` is not weakened. | Mandatory |
| C-005 | No XSS surface: all bus-derived values (agent names, roles, subjects, edge endpoints) are rendered via `textContent`/DOM construction, never `innerHTML` with interpolation. | Mandatory |
| C-006 | Windows-first: correct on Windows; no POSIX-only assumptions. | Mandatory |
| C-007 | Per-WP Codex cross-review over agenttalk before merge; fresh-eyes review before tag; CI matrix green before tag. | Mandatory |
| C-008 | Bus-native only: the dashboard never reads or imports spec-kitty for stats; "tasks done" and any spec-kitty kanban state are explicitly out of scope. | Mandatory |

## Success Criteria

1. An operator opening `/dashboard` can identify the team hierarchy (liaison/
   lead on top, developers left, reviewers right) and read each agent's
   last-seen, sent, received, owes, and composing state from the cards —
   without consulting raw JSON.
2. The conversation panel shows who is talking to whom with volume, and says
   so when the list is truncated.
3. Auto-refresh can be toggled off and a manual refresh pulls fresh state;
   neither reloads the page nor loses scroll position.
4. A consumer polling `/api/state` parses unchanged: `schema_version == 1`,
   every prior key intact, new keys additive — verified by test.
5. After any amount of dashboard interaction, every byte of every store file
   is unchanged — verified by the existing no-mutation regression.
6. `/messages/<id>` and the other non-dashboard routes keep byte-identical
   CSP and behavior — verified by test.

## Key Entities

- **Agent card** — one roster member's presentation: identity, role/groups,
  last-seen, sent/received, owes, composing.
- **Layout column** — top / left / right / center, derived client-side from
  the agent's operator-facing flag and role.
- **Conversation edge** — a directed `{from, to, count}` traffic pair.
- **Refresh control** — the manual button and the auto-refresh toggle.

## Assumptions

- "owes" is the count of open threads whose `next_owner` is the agent —
  already derivable from the existing thread rows; no new server field is
  required for it unless the plan finds it cheaper to add one.
- The conversation panel's visual form (a labelled list vs. a light node/edge
  sketch) is a plan/design-phase detail; the spec requires only that it shows
  directed pairs with counts and an honest truncation label.
- The edge cap of 50 and ~2 s default refresh interval are the agreed
  starting values; tunable in the plan if needed but not operator-facing
  configuration in this release.

## Out of Scope

- Real "tasks-done" counts or any spec-kitty kanban integration (C-008).
- A force-directed / physics graph for the whole roster (the who-talks-to-whom
  view is a labelled directed-pair panel, not an animated graph).
- A server-side `layout_hint` field on `/api/state` (layout stays client-side).
- Any new route, CSP change, write path, or mutation control from the browser.
- Operator-configurable thresholds (edge cap, refresh interval) as settings.
- Multi-root layout changes beyond rendering the new fields per existing root.
