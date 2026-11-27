<#
.SYNOPSIS
  Plants one fault at a time in the #907 sources, runs the tests that should catch it, and records which tests failed.

.DESCRIPTION
  A fault that no test notices is a hole in the tests. For each planted fault this script changes one place in
  src/agenttalk/cli.py or src/agenttalk/git_job.py, checks that the changed file still compiles, runs the tests that
  must catch it, writes one result line, and puts the file back with `git checkout`. A fault counts as caught when at
  least one test FAILS (an ERROR, such as a file that no longer compiles, is reported separately and is not a catch).

  It edits tracked files in place. Run it on a clone you can throw away: it refuses to start when ANY file that a
  selected fault changes has uncommitted edits (the list is derived from the faults themselves, never written by hand),
  or when `git status` cannot be read, or when a target is marked assume-unchanged or skip-worktree (git status cannot see
  an edit in such a file), so nothing of yours is lost; it leaves every file as it was committed, and says so only after it
  has checked every file byte for byte against the original (it writes the saved original back if git checkout did not).

  What is kept for each fault: its id, whether it was planted and compiled, how many tests ran, passed, failed, errored
  and were skipped, and how many failures came from an assertion or an exception (a weaker catch). Nothing that pytest
  prints is kept: no test name and no failure text. To see which tests failed and why, run the fault's tests locally.
  A run in which no selected fault ran any test exits 1: nothing was tested.

  Paths: every path is a parameter or relative to the repository. The results file holds no path, user name or
  temporary folder.

.PARAMETER Repo      The repository (default: three levels above this script, docs/verification/907).
.PARAMETER Python    The interpreter that runs pytest (default: python).
.PARAMETER Out       The results file (default: faults-results.txt next to this script).
.PARAMETER BaseTemp  pytest's temporary folder (default: a new folder under the system temporary folder).
.PARAMETER Only      Run only the fault with exactly this id.
#>
param(
    [string]$Repo = '',
    [string]$Python = 'python',
    [string]$Out = '',
    [string]$BaseTemp = '',
    [string]$Only = ''
)
# Continue, not Stop: Windows PowerShell 5.1 turns a stderr line of a native command (git, python) into a terminating
# error under Stop, which would end the run before its own message, or in the middle of a fault. Every real failure below
# is checked and thrown explicitly, and every file is put back in a finally block.
$ErrorActionPreference = 'Continue'
if (-not $Repo) { $Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path }
if (-not $Out) { $Out = Join-Path $PSScriptRoot 'faults-results.txt' }
if (-not $BaseTemp) { $BaseTemp = Join-Path ([IO.Path]::GetTempPath()) ('faults907-' + [guid]::NewGuid().ToString('N').Substring(0, 8)) }
# Each path is read from where the runner was started, before it changes folder: .NET file reads use the process folder.
$start = (Get-Location).ProviderPath
$Repo = (Resolve-Path -LiteralPath $(if ([IO.Path]::IsPathRooted($Repo)) { $Repo } else { Join-Path $start $Repo }) -ErrorAction Stop).ProviderPath
if (-not [IO.Path]::IsPathRooted($Out)) { $Out = Join-Path $start $Out }
if (-not [IO.Path]::IsPathRooted($BaseTemp)) { $BaseTemp = Join-Path $start $BaseTemp }

# a run that is polite to the machine; nothing of the environment is printed
$me = Get-Process -Id $PID
$me.PriorityClass = 'BelowNormal'
foreach ($n in @([Environment]::GetEnvironmentVariables().Keys)) { if ("$n" -like 'AGENTTALK*') { Remove-Item -LiteralPath "Env:$n" } }
$env:PYTHONPATH = 'src'
Set-Location -LiteralPath $Repo

$CLI = 'src/agenttalk/cli.py'
$JOB = 'src/agenttalk/git_job.py'

$fake = @('tests/test_git_write_job_907.py', 'tests/test_git_job.py')
$real = @('tests/test_git_job_windows.py', '-k', 'limits or monitor')
$static = @('tests/test_git_job.py')
$sched = @('tests/test_git_job_scheduled_task_check.py')
$lane = @('tests/test_lanes.py::test_m6_assign_provisions_worktree_by_default_and_workspace',
          'tests/test_lanes.py::test_786_successful_rollback_removes_worktree_and_branch')
