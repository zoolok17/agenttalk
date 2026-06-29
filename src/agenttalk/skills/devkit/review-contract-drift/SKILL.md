---
name: review-contract-drift
description: >-
  Review a change that retires, renames, or alters a CONTRACT (schema, config/settings
  key, public API, serialized format, renderer/output, CLI flag) for parity across all
  the places that must move together — code, tests, docs, migrations, and callers. Use
  when a feature/field/flag is removed, renamed, defaulted differently, or its shape
  changes. Do NOT use for new-feature review (use review-code), pure failure-path review
  (use review-failure-injection), or release packaging (use review-release-readiness).
reviewed-against: "0.43"
category: assurance
evidence-profile:
  - review-result
---

# review-contract-drift

A contract change is only safe when EVERY surface that depends on it moves in the same
commit. Drift — code updated, docs/tests/migrations stale — is a confidently-wrong
artifact that misleads users and breaks callers. Hunt the surfaces that did NOT change
but should have.

## TRACE — find every dependent surface
Start from the changed contract; grep the repo for the old name/shape/flag and confirm
each hit was updated or is intentionally left.
1. [ ] **Schema / serialized format** — version bump? back/forward compat? a reader of
       the OLD format still works or fails CLOSED with a clear error?
2. [ ] **Config / settings keys** — renamed/removed keys: migration or alias? default
       change called out? stale key now ignored silently (a finding) or rejected?
3. [ ] **Public API / CLI flags / signatures** — all call sites updated; removed
       symbols not referenced; deprecation path if external callers exist.
4. [ ] **Renderers / output / fixtures** — output strings, formats, golden files, and
       snapshot tests match the new shape.
5. [ ] **Tests** — tests for the OLD contract removed/updated, not left asserting dead
       behavior; new behavior actually asserted.
6. [ ] **Docs / README / changelog / help text** — every mention of the old
       name/shape/default updated; examples still run.
7. [ ] **Persistence / on-disk state** — existing stored data under the old shape:
       migrated, tolerated, or explicitly unsupported with a clear failure.

## VERIFY — adversarially
- [ ] Re-grep for the old identifier after review; a single stale hit in docs or a
      fixture is drift. Run the documented examples / the migration if one exists.

## REPORT
- [ ] Severity-tag `[blocker]/[major]/[minor]/[nit]` with file:line. A stale
      migration, a silently-ignored renamed key, or docs that teach the dead contract
      is `[blocker]` or `[major]` — confidently-wrong beats missing.

## EMIT — close-compatible evidence
Produce a `kind=review-result` the P2/P3 `agenttalk close` consumes:
- **ACCEPT** → `--meta status=approved --meta risk_class=docs-contract --meta
  release_blocker=yes|no|unknown --meta tests_referenced=<…|n/a> --meta
  tests_executed=<actual command + result/exit, or a CI run id|n/a> --meta
  residual_risk=<…|n/a> --meta evidence=<artifact/pointer>`, plus `na_reason` for any `n/a`.
- **COUNTER (changes needed)** → `--meta status=rejected --meta risk_class=docs-contract
  --meta release_blocker=<yes|unknown>` + evidence/artifacts + a concrete drift list so
  the lead can record a close counter + remediation.
- **NA (lens does not apply)** → the lightweight-approved shape: `--meta status=approved
  --meta risk_class=none --meta release_blocker=no` + n/a evidence fields + `--meta
  na_reason=<why it does not apply>`.

Primary `risk_class` is usually `docs-contract`; use a `project:<name>` extension only
if the project defines a more exact class.

**HONESTY (hard rule):** `tests_executed` is what you ACTUALLY ran (real command +
observed result/exit, or a CI run id); `tests_referenced` is inspected-only. NEVER
fabricate execution — if only referenced, `tests_executed=n/a` + `na_reason`.
Release-blocking claims anchor to an `automation_ci` gate, not self-report.

**RISK (hard rule):** one primary `risk_class` for the validator, but LIST every
touched/secondary class in the body (a renamed auth flag is also `security`). You do
NOT decide the close's risk — the lead-owned risk inventory is authoritative for P3.

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
