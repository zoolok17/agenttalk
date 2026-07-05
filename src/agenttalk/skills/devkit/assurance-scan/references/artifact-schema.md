# assurance-scan artifact schema

Path: `.agenttalk/assurance/runs/<run_id>/artifact.json`.

The artifact is the single source of truth for the scan. The runner exits 0
when this JSON is produced, even if it contains blocking findings. Consumers own
GO/HOLD policy.

Required top-level fields:

- `schema_version`: `1`
- `artifact_type`: `assurance-scan-run`
- `run_id`
- `generated_at`
- `profile`: `change`, `release`, or `deep`
- `root`
- `scanner`
- `provenance`
- `detection`
- `tools`
- `findings`
- `accepted_findings`
- `native_suppressions`
- `attestation`
- `verdict_summary`
- `residual_risk`

Volatile fields for diffing two artifacts are `run_id`, `generated_at`,
`root`, `scanner.runner_path`, and tool `duration_ms` values. Normalize those
fields before byte-level artifact comparisons across machines or runs.

`native_suppressions` is reserved for scanner-native suppressions. v1 emits the
block with `count_by_tool` and `examples`, but does not yet populate it.

Tool execution statuses are exactly:

- `pass`
- `fail-blocking`
- `fail-advisory`
- `skipped-not-installed`
- `skipped-not-applicable`
- `skipped-network-disabled`
- `error-required-tool`
- `error-optional-tool`
- `timeout-required`
- `timeout-optional`

Finding delta statuses are:

- `new`
- `unchanged`
- `worsened`
- `fixed`
- `accepted-applied`
- `accepted-expired`

Attestation values for `GOOD`, `ROBUST`, and `SECURE` are `good`, `unknown`,
or `not_assessed`. A required skipped scan yields `unknown`, never `good`.
