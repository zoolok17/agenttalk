# assurance-scan manifest schema

Path: `.agenttalk/assurance.json`.

The manifest is JSON so Python 3.10 can parse it without optional dependencies.
Missing manifests are allowed; malformed manifests become blocking validation
findings in the artifact.

Required top-level field:

- `schema_version`: `1`.

Optional top-level fields:

- `profiles`: object keyed by `change`, `release`, or `deep`.
- `tools`: object keyed by tool id.
- `thresholds`: object for complexity, coverage, duplication, and project
  thresholds.
- `custom_commands`: object such as `test`, `coverage`, `build`, and `docs`,
  each as an argv list.
- `paths`: object with `include`, `exclude`, `generated`, and `vendor` lists.
- `accepted_findings`: list of reviewed acceptances.
- `generated_artifacts`: list of generated executable artifacts to verify.
- `monorepo.packages`: list of child package descriptors.
- `python.package` or `python.packages`: package imports to resolve for
  provenance.

Unknown top-level manifest keys are validation errors. Unknown keys inside
`profiles.<profile>` are also validation errors; the profile namespace is limited
to `required_tools`, `optional_tools`, `network_allowed`, `severity_floor`,
`include_paths`, and `exclude_paths`. Per-tool config under `tools.<tool>` is
intentionally open so tool-specific options can evolve without a schema change.

Accepted findings require:

- `fingerprint`
- `reason`
- `owner`
- `scope`
- `expires`

Blanket scopes such as `*`, `**`, `all`, and `global` are invalid. Expired
acceptances are reported as `accepted-expired` and remain gate-visible.
Scopes are enforced against the matched finding path and optional dimension; a
mismatch is reported as `accepted-scope-mismatch` and does not suppress the
finding.

Generated artifacts require `id` and `path`. `kind` is recommended and is
normalized case-insensitively, with aliases such as `ps1` mapping to
`powershell`. If `kind` is omitted, executable status is inferred from the file
extension for release-profile declared-unexecuted checks. Unknown non-empty kinds
are validation errors.

Tool commands and custom commands are argv lists. Use `timeout_seconds` under
`tools.<tool>` when a local required command, such as this repository's pytest
suite, needs longer than the default timeout.

The default manifest path and all run paths use lowercase `.json`.
