---
name: review-failure-injection
description: >-
  Adversarially review a change for how it behaves when things go WRONG — malformed
  input, IO/network errors, partial writes, resource exhaustion, and teardown. Use
  when reviewing parsers, file/IO, persistence, untrusted input, long-running or
  resource-holding code, or any error/failure path. Do NOT use for general code
  health (use review-code), documentation (use review-docs), contract/rename parity
  (use review-contract-drift), or release packaging (use review-release-readiness).
reviewed-against: "0.42"
category: assurance
evidence-profile:
  - review-result
---

# review-failure-injection

Ordinary review asks "does the happy path work?". This asks "what happens when it
DOESN'T?". Inject failures mentally (and with a repro where you can) and confirm the
code degrades safely. Ground every finding in the actual code; never approve a
failure path you only assumed.

## INJECT — the failure catalogue
Walk each that the change touches; quote the exact lines for any finding.
1. [ ] **Malformed / hostile input** — truncated, oversized, wrong-type, wrong-encoding,
       injection, deeply nested, empty, duplicate keys. Is it rejected, not crashed?
2. [ ] **IO / network failures** — file missing, permission denied, disk full, partial
       read/write, timeout, connection drop mid-stream, retry storms. Errors handled,
       not swallowed?
3. [ ] **Persistence integrity** — a crash mid-write must not corrupt state. Atomic
       write / temp-then-rename? Reload after a torn write fail CLOSED, not silently?
4. [ ] **Resource limits** — unbounded buffers/queues/caches/recursion; leaked handles,
       file descriptors, connections, locks across BOTH success AND error paths.
5. [ ] **Concurrency** — races, double-consume, lost updates, deadlock if two callers
       hit the same path; reentrancy on retry.
6. [ ] **Cleanup / teardown** — does `finally` / context-manager / defer actually run on
       every exit, including early return and exception? Is partial work rolled back?
7. [ ] **Fail direction** — when uncertain, does it fail CLOSED (deny/HOLD) rather than
       open (allow/GO)? A false success is worse than a clean error.

## VERIFY — adversarially
- [ ] For each claim, read the surrounding/unchanged-but-affected code, confirm the
      symbols/APIs exist, and run a minimal repro or the test that exercises the path.
      No finding rests on "this is probably handled".

## REPORT
- [ ] Severity-tag each finding `[blocker]/[major]/[minor]/[nit]` with file:line and the
      exact failure that triggers it. A data-loss or fail-open path is `[blocker]`.

## EMIT — close-compatible evidence
Produce a `kind=review-result` the P2/P3 `agenttalk close` consumes:
- **ACCEPT** → `--meta status=approved --meta risk_class=<primary> --meta
  release_blocker=yes|no|unknown --meta tests_referenced=<…|n/a> --meta
  tests_executed=<actual command + result/exit, or a CI run id|n/a> --meta
  residual_risk=<…|n/a> --meta evidence=<artifact/pointer>`, plus `--meta
  na_reason=<why>` for any `n/a` field.
- **COUNTER (changes needed)** → `--meta status=rejected --meta risk_class=<primary>
  --meta release_blocker=<yes|unknown>` + evidence/artifacts + a concrete findings list
  so the lead can record a close counter + remediation.
- **NA (lens does not apply)** → the lightweight-approved shape: `--meta status=approved
  --meta risk_class=none --meta release_blocker=no` + n/a evidence fields + `--meta
  na_reason=<why it does not apply>`.

Pick the **primary** `risk_class` by the worst failure mode found —
`persistence` (corruption/torn write), `security` (hostile input / fail-open),
`performance` (resource exhaustion), else `quality`.

**HONESTY (hard rule):** `tests_executed` is what you ACTUALLY ran (the real command
+ its observed result/exit code, or a CI run id); `tests_referenced` is what you only
inspected. NEVER record execution you did not perform — if you only referenced, set
`tests_executed=n/a` + `na_reason`. Anything **release-blocking** must anchor to an
`automation_ci` gate, not self-report.

**RISK (hard rule):** choose ONE primary `risk_class` for the validator, but LIST every
touched/secondary risk class in the body. You do NOT decide the close's risk — the
lead-owned risk inventory is authoritative for P3 routing; your `risk_class` is an input.

## Evidence

Emit the `review-result` profile (full rules + bus-validated vs skill-policy: ../_shared/references/evidence.md).

Required fields:

- `risk_class`
- `release_blocker`
- `tests_referenced`
- `tests_executed`
- `residual_risk`
- `evidence`
- `status`
- `reviewed_ref`
- `scope`
