---
name: test-docs
description: >-
  Produce executable documentation-check evidence by running docs examples,
  doctests, link checks, command snippets, generated-reference drift checks, and
  code-vs-doc assertions. Use when documentation needs QA evidence for commands
  or generated content actually executed. Do NOT use for writing docs (use
  write-docs), adversarial documentation accuracy, audience, or prose review
  (use review-docs), integrated code review, or green claims based only on
  reading documentation.
reviewed-against: "0.51"
category: assurance
evidence-profile:
  - qa-result
---

# test-docs

Docs QA is executable evidence for documentation behavior. It complements
review-docs, which remains the adversarial review lens for accuracy, audience,
prose, and completeness. Keep tests referenced and tests executed separate.

## When To Use
- [ ] A doc change includes commands, snippets, generated references, links, or
      examples that can be executed or mechanically checked.
- [ ] A close needs documentation QA evidence rather than a prose review.
- [ ] A command, flag, generated reference, doctest, link, or code-vs-doc
      assertion may have drifted.

## Contract
- [ ] Run applicable docs examples, doctests, command snippets, link checks,
      generated-reference drift checks, and code-vs-doc assertions.
- [ ] Use isolated temp directories and explicit environment setup for commands
      that write files, configs, or stores.
- [ ] Separate `tests_referenced` from `tests_executed`. Referenced means
      inspected only; executed means actually run with observed result.
- [ ] Report exact commands, outputs, artifacts, skipped checks, environment
      gaps, and residual risk.
- [ ] If a documented command cannot be safely run, state why and return
      `status=needs-info` or include the gap in residual risk. Do not mark it
      executed.

## Procedure
1. Pin the ref and changed documentation scope. Identify every executable or
   mechanically checkable claim in that scope.
2. Classify checks: examples and command snippets, doctests, link checks,
   generated-reference drift, code-vs-doc assertions, screenshots or artifacts,
   and any intentionally non-runnable fragments.
3. Build safe execution isolation for docs commands: temp root, temp HOME or
   config, fake services where acceptable, and no live destructive targets.
4. Run the narrowest checks that prove the documented behavior, then broader
   docs gates if the project has them.
5. Compare observed output with documented output or generated references. Flag
   stale flags, missing setup steps, broken links, changed defaults, or generated
   drift as executable docs failures.
6. Emit `qa-result` evidence with tests referenced versus tests executed
   separated. Use `status=rejected` for broken executable docs; use
   `status=needs-info` when a command needs credentials, live services, or an
   operator-approved environment.

## Not For
- Writing or updating documentation. Use write-docs.
- Adversarial documentation review for accuracy, audience, prose, mode, or
  completeness. Use review-docs; it may cite test-docs evidence when executable
  checks are needed.
- General code review or implementation. Use review-code or craft-code.
- Green documentation claims based only on inspection.

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
