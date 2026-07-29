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

When a configured coverage command yields a fresh CI-attested measurement, each profile emits
exactly one blocker gate with the matching producer identity: `coverage:change`,
`coverage:release`, or `coverage:deep`. A close scope is a separate policy axis; a `dod.json`
coverage requirement must explicitly select one of those three gate names instead of deriving a
gate from the close scope. This lets `feature`, `milestone`, `hotfix`, and custom close scopes
choose the scan depth they require without creating a gate no scan can emit.

For example, a feature close can require release-depth coverage evidence:

```json
{
  "schema_version": 1,
  "scopes": {
    "feature": {
      "coverage": {
        "gate": "coverage:release",
        "min_percent": 80,
        "max_age_days": 14
      }
    }
  }
}
```

Fractional `min_percent` values are decoded exactly and normalized only at or
above the configured floor; policy conversion never lowers the threshold
toward passing.

Missing optional tools are recorded as `skipped-not-installed` plus residual
risk. Missing required tools are recorded as gate-visible evidence in the
artifact. Network-dependent tools are `skipped-network-disabled` unless the
manifest explicitly permits them for that tool.

Generated artifact `kind` values are a closed enum. Non-executable generated
artifacts must use `kind: "other"` or omit `kind`; executable extensions such as
`.py`, `.ps1`, `.sh`, and `.js` are treated as executable at release regardless
of `kind`.

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