$runner = @('tests/test_git_job_fault_runner.py')
$RUNNERFILE = 'docs/verification/907/faults.ps1'
$CONF = 'tests/conftest.py'
$SCRIPT = 'scripts/check_git_job_scheduled_task.ps1'

$setlimit = @'
        handle = self._kernel.CreateJobObjectW(None, None)
        if handle:
            buffer = (self._ctypes.c_char * 144)()
            self._ctypes.c_uint32.from_buffer(buffer, 16).value = FLAGS
            self._kernel.SetInformationJobObject(self._ctypes.c_void_p(handle), 9, buffer, 144)
        return int(handle) if handle else None
'@
$createOld = @'
        handle = self._kernel.CreateJobObjectW(None, None)
        return int(handle) if handle else None
'@

$faults = @(
  @{ id = 'F01-kill-on-close-set'; tests = $real; edits = @(, @($JOB, $createOld, $setlimit.Replace('FLAGS', '0x2000'))) },
  @{ id = 'F02-job-stopped-after-a-normal-run'; tests = $fake; edits = @(, @($CLI,
      "        if job is not None:`n            job.close()  # a normal end, whatever the exit code: the job is closed and never stopped",
      "        if job is not None:`n            job.stop()`n            job.close()  # a normal end, whatever the exit code: the job is closed and never stopped")) },
  @{ id = 'F03-count-before-stop'; tests = $fake; edits = @(, @($CLI,
      "            job.stop()  # the first step; it can be repeated safely",
      "            job.live_count()`n            job.stop()  # the first step; it can be repeated safely")) },
  @{ id = 'F04-count-above-zero-drops-the-warning'; tests = $fake; edits = @(, @($CLI,
      "    return job.stop_succeeded and count == 0", "    return job.stop_succeeded and count is not None")) },
  @{ id = 'F05-breakaway-ok-set'; tests = $real; edits = @(, @($JOB, $createOld, $setlimit.Replace('FLAGS', '0x800'))) },
  @{ id = 'F06-resume-before-assignment'; tests = $fake; edits = @(, @($JOB,
      "            assigned = bool(native.assign(handle, process))  # step 3",
      "            native.resume(process)`n            assigned = bool(native.assign(handle, process))  # step 3")) },
  @{ id = 'F07-assignment-skipped-and-reported-as-done'; tests = $fake; edits = @(, @($JOB,
      "            assigned = bool(native.assign(handle, process))  # step 3", "            assigned = True  # step 3")) },
  @{ id = 'F08-stop-through-the-wrong-handle'; tests = $fake; edits = @(, @($JOB,
      "ok = bool(self._native.terminate(handle, 1))", "ok = bool(self._native.terminate(handle + 1, 1))")) },
  @{ id = 'F09-close-skipped-on-an-ordinary-error'; tests = $fake; edits = @(, @($CLI,
      "_kill_and_reap(proc, interruptible=True, job=job)  # closes the job, never stops it",
      "_kill_and_reap(proc, interruptible=True, job=None)  # closes the job, never stops it")) },
  @{ id = 'F10-first-interruption-lost-in-the-time-out-handler'; tests = $fake; edits = @(, @($CLI,
      "        _kill_and_reap(proc, job=job, stop_job=True, deadline=deadline)`n        raise`n`n`ndef _resolve_revision",
      "        _kill_and_reap(proc, job=job, stop_job=True, deadline=deadline)`n        raise RuntimeError('replaced')`n`n`ndef _resolve_revision")) },
  @{ id = 'F11-failed-query-read-as-zero'; tests = $fake; edits = @(, @($JOB,
      "        except Exception:  # noqa: BLE001`n            return None`n        return None if count is None else int(count)",
      "        except Exception:  # noqa: BLE001`n            return 0`n        return 0 if count is None else int(count)")) },
  @{ id = 'F12-failed-stop-with-zero-count-no-warning'; tests = $fake; edits = @(, @($CLI,
      "    return job.stop_succeeded and count == 0", "    return count == 0")) },
  @{ id = 'F13-timeout-cause-lost'; tests = $fake; edits = @(, @($CLI,
      "        _close_job(job)`n        raise error from timed_out", "        _close_job(job)`n        raise error")) },
  @{ id = 'F14-count-polling-window-longer-than-two-seconds'; tests = $fake; edits = @(, @($JOB,
      "        limit = deadline.cut(COUNT_SECONDS)", "        limit = deadline.cut(CLEANUP_SECONDS)")) },
  @{ id = 'F15-stop-missing-from-the-ordinary-error-fallback'; tests = $fake; edits = @(, @($CLI,
      "_kill_and_reap(proc, interruptible=True, job=job, stop_job=True, close_job=False, deadline=deadline)",
      "_kill_and_reap(proc, interruptible=True, job=job, stop_job=False, close_job=False, deadline=deadline)")) },
  @{ id = 'F16-stop-missing-from-the-interruption-fallback'; tests = $fake; edits = @(, @($CLI,
      "        _kill_and_reap(proc, job=job, stop_job=True, deadline=deadline)`n        raise`n`n`ndef _resolve_revision",
      "        _kill_and_reap(proc, job=job, stop_job=False, deadline=deadline)`n        raise`n`n`ndef _resolve_revision")) },
  @{ id = 'F16b-stop-missing-from-the-interruption-branch-of-the-error-fallback'; tests = $fake; edits = @(, @($CLI,
      "            _kill_and_reap(proc, job=job, stop_job=True, deadline=deadline)`n            raise",
      "            _kill_and_reap(proc, job=job, stop_job=False, deadline=deadline)`n            raise")) },
  @{ id = 'F17-call-through-a-handle-after-a-successful-close'; tests = $fake; edits = @(, @($JOB,
      "        if closed:`n            self._handle = None", "        if closed:`n            pass")) },
  @{ id = 'F18-close-moved-to-a-bare-outer-finally'; tests = $fake; edits = @(
      @($CLI, "        if job is not None:`n            job.close()  # a normal end, whatever the exit code: the job is closed and never stopped`n", ""),
      @($CLI, "        _kill_and_reap(proc, job=job)`n        raise`n    return proc.returncode, out or `"`", err or `"`"",
              "        _kill_and_reap(proc, job=job)`n        raise`n    finally:`n        if job is not None:`n            job.close()`n    return proc.returncode, out or `"`", err or `"`"")) },
  @{ id = 'F19-nonzero-resume-status-taken-as-success'; tests = $fake; edits = @(, @($JOB,
      "    if status != 0:", "    if status < 0:")) },
  @{ id = 'F20-process-list-call-added'; tests = $fake; edits = @(, @($JOB,
      "            ok = bool(self._native.terminate(handle, 1))",
      "            with contextlib.suppress(Exception):`n                self._native.snapshot()`n            ok = bool(self._native.terminate(handle, 1))")) },
  @{ id = 'F21-count-after-the-close-in-the-ordinary-error-fallback'; tests = $fake; edits = @(, @($CLI,
      "            confirmed = _job_confirmed(job, deadline)`n            _close_job(job)",
      "            _close_job(job)`n            confirmed = _job_confirmed(job, deadline)")) },
  @{ id = 'F22-wall-clock-read'; tests = $fake; edits = @(, @($JOB,
      "        self._end = _monotonic() + seconds", "        time.time()`n        self._end = _monotonic() + seconds")) },
  @{ id = 'F23-close-skipped-after-a-confirmed-time-out'; tests = $fake; edits = @(, @($CLI,
      "        _close_job(job)`n        raise error from timed_out", "        raise error from timed_out")) },
  @{ id = 'F24-handle-kept-after-an-interruption-in-the-native-close'; tests = $fake; edits = @(, @($JOB,
      "        except BaseException:`n            self._handle = None`n            raise", "        except BaseException:`n            raise")) },
  @{ id = 'F25-reap-not-cut-to-the-deadline'; tests = $fake; edits = @(, @($CLI,
      "out, err = proc.communicate(timeout=5 if deadline is None else deadline.cut(5))",
      "out, err = proc.communicate(timeout=5)")) },
  @{ id = 'F26-wait-not-cut-to-the-deadline'; tests = $fake; edits = @(, @($CLI,
      "                    proc.wait(timeout=25 if deadline is None else deadline.cut(25))",
      "                    proc.wait(timeout=25)")) },
  @{ id = 'F27-hand-over-cleanup-does-not-stop-the-job'; tests = $fake; edits = @(, @($CLI,
      "    _kill_and_reap(proc, job=job, stop_job=True)", "    _kill_and_reap(proc, job=job)")) },
  @{ id = 'F28-process-listing-call-named-in-the-job-code-only-the-static-guard-sees'; tests = $static; edits = @(, @($JOB,
      "        return bool(self._kernel.CloseHandle(handle))",
      "        self._kernel.OpenProcess(0x1000, False, 0)  # planted`n        return bool(self._kernel.CloseHandle(handle))")) },
  @{ id = 'F29-default-cleanup-does-not-stop-the-job'; tests = $fake; edits = @(, @($JOB,
      "            job.stop()`n    with contextlib.suppress(BaseException):`n        proc.kill()", "            pass`n    with contextlib.suppress(BaseException):`n        proc.kill()")) },
  @{ id = 'F30-hand-over-return-outside-the-setup-cleanup'; tests = $fake; edits = @(, @($JOB,
      "        return proc, job`n    except BaseException:`n        clean_up()`n        raise`n",
      "    except BaseException:`n        clean_up()`n        raise`n    return proc, job`n")) },
  @{ id = 'F31-cleanup-clock-read-before-the-protected-sequence'; tests = $fake; edits = @(
      @($CLI, "            deadline = git_job.Deadline()`n            job.stop()", "            job.stop()"),
      @($CLI, "    deadline = None`n    try:`n        if job is not None:`n            from agenttalk import git_job`n",
              "    deadline = None`n    if job is not None:`n        from agenttalk import git_job`n`n        deadline = git_job.Deadline()`n    try:`n        if job is not None:`n")) },
  @{ id = 'F32-deadline-not-checked-before-each-count-query'; tests = $fake; edits = @(, @($JOB,
      "            if deadline.left() <= 0:  # before each query: the absolute limit, whatever the last sleep really took`n                return None`n", "")) },
  @{ id = 'F33-late-count-answer-accepted'; tests = $fake; edits = @(, @($JOB,
      "            if deadline.left() <= 0:  # before the answer is accepted: a query that returned late does not count`n                return None`n", "")) },
  @{ id = 'F34-scheduler-half-of-the-completion-rule-skipped'; tests = $sched; edits = @(, @($SCRIPT,
      "    if (-not (Test-SchedulerDone -Exists `$Exists -State `$State)) {", "    if (`$false) {")) },
  @{ id = 'F35-any-state-but-running-or-queued-taken-as-finished'; tests = $sched; edits = @(, @($SCRIPT,
      "    return (`$Exists -and (`$State -in `$FinishedStates))", "    return (`$Exists -and (`$State -notin @('Running', 'Queued')))")) },
  @{ id = 'F36-task-mode-keeps-error-preference-stop'; tests = $sched; edits = @(, @($SCRIPT,
      "        `$ErrorActionPreference = 'Continue'`n", "")) },
  @{ id = 'F37-marker-ignored-so-real-lane-tests-run-without-a-job'; tests = $lane; edits = @(, @($CONF,
      "    if request.node.get_closest_marker(`"real_git_job`") or request.node.get_closest_marker(`"windows_capability`"):",
      "    if request.node.get_closest_marker(`"windows_capability`"):")) },
  @{ id = 'F38-arguments-file-written-as-ascii'; tests = $sched; edits = @(, @($SCRIPT,
      "    [IO.File]::WriteAllText(`$Path, (`$Values | ConvertTo-Json), (New-Object Text.UTF8Encoding(`$true)))",
      "    (`$Values | ConvertTo-Json) | Set-Content -LiteralPath `$Path -Encoding ascii")) },
  @{ id = 'F39-a-failed-deletion-of-the-task-ignored'; tests = $sched; edits = @(, @($SCRIPT,
      "            `$judged = Resolve-Cleanup -Judged `$judged -DeleteExit `$deleteExit -StillExists `$stillThere -Unreadable `$unreadable -Name `$TaskName -StopNotConfirmed `$stop`n", "")) },
  @{ id = 'F40-fault-runner-checks-only-the-product-files'; tests = $runner; edits = @(, @($RUNNERFILE,
      "ForEach-Object { `$_[0] } | Sort-Object -Unique)",
      "ForEach-Object { `$_[0] } | Where-Object { `$_ -like 'src/*' } | Sort-Object -Unique)")) },
  @{ id = 'F41-fault-runner-does-not-stop-when-git-cannot-be-read'; tests = $runner; edits = @(
      @($RUNNERFILE, "if (`$LASTEXITCODE -ne 0) { throw `"Refusing to start: git could not list", "if (`$false) { throw `"Refusing to start: git could not list"),
      @($RUNNERFILE, "if (`$LASTEXITCODE -ne 0) { throw `"Refusing to start: git status failed", "if (`$false) { throw `"Refusing to start: git status failed"),
      @($RUNNERFILE, "if (`$tracked.Count -ne `$targets.Count) {", "if (`$false) {")) },
  @{ id = 'F42-final-count-does-not-expect-a-missing-deadline'; tests = $fake; edits = @(, @($CLI,
      "    if job is None or deadline is None:  # no shared deadline exists (the clock call failed): nothing can be confirmed`n        return False`n",
      "    if job is None:`n        return False`n")) },
  @{ id = 'F43-every-scheduler-error-read-as-a-task-that-is-gone'; tests = $sched; edits = @(, @($SCRIPT,
      "        if (`$_.FullyQualifiedErrorId -ne `$TaskNotFoundId) { `$none.unreadable = `$true }", "        if (`$false) { `$none.unreadable = `$true }")) },
  @{ id = 'F44-cleanup-ignores-an-unreadable-scheduler'; tests = $sched; edits = @(, @($SCRIPT,
      "    if (-not `$StillExists -and -not `$Unreadable) { return `$Judged }", "    if (-not `$StillExists) { return `$Judged }")) },
  @{ id = 'F45-task-deleted-printed-for-an-unreadable-scheduler'; tests = $sched; edits = @(, @($SCRIPT,
      "            `$gone = -not `$stillThere -and -not `$unreadable", "            `$gone = -not `$stillThere")) },
  @{ id = 'F46-preflight-registers-although-the-scheduler-cannot-be-read'; tests = $sched; edits = @(, @($SCRIPT,
      "    if ([bool]`$before.unreadable) {", "    if (`$false) {")) },
  @{ id = 'F47-a-failed-cleanup-overrides-the-runs-own-code'; tests = $sched; edits = @(, @($SCRIPT,
      "    `$code = if (`$Judged.code -eq 0) { 4 } else { `$Judged.code }", "    `$code = 4")) },
  @{ id = 'F48-fault-runner-writes-pytests-summary-line-again'; tests = $runner; edits = @(, @($RUNNERFILE,
      "    `$lines += `$line`n", "    `$lines += `"`$line -- `$(`$log | Select-Object -Last 1)`"`n")) },
  @{ id = 'F49-task-not-found-recognised-by-the-error-category-again'; tests = $sched; edits = @(, @($SCRIPT,
      "        if (`$_.FullyQualifiedErrorId -ne `$TaskNotFoundId) { `$none.unreadable = `$true }",
      "        if (`$_.CategoryInfo.Category -ne 'ObjectNotFound') { `$none.unreadable = `$true }")) },
  @{ id = 'F50-no-error-and-no-answer-read-as-a-confirmed-absence'; tests = $sched; edits = @(, @($SCRIPT,
      "    if (-not `$task) { `$none.unreadable = `$true; return `$none }", "    if (-not `$task) { return `$none }")) },
  @{ id = 'F51-a-wrong-task-not-found-error-id'; tests = $sched; edits = @(, @($SCRIPT,
      "`$TaskNotFoundId = 'CmdletizationQuery_NotFound_TaskName,Get-ScheduledTask'",
      "`$TaskNotFoundId = 'CmdletizationQuery_NotFound_TaskNam,Get-ScheduledTask'")) },
  @{ id = 'F52-fault-runner-does-not-refuse-a-hidden-target'; tests = $runner; edits = @(, @($RUNNERFILE,
      "`$hidden = @(`$flags | Where-Object { `$_ -and `$_[0] -cne 'H' } | ForEach-Object { `$_.Substring(2) })", "`$hidden = @()")) },
  @{ id = 'F53-fault-runner-does-not-check-that-a-file-was-put-back'; tests = $runner; edits = @(, @($RUNNERFILE,
      "      if (-not (Test-SameBytes `$p `$originals[`$p])) {", "      if (`$false) {")) },
  @{ id = 'F54-a-failed-delete-with-the-task-absent-says-deleted'; tests = $sched; edits = @(, @($SCRIPT,
      "            if (`$gone -and `$deleteExit -ne 0) {", "            if (`$false) {")) },
  @{ id = 'F55-the-exit-code-of-end-ignored-again'; tests = $sched; edits = @(, @($SCRIPT,
      "    if (`$EndExit -ne 0 -and -not `$wasReady) {", "    if (`$false) {")) }
)

# Preflight: every file that any selected fault changes (and restores with git checkout) must be tracked and clean.
$selected = @($faults | Where-Object { -not $Only -or $_.id -eq $Only })
if ($selected.Count -eq 0) { throw "No fault has the id '$Only'." }
$targets = @($selected | ForEach-Object { $_.edits } | ForEach-Object { $_[0] } | Sort-Object -Unique)
$tracked = @(git ls-files -- @targets 2>&1)
if ($LASTEXITCODE -ne 0) { throw "Refusing to start: git could not list the files a fault changes (exit $LASTEXITCODE): $($tracked -join ' ')" }
if ($tracked.Count -ne $targets.Count) { throw "Refusing to start: not every file a fault changes is tracked by git: $($targets -join ', ')" }
# `git status` does not see an edit in a file that is marked assume-unchanged (`h` in `git ls-files -v`) or skip-worktree
# (`S`, or `s` with both): a checkout would discard that edit, or (skip-worktree) would leave the planted fault in the file.
$flags = @(git ls-files -v -- @targets 2>&1)
if ($LASTEXITCODE -ne 0) { throw "Refusing to start: git could not show the flags of the files a fault changes (exit $LASTEXITCODE): $($flags -join ' ')" }
$hidden = @($flags | Where-Object { $_ -and $_[0] -cne 'H' } | ForEach-Object { $_.Substring(2) })
if ($hidden.Count -gt 0) {
  throw "Refusing to start: git is told not to look at these files (assume-unchanged or skip-worktree), so an edit in them would not show and could be lost or left behind: $($hidden -join ', '). Undo it with git update-index --no-assume-unchanged / --no-skip-worktree first."
}
$status = @(git status --porcelain -- @targets 2>&1)
if ($LASTEXITCODE -ne 0) { throw "Refusing to start: git status failed (exit $LASTEXITCODE), so uncommitted edits in the files a fault changes cannot be ruled out: $($status -join ' ')" }
if ($status.Count -gt 0) { throw "Refusing to start: these files, which a fault changes and restores with git checkout, have uncommitted edits: $($targets -join ', '). Commit or stash them first." }

function Get-Counts($XmlPath) {  # numbers only, from pytest's JUnit file: no test name or failure text; else unreadable
  $none = @{ readable = $false }
  if (-not (Test-Path -LiteralPath $XmlPath)) { return $none }
  try { $suite = @(([xml](Get-Content -LiteralPath $XmlPath -Raw)).testsuites.testsuite)[0]; if ($null -eq $suite) { return $none } } catch { return $none }
  $n = @{ readable = $true; assertion = 0; exception = 0 }
  foreach ($key in 'tests', 'failures', 'errors', 'skipped') {
    $value = 0
    if (-not [int]::TryParse([string]$suite.GetAttribute($key), [ref]$value)) { return $none }
    $n[$key] = $value
  }
  foreach ($case in @($suite.testcase | Where-Object { $_.failure })) {
    if ([string]$case.failure.message -match '^(assert|AssertionError|Failed:)') { $n.assertion++ } else { $n.exception++ }
  }
  return $n
}

function Test-SameBytes([string]$Path, [byte[]]$Expected) {
  if (-not (Test-Path -LiteralPath $Path)) { return $false }
  $now = [IO.File]::ReadAllBytes($Path)
  return [Linq.Enumerable]::SequenceEqual([byte[]]$now, [byte[]]$Expected)
}
$notRestored = @()
$lines = @("planted faults of #907: one result line each (caught = at least one test failed; only counts are kept, no pytest text)")
$ran = 0  # the faults whose tests actually ran
foreach ($f in $faults) {
  if ($Only -and $f.id -ne $Only) { continue }
  $files = @{}
  $originals = @{}  # the bytes of each file as it was, kept so that putting it back can be checked
  $planted = $true
  foreach ($e in $f.edits) {
    $path = Join-Path $Repo $e[0]
    if (-not $files.ContainsKey($path)) { $files[$path] = [IO.File]::ReadAllText($path); $originals[$path] = [IO.File]::ReadAllBytes($path) }
    $text = $files[$path]
    $crlf = $text.Contains("`r`n")
    # A fault's text carries the line endings of THIS file (CRLF after a Windows checkout): compare in LF, convert per target.
    $wantOld = $e[1].Replace("`r`n", "`n"); $wantNew = $e[2].Replace("`r`n", "`n")
    $oldText = if ($crlf) { $wantOld.Replace("`n", "`r`n") } else { $wantOld }
    $newText = if ($crlf) { $wantNew.Replace("`n", "`r`n") } else { $wantNew }
    $first = $text.IndexOf($oldText)
    if ($first -lt 0 -or $text.IndexOf($oldText, $first + 1) -ge 0) { $planted = $false; break }
    $files[$path] = $text.Substring(0, $first) + $newText + $text.Substring($first + $oldText.Length)
  }
  if (-not $planted) { $lines += "$($f.id): NOT PLANTED (the text to change was not found exactly once)"; continue }
  try {
    foreach ($p in $files.Keys) { [IO.File]::WriteAllText($p, $files[$p]) }
    $compiled = $true
    foreach ($p in $files.Keys) {
      if ($p -like '*.py') { & $Python -m py_compile $p 2>&1 | Out-Null; if ($LASTEXITCODE -ne 0) { $compiled = $false } }
    }  # only Python sources are compiled here; a script is parsed when its tests run it
    if (-not $compiled) { $lines += "$($f.id): DOES NOT COMPILE"; continue }
    $xml = Join-Path ([IO.Path]::GetTempPath()) ("faults907-" + [guid]::NewGuid().ToString('N') + ".xml")
    $log = & $Python -m pytest @($f.tests) -q --no-header --tb=no -rfE -p no:cacheprovider "--basetemp=$BaseTemp" "--junitxml=$xml" 2>&1
    $c = Get-Counts $xml
    if (Test-Path -LiteralPath $xml) { Remove-Item -LiteralPath $xml }
    $verdict = if (-not $c.readable) { 'NO RESULT' } elseif ($c.tests - $c.skipped -le 0) { 'NO TESTS RAN' } elseif ($c.errors -gt 0) { 'ERROR (not a catch)' } elseif ($c.failures -gt 0) { 'CAUGHT' } else { 'NOT CAUGHT' }
    $line = "$($f.id): $verdict"  # only the runner's own fault id, fixed words and numbers
    if ($c.readable) {
      $line += " -- ran $($c.tests), passed $($c.tests - $c.failures - $c.errors - $c.skipped), failed $($c.failures), errors $($c.errors), skipped $($c.skipped); failed by assertion $($c.assertion), by exception $($c.exception)"
      if ($c.tests - $c.skipped -gt 0) { $ran++ }
    }
    $lines += $line
  } finally {
    foreach ($p in $files.Keys) {
      git checkout -- ($p.Substring($Repo.Length).TrimStart('\', '/')) 2>&1 | Out-Null
      if (-not (Test-SameBytes $p $originals[$p])) {
        # git checkout did not give the file back (a flag set during the run, a locked file): write the saved bytes and look again
        [IO.File]::WriteAllBytes($p, $originals[$p])
        if (Test-SameBytes $p $originals[$p]) { $lines += "NOTE: $($f.id): git checkout did not restore $($p.Substring($Repo.Length).TrimStart('\', '/')); the saved original was written back and checked" }
        else { $notRestored += $p.Substring($Repo.Length).TrimStart('\', '/') }
      }
    }
  }
}
if ($notRestored.Count -gt 0) {
  $lines += "NOT PUT BACK: $($notRestored -join ', ') could not be restored to the original bytes; check them with git diff before anything else"
  Set-Content -LiteralPath $Out -Value $lines -Encoding utf8
  throw "A planted fault could not be taken out of: $($notRestored -join ', ')"
}
if ($ran -eq 0) {
  $lines += 'no selected fault was actually run: nothing was tested'
  Set-Content -LiteralPath $Out -Value $lines -Encoding utf8
  exit 1
}
$lines += 'done; every source file was put back (checked byte for byte against the original)'
Set-Content -LiteralPath $Out -Value $lines -Encoding utf8
