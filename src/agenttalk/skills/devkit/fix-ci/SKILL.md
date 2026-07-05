---
name: fix-ci
description: >-
  Diagnose and fix a failing local or CI check by reading the failing command
  and logs first, classifying the root cause, applying or proposing the
  smallest fix, and verifying the result. Use when a concrete test, lint,
  build, security, or CI job is red. Do NOT use for general feature work,
  broad cleanup after CI is green, or guessing at failures without logs.
reviewed-against: "0.48"
category: production
evidence-profile:
  - production-handoff
---

# fix-ci

Make an **already-failing** check green again. Read the failing command and its
full log **first**, classify the root cause, then apply or propose the **smallest**
fix that addresses that cause — and verify it. This skill never broadens into
feature work, drive-by cleanup, or self-approval.

## When To Use
- [ ] A concrete local command, CI job, test, linter, type/security check, build,
      or packaging step is failing, and the task is to diagnose it and make it green.
- [ ] An after-implementation verification step (the AFTER gate of craft-code,
      test-coverage, etc.) has gone red and needs root-causing.

## Contract
- [ ] Identify the failing command/check and the EXACT failure (message, ref, env).
- [ ] Determine the root cause: code defect, test defect, flaky test, environment,
      dependency, or CI configuration.
- [ ] Apply or propose the SMALLEST fix that addresses that root cause only.
- [ ] Run or reference the verification that the check is now green.
- [ ] Emit a production-handoff evidence record (hand to reviewers/lead; never self-approve).

## Procedure
0. If the failing assignment carries `lane_id`, run
   `python -m agenttalk lane workspace --id <lane_id>` and diagnose/fix only in
   that path. If no workspace resolves, STOP and ask the lead; never create or
   reuse your own git worktree.
1. Capture the failing command/check, ref, environment, and FULL log/output first.
   Do not infer from a summary when the log is available.
2. Reproduce locally when it is deterministic and cheap. If it is CI-only, inspect
   the CI log and record why local reproduction is not equivalent.
3. Classify the root cause as one of: code defect, test defect, flaky test,
   environment, dependency, CI configuration. If it is unclear, gather more
   evidence before editing anything.
4. Choose the smallest fix that addresses that root cause ONLY. A product-behavior
   change beyond the failure goes to craft-code; broad behavior-preserving
   restructuring goes to refactor-code.
5. Implement or propose the fix. Avoid unrelated cleanup, drive-by refactors, and
   any widening of scope.
6. Verify by rerunning the original failing command, or by citing the exact CI
   rerun/log. Add the narrowest related regression check when it is warranted.
7. Produce the production-handoff: changed files, base/head refs, summary, tests
   referenced/executed, residual risk, required review lenses, and evidence.

## Not For
- General feature work, or broad cleanup after CI has turned green.
- Guessing at a failure without reading its log/output.
- Writing a test plan before any failure exists — use qa-strategy.
- Writing broader coverage once the root cause is known — use test-coverage.
- Product-behavior changes beyond the failing check — use craft-code.

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
