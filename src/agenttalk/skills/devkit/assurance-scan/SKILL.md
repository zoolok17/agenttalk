---
name: assurance-scan
description: >-
  Produce codebase-adaptive assurance scan evidence for release gates, milestone
  closes, security reviews, and release readiness. Use when Codex needs to
  detect a repo stack, run applicable installed quality, security, dependency,
  packaging, encoding, and hygiene checks, emit a normalized JSON artifact, or
  feed ASSURANCE.md. This skill is an evidence producer, not a gate approver.
  Do NOT use to decide GO/HOLD, approve a release, auto-install scanners, or run
  network scans by default.
reviewed-against: "0.61"
category: assurance
evidence-profile:
  - qa-result
---

# assurance-scan

Produce scan evidence only. This skill never approves a release, never decides
GO/HOLD, never auto-installs tools, and never enables network checks by default.
The gate, close, release-readiness, or human reviewer consumes the artifact and
owns policy.

Run the package module:

```text
python -m agenttalk.assurance --root . --profile change --out .agenttalk/assurance/runs
python -m agenttalk.assurance --root . --profile release --out .agenttalk/assurance/runs --baseline .agenttalk/assurance/baseline.json
```

Profiles:

- `change`: changed-range evidence, universal hygiene, Python checks, and
  installed applicable scanners.
- `release`: change checks plus packaging, non-editable install smoke, and
  generated executable artifact evidence.
- `deep`: a broader evidence profile for explicit manual use.

Missing optional tools are recorded as `skipped-not-installed` plus residual
risk. Missing required tools are recorded as gate-visible evidence in the
artifact. Network-dependent tools are `skipped-network-disabled` unless the
manifest explicitly permits them for that tool.

Warning:

the scan is a cheap uniform FLOOR — the worst real bugs we shipped/nearly-shipped ($args binding, dir-vs-file mtime ordering, the lane-approval bypass chain) were caught by EXECUTED tests + adversarial review, NOT by any scanner. ASSURANCE.md must NEVER read scanned == assured.

References:

- `references/tool-matrix.md`
- `references/manifest-schema.md`
- `references/artifact-schema.md`
- `references/baseline-policy.md`

## Evidence

Emit the `qa-result` profile (full rules + bus-validated vs skill-policy:
../_shared/references/evidence.md).

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
