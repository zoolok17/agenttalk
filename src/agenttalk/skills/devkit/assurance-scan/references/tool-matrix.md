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
- coverage only when configured. A recognized coverage.py/pytest-cov terminal
  summary on stdout is the sole evidence channel; root JSON and XML reports are
  not parsed. Before the command runs, the scanner refuses if either canonical
  root path, `coverage.xml` or `coverage.json`, exists as any filesystem object.
  If postflight observes either path, it refuses the evidence without reading
  its contents, moving it, or removing it. Legacy recovery residue also causes
  refusal and names its root and backup paths for manual recovery; the scanner
  never auto-restores them. Configured commands are not filesystem-isolated and
  may create or modify paths while running; process containment or an
  owned-output protocol remains #107. A refusal produces a red automated result
  unless the persisted gate is an active, valid operator waiver, which automated
  scans preserve. Fresh CI-attested evidence updates `coverage:<profile>` unless
  such a waiver is active; a close policy must explicitly select that producer
  gate rather than infer one from its close scope. Expired, malformed, or
  incomplete waivers do not suppress fresh evidence and are invalidated red when
  a scan has no fresh measurement.

Accepted coverage stdout is built-in text, or direct UTF-8 byte input with an
optional BOM, no larger than 16 MiB at the parse boundary. It uses LF/CRLF and
must contain a coverage.py `TOTAL` row or pytest-cov `Total coverage:` summary
with an ASCII integer or dot-decimal percentage. SGR color sequences with at
most 32 digit, semicolon, or colon parameter characters may decorate the text.
The final recognized summary across both formats wins, and the configured
process must exit zero.

The parser refuses bare-carriage-return progress rewrites, unsupported or
overlong escape sequences, locale-comma decimals, malformed or out-of-range
percentages, and direct byte input that is not UTF-8 with an optional BOM.
Stdout and stderr are captured separately, so stderr-only summaries and their
ordering relative to stdout are not evidence. Coverage stdout is captured as
bytes to preserve bare CR versus CRLF, then decoded as strict UTF-8 with an
optional BOM; undecodable output records a red tool result without aborting the
scan. Agenttalk cannot identify an inner failed sub-run when a wrapper suppresses
its status and exits zero. Such wrappers must propagate failures and print the
intended aggregate summary last.

The canonical coverage-path class is exactly `coverage.xml` and `coverage.json`.
An arbitrary configured command can write another path, and the scanner neither
discovers, parses, nor cleans it. Case variants are outside this class on
case-sensitive filesystems.

A producer descendant can create a canonical report after postflight. The next
scan then refuses and requires manual cleanup, even if the late report came from
the prior producer. This deliberate false-DOWN remains until #107 provides an
owned-output protocol or process containment; refusing a named path is safer than
guessing ownership and deleting operator data. Any coverage command that spawns
subprocesses can plausibly trigger this on an ordinary setup; until #107, the
operator remedy is to inspect and manually clean up the named path. Executed
proof for Windows symlink/reparse refusal remains unconfirmed: the local symlink
cases skip with `WinError 1314`, and CI does not explicitly grant
symlink-creation privilege. Stdout evidence is limited to 16 MiB when parsed,
but `capture_output=True` buffers the complete subprocess stream before that
check. Capture-time bounding remains #106.
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
