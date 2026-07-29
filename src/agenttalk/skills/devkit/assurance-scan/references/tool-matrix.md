# assurance-scan tool matrix

Audience: agents and reviewers invoking `python -m agenttalk.assurance`.

The scanner does not mutate project source or take custody of coverage report
paths. It does write explicit AgentTalk lock/generation and gate state plus
requested assurance artifacts. It uses stdlib built-ins where possible and
runs external tools only when they are installed and applicable. It invokes
subprocesses with argv lists, never shell strings.

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

The clean-worktree probe does not exempt the `.agenttalk/` namespace as a
whole. It recognizes only complete paths in the following untracked
AgentTalk-created class:

- `agenttalk init` bootstrap state:
  `.agenttalk/config.json`, its `config.json.<8-character>` atomic-write
  sibling, `.agenttalk/state/<name>.cursor`, and its
  `<name>.cursor.<8-character>` atomic-write sibling, where `<name>` passes
  canonical `validate_agent_name()` validation and each `<8-character>`
  component matches `[a-z0-9_]{8}`; this is a pathname grammar, not proof that
  the name belongs to the current roster or that AgentTalk authored the path;
- `.agenttalk/config.lock` and its
  `.config.lock.<generation>.prepare`,
  `.config.lock.<generation>.unlink`,
  `..config.lock.<generation>.prepare.<unlink-generation>.unlink`, and
  persistent `.config.lock.generation` siblings, where each generation is 32
  lowercase hexadecimal characters (`[0-9a-f]{32}`);
- `.agenttalk/assurance/coverage.lock` and its
  `.coverage.lock.<generation>.prepare`,
  `.coverage.lock.<generation>.unlink`,
  `..coverage.lock.<generation>.prepare.<unlink-generation>.unlink`, and
  persistent `.coverage.lock.generation` siblings;
- `.agenttalk/assurance/coverage-handoff.lock` and its
  `.coverage-handoff.lock.<generation>.prepare`,
  `.coverage-handoff.lock.<generation>.unlink`,
  `..coverage-handoff.lock.<generation>.prepare.<unlink-generation>.unlink`,
  and persistent `.coverage-handoff.lock.generation` siblings;
- `.agenttalk/gates.json` and its `gates.json.<8-character>` atomic-write
  sibling; and
- default assurance output
  `.agenttalk/assurance/runs/<UTC-run-id>/artifact.json`, `summary.md`, and
  `raw/<safe-tool-id>.txt`, where `<UTC-run-id>` has the exact
  `YYYYMMDDTHHMMSS.ffffffZ` shape and `<safe-tool-id>` is a fixed point of the
  producer's `_safe_id()` sanitizer and matches `[A-Za-z0-9_.-]+`.

`agenttalk init` does not modify an adopter's `.gitignore`, so these exact
bootstrap and scanner outputs must not self-invalidate a no-ignore adopter.
This is an exact pathname-and-object-shape classification, not a claim that
authorship can be proved after the fact: every intermediate runtime component
must be a plain, non-reparse directory and every recognized leaf must be a
regular, non-reparse file under the actual plain `.agenttalk` directory.
An arbitrary neighbor such as `.agenttalk/operator-note.txt`, a similarly
prefixed path, a custom run output outside the exact default-output grammar, or
other live bus state is not in the exemption and remains dirty when Git reports
it. Any actor that creates an object matching the exact grammar is the
pathname-collision residual described below.

