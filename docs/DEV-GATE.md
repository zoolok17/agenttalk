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
| CLI display uses stored figures without checking age | `test_expiry_at_every_consumer[cli-command]`: `capacity show` prints expired figures as current (an expired figure is shown only with its age and a stale label, #596) | `test_seeded_freshness_bypass_is_detected[cli-command]` |
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
when the run starts. That question is answered once per run, by the process that owns the run: a serial run
answers at collection (when the first of these tests is collected, before any test runs), and under
`pytest-xdist` the controller answers once at configure, before any worker starts, and tells the workers. That
answer is about the controller's own machine, so it is used only for local workers: `-n N`, or a `--tx` that execnet
itself would start on this machine, which means `popen` is set and `via` is empty, in any field order, such as
`N*popen`, `id=x//popen` or `popen//ssh_config=x` (execnet takes `via` first, then `popen` over `ssh=`, `socket=` and
the like). With any other route (`--tx ssh=...`, `socket=...`, any `via=`, or one that cannot be read) the tests are
refused, with a message that names the transport, and an evidence run stops. (That early answer is wider than the
recorder's idea of a distributed run: the evidence recorder calls a run a controller with workers only where xdist
itself starts workers, after its own option handling: not under `--collect-only`, with a distribution other than `no`
and at least one transport. `--tx 2*popen --dist load` and `-n 2` are controllers with workers; `--tx 2*popen` without
`--dist` runs serially in the one process and is recorded so.) A nested
pytest session (one started from inside a running test) never probes the ports, so its tests are skipped; that
holds only while the session inherits `PYTEST_CURRENT_TEST` (the dev gate builds its pytest environment from an
allow-list that does not pass it on, so a gate run started from inside a test looks like an ordinary run). The
check binds a separate socket and sends nothing. It cannot see every live listener: on Windows a bind to
127.0.0.1 succeeds beside a listener on the IPv4 wildcard or a dual-stack `::`, so "not held" is not proof that nothing
listens, and the strict policy's refusal below inherits that limit (stop other gateways first). With `--experiment-record` (the experiment's strict policy), a
held port ends the run at configure with exit status `2`, before any test is collected, instead of skipping.

After every one of these tests that ran, a teardown check looks for a child process the test started and left
running, and for a listener still holding one of the two ports. A leftover child is ended; a listener held in the
test's own process is reported, not closed. Either one fails that test's teardown. A test that was skipped, or
stopped before its body ran (a skip in setup, or `xfail(run=False)`), is not probed for ports, but a child it left
is still ended and counted; a test marked xfail whose body did run keeps the full check. At the end of the run, if
any test left something, the exit status becomes `1` (unless it is already worse) and the output names each test and
what it left on a line that starts with "gateway port guard", even if the test was marked xfail.

One limit of that end-of-run rejection: when only the guard fails the run (for example an xfail test absorbed the
failure, or a skipped test left a child), no test is reported as failed, because the guard acts after pytest has
counted. The process exit status is `1`, and the experiment journal and its reducer record that same status, but
JUnit's failure counts, pytest's summary line, the exit codes of the xdist workers and `--last-failed` still show
success. Read the exit status (the dev gate does) or the "gateway port guard" lines, not those counts.

The teardown check does not rely on that bind check alone: it also asks the system who owns each guarded port, and a
listener it proves is the test's own is a leftover even when the bind check called the port free.

A listener counts as the test's own only if it was born during that test, from that test. One that an earlier test
started is not the test's, and one that a launcher started and then left (the launcher exits) still is. To tell, the
guard asks the system to keep the test's family together, but only while one of these tests runs with
`AGENTTALK_TEST_GATEWAY_PORTS=1`: nothing is arranged at configure or collection, and pytest itself never joins a job
object on any system. On Windows a job object holds the test's own child processes (no kill-on-close; a child can still
ask to break away); on Linux the pytest process is a "child subreaper" for the length of the test and put back after,
and the processes alive at the start are noted; on macOS neither exists. Without a job the guard follows a chain of
parent links, and a process is the test's only on a positive chain to a child the test started: one adopted by the
pytest process has lost the edge to whoever started it, so it is not told apart from an earlier test's orphan. Any
failure to observe (a job query, a process list, a `/proc` or `ps` read) counts as "cannot tell", never as empty.

