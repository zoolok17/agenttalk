# Feature Specification: Obligation Dashboard (agenttalk 0.17.0)

**Mission**: `obligation-dashboard-0170-01KTHADQ`
**Created**: 2026-06-07
**Status**: Draft
**Source**: GitHub issue #20 (design jointly agreed over the agenttalk bus: Claude proposal → Codex counter → accepted)

## Overview

Operators running multiple agent bands (e.g. 2 sessions × 4 agents) need one
glanceable browser view of **who is doing what**: the roster hierarchy, open
threads, whose court the ball is in, and what the next action is. spec-kitty's
kanban shows the *task* layer when spec-kitty is present; this dashboard shows
the *conversation/obligation* layer of the bus itself and must work without
spec-kitty.

agenttalk already ships a read-only local web dashboard (`agenttalk serve`)
with a hardened security posture: loopback-only binding with no override,
GET/HEAD-only dispatch, a strict route allowlist, HTML escaping plus CSP, and
validation parity with `recv`. 0.17.0 **extends that existing surface** — it
does not add a second server implementation. The 0.16.0 `next_owner` /
`next_action` thread fields are exactly the "who is doing what" signal; today
they have no surface beyond `threads --json`.

## User Scenarios & Testing

### Primary scenario: watch two live bands at once

1. The operator runs two concurrent agent sessions, each with its own
   `.agenttalk/` store (different project roots).
2. The operator starts the dashboard once, pointing it at both roots.
3. A single browser tab shows both roots side by side: per root, the roster
   hierarchy (operator-facing liaison first, then the other agents), each
   agent's open threads, whose turn it is (`next_owner`), and the suggested
   verb (`next_action`).
4. When an agent sends a message in either band, the page reflects it within
   a few seconds without manual reload.

### Secondary scenarios

- **Single root, zero config**: running the dashboard with no root arguments
  inside a project shows that project's store — identical to today's `serve`
  resolution.
- **Mission context without spec-kitty**: a review-request opened with
  `mission`/`wp_id` meta renders as "codex ← review-request WP03 (mission X)"
  even on a machine where spec-kitty is not installed.
- **Link-out with spec-kitty**: when a root's project contains `kitty-specs/`,
  the dashboard offers a link toward spec-kitty's own kanban instead of
  reproducing it.
- **One bad root doesn't kill the view**: if one of the supplied roots is
  corrupt or uninitialized, its panel shows the error; the other roots render
  normally.
- **Automation**: a script polls the state endpoint and gets a stable,
  versioned JSON aggregate without scraping HTML.

### Testing expectations

pytest, in line with the existing `tests/test_web.py`-style coverage (server
started on an ephemeral port in a thread; requests via stdlib HTTP client).
Tests explicitly required by the agreed design are listed under Requirements.

## Requirements

### Functional Requirements

| ID | Requirement | Status |
|----|-------------|--------|
| FR-001 | The web server exposes a new `GET /api/state` route returning a purpose-built JSON aggregate with stable top level: `schema_version` (integer, `1` for this release), `agenttalk_version`, `generated_at` (UTC ISO timestamp), and `roots` (array of root objects). It is NOT CLI-parity JSON. | Proposed |
| FR-002 | Each root object identifies its store (`project_id`, `path`, `label`) and carries `errors` (array; empty when healthy), the active roster with roles/groups/operator-facing flag, retired tombstone names, per-agent presence (last-seen recency, unread count), thread rows, a broadcast summary, and the current global epoch. Roots are array entries — never object keys derived from paths. No cross-root merging of any data. | Proposed |
| FR-003 | Thread rows reuse the existing pure thread derivation: `request_id`, `state`, opener kind, `subject`, `peer`, age, `unread`, broadcast fields (`is_broadcast`, `audience`, `responded`, `pending`) when applicable, `next_owner`, `next_action`, and — when present in opener meta — `mission`, `wp_id`, `epoch_at_send`, plus a derived `epoch_status` summary. Raw full message bodies MUST NOT appear in `/api/state`; subjects (and at most short snippets) only, with detail available via the existing `/messages/<id>` routes. | Proposed |
| FR-004 | Multi-root selection: the dashboard accepts a repeatable per-invocation root option (distinct from the global `--root`). With no roots supplied it serves the normally-resolved store; with roots supplied it serves exactly those roots. | Proposed |
| FR-005 | Per-root error isolation: a corrupt, missing, or uninitialized root yields a root object whose `errors` explains the problem while the HTTP response stays `200` and all other roots render fully. A failing root MUST NOT produce a 5xx for the aggregate. | Proposed |
| FR-006 | `agenttalk dashboard` is added as a discoverability alias that runs the same server implementation as `agenttalk serve` (same binding rules, same routes). `agenttalk serve` keeps its existing flags and behavior unchanged. | Proposed |
| FR-007 | The server renders a hierarchical obligation view per root in the browser: operator-facing liaison first, then remaining agents; per agent its open threads with whose-turn (`next_owner`) and suggested action (`next_action`); retired identities visibly separated. The view reflects new bus activity within ~2–3 seconds without the operator manually reloading. | Proposed |
| FR-008 | spec-kitty integration is meta-only: mission/WP context comes exclusively from message `meta` (`mission`, `wp_id`); when a root's project contains `kitty-specs/`, the root object and HTML carry link metadata discovered from the filesystem alone. The implementation MUST NOT import or invoke spec-kitty. | Proposed |
| FR-009 | All existing routes (`/`, `/messages/<id>`, `/api/status`, `/api/messages`, `/favicon.ico`) keep their current behavior and response shapes. | Proposed |
| FR-010 | Any bind failure (port in use, OS-denied port, or other socket error) exits with code 2 and an actionable message for both `serve` and `dashboard` — naming the host:port that failed and suggesting `--port 0` (ephemeral) or another port. Real-world repro to cover: an unrelated local app already listening on 127.0.0.1:8765 makes `agenttalk serve` fail with Windows `WinError 10013` (not the friendlier "address in use") — today this escapes the command's error handling entirely and reaches the generic top-level handler with no guidance. The message must not guess at the cause; it names the failed bind and the likely "something else is using this port" case alongside the `--port` remedies. | Proposed |

