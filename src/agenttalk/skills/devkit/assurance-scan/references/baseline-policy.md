# assurance-scan baseline policy

Path: `.agenttalk/assurance/baseline.json`.

The baseline is a reviewed snapshot of known findings. It is not a suppression
trash bin and it is not updated automatically by the scanner.

Required fields:

- `schema_version`: `1`
- `baseline_id`
- `findings`: list of known findings with at least `fingerprint`

Recommended finding fields:

- `dimension`
- `severity`
- `tool`
- `rule_id`
- `path`
- `first_seen_commit`
- `accepted`
- `acceptance_ref`

Delta mechanics:

- `new`: current fingerprint is absent from baseline.
- `unchanged`: current fingerprint is present with same or lower severity.
- `worsened`: current fingerprint is present with higher severity.
- `fixed`: baseline fingerprint is absent from the current scan.
- `accepted-applied`: current finding matched an unexpired manifest acceptance.
- `accepted-expired`: current finding matched an expired acceptance.

Default blocking is new or worsened findings at or above the configured severity
floor. Existing unchanged findings do not block scanner adoption by default.

Manifest or baseline changes in the scanned range are surfaced in provenance and
the verdict summary as self-waiver risk evidence.
