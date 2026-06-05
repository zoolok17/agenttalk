---
work_package_id: WP02
title: CLI surface
dependencies:
- WP01
requirement_refs:
- FR-002
- FR-003
- FR-004
- FR-005
- FR-006
- FR-007
- FR-008
- FR-009
- FR-010
- FR-013
planning_base_branch: master
merge_target_branch: master
branch_strategy: Planning artifacts for this feature were generated on master. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into master unless the human explicitly redirects the landing branch.
subtasks:
- T006
- T007
- T008
- T009
- T010
- T011
agent: "claude"
shell_pid: "52048"
history:
- date: '2026-06-05T16:40:00Z'
  event: created
  by: claude
authoritative_surface: src/agenttalk/
execution_mode: code_change
owned_files:
- src/agenttalk/cli.py
- tests/test_cli.py
tags: []
---

# WP02 - CLI surface

## Objective
The 0.15.0 user-visible surface per contracts/cli-surface.md
(NORMATIVE - on divergence the contract wins).

## Context
Builds on WP01. C-005: exit 5 = partial fan-out (new); 0/1/2/3/4/130
untouched, wait's 1 stays timeout-exclusive. Strict additivity for all
JSON. v0.9.0 trap: never touch reply's request_id echo logic.
Implement: `spec-kitty agent action implement WP02 --agent claude --mission team-scope-0150-01KTC934`.

## Subtasks

### T006 - broadcast --to-role + frozen meta (#15)
Add `--to-role` to the required mutually-exclusive group. Resolution
via store.resolve_role_audience (ValueError -> exit 2). EVERY fan-out
copy (all targets, not just role) gains: `audience_kind`
(role|group|all), `audience_resolved` (comma-joined recipients),
`batch_total` (str(N)); role targets also `audience_role`. Keep the
existing `audience` label key unchanged. Force-set (not setdefault) -
broadcast owns its meta like it owns the bid.

### T007 - partial-failure manifest + exit 5 (#16)
Wrap the per-recipient send loop: on ANY exception, print
`delivered=[a,b]` and `missed=[c,d]` as machine-parseable lines (and
with --json a `{"batch_id","delivered","missed"}` object), stderr
explains + remediation ("re-send to the missed members with the same
--meta request_id=<bid>, or rescind"), exit 5. Success path unchanged
(exit 0 + existing summary). Preflight unchanged (audience resolution
already precedes the loop).

### T008 - reply --na (#15, FR-004/005/006)
`--na` flag: mutually exclusive with `--kind` (argparse group or
explicit exit 2); forces kind=message, meta `response=not-applicable`;
body optional, defaults to "n/a". FR-006 refusal: resolve the anchor
(existing _resolve_reply_anchor), find the thread's opener kind via
derivation (view row); if review-request or proposal -> exit 2 ("this
thread needs a typed response: review-result / proposal-response").
Question threads (pairwise AND broadcast) accepted.

### T009 - prune command (#17)
`agenttalk prune --invalid [--dry-run] [--json] [--quiet]`. Bare
`prune` without --invalid -> exit 2 (explicit selector; future
selectors reserved). Calls store.quarantine_invalid; human output: one
line per file (id, reason, dest) + summary count; --json:
`{"selected":[...],"moved":[...],"dry_run":bool}` (moved empty on
dry-run). Zero invalid: "nothing to prune", exit 0.

### T010 - displays + warnings
1. status --json: top-level `quarantined` (int file count; key ALWAYS
   present? NO - additive: present only when > 0; human line
   `quarantined: N` likewise).
2. _thread_warnings: incomplete-batch - for broadcast rows where
   batch_total is set and len(visible opener copies) < batch_total
   (use the WP01 passthrough + audience_resolved to name the missed),
   warn once per batch with the missed members; SUPPRESSED when the
   row state is closed-superseded (rescind resolves, FR-009).
3. threads human: broadcast rows append `na=[members]` when
   responded_na non-empty; pairwise rows closed by NA show `(n/a)`.
   JSON is already handled by WP01's to_dict.
4. invalid_messages count in status: unchanged (existing surface).

### T011 - tests
Per-command coverage incl.: --to-role happy/unknown/empty (exit 2 +
known-roles text); meta freeze assertions on raw files; exit-5 fault
injection (monkeypatch Store.send to raise at position k; assert
delivered/missed lines, exit 5, k-1 files on disk); --na happy
(pairwise + broadcast member), --kind conflict, FR-006 refusals,
default body; prune dry-run/real/zero/bare-refusal + --json shapes;
incomplete-batch warning appears, names missed, disappears after
re-send to missed, suppressed after rescind; strict additivity on a
no-feature store (no quarantined key, no new row keys). Exit-code
sweep: 5 reachable ONLY via partial fan-out.

## Definition of Done
- [ ] All six subtasks; test_cli + full suite green; ruff clean
- [ ] Exit 5 documented in --help and reachable only via partial fan-out
- [ ] No store.py/threads.py/doctor.py edits
- [ ] Codex review approved (wp_id=WP02)

## Reviewer guidance (Codex)
Attack: exit-code contract (esp. wait 1 / check 3-4 untouched; 5 never
returned elsewhere); additivity (quarantined only when >0 - check the
no-feature store shape test); FR-006 refusal correctness for needs-info
threads (review-request anchor mid-ping-pong must still refuse NA);
fault-injection realism; reply request_id echo untouched (v0.9.0 trap).

## Activity Log

- 2026-06-05T16:52:42Z – claude – shell_pid=52048 – Started implementation via action command
- 2026-06-05T17:02:29Z – claude – shell_pid=52048 – CLI surface green: 534 passed
- 2026-06-05T17:09:57Z – claude – shell_pid=52048 – Moved to planned
