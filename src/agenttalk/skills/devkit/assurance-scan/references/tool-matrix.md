# assurance-scan tool matrix

Audience: agents and reviewers invoking `python -m agenttalk.assurance`.

The scanner is a read-only evidence producer. It uses stdlib built-ins where
possible and runs external tools only when they are installed and applicable.
It invokes subprocesses with argv lists, never shell strings.

Universal built-ins:

- provenance: git revision, dirty state, changed files, manifest and baseline
  hashes, and resolved package paths.
- manifest and baseline validation.
- encoding hygiene: NUL bytes, unexpected control bytes, BOM drift, mixed EOL,
  and `git diff --check`.
- generated artifact inventory from the manifest.

Python v1:

- syntax compile equivalent for all discovered Python files, without writing
  bytecode.
- ruff check and ruff format when configured or required.
- test command from the manifest, or the safe inferred pytest command.
- mypy or pyright only when configured or required.
- coverage only when configured. Evidence is accepted from a bounded root
  `coverage.json` (`totals.percent_covered`) or a recognized coverage.py/pytest-cov
  terminal summary. XML is not parsed; conventional `coverage.xml` and
  `coverage.json` paths are still protected for the command transaction. Fresh
  CI-attested evidence updates `coverage:<profile>`; a close policy must explicitly
  select that producer gate rather than infer one from its close scope.
- bandit, semgrep local rules, gitleaks, osv-scanner, and pip-audit when
  installed and applicable. Network-dependent dependency tools are skipped by
  default unless the manifest permits them.

Semgrep configs must be local when network is disabled. Registry configs such as
`p/ci` and URL configs are treated as network-required and therefore skipped
unless the active profile explicitly allows network for semgrep.

The inferred test command uses the default per-tool timeout. For repositories
whose normal test suite exceeds that budget, configure
`tools.tests.timeout_seconds` in `.agenttalk/assurance.json` so GOOD can be
assessed from executed evidence instead of a visible timeout/unknown result.

Release profile:

- `python -m build` when available.
- `twine check` when available.
- non-editable install smoke and import smoke when distribution artifacts exist.
- declared generated executable artifact coverage.

Other languages:

- JS/TS, Go, and Rust are detected and represented in the plan.
- v1 does not claim complete assurance for these stacks. Missing or stubbed rows
  create residual risk instead of false pass evidence.
- Rust SECURE is never satisfied by basic quality checks alone.
