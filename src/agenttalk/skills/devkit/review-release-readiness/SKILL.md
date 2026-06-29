---
name: review-release-readiness
description: >-
  Review whether a change (or a tag/release candidate) is SAFE TO RELEASE and conclude
  HOLD or GO — CI triggers, build artifacts, manifests/permissions, package type,
  version/docs/changelog drift, and barrier/gate readiness. Use before a release, tag,
  publish, deploy, or milestone close. Do NOT use for ordinary diff review (use
  review-code), failure paths (use review-failure-injection), or contract parity (use
  review-contract-drift).
reviewed-against: "0.42"
category: assurance
evidence-profile:
  - review-result
---

# review-release-readiness

A release is irreversible-ish and outward-facing. Decide HOLD or GO on evidence, not
optimism. The strongest evidence is automation: prefer a CI-sourced gate over your own
read. When unsure, HOLD.

## CHECK — the release surface
1. [ ] **CI actually ran the right thing** — the test/security workflows triggered on
       THIS revision and are green (not skipped, not stale, not a cached pass). Capture
       the run id.
2. [ ] **Version + changelog** — version bumped consistently (package metadata + any
       pinned references), changelog entry matches what shipped, no leftover `-dev`.
3. [ ] **Artifacts / package** — the right artifact type builds, includes what it should
       and EXCLUDES secrets/local/dev files; install/run from the built artifact works.
4. [ ] **Manifests / permissions** — declared permissions, capabilities, entitlements,
       and dependency pins are intentional and minimal; no surprise scope grab.
5. [ ] **Docs / README pins** — install/usage docs, version pins, and examples updated
       to the releasing version and actually run.
6. [ ] **Gates + barrier** — required assurance gates are GREEN from `automation_ci` or
       carry a valid operator waiver; if a release barrier/bump is part of the flow,
       confirm it is the deliberate release act, after GO.
7. [ ] **Rollback** — is there a way back (revert/yank/tag move) if the release is bad?

## VERIFY — adversarially
- [ ] Don't trust "CI is green" — open the run, confirm it is THIS sha and the jobs you
      care about actually executed. A green-but-skipped job is a HOLD.

## REPORT
- [ ] Conclude **GO** or **HOLD** explicitly, with the blocking reasons first. Tag each
      `[blocker]/[major]/[minor]/[nit]`. Any unverified release-blocking claim is a HOLD.

## EMIT — close-compatible evidence
Produce a `kind=review-result` the P2/P3 `agenttalk close` consumes:
- **ACCEPT (GO)** → `--meta status=approved --meta risk_class=release --meta
  release_blocker=no --meta tests_referenced=<…|n/a> --meta tests_executed=<CI run id
  / actual command + result|n/a> --meta residual_risk=<…|n/a> --meta
  evidence=<run id / artifact pointer>`, plus `na_reason` for any `n/a`.
- **COUNTER / HOLD** → `--meta status=rejected --meta risk_class=release --meta
  release_blocker=yes` + evidence + the concrete blocking list so the lead records a
  close counter/remediation and does NOT publish GO.
- **NA (does not apply)** → the lightweight-approved shape: `--meta status=approved
  --meta risk_class=none --meta release_blocker=no` + n/a evidence fields + `--meta
  na_reason=<why it does not apply>`.

**HONESTY (hard rule):** a release-readiness GO is the highest-stakes ACCEPT — its
`tests_executed`/`evidence` MUST point to a real `automation_ci` run (id/link), never a
self-reported "tests pass". `tests_referenced` is inspected-only. If you could not
confirm execution, HOLD; do not fabricate a green.

**RISK (hard rule):** primary `risk_class=release`, but LIST every touched/secondary
class (a release touching auth is also `security`). You do NOT decide the close's risk —
the lead-owned risk inventory is authoritative for P3 routing.
