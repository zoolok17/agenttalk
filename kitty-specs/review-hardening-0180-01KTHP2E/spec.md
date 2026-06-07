# Feature Specification: Review Hardening (agenttalk 0.18.0)

**Mission**: `review-hardening-0180-01KTHP2E`
**Created**: 2026-06-07
**Status**: Draft
**Source**: GitHub issue #21 — fixes from two independent fresh-context full-codebase reviews; design accepted by Codex (bus proposal `pp-47eae0ce`).

## Overview

After v0.17.0 shipped, two fresh-context reviewers audited the *entire*
codebase rather than a diff. Because every prior review loop (0.9→0.17) only
inspected per-feature diffs, the surviving defects are all **cross-feature /
cross-release interactions** plus one latent robustness hole in the oldest
crypto code. Two of the findings were reported *independently by both
reviewers*. This release fixes 1 BLOCKER + 4 MAJOR findings and adds one
operator-requested guardrail; it changes no public command surface beyond
additive fields and one new advisory warning.

## User Scenarios & Testing

### Scenario 1 — a malformed file must never take down the bus (BLOCKER)
An operator has HMAC signing enabled. A message file appears whose signing
metadata is well-formed except the signature value is the wrong JSON type (a
list, not a string). Today every read path crashes with an uncaught
`TypeError`, and the invalid-message machinery that would quarantine it
crashes too. After this release the file is treated as a normal invalid
message: delivery of legitimate messages continues, `status`/`doctor` report
it, and `prune --invalid` can quarantine it.

### Scenario 2 — a malformed id must not silently hide future messages
A message file carries a roster-valid payload but a non-timestamp `id` (e.g.
`zzzz`). Today it delivers and, once acked, poisons the recipient's cursor so
every later real message is invisible. After this release the malformed id is
rejected as invalid (and quarantinable); it can never deliver or move a
cursor.

### Scenario 3 — retired identities' history stays visible everywhere
An agent is retired after participating in conversations. Today the dashboard
message routes and `tail` (active-roster validators) hide that history, while
the thread panel (known-roster) still shows it — the same tool disagreeing
with itself. After this release `tail` and the dashboard message routes show
retired identities' history, consistent with the rest of 0.16.0+.

### Scenario 4 — a partial broadcast can always be completed
A broadcast partially fanned out; a still-missing recipient is then retired.
Today `broadcast --resume` refuses the retired recipient and fails (exit 5)
forever — and the error tells the operator to `--resume` again. After this
release resume skips now-retired recipients, names them as dropped, and
completes the active copies; if every remaining recipient is retired the batch
resolves successfully (exit 0).

### Scenario 5 — no obligation ever points at a tombstone
A broadcast question's audience member is retired before replying. Today the
"who owes the next move" view lists the tombstone as an awaited reply. After
this release retired members are excluded from the pending/await-reply
projection, while remaining observable (the frozen historical audience still
records that they were addressed).

### Scenario 6 — a second window for the same agent is flagged (NEW)
An operator accidentally starts a second long-lived command (`wait`/`listen`)
for an agent that is already actively waiting in another window of the same
store. Today nothing warns, and concurrent same-agent consumers can silently
lose cursor/threadstate updates. After this release the second command prints
an advisory warning naming the other live process; it never blocks and never
changes exit codes. `doctor` reports the current marker's PID and liveness.

### Testing expectations
pytest, extending the existing suites (`test_store.py`, `test_signing.py`,
`test_web.py`, `test_cli.py`, `test_threads.py`, `test_coordination.py`,
`test_doctor.py`). Every fix ships with a regression that pins the exact
reviewer repro.

## Requirements

### Functional Requirements

| ID | Requirement | Status |
|----|-------------|--------|
| FR-001 | Signature verification rejects a non-string `meta.signature` as a `ValueError` (invalid message) **before** any constant-time comparison runs, so a malformed signature value can never raise an uncaught `TypeError`. | Proposed |
| FR-002 | With such a poison file present, every read path degrades gracefully (skips it as invalid, never crashes): message delivery (`recv`/`wait`/`messages_for`), `valid_messages`, `current_epoch`, the invalid-message report (`status`/`doctor`/`list_invalid_messages`), and quarantine selection (`prune --invalid`). The file appears in the invalid report and is quarantinable. | Proposed |
| FR-003 | A message whose `id` does not match the canonical generated-id shape is classified **invalid** at scan time (and is therefore quarantinable); it is never delivered and can never advance a cursor. The canonical shape is derived from the id generator so no legitimately-generated id is ever reclassified. | Proposed |
| FR-004 | The dashboard message routes (`/api/messages`, `/api/messages/<id>`, `/messages/<id>`, index) and the `tail` command validate against the **known** roster (active ∪ retired), matching `valid_messages` / the dashboard's thread panel — so a retired identity's historical messages render rather than silently disappearing or being flagged invalid. | Proposed |
| FR-005 | `broadcast --resume` skips frozen recipients that are no longer on the active roster (retired), reports them as `dropped=[…]`, and sends the remaining active missing copies. If all still-missing recipients are retired, the batch resolves successfully (**exit 0**) with the dropped list, instead of a permanent partial-failure exit 5. | Proposed |
| FR-006 | Broadcast thread derivation excludes retired audience members from `pending` and from the `next_owner`/`await-reply` obligation projection. The frozen historical audience is preserved and retired members remain observable via an additive `audience_retired` field (absent when none). | Proposed |
| FR-007 | A starting long-lived command (`wait`, `listen`) detects when another **live** process is already acting as the same agent in the same store (via the existing active-wait marker, which records the owning process id) and prints an advisory stderr warning that names the other process id, states the one-window-per-agent assumption, and notes that concurrent same-agent use can lose cursor/threadstate updates. | Proposed |
| FR-008 | The duplicate-activation check is best-effort and fail-quiet: a stale or dead owning process produces no warning (silent crash recovery); any error in the liveness probe is swallowed; the warning never blocks startup and never changes the exit code. | Proposed |
| FR-009 | `doctor` reports the current active-wait marker's process id and liveness per agent where present, framed as advisory (it does not claim to have found all duplicates). | Proposed |

