---
work_package_id: WP02
title: CLI wiring
dependencies:
- WP01
requirement_refs:
- FR-001
- FR-002
- FR-003
- FR-005
- FR-010
- FR-011
- FR-012
- FR-013
- FR-014
planning_base_branch: master
merge_target_branch: master
branch_strategy: Execution worktree allocated per computed lane from lanes.json; work branches from master and merges into master.
subtasks:
- T006
- T007
- T008
- T009
- T010
history:
- Created by /spec-kitty.tasks for coordination-polish-0240-01KTMG5Z
authoritative_surface: src/agenttalk/cli.py
execution_mode: code_change
owned_files:
- src/agenttalk/cli.py
- tests/test_cli.py
- tests/test_threads.py
tags: []
---

# WP02 — CLI wiring

## Objective

Wire the four CLI-facing behaviors on top of WP01's store/doctor foundation: escalate
lead-fallback, the roster set-role demote/promote notice, the wake `wk-` correlation id,
and the soft owed-inbound pre-send warning. This WP owns `cli.py`.

## Context

- Depends on **WP01**: `store.sole_lead()` and the `set_role` demoted-name return must
  already exist. Branch this WP from WP01's lane base.
- `escalate` handler (cli.py ~1069-1139): resolves the target as `--to` (override) →
  `store.operator_facing()` → else error (exit 2). It mints an `esc-` request_id and
  sends a `question` carrying `needs_operator=true`. Keep all of that; only insert the
  lead-fallback between the liaison-None case and the exit-2 error.
- ID minting: `cli._AUTOGEN_REQUEST_ID_PREFIX` (cli.py:92) maps thread-opening kinds to
  prefixes; `_maybe_autogen_request_id` (cli.py:106) fills a missing `request_id` in the
  send path. `store.OPENER_KINDS` (store.py:94) — the SEPARATE set that drives thread
  derivation — must NOT gain `wake`.
- `cmd_send` send path resolves sender/recipient/body/meta then calls `store.send`. The
  owed-inbound warning goes just before the send, derived from `threads.derive_threads`.
- `threads.derive_threads` returns thread rows from an agent's perspective (open-outbound
  / owed-inbound / reply-waiting / closed), each carrying kind + request_id + the peer.

## Subtasks

### T006 — `escalate` lead-fallback (FR-001, FR-002, FR-003)

**Steps** (in the `else` branch where `target = store.operator_facing()`):
1. If `target is None`, before returning 2, try `lead = store.sole_lead()`.
2. If `lead` is not None and `lead != sender`: set `target = lead` and emit a notice
   (stderr or stdout, non-quiet) that escalation fell back to the lead because no liaison
   is configured — e.g. `agenttalk escalate: no liaison configured; routing to lead
   '<lead>'.` Continue the normal send (mint `esc-` id, print `request_id=…`). (FR-002)
3. If `lead` is None (or equals sender), return 2 — but with a remediation message that
   names BOTH fixes: `agenttalk roster set-operator-facing <agent>` AND
   `agenttalk roster set-role <agent> lead` (plus the existing `--to` hint). (FR-003)
