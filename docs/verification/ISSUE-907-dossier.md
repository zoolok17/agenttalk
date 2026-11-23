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
| C-5 | Ownership is exact. The job handle is closed on every exit path (start failure, refused assignment, refused resume, an interruption at any step, a normal end, a time-out); it is never used after a successful close; the first interruption is the one raised and a later one is absorbed; an interruption right after Git was resumed stops the job as well as the launcher. | `GitJob`, `start_in_job`, `_kill_and_reap` |
| C-6 | A job-enabled time-out has one cleanup deadline (30 seconds from the moment the write's own time-out fires). No wait runs past it, and the count is polled for at most 2 seconds, cut to what is left. | `Deadline`, `_raise_git_write_timeout` |
| C-7 | Without a job (job refused, assignment refused, Linux, macOS) the write runs exactly as before, with the same steps, times and warning. **One behaviour changes:** if `NtResumeProcess` is missing, fails or is refused, the write now fails to start where the earlier code simply ran Git. | `tests/test_git_write_job_907.py` |
| C-8 | A program in the job that asks to break away does not leave it: Windows refuses, or, under an outer job that allows breakaway, lets the request through and the program stays in this job and ends with its stop. | `tests/test_git_job_windows.py` |
| C-9 | On a real Windows host Git can be started paused, put in a new job through the handle `Popen` already holds, resumed through the same handle, and run, also when the test process is already in another job. | `tests/test_git_job_capability.py` |
| C-10 | The scheduled-task host check (`scripts/check_git_job_scheduled_task.ps1`) accepts only fresh, finished, good evidence: it refuses a missing, empty, another run's or partial log, a non-zero exit, and an exit of 0 without the host facts line. | `tests/test_git_job_scheduled_task_check.py` |
| C-11 | Every test of another subject keeps the old no-job path (so they stay about their own subject), while the product's default job path is exercised by real tests. | `tests/conftest.py`; `tests/test_git_job_windows.py` |

---

## 2. Verification method per claim

| Claim | Method | Executed or referenced-only? |
|-------|--------|-------------------------------|
| C-1 | Real Windows, real Git: a post-checkout hook and a content filter each write their process id to a ready file, then wait. The test opens a handle on that id while the program is known to be alive, lets the write time out, and requires the handle to be signalled within 5 seconds and the job's live count (read through a test seam just before the close) to be 0. Red on the old code (the hook and the filter were still running 5 seconds after the time-out), green now. | Executed on one machine (see `907/host-facts.txt`) |
| C-2 | Fake Windows layer, so each case can be forced: success wording needs a successful stop and a count of 0; a query that fails, or answers nothing, keeps the warning; a failed stop with a count of 0 keeps the warning; a count that falls from above 0 to 0 within the deadline gives the success wording; a count that stays above 0 keeps the warning. Planted faults F04, F11, F12. | Executed |
| C-3 | Real: the job has no limit flags (read through the job's own handle); a file-system monitor started by a normal write is still running after the close. Fake: a normal end, a non-zero Git exit, an ordinary error and an interruption before any time-out all close without stopping. Planted faults F01, F02, F05. | Executed |
| C-4 | The fake layer raises if the product asks for a snapshot, an open by id or a process time; `git_job.time` is replaced so any wall-clock read fails; the nine "unrelated process" histories from the issue run as one guarded test. A **static** test reads the module and the time-out functions and fails on any of seven process-walking calls, which also catches a direct `ctypes` call the fake never sees. Planted faults F20, F22, F28. | Executed |
| C-5 | A matrix test (`test_the_close`, 18 cases: six pending outcomes × before or after the native close × an ordinary failure, a first interruption or a second one) checks what is raised, how many stops and closes happen, and that nothing is called through a closed handle (the fake records any such call). Start-failure tests cover each step of `start_in_job`. Planted faults F06, F07, F09, F10, F17, F18, F23, F24, F27, F29. | Executed |
| C-6 | A fake monotonic clock: an exhausted deadline skips the waits and the count and keeps the warning; no wait is asked for longer than the time left; the count is polled to the 2 second window and no further. Planted faults F14, F25, F26. | Executed |
| C-7 | Fake layer and fake launcher: the no-job Windows steps and times (5 second reap, 25 second wait), the POSIX steps and "and was killed" wording, the fallback rows (failed stop still kills; refused creation; refused assignment). The resume behaviour change is shown by the tests of a refused resume. | Executed |
| C-8 | Real: a child of a program in our job asks Windows to break away. The test accepts a refusal (error 5) or a grant, and in the grant case requires the child to be a member of OUR job (asked through our job's own handle) and to end with our stop. Same for a program that makes its own inner job that allows breakaway. | Executed on one machine. On that machine the request was **granted** (its outer job allows breakaway) and the child stayed in our job. The refusal (error 5) was seen only in earlier design probes on hosts whose outer job does not allow breakaway; this build did not observe it. |
| C-9 | The capability check, run before any product change, then kept as a test. It prints its facts as one warning line, so every run's log keeps them. | Executed on one machine; other hosts are open evidence (§6) |
| C-10 | The verifier is run on made-up logs under both Windows PowerShell 5.1 and PowerShell 7 (ten logs, in `tests/test_git_job_scheduled_task_check.py` and again by hand in `907/verifier-probes.txt`). `-DryRun` prints the two registrations (run 1 with `/IT`, run 2 without it and with `/RP *`) and registers nothing. The task runs themselves were **not** done. | Verifier and dry run: executed. The two scheduled-task runs: **not done** (§6) |
| C-11 | Reasoned in §5 (decision D-6), plus one real test of the default path with nothing replaced. | Executed |

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
| The scheduled-task check and its test | `scripts/check_git_job_scheduled_task.ps1`, `tests/test_git_job_scheduled_task_check.py` |

**Test counts at the time of writing:** 145 new tests (38 on the job module, 68 on the write and its time-out paths,
9 on real Windows with real Git, 2 on the host check, 28 on the scheduled-task script under two PowerShell editions). All
145 pass together under two test workers. Lint (`ruff check src tests`) and the security scan (`bandit -r src`) report
nothing. Neighbouring suites (the older time-out tests, failed-provision cleanup, index safety, hidden index and the
worktree-removal guard) were run together with an earlier 113 of the new tests: 574 passed, 4 skipped, none of the
skips in the new files.

**Red first.** With the old write code put back and the new tests kept, 53 of the new tests fail (50 when this was
first measured; three later tests were added), including both real Windows tests, and the nine "unrelated process"
guards pass, which is what the design says they must do (they are guards, not fixes). With the change, all pass.

**Planted faults.** 30 faults (F01 to F29, with F16b), each planted alone in one place, each shown to compile, to be
reached, and to fail at least one test, each then put back: all 30 are caught, none only as an error.

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
6. **This round** adds the scheduled-task script and its test, this dossier, and a real test of the default path.
   A review by the project's maintainers is outstanding at the time of writing.

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
  leg, because the pytest commands the project's gate builds carry no marker expression that would deselect them. The fixture also leaves the host check alone, so that check imports nothing of
  the product.
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
- On the one machine measured, a direct breakaway request was granted, not refused (§2, C-8). The program still stays in
  our job. The refusal was seen only in earlier design probes.

**What the planted faults do and do not show.** A caught fault shows that some test notices that change in that place. It
does not show that every other fault is caught, and a fault the author did not think of is not in the list.

---

## 7. Reproduction instructions

Run from the repository root, on Windows with Git installed. Nothing below registers a scheduled task.

```text
python -m pytest tests/test_git_job.py tests/test_git_write_job_907.py tests/test_git_job_windows.py ^
    tests/test_git_job_capability.py tests/test_git_job_scheduled_task_check.py -q
ruff check src tests
python -m bandit -r src -x src/agenttalk/skills
```

The host facts line appears in the warnings summary of the first command (it is the `GitJobHostFacts` line).

Red first: put the write code of the commit before this change back (`git show <that commit>:src/agenttalk/cli.py`,
written over `src/agenttalk/cli.py`), keep everything else, run the first command, then `git checkout` the file. The
failing tests are listed in `907/red-first.txt`.

The planted faults, on a clone you can throw away (the runner edits tracked files and puts them back; it refuses to start
when the two source files have uncommitted changes):

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
single log without a scheduler: `-Mode Verify -Log <file> -RunId <id>`.
