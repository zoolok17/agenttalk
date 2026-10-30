# Development gate reference

Audience: agenttalk contributors and CI integrators who need SHA-bound evidence for a candidate change.

`agenttalk dev-gate` is the single voting command for repository tests, packaging checks, and CLI-runnable
security checks. It has no skip flags. A missing interpreter, tool, result, or evidence field blocks the run.

## Which changes run which CI checks

For contributors: a documentation-only pull request no longer waits for the
full test matrix. It still runs the documentation wording and gate-reference
tests, a full-history secret scan, workflow security checks, CodeQL, and the
client-reference tripwire. A failed check stays a failure.

The lighter path starts with `README.md`, `CHANGELOG.md`, `SECURITY.md`, and
Markdown files under `docs/`, then excludes document names mentioned anywhere
in the Python test sources. Tests use documents as inputs, so a Markdown-only
change can still break a test. README-only and CHANGELOG-only changes therefore
run the full matrix, as do the supervisor guides and other tested documents.
Even a mention in a comment or a fixture with the same name excludes a document;
this deliberately favors extra checks over skipping a test that needs to run.

A guard during pytest, including collection and fixtures, records Python file
opens under the repository. It reports a collection or test failure if a test opens
a document still eligible for the lighter path, including with parallel workers.
This catches computed document names that the source scan misses; name such a document
literally in the test before adding that dependency. The scan is refreshed from
the test sources on each CI run, so it needs no separate list of protected guides.

The guard sees only Python file opens in the pytest processes. It cannot observe
reads by external programs or subprocesses; those tests must name their document
inputs literally so the source scan protects them. It can report a missed dependency
only when the test that reads it runs. The full matrix exercises the whole suite;
the lighter job cannot discover a computed read in a test it skips. A new dependency
there is caught on a full run, not pre-emptively on a documentation-only PR.
When testing an unpacked source archive without `scripts/ci_scope.py`, the guard
prints a warning and stands down; the selected tests still run.

Files named `SKILL.md` or inside a `skills` directory are excluded even under
`docs/`. Executable examples, bundled skills, source, tests, workflows, packaging
changes, mixed code/documentation changes, and empty diffs run the full matrix.

The scope job compares the PR's merge base to its head using the full Git
history. It checks every changed path, including both sides of renames, without
an API file-count limit. It runs the base commit's classifier, not the PR's copy.
On the initial rollout, when the base has no classifier yet, it runs the full
matrix. If classification itself fails, the workflow fails rather than claiming
a documentation-only pass.

The workflow itself has no path filter. The `dev-gate aggregate` job always
reports a result and requires the selected checks to succeed. On the lighter
path it says **Documentation checks passed; full release gate not run.** It
does not create release-gate evidence. As checked on 2026-10-08, master branch
protection requires `client-reference-tripwire`; that job still runs for every
PR. Neither the trial nor its aggregate is a required check.

Code PRs, pushes to master, scheduled runs, and manual runs retain all 12
dev-gate legs and their static/security checks. CodeQL's `security-extended`
analysis and the tripwire remain in the separate security workflow. The local
full dev-gate command and all check deadlines are unchanged.

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
| `recorder-forgets-binding` | `test_account_isolation[recorded-events]`: the recorded events lose their account and cannot be published | `test_seeded_fault_is_detected[recorder-forgets-binding]` |
| `omit-provider-from-account` | `test_account_isolation[providers-same-home]`: two providers sharing a folder collapse into one account | `test_seeded_fault_is_detected[omit-provider-from-account]` |
| `ignore-read-local-binding` | `test_account_isolation[direct-foreign-stream]`: the direct reader accepts another account's stream | `test_seeded_fault_is_detected[ignore-read-local-binding]` |
| Bypass `current_view` | `test_expiry_at_every_consumer`: `cli-text`, `status-row`, `attention-budget`, `attention-context`, `attention-refusal`, `web` | `test_seeded_freshness_bypass_is_detected[<consumer>]`, one case for each named consumer |
| Bypass checkpoint `for_publication` | `test_expiry_at_every_consumer`: `checkpoint-file`, `checkpoint-sidecar` | `test_seeded_freshness_bypass_is_detected[<consumer>]`, one case for each checkpoint source |
| CLI display uses stored figures without checking age | `test_expiry_at_every_consumer[cli-command]`: `capacity show` prints expired figures | `test_seeded_freshness_bypass_is_detected[cli-command]` |
| Web display uses the publication time instead of the current time | `test_expiry_at_every_consumer[web-route]`: `/api/state` returns expired figures | `test_seeded_freshness_bypass_is_detected[web-route]` |

