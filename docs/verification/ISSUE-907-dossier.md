# Verification dossier — issue #907 (Windows: a timed-out Git write now stops the programs Git started)

**Status:** OPEN, pre-merge. Written while the change is under review; nothing here is a merge claim.

**Form followed.** [`PR-132-dossier.md`](PR-132-dossier.md), the one older dossier in this folder: the same seven
sections (claims, method per claim with "executed or referenced-only", evidence pointers, round history, decision
record, residuals, reproduction). It lives in the same folder, `docs/verification/`. That dossier is named for its
pull request; this one is named for the issue because the pull request number does not exist yet. Rename it to
`PR-<number>-dossier.md` when it does. The supporting files sit in [`907/`](907/).

## In plain words

When agenttalk changes a repository with Git on Windows (adding or removing a work folder, for example) it gives Git a
time limit. Until now, running out of time stopped only the first Git process. Programs that Git had started for that
command, such as a hook or a content filter, could keep running, and the error could only warn about that.

Now, on Windows, each changing Git command is started inside its own Windows "job": a group that Windows keeps the
list of itself. Everything Git starts joins the group. On a time-out agenttalk stops the whole group with one call,
checks that nothing in it is left, and only then says that Git and the programs in its job were stopped. A normal run is
never stopped. If the group cannot be used, the earlier behaviour and warning remain. Nothing reads a list of processes,
a process id or a start time to decide whom to stop.

This page answers one question for a reader who does not trust the author: for each thing the change claims, how was it
checked, what did the check show, and what was **not** checked. Where evidence exists only on the machine that produced
it, the files in `907/` hold it, with every path, user name and temporary folder removed.

**What this page does not do.** It does not repeat the design or the code review. The design is in the issue; the code is
in the branch. It also does not claim a result it did not produce: the runs listed in §6 as open evidence have not been
done, and the claims that depend on them say so.

---

## 1. Claims made