The same holds inside the pytest process itself: when one of these tests begins, the guard notes which sockets of the
process are already listening, so a listener that an earlier test opened and kept for later is not this test's leftover
(it is the same socket object, still listening), while a socket that was not listening when the test began and is open
at its end is, even if it reuses the old port after the old socket was closed. If the guard could not make that list,
or the system names the pytest process but the guard sees no Python socket listening there (a raw descriptor), it
cannot tell, and treats it as below. The same holds if one live socket could not be read (a partial list is not a
list), or if the system counts more listening sockets of the pytest process on the endpoint than the guard can see as
objects (a descriptor detached from its socket may sit beside an older listener). The count and the objects are compared as kernel sockets, not objects (two objects may be copies of one socket): Linux can tell, Windows and macOS cannot, so there several pre-existing objects on the endpoint are not told apart from several sockets and the guard cannot tell. An `lsof` run that reports an error counts as unreadable. A copy of an older listener (`dup`,
`fromshare`) wrapped in a new socket object counts as new. The pytest process itself is never ended.

Each owner of a busy port gets one of three answers. The test's own is reported as a leftover, named by process id, and
ended; "ended" is printed only after the exit is confirmed, otherwise the line says "not ended". Another process's is
named ("held by another process") and left alone. When the guard cannot tell (Linux and macOS, or a refused job, for a
listener whose launcher has already exited; an owner it cannot find out; any observation that failed), it never ends the
process: an ordinary run names the
port and passes, and an evidence run (`--experiment-record`) refuses it, because there a false green does more harm than
a false stop. So on those systems a real leftover of that shape can pass an ordinary run. Also not caught on Windows: a
child that is not started through `subprocess.Popen` (for example `multiprocessing`), one that breaks away, or one that
a fast launcher outruns. On Linux the flag is process-wide for the length of the test, so an unrelated orphan adopted
then stays a child of the worker until it exits.

On Windows the system's table of listening ports names the process that created a socket, and keeps naming it after the
socket was handed to another process and the creator closed its own copy. So the table's word for the guard's own
process is not enough to blame it: only the guard's own scan of its Python sockets can make a listener on that port the
test's or another's. A claim for the guard's own process that the scan cannot confirm (the scan failed, or the socket is
a raw descriptor) is "cannot tell", and it stays "cannot tell" to the final decision: an ordinary run names it and
passes, an evidence run refuses it, and the process is never ended. The claim is never set aside, so it cannot make a
held port read as free, and an outsider named beside it does not decide the case alone. The same goes for a scan that
failed while the table names only an outsider (a socket shared into this process leaves only its creator in the table):
the failed scan cannot rule out a copy held here, so the answer is "cannot tell" and not "another's", unless
something independent shows the listener is the test's. The Windows table is the IPv4 one: it lists a dual-stack `::`
listener as a `0.0.0.0` row, but not an IPv6-only listener (`::1`, or `::` with IPV6_V6ONLY on), so for an IPv6 endpoint
the number of sockets this process holds is not known (never read as zero) and an older visible listener cannot stand
for the whole story. The same holds for any busy port whose owner could
not be found at all (an empty answer from the system, an unreadable `/proc` entry, a process of another user): the bind
check has shown the port to be busy, so an empty answer means the owner was not seen. A leftover listener kept in a raw
descriptor that the scan cannot see is therefore not blamed on the test: it is "cannot tell". The scan itself never
fails a test, and a failed read never makes a listener "another's" or a clean run: the scan, or the list taken when the
test began, counts as not made, and the answer is "cannot tell".

Three limits are kept on purpose; each can only make the guard name too much or blame too little at the end of a test,
never let a guarded test start against a port another process holds (that is the one bind decision at the start).
Several socket objects for one older listener cannot be told apart from several sockets on Windows and macOS, so there
the guard cannot tell. A new wrapper (`dup`, `fromshare`) around an older listener counts as the test's own object and
is a resource that test must close. And a raw import of a listener that another process created is not seen at all: on
Windows the table can then name only the creator, a successful scan sees no Python socket, and the answer can be
"another's", so a real leftover of that shape can pass an evidence run too. The guard does not claim to catch it.

