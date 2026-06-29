---
name: review-docs
description: >-
  Adversarially review documentation: verify every claim against the current code,
  run the examples, and check it serves its reader. Use when reviewing a README,
  guide, reference, or doc change, or after a code change that touched user-facing
  docs. Do NOT use for writing/updating docs (use write-docs) or reviewing code logic
  (use review-code).
reviewed-against: "0.42"
category: assurance
evidence-profile:
  - review-result
---

# review-docs

Stale docs are worse than no docs — they actively mislead. Review the docs the same
way review-code treats code: verify against reality, severity-tag findings, don't
approve on "looks fine".

## ACCURACY — highest priority
- [ ] Check every claim against the **current code**: signatures, parameters, return
      values, flags, errors, config keys, defaults, output, and version numbers must
      match what the code does **today**. Flag any drift as **BLOCKING** — a confidently
      wrong doc is worse than a missing one.
- [ ] **Run every example / command / snippet** (or confirm CI runs them) and confirm
      the documented result. Reject illustrative-but-unrun examples that "should work".

## FIT & COMPLETENESS
- [ ] **Mode check** — the page is a single clean Diataxis mode (tutorial / how-to /
      reference / explanation), not a hybrid that half-teaches and half-references.
- [ ] **Audience & goal** — clear and consistent; the content serves that reader.
- [ ] **Completeness for the stated task** — no missing step, prerequisite, edge case,
      or error path a real user would hit.
- [ ] **Clarity & scannability** — scannable structure, descriptive headings, concise
      sentences, descriptive link text, code in code font.

## DRIFT GUARDS
- [ ] No stale screenshots, paths, env-var names, command flags, or output blocks.
- [ ] No broken links or dead anchors (run / confirm the link checker).
- [ ] Changelog / navigation / API reference updated to match the change.

## REPORT
- [ ] Severity-tag findings `[blocker] / [major] / [minor] / [nit]` like review-code;
      blockers = inaccuracies and broken examples.
- [ ] Confirm the docs-as-code CI gates (markdownlint / prose linter / link check)
      actually **ran** for the changed files — do not approve on green-but-skipped checks.

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