| ID | Claim | Source |
|----|-------|--------|
| C-1 | On Windows, when a changing Git write times out, Git **and the programs it started** (a hook, a content filter, and their children) are stopped. | CHANGELOG entry for #907; `agenttalk/git_job.py` module docstring |
| C-2 | The error says so ("Git and the programs in its Windows job were stopped") only when the stop reported success **and** the job's live count was then read as 0. A count that could not be read is never taken as 0; a failed stop with a count of 0 still warns. | `_job_confirmed` in `cli.py`; `GitJob.live_count` |
| C-3 | A normal write is never stopped. The job sets no limits (no kill-on-close, no breakaway), so a helper Git starts on purpose, such as its file-system monitor, keeps running after a normal write, whatever Git's exit code. | `git_job.py` docstring; `_git_write` |
| C-4 | Nothing reads a process list, a process id, a parent or a creation time, and no clock decides whom to stop. The only clock use is a limit on how long cleanup waits. | issue #907 design rule; `git_job.py` docstring |
| C-5 | Ownership is exact. The job handle is closed on every exit path (start failure, refused assignment, refused resume, an interruption at any step, a normal end, a time-out); it is never used after a successful close; the first interruption is the one raised and a later one is absorbed; an interruption right after Git was resumed, down to the very return of `start_in_job` that hands the launcher and the job over, stops the job as well as the launcher. | `GitJob`, `start_in_job`, `_kill_and_reap` |
| C-6 | A job-enabled time-out has one cleanup deadline (30 seconds from the moment the write's own time-out fires). No wait runs past it, and the count is polled for at most 2 seconds, cut to what is left. The clock that starts the cleanup is read inside the protected time-out sequence, and a count that is read, or that comes back, after the deadline is never taken as a confirmation. | `Deadline`, `_raise_git_write_timeout`, `GitJob.count_after_stop` |
| C-7 | Without a job (job refused, assignment refused, Linux, macOS) the write runs exactly as before, with the same steps, times and warning. **One behaviour changes:** if `NtResumeProcess` is missing, fails or is refused, the write now fails to start where the earlier code simply ran Git. | `tests/test_git_write_job_907.py` |
| C-8 | A program in the job that asks to break away does not leave it: Windows refuses, or, under an outer job that allows breakaway, lets the request through and the program stays in this job and ends with its stop. | `tests/test_git_job_windows.py` |
| C-9 | On a real Windows host Git can be started paused, put in a new job through the handle `Popen` already holds, resumed through the same handle, and run, also when the test process is already in another job. | `tests/test_git_job_capability.py` |
| C-10 | The scheduled-task host check (`scripts/check_git_job_scheduled_task.ps1`) accepts only fresh, finished, good evidence: it refuses a missing, empty, another run's or partial log, a non-zero exit, and an exit of 0 without the host facts line. In a full run it also needs the scheduler to show the task as Ready: a time limit, a task that is Running, Queued, Disabled or unknown, or a task the scheduler cannot show, is never accepted, whatever the log holds. Task mode does not stop on a line written to stderr. Its small arguments file keeps every character of a path (UTF-8 with a byte order mark: a folder named with an accent came back with a question mark before). A task is known to be gone only when the scheduler answers that there is no such task: a task that is still there after the delete, or a scheduler that could not be asked (an error other than "no such task"), makes an accepted run exit 4, with the accepted log kept and the way to look for and delete the task by hand printed; a run that was not accepted keeps its own code (2 or 3) and the same warning is printed; a scheduler that cannot be read before anything is registered stops the run with nothing registered. | `tests/test_git_job_scheduled_task_check.py` |
| C-12 | The planted-fault runner never discards an edit it did not make. It refuses to start when any file that a selected fault changes (the list is derived from the faults themselves) has uncommitted edits or is untracked, or when git cannot be read; it keeps one row for every failing test (none left out) with the first line of its failure, and flags a failure that is an exception at call time. | `907/faults.ps1`, `tests/test_git_job_fault_runner.py` |
| C-11 | Every test of another subject keeps the old no-job path (so they stay about their own subject), while the product's default job path is exercised by real tests: the real-Windows tests, a real lane assignment and a real rollback that removes the checkout. | `tests/conftest.py`; `tests/test_git_job_windows.py`; `tests/test_lanes.py` |

---

## 2. Verification method per claim

| Claim | Method | Executed or referenced-only? |
|-------|--------|-------------------------------|
| C-1 | Real Windows, real Git: a post-checkout hook and a content filter each write their process id to a ready file, then wait. The test opens a handle on that id while the program is known to be alive, lets the write time out, and requires the handle to be signalled within 5 seconds and the job's live count (read through a test seam just before the close) to be 0. Red on the old code (the hook and the filter were still running 5 seconds after the time-out), green now. | Executed on one machine (see `907/host-facts.txt`) |
| C-2 | Fake Windows layer, so each case can be forced: success wording needs a successful stop and a count of 0; a query that fails, or answers nothing, keeps the warning; a failed stop with a count of 0 keeps the warning; a count that falls from above 0 to 0 within the deadline gives the success wording; a count that stays above 0 keeps the warning. Planted faults F04, F11, F12. | Executed |
| C-3 | Real: the job has no limit flags (read through the job's own handle); a file-system monitor started by a normal write is still running after the close. Fake: a normal end, a non-zero Git exit, an ordinary error and an interruption before any time-out all close without stopping. Planted faults F01, F02, F05. | Executed |
| C-4 | The fake layer raises if the product asks for a snapshot, an open by id or a process time; `git_job.time` is replaced so any wall-clock read fails; the nine "unrelated process" histories from the issue run as one guarded test. A **static** test reads the module and the time-out functions and fails on any of seven process-walking calls, which also catches a direct `ctypes` call the fake never sees. Planted faults F20, F22, F28. | Executed |
| C-5 | A matrix test (`test_the_close`) has 18 cases: 6 with an ordinary failure of the close (one per pending outcome: a normal result, a time-out, an ordinary error, an error after the time-out, an interruption after the time-out, an interruption before it); 8 with a first interruption, before or after the native close, for the four pending outcomes that are not interruptions; and 4 with a second interruption, before or after, for the two pending interruptions. It checks what is raised, how many stops and closes happen, and that nothing is called through a closed handle (the fake records any such call). Start-failure tests cover each step of `start_in_job`. The hand-over is tested at its real point: a trace hook raises the interruption at the line `return proc, job`, after Git was resumed, through the module and through the write, and requires resume, then stop, then close. The clock that starts the cleanup is interrupted the same way. Planted faults F06, F07, F09, F10, F17, F18, F23, F24, F27, F29, F30, F31. | Executed |
| C-6 | A fake monotonic clock: an exhausted deadline skips the waits and the count and keeps the warning; no wait is asked for longer than the time left; the count is polled to the 2 second window and no further. With 0.01 s left, counts [1, 0] and a sleep that resumes a second late, no second query is made and the error does not say "stopped"; a query that itself returns after the limit is not accepted; a count read in time after an exact sleep still is. Planted faults F14, F25, F26, F32, F33. | Executed |
| C-7 | Fake layer and fake launcher: the no-job Windows steps and times (5 second reap, 25 second wait), the POSIX steps and "and was killed" wording, the fallback rows (failed stop still kills; refused creation; refused assignment). The resume behaviour change is shown by the tests of a refused resume. | Executed |
| C-8 | Real: a child of a program in our job asks Windows to break away. The test accepts a refusal (error 5) or a grant, and in the grant case requires the child to be a member of OUR job (asked through our job's own handle) and to end with our stop. Same for a program that makes its own inner job that allows breakaway. | Executed on one machine. On that machine the request was **granted** (its outer job allows breakaway) and the child stayed in our job. The refusal (error 5) was seen only in earlier design probes on hosts whose outer job does not allow breakaway; this build did not observe it. |
| C-9 | The capability check, run before any product change, then kept as a test. It prints its facts as one warning line, so every run's log keeps them. | Executed on one machine; other hosts are open evidence (§6) |
| C-10 | The verifier is run on made-up logs under both Windows PowerShell 5.1 and PowerShell 7 (ten logs, in `tests/test_git_job_scheduled_task_check.py` and again by hand in `907/verifier-probes.txt`). `-DryRun` prints the two registrations (run 1 with `/IT`, run 2 without it and with `/RP *`) and registers nothing. The whole completion rule is one function, run by `-Mode Decide` on a made-up scheduler state: a finished log with the task Running, Queued, Disabled, Unknown, with no state, or not shown by the scheduler is refused (exit 2), and so are a partial or another run's log; a non-zero exit or no facts line is rejected (exit 3); only a good log with the task Ready is accepted. Task mode is run under both PowerShell editions with a stand-in interpreter that writes one line to stderr (Windows PowerShell 5.1 used to stop there, so the log never got its last line). Planted faults F34, F35, F36. The arguments file is written by `-Mode WriteArgs` (the function `-Mode Run` uses) and read by Task mode, under both editions, with a folder named `café über`: its bytes start with a byte order mark and the path comes back unchanged; the previous ASCII file lost the accent (planted fault F38). `-Mode Run` itself, the wait and the cleanup included, is run against a stand-in scheduler (a tests-only hook replaces the two functions that talk to the scheduler): a good run is accepted, its log copied and its task deleted; a task that cannot be deleted gives exit 4 with the accepted log kept and the deletion command printed (planted fault F39); a finished log with the task shown as Running until the time limit gives exit 2 and no accepted copy. The task runs themselves were **not** done. | Verifier, decision rule, Task mode, `-Mode Run` against a stand-in scheduler and dry run: executed. The two real scheduled-task runs: **not done** (§6) |
| C-12 | The runner is run in a throw-away git repository (12 tests, under both PowerShell editions): an uncommitted edit of the scheduled-task script, of the test configuration, of a product file, or of any file when every fault is selected makes it refuse to start (it names the file and leaves the edit untouched, and writes no results); a folder that is not a repository makes it refuse; an uncommitted edit of a file that no selected fault changes does not stop it, the fault is planted and put back, and the edit is still there. Planted faults F40 (the check back on only the two product files) and F41 (git failures not stopping it). The failure lines it keeps are in `907/faults-results.txt`. | Executed |
| C-11 | Reasoned in §5 (decision D-6), plus real tests of the default path with nothing replaced: a plain write through the product, a real lane assignment and a real rollback that removes the checkout (each ends by asserting that a job was started and closed). Planted fault F37 (the marker ignored). | Executed |

---

## 3. Evidence pointers

| What | Where |
|------|-------|
| The planted faults: the runner, and one result line each | [`907/faults.ps1`](907/faults.ps1), [`907/faults-results.txt`](907/faults-results.txt) |
| Red first: the new tests against the old time-out code | [`907/red-first.txt`](907/red-first.txt) |
| The verifier's rejection probes and the dry run | [`907/verifier-probes.txt`](907/verifier-probes.txt) |
| The host facts line of the machine that produced this evidence | [`907/host-facts.txt`](907/host-facts.txt) |
| The job module | `src/agenttalk/git_job.py` |
| The wiring | `src/agenttalk/cli.py`: `_git_write`, `_raise_git_write_timeout`, `_kill_and_reap`, `_reap_after_failed_start` |
| Tests on a fake Windows layer (run on every platform) | `tests/test_git_job.py`, `tests/test_git_write_job_907.py`, with the fake in `tests/_git_job_fake.py` |
| Tests on real Windows and real Git | `tests/test_git_job_windows.py` (marker `real_git_job`) |
| The host check | `tests/test_git_job_capability.py` (marker `windows_capability`) |
| The fault runner and its test | `docs/verification/907/faults.ps1`, `tests/test_git_job_fault_runner.py` |
| The scheduled-task check and its test | `scripts/check_git_job_scheduled_task.ps1`, `tests/test_git_job_scheduled_task_check.py` |

**Test counts at the time of writing:** 221 new tests (43 on the job module, 73 on the write and its time-out paths,
9 on real Windows with real Git, 2 on the host check, 80 on the scheduled-task script under two PowerShell editions,
14 on the fault runner);
two existing real lane tests also carry the marker `real_git_job` now. Lint (`ruff check src tests`) and the
security scan (`bandit -r src`) report nothing.

**Red first**, in two parts (`907/red-first.txt` lists every failing test). A test that fails only because a name, a mode
or a wording does not exist in the old code is counted apart: it is a guard or a new interface, not evidence of a
behaviour. Part A: with the old write code put back and the job tests kept, 56 of the new job tests fail, 55 because of
behaviour (including both real Windows tests: the hook and the filter were still running 5 seconds after the time-out)
and 1 because a name is missing (the static guard, which reads functions that did not exist); the nine "unrelated
process" guards pass there, which is what the design says they must do. Part B: with the code, script and test
configuration of the previous head put back, the tests added in the fix round fail: 9 because of behaviour (the hand-over
at its real return, the clock call, the late sleep, the late query, and the stderr line under Windows PowerShell 5.1) and
26 because a name, a mode or a wording does not exist there (the `Decide` mode, the fixture of the real lane tests, and
the dry-run wording). With the change, all pass. Part C (the second delta round): the runner tests against the previous runner: 8 of 12 fail, all because of behaviour (it started and discarded edits, or did not refuse). The five new script tests, run under both editions (10 cases), against the previous script with only a test seam added: 6 of 10 fail, all because of behaviour (the accent was lost; a failed deletion returned 0); against the previous script as it was, all 10 fail because a mode or a parameter did not exist there. Part D (the product change of the second delta round): the test for an ordinary error from the clock call that starts the time-out cleanup fails on the previous `cli.py` with `AttributeError: 'NoneType' object has no attribute 'cut'` and passes with the change. Part E (round 14, `907/red-first.txt`): the 18 new scheduler tests, under both editions, against the previous script: 14 fail, 8 because of behaviour (an unreadable scheduler after the delete, or before registering, was read as a task that is gone or as absent; a failed run with an unreadable scheduler lost its warning) and 6 only because the new `Facts` mode did not exist there; the 4 that pass are the combined cases (a run that failed or has no completion evidence, and a task still shown after the delete), which pin a rule the previous script already followed (the run's own code wins, and the warning is printed); the new runner test (a fault that fails six tests must show six rows) fails on the previous runner under both editions and passes now.

**Planted faults.** 49 faults (F01 to F48, with F16b), each planted alone in one place, each shown to compile (a script
is parsed when its tests run it), to be reached, and to fail at least one test, each then put back: all 49 are caught,
none only as an error. All 49 ran again in one run after the last change (the results file is that run). The results file keeps one row for every failing test (round 13 had kept only the first four; F02 has five rows now), each with the first line of its failure, so each catch can be read: 42 faults fail tests by an assertion only, and 7 (F10, F21, F27, F29, F30, F31, F42) also or only by an exception at call time, which is a weaker catch (F42 only by one: the old code asked for a count with no deadline and failed with an AttributeError, which is exactly the real defect that fault stands for). F14 changed in the fix round: the old fault (polling to the full deadline) became an equivalent
one once the count checks the deadline before each query, so it is now "a polling window longer than two seconds".

---

## 4. Round history (condensed)

1. **A capability check first.** Before any product change, a test asked the one thing the design depends on: can Git be
   started paused, assigned to a job through the handle `Popen` holds, and resumed? It passed here, in a nested job too.
2. **The build** followed the written design (version 3) with two binding points from its last read: close the job only
   after the count in the ordinary-error fallback (with an ordering test), and judge a scheduled-task run only on its own
   completion evidence.
3. **A collection fix.** One test list was built from a set, so two test workers listed the cases in a different order and
   refused the run; it is now a tuple. Found by running two workers, shown by collecting under two hash seeds.
4. **Host facts without a workflow change.** The check reports its facts as one pytest warning of its own class
   (`GitJobHostFacts`), shown in the warnings summary without `-s`, and switched to "always" inside the test so a
   setting that turns warnings into errors cannot hide it.
5. **A first review** found no blocking point and six minor ones, all fixed: a docstring and a changelog sentence, two
   statements in the pull request text, an interruption right after the resume now also stops the job, and a standing
   static guard against process-walking calls.
6. **The scheduled-task script, this dossier and a real test of the default path** followed.
7. **A fix round after two independent reads** (one by an outside reviewer, one internal): an interruption at
   the very return of the start, the clock that starts the time-out cleanup, a count read after the shared deadline, the
   fixture that took real lane flows off the default path, and, in the script, a finished log accepted at the time limit
   without the scheduler's half, `Unknown` and `Disabled` counted as finished, and Windows PowerShell 5.1 stopping on a
   stderr line. Each code fix has a test that fails on the previous head and a planted fault. A further read of the delta
   found the product code right and three points in the new tools.
8. **A second delta round** (mostly tools, with one product change in `cli.py`, named in the CHANGELOG: an ordinary error from the clock call that starts the time-out cleanup used to end the write with an `AttributeError`, because the final count was asked for a limit that did not exist; it now ends with the write's own time-out error and the earlier warning, with a test and the planted fault F42): the fault runner now derives the files it
   must check from the faults and refuses to start on any uncommitted edit in them, or when git cannot be read (it could
   discard an edit in the scheduled-task script or the test configuration before); the arguments file keeps accents; a
   task that cannot be deleted makes the run exit 4; and the runner kept the first line of the failure of the first four failing tests (this said "each" and was corrected in round 14).
9. **A third delta round** (tools only; the product code is unchanged): the scheduled-task check no longer reads a
   scheduler that could not be asked as a task that is gone (it told "no such task" from any other error; a failed delete
   followed by a failed query printed "task deleted: True" and exited 0); a run that failed keeps its own code and still
   prints the warning; the fault runner keeps every failing test's row; and this history is corrected. Run under both
   Windows PowerShell 5.1 and PowerShell 7. A further read is outstanding at the time of writing.

---

## 5. Decision record

- **D-1: One job per write, with no limits.** A job that kills its members when closed would stop a helper Git starts on
  purpose (the file-system monitor) after every normal write. A job that allows breakaway would let a program leave it.
  So the job sets nothing, and the only way anything in it is stopped is the explicit call on a time-out.
- **D-2: Count after the stop and the reap, close last.** A non-empty job read before the launcher is reaped proves nothing.
  After a successful close every later query is a no-op, so the count must come first; the ordinary-error fallback was
  restructured for this, with an ordering test and a planted fault (F21).
- **D-3: A count that cannot be read is not zero.** "Stopped" means the stop succeeded and the count was read as 0. Anything
  else keeps the old warning.
- **D-4: No walk over processes.** Windows keeps the member list. Reading ids, parents or start times would bring back the
  guessing the job exists to remove, so a standing static test and the fake layer both refuse it.
- **D-5: The scheduled task check is a script in `scripts/`, with its verifier tested separately.** `scripts/` already holds
  the project's helper scripts; `tests/` holds tests. The registration needs a person (the password prompt of `/RP *`) and
  cannot run in CI, but the part that decides whether a log is acceptable can, so it is a mode of the same script and is
  tested on every Windows leg without registering anything. The registered command is a short call that names an arguments
  file, because `schtasks` accepts at most 261 characters; the script checks that before it registers.
- **D-6: Why the other tests do not use the job.** An automatic fixture in `tests/conftest.py` makes job creation refuse
  for every test that does not carry the marker `real_git_job`. That is right for three reasons. (a) About twenty
  older test files replace `subprocess.Popen` with stand-ins that have no process handle; a real job would break them for a reason
  that has nothing to do with their subject. (b) The change is Windows only, so turning the job on for every test would
  make the POSIX legs and the Windows legs run different code under the same tests. (c) The product's default path is
  not left untested: the real-Windows tests (marker `real_git_job`) run it with nothing replaced, among them
  `test_a_plain_write_through_the_product_runs_in_a_job_and_ends_with_it_closed`, and those tests are collected on every Windows
  leg, because the pytest commands the project's gate builds carry no marker expression that would deselect them. Two
  representative real flows carry the marker too and end by asserting that a job was used: a real lane assignment and a
  real rollback that removes the checkout (`tests/test_lanes.py`). Narrowing the replacement to only the tests that need
  fakes was not chosen: it would mean finding by hand which of about twenty files replace `Popen`, and a missed one would
  fail for a reason that has nothing to do with its subject. The fixture also leaves the host check alone, so that check
  imports nothing of the product.
- **D-8: A run is complete only on both halves.** The log says what the test did; only the scheduler says whether the task
  has ended. A log that looks finished while the scheduler still shows the task as Running or Queued, or cannot show it, may
  be a stale or partial one, so a wait that ends on its time limit is never accepted on the log alone, and only the state
  Ready (the state a one-time task is in after it ran) counts as finished.
- **D-7: Facts in the test output, not a workflow step.** A workflow change needs an extra approval; a warning line in the
  normal output reaches every leg's existing log with no change to a workflow.

---

## 6. Residuals and declared limitations

**Open evidence (not done here).**

- A host whose test process is in no job. (Here the process was already in a job; a nested job was exercised by starting
  the check inside an outer job of its own, and passed.)
- A service session (session 0). The facts line records the session id.
- The Windows CI legs. Their logs will show the facts line (`GitJobHostFacts: git-job-capability: {...}`).
- Scheduled-task run 1, interactive only (`/IT`), and run 2, without `/IT` (the operator types the password; it is never
  written down). The script, its dry run and its verifier exist and are tested; the registrations were not made.
- Linux, macOS and other Python versions (the fake-layer tests are platform neutral but were not run there), and the full
  two-worker run of the whole suite.

**Declared limitations of the change.**

- A program that another process starts on Git's behalf (a service or a scheduled task) was never in the job and is not
  covered.
- If agenttalk is killed in the short moment between starting Git paused and putting it in the job, a paused Git is left
  behind. The old code had no such moment.
- A missing, failing or refused `NtResumeProcess` now fails the write to start, where the earlier code ran Git. The paused
  Git is stopped and nothing is left running.
- A partial checkout or a Git lock may still need recovery after any time-out.
- Interruption windows outside the start and the time-out handling (for example between `start_in_job` returning and the
  write taking its result, or at the entry of the best-effort cleanup itself) stay as the earlier time-out change stated them.
- `-Mode Run` is run end to end only against a stand-in scheduler (the wait, the judging, the copy and the deletion
  are the real code; the two functions that talk to the scheduler are replaced). A real scheduler may behave in a way the
  stand-in does not, which is why the two real runs stay open evidence.
- On the one machine measured, a direct breakaway request was granted, not refused (§2, C-8). The program still stays in
  our job. The refusal was seen only in earlier design probes.

**What the planted faults do and do not show.** A caught fault shows that some test notices that change in that place. It
does not show that every other fault is caught, and a fault the author did not think of is not in the list.

---

## 7. Reproduction instructions

Run from the repository root, on Windows with Git installed. Nothing below registers a scheduled task.

```text
python -m pytest tests/test_git_job.py tests/test_git_write_job_907.py tests/test_git_job_windows.py ^
    tests/test_git_job_capability.py tests/test_git_job_scheduled_task_check.py tests/test_lanes.py -q
ruff check src tests
python -m bandit -r src -x src/agenttalk/skills
```

The host facts line appears in the warnings summary of the first command (it is the `GitJobHostFacts` line).

Red first: put the write code of the commit before this change back (`git checkout <that commit> -- src/agenttalk/cli.py`),
keep everything else, run the job tests, then `git checkout HEAD -- src/agenttalk/cli.py`. For the fix round, put back the
four files of the previous head (`src/agenttalk/cli.py`, `src/agenttalk/git_job.py`, the script and `tests/conftest.py`) the
same way. The failing tests are listed in `907/red-first.txt`.

The planted faults, on a clone you can throw away (the runner edits tracked files and puts them back; it refuses to start
when any file that a selected fault changes has uncommitted edits, or when git cannot be read):

```text
powershell -NoProfile -File docs/verification/907/faults.ps1 -Python python
```

The scheduled-task check, in two steps. The first registers nothing:

```text
powershell -NoProfile -File scripts/check_git_job_scheduled_task.ps1 -Mode Run -DryRun -Python python -OutDir out
powershell -NoProfile -File scripts/check_git_job_scheduled_task.ps1 -Mode Run -Python python -OutDir out
```

The second registers the task twice and needs a person at the console for the password of run 2; keep `-OutDir` short.
Each run is accepted only on its own completion evidence, and an accepted log is copied next to the others. To judge a
single log without a scheduler: `-Mode Verify -Log <file> -RunId <id>`; to judge it together with a made-up scheduler state
(the whole rule): `-Mode Decide -Log <file> -RunId <id> -TaskExists true -TaskState Ready`.
