---
work_package_id: WP04
title: E2e gates
dependencies:
- WP02
- WP03
requirement_refs:
- FR-002
- FR-005
- FR-008
- FR-012
planning_base_branch: master
merge_target_branch: master
branch_strategy: Planning artifacts for this feature were generated on master. During /spec-kitty.implement this WP may branch from a dependency-specific base, but completed changes must merge back into master unless the human explicitly redirects the landing branch.
subtasks:
- T014
- T015
- T016
- T017
history:
- date: '2026-06-05T16:40:00Z'
  event: created
  by: claude
authoritative_surface: tests/
execution_mode: code_change
owned_files:
- tests/test_coordination.py
tags: []
---

# WP04 - E2e gates

## Objective
Success criteria 1-4 demonstrably hold. Release gate: WP05 may not
start until green. Tests only - product bugs go back to the owning WP
via review, never hot-fixed here.

## Context
Follow test_coordination.py's in-process patterns. The 0.14.0 WP04
review lesson binds: the gate must assert the STATED outputs (texts,
shapes), not just exit codes. Implement:
`spec-kitty agent action implement WP04 --agent claude --mission team-scope-0150-01KTC934`.

## Subtasks

### T014 - role routing + freeze (SC 1)
4-agent roster (lead + 2 reviewers + 1 implementer): --to-role reviewer
delivers exactly 2 copies; implementer's threads/sync show NOTHING on
the bid; copies carry the frozen meta (audience_kind=role,
audience_role, audience_resolved, batch_total=2); then CHANGE rev-b's
role -> derivation output for the thread is byte-identical
(pending/audience unchanged); unknown role exit 2 naming known roles.

### T015 - NA lifecycle (SC 2)
Member replies --na: broadcaster sees responded incl. the member AND
responded_na=[member]; human view shows the n/a marker; the OTHER
member still pending; NA on a pairwise question closes with
na_response; FR-006 e2e: --na on a review-request thread refuses with
the typed-response text (assert stderr).

### T016 - partial fan-out (SC 3)
Monkeypatch Store.send to raise at k=1 (first), k=2 (mid), k=N (last)
of a 3-recipient roster: assert exit 5, delivered=/missed= lines exact,
k-1 copies on disk; status warning names the missed members; complete
the batch by re-sending to missed with the same request_id -> warning
disappears; alt resolution: rescind the bid -> warning suppressed.

### T017 - prune byte-identity + additivity (SC 4 / NFR-001)
Seed a store with valid traffic + N invalid files; hash every VALID
message file + cursors + threadstate; prune; assert: status invalid
empty, quarantine holds exactly the N, every hash identical, threads
output identical pre/post; strict-additivity sweep extended: no
quarantined key, no audience_kind/responded_na/na_response/batch_total
keys on a no-feature store (set-equality, the 0.14.0 gate style).

## Definition of Done
- [ ] Four subtasks; test_coordination + full suite green
- [ ] SC 1-4 each named to a passing test in the review request
- [ ] Zero product-code edits
- [ ] Codex review approved (wp_id=WP04)

## Reviewer guidance (Codex)
Attack: does T016 prove ORDER-independence of the failure position?
Are the additivity gates set-equality (not presence checks)? Is the
freeze test actually structural (roles changed AFTER send, derivation
output compared exactly)?
