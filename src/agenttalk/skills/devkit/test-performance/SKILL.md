---
name: test-performance
description: >-
  Produce executed performance-testing evidence for a concrete workload, budget,
  baseline, or suspected regression. Use when performance is a risk, a regression
  is suspected, or a close needs benchmark evidence with workload, dataset,
  environment, warmup, repetitions, variance policy, threshold, commands,
  artifacts, and residual risk. Do NOT use for speculative optimization, product
  implementation, architecture review, or performance tuning without measured
  evidence; route production changes to craft-code after the measurement proves
  the need.
reviewed-against: "0.52"
category: assurance
evidence-profile:
  - qa-result
---

# test-performance

Performance evidence is only useful when it is executable, repeatable, and tied
to a real budget or baseline. Do not green a performance claim from intuition,
code shape, or a single noisy timing.

## When To Use
- [ ] Performance is a named risk for the change.
- [ ] A regression is suspected and needs reproduction or dismissal.
- [ ] A lead or close needs executed benchmark evidence for a pinned ref.
- [ ] A workload, dataset or fixture, baseline or budget, and threshold can be
      defined before reporting a result.

## Contract
- [ ] Define the workload and what user or system behavior it represents.
- [ ] Name the dataset, fixture, scale, seed, and setup steps.
- [ ] State the budget or baseline and the regression threshold before running.
- [ ] Capture the environment: machine or runner, OS, Python/runtime version,
      dependency versions when relevant, CPU limits, and noisy-neighbor caveats.
- [ ] Include warmup, repetitions, variance policy, summary statistic, and
      outlier handling.
- [ ] Run exact commands and preserve artifacts or logs that let a reviewer
      inspect the result.
- [ ] Report residual risk, skipped dimensions, and why the evidence is
      sufficient for the requested scope.

## Procedure
1. Confirm the benchmark question: what changed, what workload could regress,
   and what decision the evidence will support.
2. Check whether a stable workload, dataset or fixture, budget or baseline, and
   regression threshold exist. If any are missing, return `status=needs-info`
   with `risk_class=performance`, `release_blocker=unknown`, and the exact
   planning gap. Do not report a green performance result.
3. Pin the ref and scope under test. Keep the run isolated from unrelated
   background work where possible.
4. Prepare the environment and record enough detail for a reviewer to reproduce
   or compare the run.
5. Run warmup and the planned repetitions. Prefer project benchmark tooling if
   it exists; otherwise use the narrowest deterministic command that exercises
   the workload.
6. Compare observed results to the predeclared budget, baseline, and threshold.
   Treat high variance or environment instability as evidence quality risk, not
   as a pass.
7. Emit `qa-result` evidence with the exact commands, results, artifacts, and
   residual risk. If the benchmark fails, include the failing output and the
   suspected bottleneck only as a hypothesis unless measured.

## Not For
- Optimizing or rewriting production code. Use craft-code after benchmark
  evidence proves a change is needed.
- General code review, architecture review, or complexity critique. Use
  review-code.
- Planning which performance checks are necessary. Use qa-strategy.
- Load tests that require live external services unless the operator explicitly
  approved that environment and risk.
- A green claim when no stable workload, budget, or baseline exists.

## References
- QA-result evidence rules: ../_shared/references/evidence.md
- Routing precedence and negative triggers: ../_shared/references/routing.md

## Evidence

Emit the `qa-result` profile (full rules + bus-validated vs skill-policy: ../_shared/references/evidence.md).

Required fields:

- `status`
- `reviewed_ref`
- `scope`
- `risk_class`
- `release_blocker`
- `tests_referenced`
- `tests_executed`
- `evidence`
- `residual_risk`
