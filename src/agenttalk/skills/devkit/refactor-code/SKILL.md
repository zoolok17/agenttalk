---
name: refactor-code
description: >-
  Restructure existing production code while preserving behavior: simplify
  boundaries, reduce duplication, improve names/layout, extract or inline
  abstractions, and make code easier to maintain without changing the observable
  contract. Use when Codex is asked for behavior-preserving cleanup, internal
  restructuring, simplification, or refactoring after scope is clear. Do NOT use
  for feature work, bug fixes or product changes that alter behavior (use
  craft-code), writing tests directly as the primary task (use test-coverage),
  reviewing an existing diff (use review-code), or opportunistic broad cleanup
  mixed into behavior-changing work.
reviewed-against: "0.48"
category: production
evidence-profile:
  - production-handoff
---

# refactor-code

Restructure code with **no intended behavior change**. The first deliverable is
always the behavior-preservation scope: what observable behavior must remain the
same, what files/modules are in scope, and what is explicitly out of scope.

## When To Use
- [ ] If the assignment carries `lane_id`, run
      `python -m agenttalk lane workspace --id <lane_id>` and work only in that
      path. If no workspace resolves, STOP and ask the lead; never create or
      reuse your own git worktree.
- [ ] Behavior must stay unchanged and the goal is structure, duplication,
      boundaries, naming, module shape, or simplification.
- [ ] The requested change is easier reviewability or maintainability, not new
      product behavior.
- [ ] A cleanup is already scoped narrowly enough to prove preservation.

## Contract
- [ ] State the behavior-preservation scope before editing.
- [ ] Make no behavior change without explicit approval; if a behavior change is
      needed, stop and route that work to craft-code.
- [ ] Keep changes local, incremental, and reviewable.
- [ ] Prove behavior preservation with tests, or explain the exact unprovable gap.
- [ ] Emit a production-handoff evidence record; never self-approve.

## Procedure
1. Name the exact scope and the behavior-preservation claim first. Include the
   observable behavior, public interfaces, data formats, CLI/API output, config
   defaults, error behavior, and performance assumptions that must not change.
2. Read the target code plus nearby callers/tests. Find existing contracts,
   fixtures, snapshots, docs, and build/test commands before editing.
3. Establish a baseline. Run the relevant existing tests when cheap; if the
   baseline is red, route the failure to fix-ci before refactoring. If behavior
   is under-specified, add only the narrow characterization needed to prove the
   refactor, or hand off broader test work to test-coverage.
4. Plan small mechanical steps. Prefer moving, renaming, extracting, inlining,
   deduplicating, and boundary tightening that preserve inputs, outputs, side
   effects, ordering, persistence, and errors.
5. Edit in local reviewable increments. Do not mix in feature work, bug fixes,
   opportunistic broad cleanup, dependency changes, formatting churn, or public
   contract changes.
6. Run the same relevant tests/checks after the refactor. If exact before/after
   proof is impossible, state the gap, why it remains acceptable or needs review,
   and what evidence was still collected.
7. Re-read the diff as a reviewer: every line should support the scoped
   preservation claim. Remove unrelated cleanup, stale comments, dead code, and
   accidental behavior changes.
8. Produce the production-handoff: changed files, base/head refs, preservation
   claim, tests referenced/executed, residual risk, required review lenses, and
   evidence.

## Not For
- Feature work.
- Bug fixes or product changes that alter behavior - use craft-code.
- Opportunistic broad cleanup mixed into product changes.
- Making an already-failing check green - use fix-ci.
- Writing broader test coverage as the primary task - use test-coverage.
- Reviewing or approving an existing diff - use review-code.

## References
- Production-handoff evidence rules (bus-validated vs skill-policy fields):
  ../_shared/references/evidence.md
- Routing precedence and negative triggers: ../_shared/references/routing.md

## Evidence

Emit the `production-handoff` profile (full rules + bus-validated vs skill-policy: ../_shared/references/evidence.md).

Required fields:

- `changed_files`
- `base_ref`
- `head_ref`
- `summary`
- `tests_referenced`
- `tests_executed`
- `residual_risk`
- `required_review_lenses`
- `evidence`
