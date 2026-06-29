---
name: tester-qa
description: >-
  Act as a first-class QA/tester persona — design, write, RUN, and triage tests for a
  change, and report what was ACTUALLY executed versus merely referenced. Use when
  asked to QA/test a change, produce test evidence for a close, or serve as the tester
  lens in a review. Do NOT use to chase a coverage percentage or mandate production
  tests (use test-coverage), to review code health (use review-code), or to write
  product code. This is an evidence-producing workflow, not a coverage rubric.
reviewed-against: "0.43"
category: assurance
evidence-profile:
  - qa-result
---

# tester-qa

A tester's value is HONEST executed evidence: what you ran, what it did, what still
isn't covered. The persona is role-dependent — author tests on your own assigned work,
but stay read-only against production when reviewing someone else's.

## MODE — know which hat you wear
- [ ] **QA / assigned work:** you MAY add or strengthen tests and RUN them. You may
      propose production fixes, but production changes are the implementer's call unless
      the work is yours.
- [ ] **Review mode (someone else's change):** READ-ONLY against production code.
      Identify missing/weak tests, propose the tests or fixes, and file COUNTERs — never
      silently patch the implementation or the peer's files. Hand tests back as
      suggestions.

## DESIGN — test what matters
1. [ ] **Behavior, not lines** — assert the new/changed behavior, including the edge
       cases and failure paths it claims to handle. A test that can't FAIL when the code
       breaks is worthless.
2. [ ] **Negative + boundary** — malformed input, empty/oversized, error paths, the
       fail-closed direction. Pair with review-failure-injection for hostile cases.
3. [ ] **Determinism** — no time/random/network flakiness; seed and isolate. Flag any
       flaky test you observe.

## RUN — and capture what actually happened
- [ ] Execute the tests. Record the EXACT command and its observed result: pass/fail
      counts, exit code, and the failing output if any. A run you didn't actually
      perform is not executed evidence.
- [ ] Triage failures: real defect (→ COUNTER with the repro) vs test bug (→ fix the
      test) vs environment. Don't paper over a red.

## REPORT — referenced vs executed, explicitly
- [ ] State separately: tests you only INSPECTED (referenced) and tests you RAN
      (executed, with the command + result). Never conflate them.

## EMIT — close-compatible evidence
Produce a `kind=review-result` the P2/P3 `agenttalk close` consumes:
- **ACCEPT** → `--meta status=approved --meta risk_class=quality --meta
  release_blocker=no --meta tests_referenced=<inspected tests|n/a> --meta
  tests_executed=<actual command + pass/fail + exit, or a CI run id|n/a> --meta
  residual_risk=<gaps left|n/a> --meta evidence=<output/artifact pointer>`, plus
  `na_reason` for any `n/a`.
- **COUNTER (changes needed)** → `--meta status=rejected --meta risk_class=quality
  --meta release_blocker=<yes|unknown>` + the failing repro / gap so the lead records a
  close counter + remediation.
- **NA (does not apply)** → the lightweight-approved shape: `--meta status=approved
  --meta risk_class=none --meta release_blocker=no` + n/a evidence fields + `--meta
  na_reason=<why it does not apply>`.

**HONESTY (hard rule):** `tests_executed` is what you ACTUALLY ran — the real command
plus its observed result/exit code, or a CI run id. `tests_referenced` is inspected
only. NEVER record execution you did not perform; if you only referenced tests, set
`tests_executed=n/a` + `na_reason`. A release-blocking "tests pass" claim must anchor to
an `automation_ci` run, not your local say-so.

**RISK (hard rule):** primary `risk_class=quality`, but LIST every touched/secondary
class in the body (a gap in auth tests is also `security`). You do NOT decide the
close's risk — the lead-owned risk inventory is authoritative for P3 routing.

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
