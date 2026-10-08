# Development gate reference

Audience: agenttalk contributors and CI integrators who need SHA-bound evidence for a candidate change.

`agenttalk dev-gate` is the single voting command for repository tests, packaging checks, and CLI-runnable
security checks. It has no skip flags. A missing interpreter, tool, result, or evidence field blocks the run.

## Early regression feedback

Changes in recurring failure areas also follow the [danger-area rule](DANGER-AREAS.md).
The focused checks below provide early feedback; they do not replace this gate.

## Permanent capacity regression checks

Run one file at a time with the candidate's `src` on `PYTHONPATH`, `AGENTTALK_ROOT`
unset, and a fresh `--basetemp` outside the worktree under your owned scratch directory.
The following command templates use `<scratch>` for that directory:

```text
python -m pytest tests/test_capacity_account_class.py -q --basetemp <scratch>/capacity-account
python -m pytest tests/test_capacity_expiry_class.py -q --basetemp <scratch>/capacity-expiry
```

Both files include healthy, deliberately broken, and restored checks in the same test.
To rerun only the fault proofs, add `-k seeded` to either command. A fault proof passes
only if the normal behavior assertion fails with its expected reason while the fault
is present. An unrelated exception fails the proof. Production files stay unchanged.

| Named fault | Normal check that turns red | Permanent fault proof |
| --- | --- | --- |
| `omit-home-from-account` | `test_account_isolation[relocated-claude]`: separate homes collapse into one account | `test_seeded_fault_is_detected[omit-home-from-account]` |
| `read-callers-statusline` | `test_account_isolation[manual-other-saved]`: the caller's percentage replaces the target's | `test_seeded_fault_is_detected[read-callers-statusline]` |
| `borrow-callers-thread` | `test_account_isolation[manual-codex-no-thread]`: an unidentified seat borrows the caller's conversation | `test_seeded_fault_is_detected[borrow-callers-thread]` |
| `linked-shared-is-private` | `test_account_isolation[codex-linked-shared]`: a shared home reached through a link admits a reading without a thread | `test_seeded_fault_is_detected[linked-shared-is-private]` |
| `ignore-explicit-binding` | `test_account_isolation[manual-explicit-mismatch]`: a file from a different account is admitted | `test_seeded_fault_is_detected[ignore-explicit-binding]` |
| Bypass `current_view` | `test_expiry_at_every_consumer`: `cli-text`, `status-row`, `attention-budget`, `attention-context`, `attention-refusal`, `web` | `test_seeded_freshness_bypass_is_detected[<consumer>]`, one case for each named consumer |
| Bypass checkpoint `for_publication` | `test_expiry_at_every_consumer`: `checkpoint-file`, `checkpoint-sidecar` | `test_seeded_freshness_bypass_is_detected[<consumer>]`, one case for each checkpoint source |

These are deterministic synthetic checks. They need no provider credentials, gateway,
network service or real model session. Directory-link cases use a native junction on
Windows and a directory symlink elsewhere. Cleanup removes only the link entry.

## Prerequisites