4. Preserve: `--to` override path (FR-001), the self-target guard ("you ARE the
   operator-facing agent"), and the existing `esc-` id behavior. Exit code stays 2 when
   there is genuinely no target (NFR-004).

**Validation**: see the escalate matrix in `contracts/cli-behavior.md`.

### T007 — roster `set-role` notice (FR-005 print half)

**Steps**: in the CLI `roster set-role` handler, after calling `store.set_role(...)`,
detect whether a prior lead was demoted (via WP01's returned demoted name). If so, print
`demoted <old>, promoted <new> to lead`. No `--force` flag is introduced or required.
Idempotent self-set prints no demotion line.

**Validation**: switching the lead prints the demote/promote line in one command;
re-setting the same lead prints no demotion line.

### T008 — wake `wk-` correlation id (FR-010, FR-011)

**Steps**:
1. Add `"wake": "wk-"` to `cli._AUTOGEN_REQUEST_ID_PREFIX`.
2. Update the comment above that dict so it's explicit: most entries are thread-OPENING
   kinds, but `wake` mints a correlation id WITHOUT opening a thread (it is not in
   `store.OPENER_KINDS`). The id only lets a reply echo it.
3. Confirm `_maybe_autogen_request_id` already honors an explicit `request_id` (it does —
   it only fills a missing one); no change needed for the explicit-id path.
4. Do NOT touch `store.OPENER_KINDS`.

**Validation**: `send --kind wake` mints `wk-…`; explicit `--meta request_id=Z` is kept
verbatim; a wake creates no thread row (asserted in T010 via OPENER_KINDS).

### T009 — owed-inbound pre-send warning (FR-012, FR-013, FR-014) — CUTTABLE (C-004)

**Steps** (in `cmd_send`, just before `store.send`):
1. Best-effort, wrapped so any exception is swallowed (never fail/alter the send):
   derive the sender's threads (`threads.derive_threads`), find rows where the sender
   owes the **recipient** a response AND the thread is a decision-request — opener kind
   `proposal` OR an operator escalation (`needs_operator` true).
2. If any such owed decision-request exists AND the outgoing message is NOT a reply on
   that same `request_id` (FR-013), write a soft stderr warning naming the owed id(s):
   e.g. `agenttalk send: warning: you owe <recipient> an open proposal (pp-…); answer or
   rescind it before unrelated traffic.` Then proceed with the send unchanged.
3. Do not warn for non-decision traffic, nor when replying on the same id, nor when the
   owed thread is a plain question/review.

**If this balloons** (e.g. the derivation needs awkward plumbing or the suppression logic
gets hairy), CUT it: drop T009 and FR-012..014 from this release per C-004, and note the
cut in WP03's CHANGELOG. Do not stretch the release for it.

**Validation**: see the pre-send warning matrix in `contracts/cli-behavior.md`.

### T010 — tests: `tests/test_cli.py` + `tests/test_threads.py`

- `test_cli.py`: escalate fallback matrix (liaison routes; no-liaison+sole-lead routes to
  lead with notice; no-liaison+no-lead exits 2 with both-remediation message; `--to`
  overrides; two-legacy-leads exits 2); set-role notice (demote/promote line; idempotent
  no line); wk- mint + explicit-id honored; owed-inbound warn / suppressed-on-same-id /
  best-effort-does-not-fail-send. If T009 is cut, drop its three asserts and say so.
- `test_threads.py`: assert `"wake" not in store.OPENER_KINDS` (locks FR-011), and that a
  sent wake produces no owed/open thread row for the recipient.

## Test Strategy

pytest, stdlib. Capture stderr/stdout with capsys; assert exit codes via the command
return value. Build rosters with a liaison / a lead / neither to exercise escalate. For
owed-inbound, create a real `proposal` then attempt an unrelated `send` and assert the
warning text + that the message was still delivered.

## Definition of Done

- FR-001..003, FR-005 (print), FR-010..011 implemented; FR-012..014 implemented OR
  explicitly cut per C-004 (with a note for WP03).
- `pytest tests/test_cli.py tests/test_threads.py -q` green; `ruff check src tests` clean.
- `escalate` exit-2 contract preserved; `--to` override intact; `OPENER_KINDS` unchanged.
- The pre-send warning never fails a send.
- No edits outside `owned_files`.

## Branch Strategy

Planning base **master**; final merge target **master**. Branch from WP01's lane base
(dependency). Execution worktree allocated from `lanes.json`.

## Reviewer Guidance

- Confirm escalate resolution order is exactly `--to` → liaison → sole_lead → exit 2, and
  the exit-2 message names BOTH remediation commands.
- Confirm `wake` mints `wk-` but `store.OPENER_KINDS` is untouched (grep it).
- Confirm the owed-inbound warning is soft, same-peer, decision-kind only, suppressed on
  same request_id, and best-effort (force a derivation error → send still succeeds).
- Confirm no new dependency and no message-history mutation.

## Implementation command

`spec-kitty agent action implement WP02 --agent <name>`