A busy port does not always mean the test that just finished left it. The guard blames a test for a port only when
it can show the listener is the test's own: the listener belongs to the test's process or to a child the test started
(it asks the system who owns the port: the Windows TCP table, `/proc` on Linux, `lsof` elsewhere). The owner must
reach the exact address the guard checked, not only the same port number: a listener on another address, or an
IPv6-only one, does not count; an IPv4 wildcard one does, and an IPv6 wildcard one only where it is known to be
dual-stack (the guard knows that only for its own process's sockets). A port held by another process is
only named, on a line that says "held by another process"; it does not fail that test or change the exit status. An
owner that cannot be found out (which can include a real leftover, for example a socket the guard cannot see) is "cannot
tell": an ordinary run names it ("the guard could not tell whose listener it is") and passes, an evidence run
(`--experiment-record`) refuses it, and it is never ended.

One limit follows from this. A gateway that another process starts after the decision is not stopped by the guard,
and the guard does not fail the run for it: the next of these tests that finishes while the port is held is named
as "held by another process" and the run still passes. So stop any other gateway before such
a run and keep it stopped until the run ends. CI sets the variable on every dev-gate leg,
and the gate passes it to its pytest runs only as the exact value `1`. To run them yourself on a machine with
no gateway running, set the variable, for example `AGENTTALK_TEST_GATEWAY_PORTS=1 python -m pytest
tests/test_ovh_gateway_service.py`. On a machine that runs the live gateway, leave it running and do not set the
variable: the tests skip themselves, and CI covers them. A new test that touches those ports belongs in
`_GATEWAY_PORT_TEST_NAMES` in `tests/conftest.py`.

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

The trial applies only to Windows, Python 3.10 through 3.13, and only on
master: once a day (the scheduled run at 02:23 UTC) and on a manual run. It does
not run on every push to master, and neither its eight mode jobs nor its
aggregate runs on PRs. Its eight jobs run about 2.5 hours each, so on every push
they held eight of the repository's 20 concurrent runners and PR checks queued
behind them; a daily run leaves those runners to PRs and merges. The weekly
scheduled run (Monday 09:17 UTC) does not run the trial: the jobs tell the two
schedules apart by the schedule text. Each scheduled or manual run on master also
carries its own full dev-gate legs, and the aggregate compares the trial with the
legs of that same run. The daily and manual runs wait in a queue of their own,
apart from the pushes to master: GitHub keeps only the newest waiting run in a queue,
and a push has no trial, so in one queue a push would replace a waiting daily run and
lose the day's measurement. A master run that is already running is not cancelled by a later run;
a waiting run can still be replaced by a newer waiting run of its own queue. The existing full
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

The contract exposes the frozen installer described below. Production execution
still refuses until the P3 journal adapter is connected. Local tests exercise
fixture wheels, fake process collaborators and fixture inventories; the command
line has no option to select those fixtures. Real Python 3.12 installs belong to
the runner dry run. This preparation does not enable a probe or acceptance job.

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

For contributors preparing the experiment, P7 supplies
`PinnedInstaller(creator, observer_sources=None)`, with
`prepare(request, mode, workdir)` and `finish(prepared)`. `creator` is the gate's
`InterpreterInfo`. Its exact Python patch must match the frozen environment plan.
Production execution still refuses until the P3 recorder adapter is connected;
having lock files does not enable a probe or acceptance job.

Each Python minor will have four locks under `locks/<minor>/` (today only
`locks/3.12/` exists): `host.txt`, `build.txt`,
`runtime.txt` and `wheel-test.txt`. Every non-comment line has this reference
shape (the values below are placeholders, not an installable lock):

```text
package==version --hash=sha256:<64 lowercase hexadecimal digits> # wheel=<exact wheel filename>
```

Locks pin pip in every role, and setuptools on Python 3.10 and 3.11. The host
contains the gate tools and hatchling; build contains the build frontend and
hatchling. Runtime contains the bootstrap and the candidate's runtime dependencies,
with no pytest, xdist, pluggy, execnet or build backend. Wheel-test adds the test
packages and their complete dependencies. Names are compared after PEP 503
normalization, so case and runs of hyphens, underscores and dots do not hide an
extra package. Empty locks, missing bootstrap or role roots, duplicate normalized
names and non-wheel provenance are refused before pip runs.

`lock_install_argv(python, lock)` builds `python -I -m pip install` with
`--require-hashes --no-deps --no-cache-dir --only-binary=:all: -r <lock>`.
It also uses `--force-reinstall` so an already-satisfied bootstrap package cannot
skip verification of the locked wheel's hash.
Role locks must be plain single-link files inside the checkout, with no file
symlinks. Before creating a role environment, the installer reads its lock once,
checks those bytes against the frozen hash and pins, and writes them to a fresh
file in the run's work folder. Pip installs from that copy; a later change to the
checkout's lock cannot change the installed requirements. The gate requirements
file also stays inside the checkout and must be plain; it is read for validation,
not passed to pip. Whitespace-introduced inline comments are removed as pip does;
URL fragments remain intact. This preprocessing does not apply to strict lock
files, whose `# wheel=...` provenance remains required.
Plan validation checks the host lock against every active gate
requirement, the build role's build and hatchling pins against the requirements
file, and wheel-test's pytest and pytest-xdist pins against `dev-gate.json`.
These checks use the frozen Windows target for markers, refuse active extras and
direct URLs, and compare versions as PEP 440 identities while keeping their
original spelling in the evidence. The candidate wheel is copied into the work
folder too, and that copy's hash is checked before installation.
`role_requirements(checkout, role, minor=...)` derives role roots from the gate
requirements, pytest configuration or project dependencies, plus bootstrap roots.
The host, runtime and wheel-test roles also include the candidate's static project
dependencies; build does not install the candidate. Missing or invalid project
metadata and dynamically supplied dependencies are refused for candidate roles.
Passing `checkout=` to `validate_lock_wheels` uses these roots and refuses a
supplied requirements list that differs. Comparison is order-independent and
uses complete parsed requirements: canonical names and extras, specifiers,
URLs and markers. Reordering roots preserves every constraint and duplicate;
it does not reduce the binding to package names. The derived root order still
drives validation. Calls without `checkout=` keep the
explicit fixture-root interface; the runner does not yet provide a wheelhouse.
Wheel validation reads each wheel once, refuses file links and wheels larger than
256 MiB (268,435,456 bytes), and hashes and inspects the same bytes. Replacing a
downloaded wheel during validation cannot mix one wheel's hash with another
wheel's dependencies or Python-version requirements. Inventory likewise parses
the RECORD bytes used for its reported hash and uses one METADATA snapshot for
both package identity and the RECORD hash check.
Role-lock installation can download those hash-checked wheels. The installer
receives lock files and a frozen plan, but no validated wheelhouse path; it cannot
reuse the offline validator's wheel folder. Passing that folder through the runner
and adding `--no-index --find-links` to role installation remains a follow-up.
The candidate's committed host export is installed offline with
`--no-build-isolation` using the host's locked backend. Runtime and wheel-test
use `candidate_install_argv`, which checks the separately frozen wheel hash before
the local `--no-deps --no-index` install. Builds run with `--no-isolation` and
`PIP_NO_INDEX=1`; a missing backend fails instead of being downloaded. Every fresh
role records the ensurepip pip version before the locked install and runs pip's
dependency consistency check afterwards. The ordinary gate installers and lower
bounds remain unchanged.
The host's committed source export is passed to pip as a directory after the
earlier Git binding check.
The run folder is trusted: these checks do not protect it from writes by the same
operating-system account.

`make_environment_plan` returns the object to freeze under `environments[minor]`.
It includes `python_version`, `bootstrap_pip_version`, `candidate_version`,
`wheel_sha256`, `roles` (each lock's `sha256` and complete pins), `observers`, and
the canonical `freeze_sha256`. A changed patch, bootstrap, lock, candidate or
observer plan changes this hash. Freeze all four minors' plans before stage 1 (the
3.10, 3.11 and 3.13 locks are still to come).

`environment_inventory` obtains isolated target-interpreter facts, then the
gate process inventories the reported folders without running target startup hooks.
`check_installed_set` compares its complete distributions to the lock plus the
candidate where installed. It records each distribution's `RECORD` SHA256 and
parses raw RECORD rows to check every listed file. Hashed entries must match
the SHA256 listed in their row; unhashed entries (including RECORD itself and
generated `.pyc` files) are checked for presence only. These checks cover recorded files within
the isolated environment, including installed scripts, and refuse paths outside it.
It separately inventories unowned site-packages
files: a distribution list cannot detect an injected `.pth` or observer module.
Probe-only observer paths and SHA256 values are frozen per role and copied from
explicit `observer_sources`; acceptance permits none. `validate_install` compares
each installed role's `lock_sha256` with the frozen role's `sha256`; `execute`
passes the frozen `roles` map through the required `frozen_roles` argument.
Before pytest, `execute` independently inventories its prepared interpreter (host
for source mode, wheel-test for wheel mode), checks the frozen pins and installed
files, and compares RECORD hashes with the installer's report. The gate's own
process uses `os.walk` on the reported folders and checks file ownership, contents, and
RECORD hashes; prepared-interpreter startup hooks do not run in that process.
The measurement is
kept at `calls[].prepared_inventory`. The probe refuses site directories outside
the environment prefix and additional nonstandard import paths, including those
added by `.pth` files. A venv's root accepts only its standard layout and
RECORD-owned entries; unexpected files or folders there are refused too.
The raw inventory's optional `refused_pth_files` map adds closed reasons for
refused startup files: non-ASCII text, an ambiguous line separator or an
unsupported import line. Every such filename remains in `unmeasured_import_paths`.
The validator reports these separately from ordinary import paths, with the same
escaping and 20-entry limit. A missing map retains legacy behavior; a malformed
map or unknown reason refuses. Successful role proofs, frozen plans and call
evidence keep their existing shapes.
Python identity uses its reported version string; the executable bytes,
standard-library bytes, and the base interpreter's install folders (including
its root and `DLLs` on Windows) are outside this measurement.
Installed RECORD files are compared with the installer's report, not with a
frozen value, so a RECORD rewritten together with its files by the same account
is not detected (the run folder is trusted against that account).
The probe runs with `-I -S -B`: it does not execute `.pth` import lines,
`sitecustomize`, or `usercustomize`. The gate reads `.pth` files as text and adds
their existing path lines itself. Import lines are refused even when listed in
RECORD, with two exact filename/line exceptions: `pywin32.pth`'s
`import pywin32_bootstrap` and `distutils-precedence.pth`'s standard setuptools
`SETUPTOOLS_USE_DISTUTILS` / `_distutils_hack.add_shim()` line. Prepared `*._pth`
settings beside the executable or at the venv root are refused before the probe
starts, since they can enable site processing independently of command-line flags.
The `.pth` content policy is ASCII-only, including comments and path lines.
Non-ASCII bytes and ambiguous line separators are refused conservatively across
the supported Python versions, even when a particular interpreter could read them.
For `pyvenv.cfg`, only the normalized `include-system-site-packages` key enables
system sites; unrelated true settings do not. Differently cased or spaced keys
are retained, and conflicting declarations with any true value cause refusal.
An exactly repeated raw key uses its last value; a UTF-8 BOM is accepted.
The reported interpreter identity, initial standard paths, and sysconfig data
remain trusted under the executable/base-install limit above.
A launcher that reports different facts for this probe and for pytest is outside
this check. The full runtime effects of the allowed import lines and RECORD-owned customization modules under
the real pytest arguments and working folder are not modeled. Their files are
still checked; a forged RECORD that adds them is the RECORD limit above.
Build and runtime are measured inside the
installer only; `execute` does not receive their interpreter paths.
It also compares
each installed role's `observer_hashes` to the frozen `observers` declaration
(role → relative filename → SHA256), and checks that `probe_files` is the sorted
union of those filenames. Source mode checks the host; wheel mode checks all four
roles. Missing, extra or changed observers are refused by name. Their individual
hashes remain in the artifact at `calls[].installed_set.environments[role].observer_hashes`.
An empty observer declaration keeps the ordinary empty-file behavior.
Missing recorded files,
changed hashed files, and unowned files outside the observer plan invalidate the
proof. Ownership applies to each directory entry: an unrecorded symlink or Windows
junction is refused even when its target is a recorded file or directory.
Changes to unhashed file contents are not checked. Per-role evidence includes `python_version`,
`bootstrap_pip_version`, installed `pip_version`, `packages`, `records`,
`observer_hashes`, `lock_sha256`, and the install arguments. The aggregate
`installed_set` keeps these under `environments`, alongside the freeze hash,
gateway policy, probe file list and the wheel hash for wheel mode. Isolated
environments and run evidence are retained by `finish`; it removes no basetemp.

Real installs of all four Windows CPython 3.12 locks are a required part of the
P8 runner dry run and first probe preflight. The frozen pytest 9.2.0 / xdist 3.8.0
combination is exercised only on that runner (P8) and needs the gate's grouped-ID repair.
Fixture-wheel tests on another Python version do not replace that check. Lock generation requires a networked
operator run; no lock should be filled with invented hashes or fixture pins.
Before adding generated locks, `validate_lock_wheels` checks their actual downloaded
wheels offline: hashes, metadata identities, `Requires-Python`, Windows CPython
target tags, active dependency markers, version constraints and the complete role closure. Its
wheel root must contain exactly one matching file for each locked wheel filename;
multiple matches are refused even when their bytes are identical. Its
required `python_version` argument is the exact frozen patch, such as `3.12.9`;
it supplies both `python_full_version` and `implementation_version`. Markers using
`platform_release` or `platform_version` are refused because the freeze does not
carry those Windows target values. Dependency closure includes the base package's
requirements and the requirements of every requested extra, matching pip. The
`requirements` argument is the original role requirement list used by the generator;
an omitted transitive dependency or an unrelated extra pin fails that check.

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
