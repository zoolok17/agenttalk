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
- coverage only when configured.
- bandit, semgrep local rules, gitleaks, osv-scanner, and pip-audit when
  installed and applicable. Network-dependent dependency tools are skipped by
  default unless the manifest permits them.

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
