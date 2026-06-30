---
name: test-security
description: >-
  Produce executable security-testing and abuse-case evidence for a touched
  surface. Use when a change needs threat-model-backed tests or checks for
  authz, input validation, injection, path/env/command handling, unsafe
  deserialization, secrets/log hygiene, dependency or supply-chain exposure, or
  abuse cases. Do NOT use as a second review-code security pass, for general
  code review, for speculative vulnerability hunting without scoped tests, or
  for production fixes without an assigned implementation task.
reviewed-against: "0.52"
category: assurance
evidence-profile:
  - qa-result
---

# test-security

Security testing proves concrete abuse paths were exercised. Keep it distinct
from review-code: review-code owns integrated diff review and security design
critique; this skill owns executable tests, tool runs, fixtures, skipped checks,
and residual risk.

## Hard Safety Boundary

This skill is only for authorized, defensive, in-repo testing of this project.
Do not test external targets. Do not run network attacks, denial-of-service
activity, exploit development beyond this repository's test surface, or
detection-evasion techniques. If the requested work falls outside this boundary,
stop and escalate to the operator before taking action.

## When To Use
- [ ] The change touches authorization, authentication-adjacent behavior, input
      parsing, filesystem paths, environment variables, command execution,
      serialization, secrets, logs, dependencies, or sandbox boundaries.
- [ ] A reviewer, lead, or close needs executable abuse-case evidence for a
      pinned ref.
- [ ] Security behavior must be added to or verified by tests, fixtures, static
      checks, dependency checks, or command-level probes.

## Contract
- [ ] Consume an existing threat model for the touched surface, or create a
      minimal one before choosing tests.
- [ ] State assets, trust boundaries, attacker capabilities, entry points, and
      expected fail-closed behavior.
- [ ] Run or add tests for applicable risks: authz bypass, input validation,
      injection, path traversal, env/command handling, unsafe deserialization,
      secrets/log hygiene, dependency and supply-chain checks, and abuse cases.
- [ ] Record exact commands, tool output, fixtures, skipped checks, false
      positives, and residual risk.
- [ ] Keep the result executable and evidence-backed. A prose-only security
      opinion belongs in review-code, not here.

## Procedure
1. Pin the ref and scope. Identify the touched surface and whether you are
   testing your own assigned work or reviewing someone else's change.
2. Read or write the minimum threat model needed to select tests: assets, trust
   boundaries, attacker inputs, privileged operations, and fail-closed
   expectations.
3. Select applicable test families. Prefer existing project security tests and
   fixtures; add or strengthen tests only when you own the implementation scope.
   In review mode, stay read-only and report the missing executable evidence.
   If the test would reach outside this repository or require network attack,
   denial-of-service, exploit-development, or detection-evasion activity, stop
   and escalate to the operator.
4. Run deterministic in-repo checks. Examples include unit or integration abuse
   cases, CLI probes with hostile input against local fixtures, dependency or
   supply-chain scans available in the project, and targeted static checks.
   Operator approval can cover only the in-repo test environment. It is never a
   license for external targets, network or denial-of-service activity, exploit
   development beyond this repository's test surface, or detection-evasion
   techniques; those are out of bounds, so stop and escalate.
5. Triage findings as real defect, test bug, false positive, environment issue,
   or unresolved. Include enough output for a reviewer to reproduce the result.
6. Emit `qa-result` evidence. Use `status=rejected` for a proven exploitable
   path or missing required security test; use `status=needs-info` when the
   threat model or authorization contract is unknown.

## Not For
- Integrated code review or security design critique. Use review-code and its
  security reference.
- Cosmetic changes that do not touch security-relevant input, state, process,
  dependency, sandbox, or secret surfaces.
- External targets, network attacks, denial-of-service activity, exploit
  development beyond this repository's test surface, or detection-evasion
  techniques.
- Rewriting production code unless that implementation work is explicitly yours.
- Planning all release QA coverage. Use qa-strategy.
- Reporting a green result from inspection only.

## References
- QA-result evidence rules: ../_shared/references/evidence.md
- Routing precedence and negative triggers: ../_shared/references/routing.md
- Integrated security review reference for review-code:
  ../review-code/references/security.md

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
