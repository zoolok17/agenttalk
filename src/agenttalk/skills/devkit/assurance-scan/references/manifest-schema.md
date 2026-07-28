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

A custom `coverage` command must print a recognized coverage.py or pytest-cov
terminal summary on stdout. Stdout is the sole coverage-evidence channel; root
JSON and XML reports are not parsed. The parser rejects stdout evidence over
16 MiB, but `capture_output=True` buffers the complete subprocess stream before
that parse-time check; capture-time bounding remains #106.

Before executing the command, the scanner refuses when either canonical root
path, `coverage.xml` or `coverage.json`, exists as any filesystem object,
including a regular file, symlink, or directory. It names every conflicting path
and does not run the command. If postflight observes either path after the
command, it refuses the evidence without reading the observed object's contents,
moving it, or removing it. A refusal produces a red automated result unless the
persisted gate is an active operator waiver, which automated scans preserve.
Legacy recovery residue likewise causes refusal and names its root and backup
paths for manual recovery; agenttalk never treats legacy marker contents as
authorship, restores a backup, or removes a report. Configured commands are not
filesystem-isolated: they may create or modify paths while running. Process
containment or an owned-output protocol remains #107.

The recognized path class is exactly `coverage.xml` and `coverage.json`. An
arbitrary custom command can write another path, and agenttalk neither discovers,
parses, nor cleans it. Case variants are outside the class on case-sensitive
filesystems. A producer descendant can also create a canonical path after
postflight, making a later scan refuse until an operator removes it. This
deliberate false-DOWN remains until #107 provides owned-output containment and is
preferred to guessing ownership and deleting operator data. Any coverage command
that spawns subprocesses can plausibly trigger this on an ordinary setup; until
#107, the operator remedy is to inspect and manually clean up the named path.
Executed proof for Windows symlink/reparse refusal remains unconfirmed: the local
symlink cases skip with `WinError 1314`, and CI does not explicitly grant
symlink-creation privilege.

Absent an active persisted operator waiver, fresh coverage evidence is published
under the scan profile's finite producer gate: `coverage:change`,
`coverage:release`, or `coverage:deep`. In the separate `dod.json` close policy,
`coverage.gate` is required and must name one of those gates. It is not derived
from the close scope.

Example `.agenttalk/dod.json`:

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

Unknown top-level manifest keys are validation errors. Unknown keys inside
`profiles.<profile>` are also validation errors; the profile namespace is limited
to `required_tools`, `network_allowed`, and `severity_floor`. Use top-level
`paths.include` and `paths.exclude` for scan scope. Per-tool config under
`tools.<tool>` is intentionally open so tool-specific options can evolve without
a schema change.

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

Generated artifacts require `id` and `path`. `kind` is a closed enum: executable
kinds are `binary`, `js`, `powershell`, `python`, and `shell`, normalized
case-insensitively with aliases such as `ps1` mapping to `powershell`.
Non-executable generated artifacts must use `kind: "other"` or omit `kind`.
Unknown non-empty kinds, including descriptive values such as `json`, `yaml`,
`data`, `config`, `html`, or `text`, are validation errors.

If `kind` is omitted, executable status is inferred from the file extension for
release-profile declared-unexecuted checks. Executable extensions such as `.py`,
`.ps1`, `.sh`, and `.js` are treated as executable regardless of `kind`.

Tool commands and custom commands are argv lists. Use `timeout_seconds` under
`tools.<tool>` when a local required command, such as this repository's pytest
suite, needs longer than the default timeout.

The default manifest path and all run paths use lowercase `.json`.
