---
name: test-integration
description: >-
  Validate behavior across real integration boundaries such as CLI plus store,
  filesystem behavior, config loading, migrations, multiple modules, or
  process/supervisor interactions. Use when confidence depends on exercising a
  real boundary with isolated temp roots and deterministic commands. Do NOT use
  for pure-function or low-level behavior tests where test-coverage is enough,
  full user journeys that require unstable sleeps or external services, fixing
  an already-failing check (use fix-ci), product implementation/refactoring (use
  craft-code or refactor-code), or reviewing an existing diff (use review-code).
reviewed-against: "0.49"
category: assurance
evidence-profile:
  - qa-result
  - production-handoff
---

# test-integration

Test the behavior that only appears when real project boundaries meet. Prefer
real boundaries where they are deterministic and cheap; isolate them so the test
is repeatable and cannot mutate the operator's live state.

## When To Use
- [ ] Confidence depends on CLI plus store behavior, filesystem behavior, config
      loading, migrations, multiple modules, or process/supervisor boundaries.
- [ ] A unit test would miss the contract because the bug risk lives in wiring,
      serialization, subprocess/env handling, paths, locks, or persisted state.
- [ ] A reviewer or lead needs executed integration evidence for a concrete ref,
      or production integration tests must be added/changed.

## Contract
- [ ] Prefer real boundaries where deterministic and cheap.
- [ ] Use isolated temp roots, temp config/home paths, and scoped fixtures.
- [ ] Do not fake the boundary under test; fake only unrelated external cost.
- [ ] Record the exact command/result and the ref/scope it covers.
- [ ] Emit a qa-result when reporting QA evidence, or a production-handoff when
      handing off production test changes.

## Procedure
1. Choose the mode first:
   - QA/report mode: run integration checks and emit `qa-result` evidence.
   - Production test-change mode: add or modify integration tests and emit
     `production-handoff` evidence for reviewers/lead.
2. Name the boundary under test and the contract it proves. If the target is a
   pure function or low-level behavior, route to test-coverage instead.
3. Build isolation before execution: temp root/store, temp HOME/CODEX_HOME/config,
   unique ports/files, deterministic clocks, and cleanup. Never point a test at
   the operator's live store or user config.
4. Exercise the real boundary under test. Use the real CLI/store/filesystem/
   process path being validated. Stub only unrelated external services or
   expensive dependencies, and state that stub explicitly.
5. Avoid unstable user-journey tests: no unbounded sleeps, no live network/service
   dependencies, no timing assumptions without bounded polling or injected clocks.
6. Run the narrowest command that proves the boundary, then the relevant broader
   command when the blast radius justifies it. Capture exact command, result,
   environment notes, and any skipped/unavailable boundary.
7. If adding/changing tests, keep the production diff local to integration
   coverage; do not mix product fixes or refactors into the test change.
8. Produce evidence in the chosen mode: qa-result for executed assurance, or
   production-handoff for production test changes.

## Not For
- Pure-function or low-level behavior tests where test-coverage is enough.
- Full user journeys that require unstable sleeps or external services.
- Making an already-failing check green - use fix-ci.
- Product behavior changes - use craft-code.
- Behavior-preserving production refactors - use refactor-code.
- Reviewing or approving an existing diff - use review-code or the requested
  review/QA lens.

## References
- QA-result and production-handoff evidence rules (bus-validated vs skill-policy fields):
  ../_shared/references/evidence.md
- Routing precedence and negative triggers: ../_shared/references/routing.md

## Evidence

Emit one of the declared profiles:

- Default QA/report mode: emit the `qa-result` profile.
- Production test-change mode: emit the `production-handoff` profile using the
  canonical field list in ../_shared/references/evidence.md. The formal stub
  below is intentionally only `qa-result` because current skill-currency parses
  one in-skill stub per skill.

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
