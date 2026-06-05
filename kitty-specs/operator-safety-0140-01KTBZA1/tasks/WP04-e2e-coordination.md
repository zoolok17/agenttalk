---
work_package_id: WP04
title: End-to-end coordination tests
dependencies:
- WP02
- WP03
requirement_refs:
- FR-003
- FR-005
- FR-013
- FR-014
planning_base_branch: master
merge_target_branch: master
branch_strategy: Planning artifacts for this feature were generated on master. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into master unless the human explicitly redirects the landing branch.
subtasks:
- T019
- T020
- T021
agent: "claude"
shell_pid: "16804"
history:
- date: '2026-06-05T13:34:21Z'
  event: created
  by: claude
authoritative_surface: tests/
execution_mode: code_change
owned_files:
- tests/test_coordination.py
tags: []
---

# WP04 — End-to-end coordination tests

## Objective

Prove the two production incidents are structurally dead, at the level the
spec's Success Criteria demand: a scripted rescind race where the executor
always aborts, a scripted liaison flow where operator questions route
through exactly one channel, and the NFR-001 mixed-version compatibility
sweep. These tests are the release gate — WP05 may not start until they
are green.

## Context

Read: spec.md Success Criteria 1–3 + Scenarios 1–4, quickstart.md (the
manual versions of these flows), tests/test_coordination.py (existing
two-agent patterns — reuse its store/agent fixtures and in-process CLI
invocation style; no real subprocesses, no real sleeps near timeout
boundaries).

Ownership: ONLY tests/test_coordination.py. If a test exposes a product
bug, file it back to the owning WP via the review loop — do not hot-fix
cli.py/store.py/threads.py here.

Implement: `spec-kitty agent action implement WP04 --agent claude`.

## Subtasks

### T019 — rescind race end-to-end (#12, Success Criterion 1)

**Purpose**: the HOLD/fire crossing, reproduced and killed both ways.

**Steps** — three-agent-free, two agents suffice (lead, exec):
1. **Wake path**: lead sends tracked request to exec; exec arms
   `wait --to-request` (in-process, short poll); lead rescinds with a
   reason; assert wait returns exit 3, output contains RESCINDED banner +
   the reason; assert exec's global cursor did NOT advance; assert
   `threads --for exec` shows closed-superseded.
2. **Check-gate path** (the already-drained race): lead sends; exec
   `drain`s (consumes, cursor advances); lead rescinds; exec runs
   `check --to-request` → assert exit 3 + superseded; simulate the
   contract: exec does NOT act, replies declining instead; assert thread
   terminal, no double-execution artifact.
3. **Negative control**: same flow without rescind → check exits 0, exec
   proceeds. This guards against check false-positives — Success
   Criterion 1's "100% abort" is only meaningful if live requests pass.
4. **Crossing variant**: rescind lands while exec is mid-`drain`
   (write rescind between send and drain) — exec's subsequent check must
   still catch it (ordering by message id, not arrival).

### T020 — liaison flow end-to-end (#18, Success Criterion 3)

**Steps** — three agents (lead=liaison, worker-a, worker-b):
1. Full happy loop: set-operator-facing lead; worker-a escalates (assert
   printed `request_id=esc-...` line parses); assert
   `sync --for lead --json` escalations array has exactly the one entry;
   lead replies on the rid with `operator_answer=true`; assert
   escalations array empties; assert `threads --for worker-a` shows the
   escalation answered; assert worker-b's views never show the
   escalation (scoped visibility).
2. Refusal matrix end-to-end: no liaison configured → escalate exit 2;
   `--to lead` override succeeds anyway; liaison self-escalate exit 2;
   liaison cleared mid-flight (pending escalation survives — answered
   path still closes it).
3. Single-channel invariant: with liaison set, both workers escalate;
   assert both land in lead's bucket and zero escalation messages were
   addressed to anyone but lead.

### T021 — backward-compat sweep (NFR-001, Success Criterion 5)

**Steps**:
1. Build a store via 0.13.0-shaped operations only (no new commands);
   snapshot `status/sync/threads/whoami --json` outputs; assert every
   pre-existing key present with unchanged types/semantics, and that
   NO new keys appear when no new features were used (strict additivity:
   absent, not null — this matches how 0.12.x→0.13.0 additions behaved).
2. Old-reader simulation: write a rescind + an escalation into a store,
   then assert the PRE-EXISTING read paths still work: `drain` prints the
   rescind as ordinary content; `recv` unread counts include it; cursor
   advance over it works; thread derivation of an UNRELATED thread is
   unaffected.
3. Marker/config tolerance: hand-corrupt `<agent>.composing.json` and set
   `operator_facing: null` / `operator_facing: 123` in config.json;
   assert every command behaves exactly as if absent (no crash, no
   warning storm — at most one diagnostic line in doctor).

## Definition of Done

- [ ] Three subtasks complete; `pytest tests/test_coordination.py -q` green; full suite green
- [ ] Success Criteria 1, 3, 5 of spec.md each demonstrably mapped to passing assertions (name the test per criterion in the review request)
- [ ] No flaky timing: no test depends on wall-clock margins tighter than the suite's existing poll-interval conventions
- [ ] Zero product-code edits; any bug found is reported to the owning WP, not patched here
- [ ] Cross-review by Codex approved (`wp_id=WP04`)

## Reviewer guidance (Codex)

Attack: does T019.4 really cover the crossing (id-ordering vs
arrival-ordering)? Is T021's strict-additivity assertion robust to
incidental dict-ordering changes? Are refusal-matrix tests asserting
remediation hints, not just exit codes? Hunt for hidden timing
assumptions (anything that would flake on a slow CI box).

## Activity Log

- 2026-06-05T15:12:49Z – claude – shell_pid=16804 – Started implementation via action command
- 2026-06-05T15:16:56Z – claude – shell_pid=16804 – Release-gate e2e green; success criteria 1/3/5 mapped to passing tests
- 2026-06-05T15:21:40Z – claude – shell_pid=16804 – Moved to planned
- 2026-06-05T15:30:16Z – claude – shell_pid=16804 – Review passed: WP04 release-gate assertions hardened; narrow WP02 whoami addendum accepted; targeted and full pytest suites passed.
