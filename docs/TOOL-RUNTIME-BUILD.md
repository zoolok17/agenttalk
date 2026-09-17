# Tool runtime build and switch procedure

This documents the fleet's actual, current mechanism for running `agenttalk`
outside a checkout, and the procedure for building a new pinned runtime and
switching seats onto it. It does not exist anywhere else in this repository
today - there is no build script and no prior document for it.

## 1. What a pinned tool runtime is, and why seats never run from a checkout

Every wrapped seat's Python interpreter is a separate, self-contained venv
under `agenttalk-tool-runtime/<version>/` (`Include/`, `Lib/` (or `lib/` -
observed casing varies by runtime, see below), `Scripts/`, `pyvenv.cfg`),
with `agenttalk` pip-installed into it non-editably. A seat's launch script
points `python.exe` at that venv, never at a checkout's own interpreter or
`PYTHONPATH`. The purpose is stability and reproducibility: a wrapped agent's
own bus commands (`agenttalk reply`/`send`/etc.) must resolve to a fixed,
known-good `agenttalk` release, not to whatever a live checkout happens to
contain at that moment (mid-edit, mid-merge, or simply a different branch
than the one the wrapper itself was launched from). Hardening this into an
actively-enforced, config-driven guarantee (rejecting ambient/checkout
`PYTHONPATH` even if something tries to inject one) is the subject of GitHub
PR #92 (issue #93) - open, not yet merged as of this writing, and reviewed
in depth (two rounds: an initial finding-driven review, and a narrower
confirmatory pass that caught an execution-only defect the first pass's
structural read missed). That PR's own review record is the design history
for the *enforcement* mechanism; **today's actual mechanism is simpler and
purely operational**: separate venvs, plus two launch scripts that each hold
one hardcoded runtime-version path literal, plus a per-agent duplicate-
wrapper refusal check (`Get-CimInstance Win32_Process` matched on
`wrap --for <agent>` in the command line) so a launch never stacks a second
wrapper for the same seat. There is no code in this repository today that
force-clears an ambient `PYTHONPATH`/`PYTHONHOME`/`PYTHONUSERBASE` for a
wrapped child - the isolation is entirely the separate-venv convention
itself, not an enforced guard.

## 2. Build procedure: a new runtime for release version `N`

