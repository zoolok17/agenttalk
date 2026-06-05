---
work_package_id: WP02
title: Next-owner / next-action derivation
dependencies: []
requirement_refs:
- FR-014
- FR-015
planning_base_branch: master
merge_target_branch: master
branch_strategy: Planning artifacts for this feature were generated on master. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into master unless the human explicitly redirects the landing branch.
created_at: '2026-06-05T20:35:00Z'
subtasks:
- T008
- T009
- T010
- T011
agent: "claude"
shell_pid: "16400"
history:
- date: '2026-06-05T20:35:00Z'
  event: created
  by: claude
authoritative_surface: src/agenttalk/
execution_mode: code_change
owned_files:
- src/agenttalk/threads.py
- tests/test_threads.py
tags: []
---

# WP02 — Next-owner / next-action derivation

## Objective

Add read-only `next_owner` / `next_action` to thread rows, derived **purely** from
existing thread state. This is the tool-visible "who owes the next move" hint the
band asked for (the soft-deadlock pain). You own `src/agenttalk/threads.py` and
`tests/test_threads.py` only.

Read first: `spec.md` (FR-014/015), `research.md` **D6** (the derivation table),
`data-model.md` §5, and the existing `threads.py` (`ThreadRow`, `to_dict`,
`derive_threads`, the `state`/`operator_state`/`needs_operator`/`responded_na`
fields).

Standing constraints: **stdlib only**; **strictly additive** — do not change any
existing `to_dict` key, field, or thread ordering; the new fields are a pure
projection of state already computed. They must NEVER be settable by a sender and
must NEVER affect delivery, unread counts, or thread closure.

## Context

- `ThreadRow` (threads.py ~109) already has `state`
  (`reply-waiting | owed-inbound | open-outbound | closed | closed-superseded`),
  `needs_operator`, `operator_state` (`pending | answered | closed`),
  `responded_na`, and the broadcast/responded tracking.
- `to_dict()` (~160) serializes the row for `threads --json` / `sync --json`;
  it already conditionally includes keys (e.g. `operator_state`, `responded_na`).
  Follow that exact conditional-inclusion idiom.
- `derive_threads()` (~376) builds rows.

## Subtasks

### T008 — fields on `Thread` (NOT emitted by `to_dict`)
**Steps**:
1. Add `next_owner: str | list[str] | None = None` and
   `next_action: str | None = None` to the `Thread` dataclass (defaults absent).
2. **`to_dict()` does NOT emit them** (revised decision). Unlike the 0.15.0 keys
   (`responded_na` etc., emitted only when a feature is used), `next_*` appear
   on EVERY open thread, so emitting them from `to_dict` changes the baseline
   thread JSON shape and trips the 0.15.0 additivity gates in
   `test_coordination.py` (a WP03-owned file). Surfacing into `threads`/`sync
   --json` is therefore done at the CLI layer in **WP03/T015**, which owns +
   updates those additivity tests. WP02 keeps `to_dict` shape-stable; WP03 reads
   `t.next_owner` / `t.next_action` off the `Thread` and injects them.

### T009 — derivation function
**Purpose**: Map state → `(next_action, next_owner)` per research D6.

**Steps**: Add a pure helper, e.g.
`_derive_next(row_state, *, self_agent, peer, needs_operator, operator_state,
non_responders) -> tuple[str|None, str|list[str]|None]` returning:

CRITICAL — match the REAL state semantics (the first-draft table inverted
`reply-waiting`/`open-outbound`): in `derive_threads`, `reply-waiting` means a
reply addressed to `self` is sitting UNREAD (ball back with self), while
`open-outbound` means `self` is waiting on the peer.

| state (from `self`'s view) | next_action | next_owner |
|---|---|---|
| needs_operator + operator_state=="pending" | `answer-operator` | `self_agent` |
| `owed-inbound` (you owe a reply) | `reply` | `self_agent` |
| `reply-waiting` (a reply to you is unread) | `read-reply` | `self_agent` |
| `open-outbound` pairwise (you await the peer) | `await-reply` | the peer |
| `open-outbound` broadcast (members still owe) | `await-reply` | non-responders |
| `closed` / `closed-superseded` | `None` | `None` |

- A post-pass over the assembled `threads` list (covers pairwise AND broadcast)
  is the cleanest call site — `_derive_next(t, agent)` reads off the `Thread`.
  For broadcast `open-outbound`, `next_owner` is the `pending` list (reuse it).
- `act-or-rescind` is DROPPED — no current state produces it; keep the
  vocabulary closed to values actually emitted (`reply`/`read-reply`/
  `await-reply`/`answer-operator`).

### T010 — wire into `derive_threads`
**Steps**: In a post-pass over the assembled `threads` list (before the final
sort), call `_derive_next(t, agent)` and set `t.next_action`/`t.next_owner`.
Terminal rows get `None`/`None`. Do not reorder rows or change any other field.

### T011 — tests (`tests/test_threads.py`)
Cover:
- `owed-inbound` → `next_action="reply"`, `next_owner=self`.
- `reply-waiting` (unread reply for self) → `next_action="read-reply"`,
  `next_owner=self`.
- `open-outbound` pairwise → `next_action="await-reply"`, `next_owner=peer`.
- operator-pending escalation → `next_action="answer-operator"`.
- broadcast `open-outbound` with partial responses → `next_owner` = exactly the
  non-responders (order-stable), `next_action="await-reply"`.
- `closed` / `closed-superseded` → both fields `None`.
- shape-stability: `to_dict()` NEVER emits `next_*` (open AND closed rows) — the
  surfacing is WP03's job, so the 0.15.0 additivity gates stay green here.

## Branch Strategy

Planning branch: `master`. Final merge target: `master`. Worktree allocated from
`lanes.json` during `/spec-kitty.implement`.

Implement command: `spec-kitty agent action implement WP02 --agent claude`

## Definition of Done

- T008–T011 complete; `pytest tests/test_threads.py` green; full `pytest` green.
- No existing `to_dict` key/ordering changed; new keys absent when not derivable.
- No file outside `owned_files` modified.
- Codex cross-review (meta mission + wp_id WP02) approved.

## Reviewer guidance (for Codex)

- Confirm the fields are a pure projection — grep that nothing reads a
  sender-supplied `next_owner`/`next_action` and that derivation has no side
  effects (no writes, no cursor/threadstate mutation).
- Confirm terminal threads omit both keys and that broadcast `next_owner` is
  exactly the non-responder set, not all recipients.
- Confirm `next_action` only ever takes values the code actually produces (closed
  vocabulary; no speculative `act-or-rescind` leaking out unused).

## Activity Log

- 2026-06-05T20:59:47Z – claude – shell_pid=16400 – Started implementation via action command
- 2026-06-05T21:16:08Z – claude – shell_pid=16400 – next_owner/next_action derivation; 8 tests; full suite 580 pass
