# Development gate reference

Audience: agenttalk contributors and CI integrators who need SHA-bound evidence for a candidate change.

`agenttalk dev-gate` is the single voting command for repository tests, packaging checks, and CLI-runnable
security checks. It has no skip flags. A missing interpreter, tool, result, or evidence field blocks the run.

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
                   [--keep-run-dir]
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

### The run folder

Every run exports the candidate, builds its package, and installs/tests the wheel inside one temporary run
folder under `--temp-root` (or the system temp directory by default), named `agenttalk-dev-gate-<random>`. A
run that passes removes this folder itself once its evidence is durably written, using the same careful,
never-escalating removal `agenttalk janitor` itself uses for a locked candidate export: the plain removal step,
nothing stronger. A run that is blocked, fails, is interrupted (Ctrl-C), is asked to keep it with
`--keep-run-dir`, or whose own removal attempt fails, leaves the folder in place - every one of those outcomes
names the kept folder's path: in the command's own JSON summary (`run_dir`) for anything that completes or
blocks, and on stderr for a failure or an interruption, which print no JSON summary at all.

`agenttalk janitor` does **not** look for these folders. A folder's name is not proof that it is disposable -
a permanent `--evidence` destination can happen to look just like one - so janitor leaves every
`agenttalk-dev-gate-*` folder to the gate's own cleanup above. A run that never got to clean up after itself
(an old failed or interrupted run, or one that genuinely needed `--keep-run-dir`) accumulates under the temp
root until removed by hand; the gate's own summary always says where to find it.

Every check's full log, and every package file a passing record names (the built sdist, wheel, and - when
`pip-audit` ran - the dependency snapshot it audited), is copied straight into its own namespace next to the
evidence JSON, named after that run's own ID (`<run_id>/logs/...`, `<run_id>/artifacts/<kind>/...`), created
only if nothing is already there, with each file's hash checked right after the copy. Only once every file the
record will name is already real and verified is the evidence JSON itself written, last - so a reader can
never see a saved record naming a file that does not exist yet, and a failure at any point leaves whatever
record was already at that path exactly as it was. Two evidence files commonly share a parent directory - the
default temp location, for one - so this per-run namespace, created fresh and never reused, is what keeps a
later run's copies from silently overwriting an earlier run's. A failure partway through copying keeps the
unfinished namespace rather than deleting it, and names it in the command's own output, right alongside a kept
run folder.

A run folder is the gate's own private scratch space for the duration of that one run - a file a person places
in it by hand, with nothing of the gate's own in it, is the one accepted exception to everything below, and is
never protected from the gate's own cleanup.

Everything the gate itself writes, though, is always protected. A run also writes its own small ownership
marker into its folder at allocation, and into the copy namespace above. A second run's own `--temp-root` or
`--evidence` choice is refused outright if it would land inside any folder already carrying a marker - active
or kept - so one run's cleanup can never remove data a second run still owns. As a second line of defense, a
run's own cleanup re-scans its folder for any marker it does not recognize as its own immediately before
deleting it, and keeps the folder instead if it finds one - including when that marker cannot even be read,
which is treated the same as finding somebody else's - and the same scan never follows a symlink or Windows
junction while looking.

That cleanup step itself only ever removes a folder it is certain is safe to remove. Deleting a folder
recursively can, in the moment between checking an entry and actually removing it, have that entry secretly
replaced with a link pointing somewhere else - so the real removal can be tricked into deleting through that
link instead of the folder it was asked to remove. Where the operating system can prove to Python that this
particular trick is not possible, cleanup proceeds; everywhere else - including every ordinary, non-administrator
run on Windows today - cleanup still proceeds, accepting one known, narrow exception: someone with access to the
SAME user account the gate is running as, racing to replace that one folder entry at that exact moment, could in
principle reach a file outside it. Accepting this is not a new weakness, because a same-account actor capable of
staging that race could already have deleted or changed those same files directly, at any time, without needing
the race at all. A run started with elevated/administrator rights, or as the POSIX root account, never runs this
automatic removal at all, even for the plainest folder - its folder is always kept and reported - because there
the gap would let it reach files an ordinary account genuinely could not touch on its own.

The dozen tests that bind or call the paid gateway's real ports (127.0.0.1:4000 and 4001) are opt-in, so a
machine that runs the live gateway never reaches it by accident. They are skipped unless
`AGENTTALK_TEST_GATEWAY_PORTS=1` is set, and even then they are skipped if either port is already occupied
when tests are collected; that check binds a separate socket and sends nothing. It runs only once, at
collection, so a gateway started later in the run is not noticed. CI sets the variable on every dev-gate leg,
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
`dev-gate-evidence.json`. For each failing check, follow `checks[].log.artifact_path` -
whatever relative path it actually names in that bundle (current bundles use
`<run_id>/logs/<check-id>.log`; do not assume the exact shape, follow the field) - to
find the log in the same artifact. This file contains the complete combined
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