### Non-Functional Requirements

| ID | Requirement | Status |
|----|-------------|--------|
| NFR-001 | Read-only guarantee, proven: a regression test issues at least 10 mixed `/api/state` + HTML requests (across at least 2 roots) and asserts the byte content of every file under each store tree — including cursors and thread/ack state — is hash-identical before and after. The check MUST hash file contents, not rely on directory mtimes (unreliable on Windows). | Proposed |
| NFR-002 | Loopback-only stays absolute: the alias and every new route are covered by the existing no-override binding and per-request peer checks; a test proves `dashboard` refuses a non-loopback host exactly as `serve` does, and that no write-method route exists (POST/PUT/DELETE/PATCH → 405 after the peer gate). | Proposed |
| NFR-003 | `/api/state` for a store of 1,000 validated messages completes in under 2 seconds on CI hardware, so a ~2 s polling cadence cannot pile up requests. | Proposed |
| NFR-004 | The full test suite passes on the CI matrix (Python 3.10–3.13 × Ubuntu/Windows/macOS) before any release tag. | Proposed |
| NFR-005 | Backward compatibility of existing JSON surfaces: `/api/status` and `/api/messages` response shapes are unchanged (existing tests stay green unmodified, except where they enumerate routes). | Proposed |

### Constraints

| ID | Constraint | Status |
|----|-----------|--------|
| C-001 | Stdlib-only runtime: no new third-party imports anywhere in `src/`. | Mandatory |
| C-002 | Additive and backward-compatible: existing CLI commands, exit codes, and JSON shapes are preserved; new behavior is opt-in. | Mandatory |
| C-003 | Message history is immutable: the dashboard performs no writes of any kind to any store (no send/ack/drain/wait/reset/prune paths reachable from request handling). | Mandatory |
| C-004 | Exit-code contract preserved (0 ok, 1 wait-timeout, 2 usage/refusal, 3 superseded/stale, 4 unknown rid, 5 partial fan-out, 130 SIGINT). | Mandatory |
| C-005 | Windows-first: everything works on Windows paths/filesystems; no POSIX-only assumptions. | Mandatory |
| C-006 | Per-WP cross-review by Codex over agenttalk before merge; fresh-eyes review before tag. | Mandatory |
| C-007 | The loopback wall is non-negotiable: no flag, alias, or route may expose the server beyond loopback; remote viewing remains "SSH-tunnel it". | Mandatory |

## Success Criteria

1. An operator running two sessions can see both bands' rosters, open threads,
   whose turn it is, and next actions in **one browser tab**, without running
   any CLI command after starting the dashboard.
2. New bus activity appears in the browser within 3 seconds of the message
   landing, with no manual reload.
3. A deliberately corrupted root renders as an inline error panel while the
   healthy root's data remains complete — verified by test.
4. After any number of dashboard requests, every byte of every store file is
   unchanged — verified by hash-comparison test.
5. A consumer can build against `/api/state` using only `schema_version` and
   the documented field list, with no HTML scraping.

## Key Entities

- **Root object** — one store's full dashboard snapshot: identity, roster,
  presence, threads, broadcasts, epoch, errors.
- **Thread row** — one obligation: who opened it, its state, whose turn
  (`next_owner`), what to do (`next_action`), mission/WP context when carried
  in meta.
- **Presence entry** — per-agent recency (last seen) and unread backlog.
- **Link metadata** — optional spec-kitty pointer discovered from the
  filesystem (never by importing spec-kitty).

## Assumptions

- The refresh mechanism (client polling vs. page refresh) is a design-phase
  decision; the spec requires only the ≤3 s reflection of new activity and
  that the existing CSP/no-inline-JS posture is not weakened carelessly.
- The exact HTML route for the hierarchical view (reuse `/` vs. a new path)
  is a design-phase decision; FR-009 requires existing routes keep behavior.
- `generated_at` in `/api/state` is the one place a wall-clock timestamp is
  emitted; it is informational and carries no ordering semantics.
- Roster hierarchy is the existing flat roster enriched by role/groups and
  the operator-facing flag; the dashboard adds no new roster semantics.

## Out of Scope

- Any mutation from the browser (acking, replying, rescinding) — explicitly
  deferred until the identity/authz RFC's later phases land.
- Remote (non-loopback) access in any form.
- Authentication/authorization — the server remains single-human-local.
- Duplicating spec-kitty's kanban or reading spec-kitty's internal state
  files beyond detecting `kitty-specs/` for a link.
- Push transports (websockets/SSE); polling is sufficient at local-disk
  latency.
- Historical analytics/timelines; the dashboard is a *current obligations*
  view.
