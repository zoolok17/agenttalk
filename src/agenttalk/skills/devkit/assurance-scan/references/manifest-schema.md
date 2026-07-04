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

Accepted findings require:

- `fingerprint`
- `reason`
- `owner`
- `scope`
- `expires`

Blanket scopes such as `*`, `**`, `all`, and `global` are invalid. Expired
acceptances are reported as `accepted-expired` and remain gate-visible.

The default manifest path and all run paths use lowercase `.json`.
