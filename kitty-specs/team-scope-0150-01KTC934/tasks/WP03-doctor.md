---
work_package_id: WP03
title: Doctor store hygiene
dependencies:
- WP02
requirement_refs:
- FR-013
planning_base_branch: master
merge_target_branch: master
branch_strategy: Planning base master; completed changes merge into master; execution worktrees are allocated per computed lane from lanes.json.
subtasks:
- T012
- T013
history:
- date: '2026-06-05T16:40:00Z'
  event: created
  by: claude
authoritative_surface: src/agenttalk/
execution_mode: code_change
owned_files:
- src/agenttalk/doctor.py
- tests/test_doctor.py
tags: []
---

# WP03 - Doctor store hygiene

## Objective
One doctor check making store debris visible: invalid count (with the
prune remediation) + quarantined count.

## Context
doctor.py only (cmd_doctor wiring stays as-is). The 0.14.0 CI lesson
binds: tests must pin the environment, never assert doctor exit codes
on an unpinned host. Implement:
`spec-kitty agent action implement WP03 --agent claude --mission team-scope-0150-01KTC934`.

## Subtasks

### T012 - store-hygiene check (#17, FR-013)
`_check_store_hygiene(store)`: invalid==0 and quarantined==0 -> ok
("clean"); invalid>0 -> warn with count + fix
"`agenttalk prune --invalid --dry-run` to inspect, then without
--dry-run to quarantine (recoverable)"; quarantined>0 (only) -> ok,
informational count ("N quarantined file(s) - restore by moving back
into messages/"). data payload: {"invalid": n, "quarantined": m}.
Wire into run() after _check_operator_facing (initialized stores only).

### T013 - tests (env-pinned)
All states (clean / invalid>0 / quarantined>0 / both); fix text names
prune --dry-run first; counts in data payload; NO unpinned exit-code
assertions (cite the CI-gate rule in a comment); existing doctor tests
untouched.

## Definition of Done
- [ ] Both subtasks; test_doctor + full suite green; ruff clean
- [ ] doctor.py only; no enforcement-ish or scary wording (quarantine is recoverable)
- [ ] Codex review approved (wp_id=WP03)

## Reviewer guidance (Codex)
Attack: remediation order (--dry-run FIRST - the install-skills --force
lesson pattern); counts must come from the shared store methods, not a
re-implemented scan.