**Precedent, observed directly (`direct_url.json` inside each existing
runtime's own `agenttalk-<version>.dist-info/`), not assumed:**

| Runtime | Installed from (`direct_url.json`) |
|---|---|
| v0.79.1 | a plain local directory: `file:///.../at-v0791-export` (no VCS record) |
| v0.85.0 | a plain local directory: `file:///D:/Projects/Claude/agenttalk-rel-085` (no VCS record) |
| v0.86.0 | a plain local directory under a temp scratchpad (no VCS record) |
| v0.87.0 | a **git-VCS-tracked** install: `vcs_info.commit_id` recorded, `requested_revision: "v0.87.0"` |

None of the four used a standalone `.whl` file on disk (confirmed: every one
carries a normal `WHEEL`/`RECORD`/dist-info set, which `pip` writes for any
install path once it builds a wheel internally via the backend - that is
not evidence a `.whl` file was ever saved separately). Three of the four
also carry **no verifiable record of which commit** they were built from -
only an informal, human-chosen temp-directory name (`wt-v086`,
`agenttalk-rel-085`, `at-v0791-export`). The recommended procedure below
closes that gap by using a real, git-metadata-bearing checkout, matching
what CI itself does (`.github/workflows/tests.yml`: `python -I -m pip
install --no-deps --no-build-isolation .` from an actual checkout, not a
wheel file) while still producing an inspectable, archivable `.whl` -
the `build` package this needs is already a declared project dependency
(`pyproject.toml`: `dev = ["pytest>=8.0", "build>=1.2"]`; backend
`hatchling`, per `[build-system]`).

**Steps** (from the operator's own machine, in the main checkout - never
from a wrapped agent's own seat clone, and never touching any running
wrapper):

1. **Export the exact tag into a clean, disposable location** - do not
   reuse a live seat checkout or the main checkout's own working directory:
   ```
   git worktree add --detach <clean-tmp-dir> vN
   ```
2. **Build the wheel** from that clean export:
   ```
   cd <clean-tmp-dir>
   python -m build --wheel
   ```
   This produces `dist/agenttalk-N-py3-none-any.whl` (confirmed tag/format
   from the existing v0.87.0 install's own `WHEEL` file: `Tag: py3-none-any`,
   `Generator: hatchling 1.32.0`, `Root-Is-Purelib: true`).
3. **Choose and record the interpreter.** Read every existing runtime's own
   `pyvenv.cfg` before choosing - do not assume:

   | Runtime | CPython (`pyvenv.cfg: version =`) |
   |---|---|
   | v0.79.1 | 3.14.2 |
   | v0.85.0 | 3.14.6 |
   | v0.86.0 | 3.14.6 |
   | v0.87.0 | 3.10.11 |

   `pyproject.toml` declares `requires-python = ">=3.10"` with no upper
   bound, so 3.10.11 is not a violation - but it is the one outlier among
   four runtimes, and its divergence from the other three was never a
   documented, deliberate choice (traced during issue #145: no commit or
   design note pins it to 3.10 specifically). Recommendation: **pin the new
   runtime to the same interpreter family as the current majority baseline
   (3.14.x, matching the most recent non-outlier runtimes) unless the
   release being built has a specific, stated reason to need a different
   one** - not because 3.10 was proven unsafe (issue #145's own
   interpreter-isolation reproduction ran the same wrapper code under both
   3.14.6 and 3.10.11 and got identical results for the failure shapes it
   tested), but because an *undocumented* interpreter drift across releases
   is itself an avoidable, previously-unflagged source of confusion when
   debugging a version-to-version behavior difference - as it was here.
4. **Create the venv with that explicit interpreter**, e.g.:
   ```
   <chosen-interpreter>\python.exe -m venv D:\Projects\Claude\agenttalk-tool-runtime\vN
   ```
5. **Install the wheel non-editably, no build isolation needed at this step**
   (the wheel is already built):
   ```
   D:\Projects\Claude\agenttalk-tool-runtime\vN\Scripts\python.exe -m pip install --no-deps <path-to>\agenttalk-N-py3-none-any.whl
   ```
6. **Verify from the runtime itself**, not the checkout. Both commands below
   were actually run (read-only) against the **existing v0.87.0 runtime**
   for this document - observed output quoted verbatim, not the expected
   shape:

   ```
   D:\Projects\Claude\agenttalk-tool-runtime\v0.87.0\Scripts\python.exe -m agenttalk --version
   ```
   observed:
   ```
   agenttalk 0.87.0
   ```

   ```
   D:\Projects\Claude\agenttalk-tool-runtime\v0.87.0\Scripts\python.exe -c "import agenttalk, sys; print('FILE', agenttalk.__file__); print('VERSION_ATTR', getattr(agenttalk, '__version__', 'n/a')); print('SYS_VERSION', sys.version)"
   ```
   observed:
   ```
   FILE D:\Projects\Claude\agenttalk-tool-runtime\v0.87.0\lib\site-packages\agenttalk\__init__.py
   VERSION_ATTR 0.87.0
   SYS_VERSION 3.10.11 (tags/v3.10.11:7d4cc5a, Apr  5 2023, 00:38:17) [MSC v.1929 64 bit (AMD64)]
   ```
   The `FILE` path resolves inside `<runtime>\...\site-packages\agenttalk\`
   - confirming the import comes from the runtime's own installed copy, not
   any checkout (a checkout-resolved import would show a `src\agenttalk\`
   path instead). Note the observed path casing is `lib`, not `Lib`, even
   though the venv's own top-level directory is `Lib\` (Windows path
   comparisons are case-insensitive; the printed casing simply reflects how
   the interpreter constructed the string internally - not a discrepancy to
   chase.)

## 3. Switch procedure

**The exact lines that carry the version** (read directly from both scripts,
current content):

- `launch-claude-seat.ps1`, line 6:
  `$py = "D:\Projects\Claude\agenttalk-tool-runtime\v0.86.0\Scripts\python.exe"`
  (lines 2-3 carry a comment explaining the pin exists specifically because
  of issue #145 - update or remove that comment once the fix has shipped in
  a release this script moves onto).
- `launch-codex-estate.ps1`, line 16:
  `$py = "D:\Projects\Claude\agenttalk-tool-runtime\v0.87.0\Scripts\python.exe"`

**Relaunch one seat at a time, by PID:**

1. Identify the running wrapper's exact PID for the seat being moved (e.g.
   `Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match
   "wrap --for <agent>" }`).
2. Stop that exact PID (never a broader kill-by-name).
3. Edit the launch script's `$py` line to the new runtime path.
4. Re-run the launch script for that one seat/agent. Both scripts already
   refuse outright ("REFUSED: wrapper already running for ... (pid ...)")
   if a wrapper for that same agent is still detected running - the
   refusal is real, existing behavior, not something this procedure adds.
5. Repeat per seat/agent - `launch-codex-estate.ps1` supports `-Only
   <agent>` to move one codex seat without touching the others already on
   the old runtime.

**Glance afterward:**

- `agenttalk status` - the just-relaunched agent should show a fresh
  heartbeat and normal (not `spawn_exec_error`/`config_blocked`) health;
  `launch-codex-estate.ps1`'s own trailing message already states this
  exact check.
- `agenttalk doctor` - confirm no new heartbeat/health check regresses for
  that agent specifically (a stale `PYTHONPATH`/wrong-interpreter mistake
  in the edited line typically surfaces here first, as an unhealthy or
  `spawn_exec_error` state, not a silent success).

**Rollback:** the previous runtime directory is never deleted by this
procedure - point the script's `$py` line back at the previous
`agenttalk-tool-runtime\v<old>\Scripts\python.exe` and relaunch that one
seat the same way (stop by PID, edit, relaunch, refusal guard as above).

## 4. Verification checklist (for whoever runs this procedure)

- [ ] New venv's `pyvenv.cfg` shows the intended, deliberately-chosen
      interpreter version (read it - don't assume the `venv` command picked
      the one you expected).
- [ ] `<new-runtime>\Scripts\python.exe -m agenttalk --version` prints the
      expected release version.
- [ ] `<new-runtime>\Scripts\python.exe -c "import agenttalk; print(agenttalk.__file__)"`
      resolves inside `<new-runtime>\...\site-packages\agenttalk\...` - not
      any `src\agenttalk\` checkout path.
- [ ] The launch script's version-literal line was edited for exactly one
      seat/agent at a time, not a blanket find-and-replace across seats
      still expected to stay on the old runtime.
- [ ] The seat being moved had no wrapper process running before relaunch
      (stopped by exact PID, confirmed gone) - the script's own refusal
      check is a backstop, not a substitute for checking first.
- [ ] `agenttalk status` shows a fresh heartbeat and healthy state for the
      moved agent within the first normal poll interval.
- [ ] `agenttalk doctor` raises nothing new for that agent.
- [ ] The previous runtime directory still exists on disk, untouched -
      confirmed available for rollback before considering the switch done.
