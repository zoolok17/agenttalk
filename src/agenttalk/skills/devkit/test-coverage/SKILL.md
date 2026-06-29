---
name: test-coverage
description: >-
  Cover every applicable behavior with deterministic, behavior-focused tests,
  including a failing-first regression test for any bug fix. Use when writing or
  strengthening tests, right after implementing/fixing code, or when asked whether
  a change is "fully tested". Do NOT use to chase a coverage percentage as a goal,
  or to write production code (use craft-code).
reviewed-against: "0.42"
category: assurance
evidence-profile:
  - qa-result
---

# test-coverage

"Covered by all applicable tests" means **a test would fail if the behavior were
wrong** — not a line-coverage number. Test behavior through the public contract,
keep tests deterministic, and prove the failure modes, not just the happy path.

## ENUMERATE — before writing tests
- [ ] List the behaviors to cover: the **happy path**; every **boundary**
      (empty / one / many, min / max, off-by-one, zero / negative, overflow);
      every **error/failure path** (invalid input, dependency failure/timeout,
      partial failure); and **concurrency/ordering** if relevant. Confirm a test
      exists for each.
- [ ] **Bug fix → failing-first (non-negotiable):** write a regression test that
      FAILS on the current buggy code and PASSES after the fix, shipped in the same
      change. No fix without a reproducing test.

## WRITE — one behavior per test
- [ ] Test **observable behavior via the public API/contract** — one test per
      behavior, not per method. Reject tests that assert private internals, exact
      mock call sequences, or log strings (change-detector smell — they break on
      refactors and prove nothing).
- [ ] Structure **Arrange-Act-Assert**: one action, one logical assertion. Name the
      test `scenario_expectedBehavior` (e.g. `withdraw_moreThanBalance_raisesInsufficientFunds`)
      so the failure message alone localizes the defect.
- [ ] **Determinism gate:** no real wall-clock `now()`, no unseeded randomness, no
      `sleep()`-based waiting (poll a condition), no dependence on test order or shared
      mutable/global state. When the filesystem IS the behavior under test (file-backed
      stores, serializers, CLI/config writers), use an ISOLATED temp dir — never a
      shared/global path. Avoid real network/DB unless the test level genuinely needs
      it; otherwise inject clock / RNG / IO via fakes. Each test is self-contained and
      parallel-safe.
- [ ] **Minimize mocking:** use real collaborators or hand-written in-memory fakes by
      default; double only slow / nondeterministic / external dependencies. Never mock
      a type you don't own — wrap it and fake the wrapper.
- [ ] Reach for **property-based tests** when an invariant exists: round-trip
      (`decode(encode(x)) == x`), idempotence, an oracle/model, or a contract
      postcondition. Log the seed; enable shrinking.
- [ ] Pick the **lowest test level** that gives real confidence; keep the suite fast
      and mostly low-level with a thin integration/E2E layer. Treat types/lint/static
      analysis as the base layer.
- [ ] Test code is production code: no copy-paste blocks where a parameterized table
      fits, no logic in assertions.

## VERIFY — coverage as a gap report, then gate
- [ ] Use coverage to find **untested branches**, then add tests for the missing
      behavior. Flag assertion-free or trivially-passing tests. For critical logic,
      consider mutation testing (PIT/Stryker/mutmut) and address surviving mutants.
- [ ] AFTER (gate): run the new/changed tests; confirm they execute and pass, and
      where cheap confirm they **FAIL when the behavior is deliberately broken** — a
      test that can never fail is not coverage. Use the project's test command from
      `AGENTS.md`/`CLAUDE.md`.
