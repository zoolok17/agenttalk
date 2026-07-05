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
- `fixed`: baseline fingerprint is absent from the current scan and the finding's
  originating tool ran with executed evidence.
- `accepted-applied`: current finding matched an unexpired manifest acceptance.
- `accepted-expired`: current finding matched an expired acceptance.

Default blocking is new or worsened findings at or above the configured severity
floor. Existing unchanged findings do not block scanner adoption by default.

Manifest or baseline changes in the scanned range are surfaced in provenance and
the verdict summary as self-waiver risk evidence.

Acceptance scope is not free-form prose. The accepted finding's `scope` must
match the finding path or a parent path, and `dimension` must match when supplied.
A mismatch leaves the original finding unsuppressed and emits
`accepted-scope-mismatch`.

When a legitimate manifest or baseline edit is part of the same scan range, keep
the self-waiver evidence visible. Reviewers should either accept the distinct
manifest/baseline-changed finding with a short-lived owner/reason or rebaseline
after the configuration change is reviewed. The scanner will not silently treat a
new untracked baseline as clean.

Fingerprints include normalized message text, so upstream tool message churn can
make a known issue appear new after a tool upgrade. Treat that as a review event:
confirm equivalence, then update the baseline with the new fingerprint.
