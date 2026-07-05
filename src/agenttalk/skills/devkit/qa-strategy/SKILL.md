---
name: qa-strategy
description: >-
  Plan the QA strategy for a change: identify risk areas, recommend test levels
  and review lenses, state which checks are not needed and why, include cost
  notes, and identify required close evidence. Use when deciding which
  tests/checks/lenses are necessary before implementation, review, or release
  gating. Do NOT use for writing tests directly or replacing test-coverage.
reviewed-against: "0.45"
category: production
evidence-profile:
  - planning-artifact
---

# qa-strategy

Plan the QA and review coverage a change needs **before** anyone writes tests,
reviews a diff, or gates a release. This is a **planning** skill: it emits a
QA/review plan plus the evidence each step must return. It does **not** mutate
code, write or run tests, or approve a change — hand those to the skills it names.

## When To Use
- [ ] Deciding what tests, checks, reviewers, fresh-context lenses, and close
      evidence a change needs.
- [ ] Pre-implementation planning: scope the QA surface before craft-code starts.
- [ ] Pre-review planning: pick the review lenses a diff actually warrants.
- [ ] Lead close planning when the risk is ambiguous and coverage must be justified.

## Contract
- [ ] Identify the risk areas the change touches.
- [ ] Recommend test levels and review lenses matched to those risks.
- [ ] State which checks are NOT needed, and why.
- [ ] Include cost notes — cheap, moderate, expensive — for each recommended check.
- [ ] Identify the evidence required for close.
- [ ] Emit a planning-artifact evidence record (a plan, never an approval).

## Procedure
1. Read the task, design, and/or diff. Name the exact scope; if it is not yet
   pinned, say the scope is **provisional** and plan against the stated intent.
2. Identify risk areas: behavior; persistence/state; CLI/process/filesystem;
   config/install/package; docs contract; security (input/path/env); performance;
   compatibility; release/close authority; end-user / operator-facing workflow
   (a human uses this directly via a UI, chat, or CLI).
3. Map each risk to the checks that catch it: lint/static, unit/regression,
   integration/boundary, failure-injection, contract-drift, docs-QA,
   release-readiness, end-to-end user-path exercise, and fresh-context evidence.
   For an end-user-facing feature, the plan MUST include running the REAL user
   journey against a realistic setup; unit / contract / security coverage does
   NOT substitute for it.
4. Assign each recommended check a cost — cheap, moderate, or expensive — and
   justify every expensive one (its token/time cost against the risk it retires).
5. State explicitly what is NOT needed and why (for example: no security review
   for a change that touches no auth, input, filesystem, process, env, or secret
   surface).
6. Produce the handoff: which skill or lens runs next, in what order, and the
   exact evidence each must return for close.

## Not For
- Writing tests directly — use test-coverage.
- Replacing test-coverage or avoiding obvious required tests — the plan names the
  required tests, it does not excuse them.
- Implementing production code — use craft-code (or refactor-code / fix-ci later).
- Diagnosing an already-failing CI command — use fix-ci; qa-strategy may
  recommend post-fix verification but does not do the log / root-cause work.
- Final review approval — use review-code, tester-qa, review-release-readiness,
  and the other review lenses.

## References
- Planning-artifact evidence rules (bus-validated vs skill-policy fields):
  ../_shared/references/evidence.md
- Routing precedence and negative triggers: ../_shared/references/routing.md

## Evidence

Emit the `planning-artifact` profile (full rules + bus-validated vs skill-policy: ../_shared/references/evidence.md).

Required fields:

- `artifact_type`
- `scope`
- `decision`
- `assumptions`
- `alternatives`
- `risks`
- `required_reviews`
- `open_questions`
- `evidence`