Run the command from a clean Git worktree. The local profile invokes both direct interpreters, so provision the
gate dependencies in **both** CPython 3.10 and CPython 3.14 (a CI leg needs them only in that leg's interpreter):

```text
python3.10 -I -m pip install -r dev-gate-requirements.txt
python3.14 -I -m pip install -r dev-gate-requirements.txt
```

Install `gitleaks` on `PATH` before a local run or the canonical `linux/3.12` CI leg. The gate resolves the
binary to an absolute path, records its version, requires full Git history, and owns the scan arguments.

The local profile requires direct CPython 3.10 and 3.14 executables. Use `--python` when the executables are
not discoverable:

```text
agenttalk dev-gate --profile release \
  --python 3.10=/absolute/path/to/python3.10 \
  --python 3.14=/absolute/path/to/python3.14
```

On Windows, use the equivalent absolute `python.exe` paths. A version bump must be committed before the gate
runs so the candidate SHA and the tested package version describe the same revision.

## Command surface

```text
agenttalk dev-gate [--profile release]
                   [--ci-leg OS/PYTHON | --aggregate DIRECTORY]
                   [--evidence ABSOLUTE_PATH]
                   [--temp-root ABSOLUTE_DIRECTORY]
                   [--python MINOR=ABSOLUTE_EXE ...]
```

- With neither `--ci-leg` nor `--aggregate`, the command runs the local fast precheck on Python 3.10 and 3.14.
  Its artifact has `complete: true` for the local scope, but it is not the authoritative CI matrix decision.
- `--ci-leg` accepts one declared matrix member: Linux, Windows, or macOS on Python 3.10 through 3.13. A leg
  artifact always has `complete: false`; it cannot claim a full gate pass by itself.
- `--aggregate` reads leg artifacts recursively, rejects malformed, missing, duplicate, stale, or mixed-bound
  evidence, and compares the common SHA/tree/manifest binding with the current clean checkout. Only the exact
  12-leg set can produce `complete: true`.
- `--evidence` and `--temp-root` must resolve outside both the candidate worktree and `AGENTTALK_ROOT`. Defaults
  use the system temporary directory. Pytest basetemps are short children of that external run directory.
- The same rule applies when running `pytest` directly (not via `agenttalk dev-gate`), e.g. for a targeted
  `tests/test_comprehension_*.py` pass: pass `--basetemp` pointing OUTSIDE any Git worktree, never a path nested
  inside one. Several comprehension-plane privacy tests require a genuine "no real Git repository present"
  precondition (they assert refusal/behavior specific to the absence of `.git`) - a basetemp placed inside a
  worktree puts every test's own temp directory underneath a real repository root instead, breaking that
  precondition and producing false-red failures unrelated to the change under test.

The dozen tests that bind or call the paid gateway's real ports (127.0.0.1:4000 and 4001) are opt-in, so a
machine that runs the live gateway never reaches it by accident. They are skipped unless
`AGENTTALK_TEST_GATEWAY_PORTS=1` is set, and even then they are skipped if either port is already occupied
when tests are collected (when pytest first collects the tests - its one pass over the test files, before
any test runs); that check binds a separate socket and sends nothing. It runs only once, at collection, so a
gateway started later in the run is not noticed. CI sets the variable on every dev-gate leg,
and the gate passes it to its pytest runs only as the exact value `1`. To run them yourself on a machine with
no gateway running, set the variable, for example `AGENTTALK_TEST_GATEWAY_PORTS=1 python -m pytest
tests/test_ovh_gateway_service.py`, and keep the gateway stopped until the run ends. On a machine that runs
the live gateway, leave it running and do not set the variable: the tests skip themselves, and CI covers them. A new test that touches those ports belongs in `_GATEWAY_PORT_TEST_NAMES` in
`tests/conftest.py`.

Exit status `0` means the requested scope passed. Status `1` means a complete execution produced blocking
check evidence. Status `2` means a preflight, schema, binding, or invocation error blocked execution; when an
external evidence path can be established, the command still writes a normalized
`agenttalk-dev-gate-preflight-block` artifact with `complete: false` and the stable blocker code.

## Committed plan

[`dev-gate.json`](../dev-gate.json) is the strict plan. The command reads its committed `HEAD` blob, not an
uncommitted working-tree copy, and records both its Git blob ID and SHA-256 digest. The runner also records a
logical plan digest, the candidate commit/tree, and the committed runner blob ID/digest. Before executing the plan,
the CLI re-enters a temporary committed Git export through isolated Python, so index flags and candidate-root
module shadows cannot make mutable checkout code masquerade as the attested runner. Every Python-backed tool is
resolved before the candidate import root is exposed. [`dev-gate-requirements.txt`](../dev-gate-requirements.txt) provisions tools only; it
does not own check selection or argv.

Every local run checks:

- full pytest in source mode and built-wheel mode on Python 3.10 and 3.14;
- one sdist and one wheel built without build isolation;
- sdist exclusion sentinels and required shipped files;
- dependency-resolving wheel installation in fresh `system_site_packages=False` runtime and test environments,
  using copied venv launchers and disabled pip configuration/cache, followed by `pip check`, package-version
  provenance, and byte-equal console CSS/JavaScript;
- Ruff, Bandit, full-history gitleaks with a Git-only child `PATH`, pip-audit over the frozen dependency snapshot
  without pip re-resolution, Semgrep, and zizmor;
- clean and stable Git binding before and after execution.

Every CI leg runs the source/wheel, packaging, and binding checks for its one interpreter. The canonical
`linux/3.12` leg additionally runs Ruff, Bandit, gitleaks, pip-audit, Semgrep, and zizmor. Two checks are
declared CI-native exceptions, each for a stated reason rather than left to be discovered: CodeQL, because
GitHub owns its analysis runtime, and the client-reference tripwire (`.github/workflows/security.yml`),
because its pre-commit and CI halves must run with different fail-open/fail-closed semantics (task #216) -
a distinction this plan's single-command, identical-everywhere model cannot express.

Wheel dependency/test-tool resolution, Semgrep registry rules, and the PyPI advisory database are live external
inputs. Their locators, observation times, and explicitly unversioned identities appear in `external_inputs`;
the evidence never presents them as content-addressed inputs.

## Evidence contract

Run artifacts use `artifact_type: agenttalk-dev-gate-run`. They contain exact required check IDs, commands,
resolved tool paths and versions, exit codes, durations, log hashes and tails, isolated-venv creator/prefix
proofs, pip-configuration and child-`PATH` isolation assertions, import provenance, package
artifact hashes, isolation assertions, blockers, and recomputed summary counts. The writer validates the full
schema before and after a normalized durable JSON write.

Aggregate artifacts use `artifact_type: agenttalk-dev-gate-aggregate`. Each leg entry binds the raw input
artifact SHA-256. A passing aggregate proves that all declared legs share the same candidate SHA, tree,
version, manifest blob/digest, and logical plan digest and that the current checkout still matches them.

CI uploads every leg artifact even when its gate command blocks. The always-run aggregate converts a crashed
or evidence-less leg into an incomplete blocking artifact instead of silently reducing the matrix.

To investigate a red lane, download its `dev-gate-leg-<os>-<python>` artifact and open
`dev-gate-evidence.json`. For each failing check, follow `checks[].log.artifact_path`
to `logs/<check-id>.log` in the same artifact. This file contains the complete combined
stdout/stderr, including pytest's captured product output and output written before a
timeout; `diagnostic` is only a 2,000-character summary. The original absolute runner
`log.path` and `log.sha256` remain provenance fields; the collected file must match that
hash at collection and again when the downloaded bundle is read for aggregation;
missing or changed logs block the aggregate. Older JSON artifacts may lack the relative link.

Each collected check log is limited to 16 MiB. Larger logs block with
`check_log_size_exceeded`, rather than silently omitting assertion output. Collection
reads at most the limit plus one byte and does not copy an oversized log; command
completion also marks oversized output as an error. This is an evidence acceptance
limit, not a live subprocess disk quota: raw runner output can exceed it before the
command completes or times out. No truncated log is accepted as complete evidence.

### Per-test durations and time limits

Every pytest run logs how long each test took, so slow tests can be found from an
ordinary CI run. The committed arguments add `--durations=0 --durations-min=0` to
`-q -rs`. pytest then prints a `slowest durations` section, one line per setup, call
and teardown of every test, slowest first, for example `12.34s call     tests/test_x.py::test_y`.

The section sits in the same `logs/<check-id>.log`, before the skip reasons and the final
result line, so the 2,000-character `diagnostic` still ends with that result line. For
about 10,500 tests it adds roughly 3.5 MiB to a log that is otherwise about 20 KB, well
under the 16 MiB limit.

A run stopped by its time limit never reaches that section. Its log ends with
`agenttalk dev-gate: timed out after <N>s`, then
`agenttalk dev-gate: per-test durations unavailable - pytest was stopped before it printed them`.
Treat such a run's durations as unknown, never as zero.

Each pytest run (source and wheel) may take up to `checks.pytest.timeout_seconds`, which
is 7,200 seconds on Linux and macOS. On Windows the limit is `windows_timeout_seconds`,
9,000 seconds: a temporary margin, recorded with its reason in `windows_timeout_reason`
(#378, expires 2026-11-06). A CI leg only runs on its declared OS, so the leg's OS
chooses the limit.

The CI job ceilings in `.github/workflows/tests.yml` follow from these limits:
- **Windows: 330 minutes.** Two pytest runs of up to 150 minutes each, plus about 30
  minutes for setup and the other checks.
- **Linux and macOS: 90 minutes.**

# Windows source/wheel trial (stage 1)

For contributors and operators measuring CI time: the extra Windows jobs run the
same checks on two separate machines for each Python version. They run alongside
the existing gate. They cannot authorize a release, and the local full dev-gate
command behaves as before. Changing which checks authorize release is a separate
decision after repeated runs of the same code establish both equivalence and a
material reduction in total waiting time.

The trial applies only to Windows, Python 3.10 through 3.13. The existing
`dev-gate aggregate` remains the required check; its dependencies and inputs are
unchanged. The extra jobs do consume more runner slots while this experiment is
active. Queueing may erase the saving predicted from running the suites in
parallel, so a shorter suite alone is not success.

| Existing work | Trial location | Proof retained |
| --- | --- | --- |
| Candidate, tree, manifest and cleanliness before/after execution | Both modes | Each machine independently checks the same candidate |
| Source pytest suite | Source mode | Complete suite, source import provenance and existing command arguments |
| Wheel and source archive construction | Wheel mode | Package checks, both package files and their hashes |
| Wheel installation into a clean runtime environment | Wheel mode | Exact built-wheel path, dependency check and runtime import/resource contract |
| Installed-package pytest suite | Wheel mode | Complete suite in a separate test environment, test import provenance, and the exact wheel installation command and digest |
| Static/security checks and non-Windows suites | Existing gate | No reassignment; the canonical static leg remains Linux/3.12 |

The partition is computed from the committed manifest. Only the candidate
binding checks appear in both modes; every other Windows check belongs to
exactly one. Nothing runs in parallel inside either Windows pytest process.
All check deadlines stay the same. The temporary Windows pytest allowance is
still 9,000 seconds per suite and still expires on 2026-11-06. The existing
330-minute Windows job ceiling and 20-minute aggregation ceiling remain in use.

## Partial evidence cannot vote

The internal CI runner is `python -I -m agenttalk.dev_gate_trial`. It has `run`
and `aggregate` actions; the workflow supplies its context. It is not a new
option on the public `agenttalk dev-gate` command.

Each mode writes an ordinary full-plan `partial.json` with the other mode's
checks explicitly blocked. Its verdict must be `block`, and `complete` must be
false. A separate `trial.json` records which partition finished, the candidate
commit and tree, manifest digest, repository, workflow, run, attempt, OS/Python
leg and mode. It hashes the collected evidence files. A mode can finish its own
work successfully but cannot report that the full leg passed.

The collector requires exactly one source artifact and one wheel artifact for
every declared Windows/Python pair. It obtains jobs from the exact attempt API,
requires every corresponding job to have succeeded, and enumerates artifacts
before downloading anything. It downloads by artifact ID and verifies the
downloaded archive against GitHub's SHA-256 digest. It then verifies the inner
file hashes and the existing gate's strict command, import, runtime and binding
proofs. The packaged wheel bytes must match both the build record and the wheel
installed into the test environment. Missing, duplicate, cancelled, mixed or
corrupt evidence blocks the trial.

The result is `trial-complete` with `authoritative: false`, never `pass` or GO.
It is a distinct artifact type that the existing release aggregator does not
accept. The required gate does not download the trial artifacts.

## Retries and measurements

Artifact names contain the workflow run and attempt as well as the leg and
mode. A retry must rerun **all trial modes in one attempt**. A failed-jobs-only
retry or an aggregate-only retry cannot borrow successful artifacts from the
previous attempt: it blocks for missing current-attempt results. Old artifacts
remain available for comparison, but are never substituted. A reused output
directory is refused rather than merged or overwritten.

The comparison bundle retains the raw run, attempt, job and artifact metadata,
verified downloaded archives and their IDs/digests. Its per-mode rows record
queue delay from attempt start, setup before the mode command, pytest duration,
whole-job duration and finish time. It records collector elapsed time and the
existing Windows jobs with their step timings. The trial collector waits for
the existing legs so their comparison data is available; that deliberate wait
must not be mistaken for the split's intrinsic aggregation cost.

Do not claim a speedup from this implementation alone. Compare repeated
same-code runs, including queue/setup cost and the full workflow's finish time.
Stop the experiment if either mode can claim GO alone, retry provenance becomes
ambiguous, a check/deadline weakens, or queue/setup overhead erases the gain.
Cutover needs its own reviewed change. There is no user-facing changelog entry
for this measurement-only stage.