### Non-Functional Requirements

| ID | Requirement | Status |
|----|-------------|--------|
| NFR-001 | Backward compatibility: any marker/heartbeat reader tolerates the pre-0.18.0 on-disk format (e.g. a plain ISO heartbeat string, a marker without a pid). New fields are additive and absent-not-null when unused; no existing JSON shape changes. Existing tests that pin those shapes pass unmodified. | Proposed |
| NFR-002 | The full test suite passes on the CI matrix (Python 3.10–3.13 × Ubuntu/Windows/macOS) before any release tag. | Proposed |
| NFR-003 | The duplicate-activation liveness probe adds no measurable startup latency in the common (no-duplicate / no-marker) case — at most one marker read and, only when a fresh foreign pid is present, one O(1) liveness syscall. | Proposed |

### Constraints

| ID | Constraint | Status |
|----|-----------|--------|
| C-001 | Stdlib-only runtime: no new third-party imports. The Windows liveness probe uses `ctypes` against the Win32 API; POSIX uses `os.kill(pid, 0)`. | Mandatory |
| C-002 | Additive and backward-compatible: existing commands, exit codes, and JSON shapes preserved; new behavior is additive (one new warning, additive fields, stricter invalid-classification of already-undeliverable inputs). | Mandatory |
| C-003 | Message history is immutable; the frozen broadcast audience is never rewritten. Retired-member handling is presentation/derivation only. | Mandatory |
| C-004 | Exit-code contract preserved (0 ok, 1 wait-timeout, 2 usage/refusal, 3 superseded/stale, 4 unknown rid, 5 partial fan-out, 130 SIGINT). FR-005 narrows when 5 is emitted (all-retired → 0); it never broadens it. The new warning never changes any exit code. | Mandatory |
| C-005 | Windows-first: liveness, marker reads, and id validation all correct on Windows. | Mandatory |
| C-006 | Same-agent concurrent consumption is **unsupported** by design and is documented as such (README/SECURITY). 0.18.0 warns, it does not enforce — no file locking / single-writer machinery. | Mandatory |
| C-007 | Per-WP Codex cross-review over agenttalk before merge; fresh-eyes review before tag; CI matrix green before tag. | Mandatory |
| C-008 | Scope honesty: id-shape validation (FR-003) fixes the malformed-id attack only. Cross-machine clock skew produces well-formed future-dated ids that it does NOT catch; that remains a documented constraint, never claimed fixed. | Mandatory |

## Success Criteria

1. A single malformed message file, with signing enabled, no longer prevents
   delivery of any legitimate message and is quarantinable — verified by a
   regression that reproduces the exact poison file.
2. A message with a non-generated `id` can never be delivered or advance a
   cursor — verified by the `zzzz` repro.
3. A retired identity's historical messages appear identically across the
   dashboard message routes, `tail`, and the thread panel — verified on both
   surfaces.
4. A partial broadcast with a since-retired recipient can be driven to
   completion via `--resume` with a clear dropped report — verified end to
   end; the all-retired case exits 0.
5. No "who owes the next move" output ever names a retired identity — verified
   in derivation and CLI.
6. Starting a second `wait`/`listen` for an already-actively-waiting agent
   prints the advisory warning; a stale/dead marker does not — verified with a
   simulated live and a simulated dead owner.
7. Documentation states plainly that one window per agent is assumed and
   same-agent concurrency is unsupported.

## Key Entities

- **Invalid message** — a file failing parse/schema/roster/signature/id-shape
  validation; surfaced in the invalid report, quarantinable, never delivered.
- **Active-wait marker** — the existing per-agent marker for a live blocking
  wait; gains/uses an owning process id for duplicate detection.
- **Frozen broadcast audience** — the immutable send-time recipient set;
  retired members are now projected out of obligations but preserved for
  observability.
- **Liveness probe** — a stdlib, fail-quiet check of whether a process id is
  currently alive.

## Assumptions

- The active-wait marker already records (or can additively record) the owning
  process id; if a structured form is introduced, readers stay
  backward-compatible (NFR-001). The exact marker is a plan-phase detail.
- "Long-lived command" for FR-007 means the blocking-wait commands (`wait`,
  `listen`); one-shot commands are out of scope for the warning.
- The canonical id shape is whatever the id generator emits today; the
  validating pattern is derived from it so the two cannot drift.

## Out of Scope

- File locking / single-writer enforcement for same-agent concurrency (C-006).
- Fixing cross-machine clock skew ordering (C-008) — documented, not fixed.
- Any change to the broadcast send-time audience freeze or message history.
- Rewriting the dashboard render beyond the roster-parity validation switch.
- A complete live-process registry — duplicate detection is explicitly
  best-effort (FR-008/FR-009).