The account file also has one strict expected failure for the open checkpoint gap (#408).
`test_checkpoint_for_another_seat_never_borrows_callers_context` fails today because it
receives the caller's conversation fill. When the separate fix lands, its unexpected
pass fails the suite: remove the marker and keep the regression test.

These are deterministic synthetic checks. They need no provider credentials, gateway,
external service or real model session. The HTTP case starts its own loopback server on
an assigned temporary port, then closes it and joins its thread. Directory-link cases use a native junction on
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

The trial applies only to Windows, Python 3.10 through 3.13, on pushes to
master. Neither its eight mode jobs nor its aggregate runs on PRs, scheduled
runs, or manual runs. This leaves more runner capacity for PR checks while
continuing to gather measurements after merges. The existing full
`dev-gate aggregate` remains the release decision on master. The trial still
consumes extra runner slots there. Queueing may erase the saving predicted
from running the suites in parallel, so a shorter suite alone is not success.

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

## Windows worker preparation experiment

Contributors preparing the Windows worker audit use a separate entry point and
`dev-gate-experiment.json`. The ordinary gate, its Windows serial rule, and the
source/wheel trial above stay unchanged. Experiment evidence has type
`agenttalk-windows-workers-preparation-v1` and always has `authoritative: false`;
neither the gate nor the trial accepts it as voting evidence.

This first step defines the contract and the adapter interfaces. Production
execution refuses to start until the real P7 frozen installer and P3 journal
adapter are connected. The tests supply fake installers and fixture inventories;
the command line has no option to select them. No probe job or acceptance run is
enabled by this change.

The installed entry point requires an explicit checkout, even under isolated
Python. To inspect the available options after installing the candidate:

```powershell
python -I -m agenttalk.dev_gate_experiment accept --help
```

Once the dependencies exist, an acceptance invocation has this shape (replace
each angle-bracket value with a real path or frozen input):

```text
python -I -m agenttalk.dev_gate_experiment accept --checkout <checkout> --arm serial --leg windows/3.12 --modes source,wheel --record <frozen.json> --output <fresh-output>
```

Probe invocations use the separate `run` subcommand and `--probe p8|p8-gateway-repeat|p9|p10|p11`
instead of `accept` and `--arm`. The closed contract maps each probe to reserved
plugin arguments; those plugins belong to later probe work. Acceptance rejects
probe plugins or observer files, and missing plugin inventories fail closed.
The POSIX parallel setting must be absent, including an empty inherited value.
P11 uses the serial arm; the gateway repeat probe uses the parallel arm. P9 alone
accepts `--cases worker`, `--cases controller` or `--cases worker,controller`.

The frozen JSON input contains `commit`, `contract_sha256`, and `environments`.
Each environment is keyed by Python minor, such as `3.12`, and contains its
`freeze_sha256`. The contract digest uses UTF-8 canonical JSON with sorted keys
and fixed separators, so checkout line endings do not change it. Before stage 1,
freeze all four minors' environment plans and the evidence method. The installed
candidate must be a noneditable local install: `direct_url.json` names the
resolved checkout URL and contains a noneditable `dir_info` object. Its package
RECORD hashes and installed bytes must match the checkout's source files, including
the installed entry point. The commit comes from the clean checkout binding;
the installed copy also exposes its version and dist-info RECORD fingerprint. A tested wheel's
installation is a separate installer proof.

The allowed differences are D1 (frozen installs for host, build, runtime and
wheel-test roles), D2 (held gateway ports are an error), D3 (passive journals),
and D4 (the parallel arguments, only in the parallel arm). Test paths, ordered
checks, time limits, and other command arguments must agree with the gate.
Windows pytest calls retain the manifest's 9,000-second limit. Comparison folds
Windows environment-variable names and replaces each call's own run-root prefix;
it does not erase other differences. A bounded fixture run checks runtime skips
and setup/cleanup separately from collection identities and marks.
Credentialed proxy URLs are refused because exact environment evidence must not
disclose their credentials; the refusal does not echo the supplied URL.
Until P1 lands, D2 is checked only by the installer's proof.
Before execution, the call is compared with an independent call built from the
ordinary gate's environment and temporary-folder layout (`pt-s-py312` or
`pt-w-py312` for Python 3.12), plus the declared experiment arguments.

P3's `tests/experiment_record.py` defines the `--experiment-record` option;
`tests/conftest.py` registers it through its `pytest_addoption` hook. Pytest loads conftest files even with
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`; that setting disables automatic discovery of
installed plugins. Preflight requires both files, then runs an isolated `pytest --help` child
in the checkout with autoload disabled and requires the option in its help
output before either arm can start. The child names `tests` explicitly so pytest
loads that folder's conftest without collecting or running test bodies.

P7 implements `Installer.prepare(request, mode, workdir)` and `finish(prepared)`.
Preparation returns a `PreparedEnvironment`: interpreter, committed source and
import roots, installed-set proof (`freeze_sha256`, empty `probe_files`, and
`gateway_policy: error`), ordered setup steps, and passing records for the mode's
non-pytest checks. P3 supplies `Recorder.inventory(journal_dir)`, returning
`complete: true` and a nonempty `plugins` list only after every process's inventory
is present. Its adapter maps the documented journal fields; it cannot invent
missing observations. The real dependency factory is the only production path.
Wheel preparation also requires a `wheel_sha256` in the installed-set proof,
separate from the installed entry point's RECORD hash.

Output must be fresh, outside the checkout and every message store. Like the
ordinary gate, output retains pytest's temporary folder, exports and logs; it
also retains `experiment.json` and journals. The record states that no basetemp
removal took place. Missing input files, dirty or changed candidates,
different contract hashes, incomplete proofs, and extra command arguments refuse
the run. The local tests cover these boundaries; the full two-worker local run
and the later hosted probes are separate evidence.
Execution failures record their type and message in the partial artifact and end
with exit code 1; preflight refusals use exit code 2. Cleanup failures are recorded
separately so they cannot hide the original execution failure.