Existing selected assurance manifests and baselines are controlling inputs and
must be tracked repository files even when an ignore rule hides them from
`git status`. Their exact loaded bytes are retained for the later attestation;
the same lexical path and filesystem identity must still contain those bytes,
and their content after normal Git clean filters must hash to the selected index
blob. Thus CRLF normalization remains supported while case aliases, hardlinked
alternate pathnames, later restore-to-clean tricks, and
`skip-worktree`/`assume-unchanged` status hiding cannot bypass the rule. A
selected path outside the selected assurance root (and therefore a nested
root's boundary), a tracked-but-missing selected path, or any selected
non-regular/reparse object fails closed; only an absent untracked input selects
the built-in default. The actual `.agenttalk` runtime root and every existing
intermediate runtime component must be a plain directory, not a symlink,
junction/reparse point, or other filesystem object.

No tracked path is exempt. A worktree, index, deletion, or other modification
to any tracked AgentTalk path is repository dirt, including exact persistent
lock/generation/gate names, transient siblings, run outputs, policies, and
similarly prefixed operator files. More generally, any tracked path hidden
from normal status by `skip-worktree` or `assume-unchanged` makes the
attestation fail closed. A selected manifest/baseline is the sole exception:
its exact loaded bytes, lexical path and filesystem identity, clean-filtered
content, and stage-zero index blob are independently proved. For nested assurance roots,
repo-root-relative Git paths are rebased before runtime classification, while a
dirty sibling elsewhere in the repository remains dirty. Any other Git status
entry makes coverage evidence ineligible. Untracked files hidden by an ignore
rule remain outside Git's cleanliness signal unless they are a selected
manifest or baseline; force-track such a file when revision binding is
required. Root `coverage.xml`, `coverage.json`, legacy recovery residue, and
arbitrary configured-command outputs are not part of any exemption.

Custom `--out` and `--summary` destinations outside the exact default-output
grammar are ordinary dirty paths when Git reports them. Authorship cannot be
proved after the fact: output from a configured command, an operator, or a
concurrent external writer that deliberately has the exact default run-id and
leaf shape is observationally identical to scanner output and therefore
inherits the exemption. This pathname-collision false-GREEN is an explicit
residual because there is no output receipt proving authorship. Any untracked
destination inside an ignored `.agenttalk/` also inherits Git's existing
invisibility; tracked changes are reported and remain dirty. Ignore, commit,
clean, or move custom outputs before attesting. Use the default
`.agenttalk/assurance/runs` destination and default summary name for recognized
runtime-only output.

Each coverage holder first acquires the coverage transaction lock, crosses a
coverage-only handoff, and stages a unique provisional red unless an active,
valid waiver must be preserved. After a successful command it briefly acquires
the config lock, advancing and recording the current-client acquisition token
in `.config.lock.generation`, then runs Git revision/worktree probes while the
coverage lock still excludes a second coverage command and without holding the
global config lock. Finalization takes the handoff lock again, releases the
coverage lock, and briefly reacquires the config lock. If the prior acquisition
token still matches, it applies the provisional-gate compare-and-swap. A
current-client config transaction that interposed forces a bounded re-probe
outside the config lock; repeated churn fails closed red. Ordinary concurrent
activity from any `config.lock` caller can exhaust the two-re-probe budget and
produce an avoidable red; re-run the scan before concluding that coverage
regressed. A newly admitted coverage holder may acquire the transaction lock
but cannot cross its initial handoff or start its command until the prior
finalization completes.
Gate, waiver, roster, and configuration operations can contend during the
brief fence or commit transaction, but do not wait on coverage subprocesses,
Git probes, or coverage-lock release. Release-failure fallback uses the same
provisional-gate CAS.

The acquisition-token fence requires a homogeneous current AgentTalk client. A
legacy client that obeys `config.lock` but does not advance the token is not
detected. The coverage handoff likewise orders only current coverage producers:
a legacy or mixed-version producer that honors `coverage.lock` but does not
cross `coverage-handoff.lock` can enter after coverage-lock release and before
the current holder's final CAS. This producer is not yet released, so there is
no released migration, but branch-local mixed binaries remain a named
residual. Neither the config nor coverage lock serializes arbitrary repository
writers, so evidence remains a point-in-time attestation and a mutation after
the last probe is the existing #66/#31 residual for a later verifier to detect.
Existing and newly created lock and output directory components are validated
as plain, non-reparse directories. Assurance output uses fresh run/raw
directories and creates every leaf exclusively without following links. A
hostile writer that replaces a validated parent before the following
filesystem operation remains a check/use residual; portable pathname APIs do
not provide directory-handle-relative isolation here.

Accepted coverage stdout is built-in text, or direct UTF-8 byte input with an
optional BOM, no larger than 16 MiB at the parse boundary. Bounded SGR
decoration is removed before the following complete-line grammar is applied:

| Input class | Structural requirement | Disposition |
| --- | --- | --- |
| coverage.py statement-only terminal row | One LF/CRLF-delimited line with optional horizontal indentation, literal `TOTAL`, ASCII unsigned integers `Stmts=S` and `Miss=M`, then an ASCII integer/dot-decimal `actual` percentage as the final field. The parser requires `M <= S` and requires `actual` to equal coverage.py's configured-precision rendering of `(S-M)/S` (`S=0` renders `100`). | Handled |
| coverage.py branch terminal row | The same complete-line form with four captured counts: `Stmts=S`, `Miss=M`, `Branch=B`, and `BrPart=P`, then `actual`. The parser requires `M <= S`, `P <= B`, `P > 0` only when at least one statement executed, a producer-possible branch shape, and a displayed percentage possible for some hidden total `H` of missing branch arcs: `100 * ((S-M)+(B-H))/(S+B)`. `BrPart` is only missing arcs whose source statement ran, not `H`. Each branch source contributes at least two arcs, so hidden misses beyond `P` require a distinct unexecuted source and add either zero (`no branch`) or at least two arcs; the parser checks only those aggregate-feasible ranges. Both endpoints are real: `no branch` arcs can make `H` smaller than the branch structure suggests, while a condition that raises after its statement executes can make `BrPart == H == Branch`. This preserves genuine rows while rejecting percentages outside every possible count relationship. | Handled conservatively |
| pytest-cov native fail-under summary | One LF/CRLF-delimited line with optional horizontal indentation: success `Required test coverage of <required>% reached. Total coverage: <actual>%` or failure `FAIL Required test coverage of <required>% not reached. Total coverage: <actual>%`. `required` must be an ASCII integer/dot decimal, optionally with a decimal exponent of at most four digits, whose parsed float is in `(0, 100]`; `actual` must be a two-decimal ASCII value in `[0, 100]`; and both are captured. Because pytest-cov compares its unrounded float total but displays `actual` with `.2f`, success is checked against the `.2f` rendering of that parsed requirement, and failure against the rendering of its immediate float predecessor. This preserves genuine rounded boundary output while rejecting relationships impossible under Python's ties-to-even formatting; an impossible final line does not expose earlier evidence. Only horizontal whitespace may follow. | Handled conservatively |
| Legacy/custom bare pytest-cov summary | A complete `Total coverage: <actual>%` line with optional horizontal indentation and only horizontal whitespace after the percentage. This form captures one number, so it has no second numeric field or cross-field consistency relation to validate; the percentage must still be finite and in `[0, 100]`. | Handled |
| Incidental percentage prose | Text such as `application log says Total coverage: 99% but no report` or `TOTAL deployment success 99%` does not have the required complete-line/numeric-column structure. | Refused |
| Percentage precision | The displayed token is parsed as an exact decimal. Its persisted float never serializes to a decimal above that token; conversion steps down when nearest-float conversion would round upward. This prevents agenttalk from adding an upward round, but cannot undo producer display quantization. Coverage.py consistency uses its actual configured-precision ties-to-even rendering, including its rule that only exact zero or exact 100 displays those endpoints; pytest-cov's two-decimal display can exceed its unrounded total by at most `0.005` percentage points. | Handled conservatively; producer quantization is residual |
| Coverage process outcome | Success requires exit zero without timeout, spawn, or stdout-decoding failure. Generic scanner-shaped JSON in stdout is not interpreted as coverage findings; nonzero/failed execution remains red even with a valid summary. | Handled |

SGR sequences may contain at most 32 digit, semicolon, or colon parameter
characters. The final structurally recognized summary across all forms wins.

The parser refuses bare-carriage-return progress rewrites, unsupported or
overlong escape sequences, locale-comma decimals, malformed or out-of-range
percentages, and direct byte input that is not UTF-8 with an optional BOM.
Scientific notation is accepted only for the native pytest-cov requirement and
only with a decimal exponent of at most four digits. It remains outside every
actual-coverage token and the legacy/custom grammar.
Stdout and stderr are captured separately, so stderr-only summaries and their
ordering relative to stdout are not evidence. Coverage stdout is captured as
bytes to preserve bare CR versus CRLF, then decoded as strict UTF-8 with an
optional BOM; undecodable output records a red tool result without aborting the
scan. Scanner-shaped JSON in coverage stdout remains raw diagnostic text and
does not enter the generic finding parser. Agenttalk cannot identify an inner
failed sub-run when a wrapper suppresses its status and exits zero. Such wrappers
must propagate failures and print the intended aggregate summary last.

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
proof for the file-symlink exclusion branches on Windows remains unconfirmed.
The two canonical-report cases, the `.agenttalk` runtime-alias case, and the
config-lock marker case skip locally with `WinError 1314`; selected policy-file
and pre-existing output-leaf symlink refusal also have no executed Windows
proof, and CI does not explicitly grant symlink-creation privilege. Windows
junction/reparse-directory refusal is exercised separately. Stdout evidence is
limited to 16 MiB when parsed,
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
