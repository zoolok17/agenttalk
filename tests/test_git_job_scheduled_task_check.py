"""#907: the scheduled-task host check (`scripts/check_git_job_scheduled_task.ps1`), tested without a scheduler.

The script registers a task, waits for it and judges its log. Registering needs a person (the second run asks for a
password), so the tests here cover the two parts that need nothing: the verifier, which judges a made-up log once, and
`-DryRun`, which prints every command it would run and registers nothing. They run on every machine that has a
PowerShell (Windows PowerShell 5.1 and PowerShell 7 are both tried when both are there).
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


def plain(text):
    """Error text as words only: PowerShell 7 frames and wraps a long error over several lines with a bar in front, and
    can put colour codes (also inside a word) when it wraps. The codes, the bars and the line breaks are taken out."""
    return " ".join(ANSI.sub("", text).replace("|", " ").split())


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_git_job_scheduled_task.ps1"
SHELLS = [found for found in (shutil.which("powershell"), shutil.which("pwsh")) if found]
RUN_ID = "run-0123456789ab"

pytestmark = pytest.mark.skipif(not SHELLS, reason="no PowerShell on this machine")

FACTS = {
    "direct": {"assigned": True, "git_in_our_job": True, "resume_status": 0},
    "host": {"windows": "Windows-11-example", "session_id": 1, "pytest_process_in_a_job": True},
}
FACTS_LINE = "  C:/x/tests/test_git_job_capability.py:237: GitJobHostFacts: git-job-capability: " + json.dumps(FACTS)


def log_lines(*, first=f"run-id={RUN_ID}", facts=FACTS_LINE, last="exit=0"):
    lines = [first, "C:\\Program Files\\Git\\cmd\\git.exe", "=== test session starts ===", "1 passed, 1 warning"]
    if facts is not None:
        lines.insert(3, "=== warnings summary ===")
        lines.insert(4, facts)
    if last is not None:
        lines.append(last)
    return lines


# (id, the lines of the log or None for no file, the exit code the verifier must give, the words its answer must hold)
CASES = [
    ("good", log_lines(), 0, "OK:"),
    ("missing", None, 2, "NO COMPLETION EVIDENCE"),
    ("empty", [], 2, "NO COMPLETION EVIDENCE"),
    ("another-runs-log", log_lines(first="run-id=run-ffffffffffff"), 2, "NO COMPLETION EVIDENCE"),
    ("partial-no-exit-line", log_lines(last=None), 2, "NO COMPLETION EVIDENCE"),
    ("exit-line-not-last", log_lines() + ["a line that came after"], 2, "NO COMPLETION EVIDENCE"),
    ("non-zero-exit", log_lines(last="exit=1"), 3, "exit code was 1"),
    ("exit-0-without-the-facts-line", log_lines(facts=None), 3, "no host facts line"),
    (
        "facts-line-of-the-old-form",
        log_lines(facts="git-job-capability: " + json.dumps(FACTS)),
        3,
        "no host facts line",
    ),
    (
        "facts-without-a-session-id",
        log_lines(facts=FACTS_LINE.replace("session_id", "other")),
        3,
        'do not name "session_id"',
    ),
]


def run_script(shell, *args):
    return subprocess.run(  # noqa: S603
        [shell, "-NoProfile", "-File", str(SCRIPT), *args], capture_output=True, text=True, timeout=120, check=False
    )


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
@pytest.mark.parametrize("name, lines, code, words", CASES, ids=[case[0] for case in CASES])
def test_the_verifier_judges_a_made_up_log(tmp_path, shell, name, lines, code, words):
    log = tmp_path / "run.log"
    if lines is not None:
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    done = run_script(shell, "-Mode", "Verify", "-Log", str(log), "-RunId", RUN_ID)
    assert done.returncode == code, done.stdout + done.stderr
    assert words in done.stdout, done.stdout


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
def test_a_good_log_that_starts_with_a_byte_order_mark_is_still_accepted(tmp_path, shell):
    """Windows PowerShell 5.1 writes a byte order mark when it makes a log: the first line must still be this run's."""
    log = tmp_path / "run.log"
    log.write_bytes(b"\xef\xbb\xbf" + ("\n".join(log_lines()) + "\n").encode("utf-8"))
    done = run_script(shell, "-Mode", "Verify", "-Log", str(log), "-RunId", RUN_ID)
    assert done.returncode == 0 and "OK:" in done.stdout, done.stdout + done.stderr


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
def test_the_dry_run_registers_nothing_and_shows_the_two_registrations(tmp_path, shell):
    out = tmp_path / "out"
    # the limit is raised only here: a long temporary folder of this machine must not decide the shape being checked
    done = run_script(
        shell,
        "-Mode",
        "Run",
        "-DryRun",
        "-Python",
        "python",
        "-OutDir",
        str(out),
        "-User",
        "example-user",
        "-CommandLengthLimit",
        "5000",
    )
    assert done.returncode == 0, done.stdout + done.stderr
    creates = [line for line in done.stdout.splitlines() if "would run: schtasks /Create" in line]
    assert len(creates) == 2, done.stdout
    first, second = creates
    assert "[interactive]" in first and " /IT " in first and "/RP" not in first  # run 1: interactive only
    assert "[background]" in second and " /IT " not in second and "/RP *" in second  # run 2: asks for the password
    assert all("-Mode Task -ArgsFile" in line for line in creates)
    assert all("/RU" in line and "example-user" in line for line in creates)
    assert (
        "run: schtasks /Delete /TN" in done.stdout and "Ready (not Running, Queued, Disabled or unknown)" in done.stdout
    )
    assert not out.exists(), "a dry run must not write anything"


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
def test_a_command_too_long_for_schtasks_is_refused_before_anything_is_registered(tmp_path, shell):
    long_out = tmp_path / ("a" * 200) / ("b" * 200)
    done = run_script(shell, "-Mode", "Run", "-DryRun", "-Python", "python", "-OutDir", str(long_out), "-User", "u")
    assert done.returncode != 0
    assert "261" in done.stdout + done.stderr


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
def test_the_script_never_asks_for_or_records_a_password(shell):
    """The password is typed at the prompt of `schtasks /RP *`: the script has no parameter for one, never reads it."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert "/RP *" in text and "-Password" not in text and "ConvertTo-SecureString" not in text
    assert "Read-Host" not in text and "Get-Credential" not in text
    assert shell  # run once per shell only to keep the parametrization shape of the file


# --- the whole completion rule (both halves), without a scheduler: `-Mode Decide` takes a made-up state ----------

SCHEDULER_NOT_DONE = "NO COMPLETION EVIDENCE FROM THE SCHEDULER"

# (id, the lines of the log, whether the scheduler shows the task, its state, the exit code, the words of the answer)
DECIDE_CASES = [
    ("ready-and-a-good-log", log_lines(), "true", "Ready", 0, "OK:"),
    ("running", log_lines(), "true", "Running", 2, SCHEDULER_NOT_DONE),
    ("queued", log_lines(), "true", "Queued", 2, SCHEDULER_NOT_DONE),
    ("disabled", log_lines(), "true", "Disabled", 2, SCHEDULER_NOT_DONE),
    ("unknown", log_lines(), "true", "Unknown", 2, SCHEDULER_NOT_DONE),
    ("no-state-at-all", log_lines(), "true", "", 2, SCHEDULER_NOT_DONE),
    ("the-scheduler-cannot-see-the-task", log_lines(), "false", "Ready", 2, SCHEDULER_NOT_DONE),
    ("ready-but-the-log-is-partial", log_lines(last=None), "true", "Ready", 2, "NO COMPLETION EVIDENCE: the log is"),
    ("ready-but-another-runs-log", log_lines(first="run-id=run-ffffffffffff"), "true", "Ready", 2, "'other-run'"),
    ("ready-but-a-non-zero-exit", log_lines(last="exit=1"), "true", "Ready", 3, "exit code was 1"),
    ("ready-but-no-facts-line", log_lines(facts=None), "true", "Ready", 3, "no host facts line"),
]


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
@pytest.mark.parametrize(
    "name, lines, exists, state, code, words", DECIDE_CASES, ids=[case[0] for case in DECIDE_CASES]
)
def test_a_run_is_complete_only_when_the_log_is_finished_and_the_task_is_ready(
    tmp_path, shell, name, lines, exists, state, code, words
):
    """A finished log alone is never enough: the wait of `-Mode Run` can end at its time limit with a finished log while
    the scheduler still shows the task as Running or Queued, or cannot show it at all. That must not be accepted. Only
    the state Ready counts as finished; Disabled, Unknown, an empty state and anything else do not."""
    log = tmp_path / "run.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    done = run_script(
        shell, "-Mode", "Decide", "-Log", str(log), "-RunId", RUN_ID, "-TaskExists", exists, "-TaskState", state
    )
    assert done.returncode == code, done.stdout + done.stderr
    assert words in done.stdout, done.stdout


@pytest.mark.skipif(os.name != "nt", reason="the stand-in interpreter is a Windows command file")
@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
def test_a_line_on_stderr_does_not_stop_the_task_mode_before_it_writes_its_last_line(tmp_path, shell):
    """Windows PowerShell 5.1 turns a stderr line of a native command (redirected with 2>&1) into a terminating error if
    `$ErrorActionPreference` is Stop, so the log stopped after the facts line and every run ended as "partial". With a
    stand-in interpreter that prints the facts line, then one line on stderr, then exits 0, Task mode must still write
    `exit=0` last, and the verifier must accept the log."""
    stand_in = tmp_path / "interpreter.cmd"
    stand_in.write_text(
        "@echo off\r\n"
        'echo   x.py:1: GitJobHostFacts: git-job-capability: {"windows": "made up", "session_id": 1, '
        '"pytest_process_in_a_job": true}\r\n'
        "echo a warning on stderr 1>&2\r\n"
        "exit /b 0\r\n",
        encoding="ascii",
    )
    log = tmp_path / "task.log"
    done = run_script(
        shell, "-Mode", "Task", "-Repo", str(tmp_path), "-Python", str(stand_in), "-Log", str(log),
        "-RunId", RUN_ID, "-BaseTemp", str(tmp_path / "bt"),
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
    lines = [line for line in log.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    assert lines[0] == f"run-id={RUN_ID}" and lines[-1] == "exit=0"
    assert any("a warning on stderr" in line for line in lines)  # the stderr line is in the log as plain text
    verified = run_script(shell, "-Mode", "Verify", "-Log", str(log), "-RunId", RUN_ID)
    assert verified.returncode == 0 and "OK:" in verified.stdout, verified.stdout + verified.stderr


# --- the arguments file keeps every character of a path ---------------------------------------------------------------

ACCENTED = "café über"  # a folder name with accents and a space


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
def test_the_arguments_file_keeps_a_folder_name_with_an_accent(tmp_path, shell):
    """The arguments file used to be saved as ASCII, so a folder named `café` came back as `caf?` in both editions and
    the task could not find its repository. It is UTF-8 with a byte order mark now, written the same way by both
    editions."""
    repo = tmp_path / ACCENTED
    repo.mkdir()
    args_file = tmp_path / "task.args.json"
    done = run_script(
        shell, "-Mode", "WriteArgs", "-ArgsFile", str(args_file), "-Repo", str(repo), "-Python", str(repo / "py.cmd"),
        "-Log", str(repo / "task.log"), "-RunId", RUN_ID, "-BaseTemp", str(repo / "bt"),
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
    data = args_file.read_bytes()
    assert data.startswith(b"\xef\xbb\xbf"), (
        "UTF-8 with a byte order mark, so Windows PowerShell 5.1 reads it as UTF-8 too"
    )
    saved = json.loads(data.decode("utf-8-sig"))
    assert saved["Repo"] == str(repo) and saved["Log"] == str(repo / "task.log") and "?" not in saved["Repo"]
    assert saved["RunId"] == RUN_ID


@pytest.mark.skipif(os.name != "nt", reason="the stand-in interpreter is a Windows command file")
@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
def test_task_mode_finds_its_repository_when_the_folder_name_has_an_accent(tmp_path, shell):
    """The whole path through the file: written by `-Mode WriteArgs` (the function `-Mode Run` uses), read by Task mode
    under the same edition, which then changes into the accented folder, runs the interpreter that is in it, and writes
    the log there."""
    repo = tmp_path / ACCENTED
    repo.mkdir()
    stand_in = repo / "interpreter.cmd"
    stand_in.write_text(
        "@echo off\r\n"
        'echo   x.py:1: GitJobHostFacts: git-job-capability: {"windows": "made up", "session_id": 1, '
        '"pytest_process_in_a_job": true}\r\n'
        "exit /b 0\r\n",
        encoding="ascii",
    )
    log = repo / "task.log"
    args_file = tmp_path / "task.args.json"
    run_script(
        shell, "-Mode", "WriteArgs", "-ArgsFile", str(args_file), "-Repo", str(repo), "-Python", str(stand_in),
        "-Log", str(log), "-RunId", RUN_ID, "-BaseTemp", str(repo / "bt"),
    )  # fmt: skip
    done = run_script(shell, "-Mode", "Task", "-ArgsFile", str(args_file))
    assert done.returncode == 0, done.stdout + done.stderr
    verified = run_script(shell, "-Mode", "Verify", "-Log", str(log), "-RunId", RUN_ID)
    assert verified.returncode == 0 and "OK:" in verified.stdout, verified.stdout + verified.stderr


# --- `-Mode Run` against a stand-in scheduler (nothing is registered) -------------------------------------------

STUB_SCHEDULER = """
$script:stub = @{ exists = $false; deleted = $false }
function Get-TaskFacts {
    # STUB_QUERY: "ok" answers truthfully; "always" and "after-delete" make the scheduler unreadable (an error, not a
    # "no such task") from the start, or from the first delete on.
    $broken = ($env:STUB_QUERY -eq 'always') -or ($env:STUB_QUERY -eq 'after-delete' -and $script:stub.deleted)
    if ($broken) {
        return @{ exists = $false; unreadable = $true; status = 'unknown'; logon = 'unknown'; result = 'unknown' }
    }
    if (-not $script:stub.exists) {
        return @{ exists = $false; unreadable = $false; status = 'unknown'; logon = 'unknown'; result = 'unknown' }
    }
    $state = if ($env:STUB_TASK_STATE) { $env:STUB_TASK_STATE } else { 'Ready' }
    return @{ exists = $true; unreadable = $false; status = $state; logon = 'a stand-in'; result = '0' }
}
function Invoke-Schtasks {
    param([string]$Line)
    if ($env:STUB_CALLS) { Add-Content -LiteralPath $env:STUB_CALLS -Value $Line }
    if ($Line -like '/Create*') { $script:stub.exists = $true; return 0 }
    if ($Line -like '/Run*') {
        # the task "runs": the finished log that Task mode would have written, for this run's id
        $file = Get-ChildItem -LiteralPath $OutDir -Filter '*.args.json' |
            Sort-Object LastWriteTime | Select-Object -Last 1
        $saved = Read-ArgsFile -Path $file.FullName
        Set-Content -LiteralPath $saved.Log -Encoding utf8 -Value @(
            "run-id=$($saved.RunId)",
            ('  x.py:1: GitJobHostFacts: git-job-capability: {"windows": "made up", "session_id": 1, ' +
             '"pytest_process_in_a_job": true}'),
            "exit=$(if ($env:STUB_PYTEST_EXIT) { $env:STUB_PYTEST_EXIT } else { '0' })")
        return 0
    }
    if ($Line -like '/Delete*') {
        $script:stub.deleted = $true
        if ($env:STUB_DELETE_FAILS -eq '1') { return 1 }
        if ($env:STUB_DELETE_FAILS -eq 'gone') { $script:stub.exists = $false; return 1 }  # failed, task gone
        $script:stub.exists = $false
        return 0
    }
    return 0
}
"""


def run_with_a_stand_in_scheduler(
    tmp_path,
    shell,
    *,
    state="Ready",
    delete_fails=False,
    seconds=60,
    query="ok",
    pytest_exit=0,
    base_temp=None,
    runs="Interactive",
):
    """Runs `-Mode Run` against the stand-in. `query` is how the scheduler answers: "ok", "always" unreadable, or
    "after-delete" unreadable once the task has been deleted. Returns (the finished process, the out folder, the
    schtasks commands the script issued)."""
    stub = tmp_path / "stub-scheduler.ps1"
    stub.write_text(STUB_SCHEDULER, encoding="utf-8")
    out = tmp_path / "out"
    calls = tmp_path / "schtasks-calls.txt"
    env = dict(
        os.environ,
        STUB_TASK_STATE=state,
        STUB_DELETE_FAILS=delete_fails if isinstance(delete_fails, str) else ("1" if delete_fails else "0"),
        STUB_QUERY=query,
        STUB_PYTEST_EXIT=str(pytest_exit),
        STUB_CALLS=str(calls),
    )
    done = subprocess.run(  # noqa: S603
        [
            shell, "-NoProfile", "-File", str(SCRIPT), "-Mode", "Run", "-Runs", runs, "-Python", "python",
            "-OutDir", str(out), "-User", "example-user", "-TaskName", "stub-task", "-TimeoutSeconds", str(seconds),
            "-CommandLengthLimit", "5000", "-SchedulerStub", str(stub),
        ] + (["-BaseTemp", str(base_temp)] if base_temp is not None else []),
        capture_output=True, text=True, timeout=180, check=False, env=env,
    )  # fmt: skip
    return done, out, calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
@pytest.mark.parametrize(
    "settings, code",
    [
        ({"delete_fails": True}, 4),
        ({"query": "after-delete"}, 4),
        ({"delete_fails": True, "pytest_exit": 1}, 3),
        ({"query": "after-delete", "pytest_exit": 1}, 3),
        ({"delete_fails": True, "state": "Running", "seconds": 1}, 2),
    ],
)
def test_both_runs_stop_if_first_task_is_not_known_gone(tmp_path, shell, settings, code):
    done, _, calls = run_with_a_stand_in_scheduler(tmp_path, shell, runs="Both", **settings)
    text = done.stdout + done.stderr
    assert done.returncode == code, text
    assert len([call for call in calls if call.startswith("/Create")]) == 1, calls
    assert "next run was not started because the earlier task may still exist" in text


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
@pytest.mark.parametrize("delete_fails", [False, "gone"])
def test_both_runs_continue_if_first_task_is_known_gone(tmp_path, shell, delete_fails):
    done, out, calls = run_with_a_stand_in_scheduler(tmp_path, shell, runs="Both", delete_fails=delete_fails)
    assert done.returncode == 0, done.stdout + done.stderr
    assert len([call for call in calls if call.startswith("/Create")]) == 2, calls
    assert (out / "interactive-accepted.log").exists() and (out / "background-accepted.log").exists()


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
def test_run_creates_missing_base_temp_before_registering(tmp_path, shell):
    base_temp = tmp_path / "missing" / "base"
    done, out, calls = run_with_a_stand_in_scheduler(tmp_path, shell, base_temp=base_temp)
    assert done.returncode == 0, done.stdout + done.stderr
    assert base_temp.is_dir()
    assert (out / "interactive-accepted.log").exists()
    assert any(call.startswith("/Create") for call in calls)


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
def test_dry_run_reports_missing_base_temp_without_creating_it(tmp_path, shell):
    base_temp = tmp_path / "missing" / "base"
    out = tmp_path / "out"
    done = run_script(
        shell, "-Mode", "Run", "-DryRun", "-Runs", "Interactive", "-Python", "python", "-OutDir", str(out),
        "-BaseTemp", str(base_temp), "-User", "u", "-CommandLengthLimit", "5000",
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
    assert "would create -BaseTemp" in done.stdout and str(base_temp) in done.stdout
    assert not base_temp.exists() and not out.exists()


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
def test_run_refuses_uncreatable_base_temp_before_registering(tmp_path, shell):
    parent = tmp_path / "file"
    parent.write_text("not a directory", encoding="utf-8")
    done, _, calls = run_with_a_stand_in_scheduler(tmp_path, shell, base_temp=parent / "base")
    assert done.returncode == 9, done.stdout + done.stderr
    assert "-BaseTemp" in done.stdout + done.stderr
    assert calls == []


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
def test_a_run_whose_task_is_deleted_is_accepted_and_copied(tmp_path, shell):
    done, out, _ = run_with_a_stand_in_scheduler(tmp_path, shell)
    assert done.returncode == 0, done.stdout + done.stderr
    assert (out / "interactive-accepted.log").exists()
    assert "task deleted: True" in done.stdout


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
def test_a_failed_deletion_of_the_task_is_not_a_success(tmp_path, shell):
    """The run is accepted and its log copied, but the task is still there: the exit code is not 0 (it is 4), the output
    says the task was not deleted and how to delete it by hand, and the accepted evidence is kept."""
    done, out, _ = run_with_a_stand_in_scheduler(tmp_path, shell, delete_fails=True)
    assert done.returncode == 4, done.stdout + done.stderr
    assert "task deleted: False" in done.stdout and "TASK NOT DELETED" in done.stdout
    assert "schtasks /Delete /TN stub-task /F" in done.stdout
    assert (out / "interactive-accepted.log").exists()


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
def test_a_finished_log_is_not_accepted_when_the_wait_ends_with_the_task_still_running(tmp_path, shell):
    """The wait of `-Mode Run`, end to end: the log is finished, but the scheduler keeps showing the task as Running
    until the time limit. The limit must not turn that into an acceptance: exit code 2, no accepted copy, and the task
    is still deleted."""
    done, out, _ = run_with_a_stand_in_scheduler(tmp_path, shell, state="Running", seconds=1)
    assert done.returncode == 2, done.stdout + done.stderr
    assert "NO COMPLETION EVIDENCE FROM THE SCHEDULER" in done.stdout
    assert not (out / "interactive-accepted.log").exists()
    assert "task deleted: True" in done.stdout


# --- a scheduler that cannot be read is not a task that is gone (round 14) -------------------------------------

HOW_TO_DELETE = "schtasks /Delete /TN stub-task /F"


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
@pytest.mark.parametrize("delete_fails", [True, False], ids=["delete-failed", "delete-said-ok"])
def test_an_unreadable_scheduler_after_the_delete_is_not_a_confirmed_cleanup(tmp_path, shell, delete_fails):
    """The delete returned 1 (or even 0), then the query failed with an error: the absence is not established. The run
    was accepted and its log is kept, but the exit code is 4, the output says the task is NOT CONFIRMED deleted and how
    to look for it and delete it by hand, and it never prints that the task was deleted."""
    done, out, _ = run_with_a_stand_in_scheduler(tmp_path, shell, delete_fails=delete_fails, query="after-delete")
    text = done.stdout + done.stderr
    assert done.returncode == 4, text
    assert "task deleted: False" in done.stdout and "task deleted: True" not in done.stdout, text
    assert "TASK NOT CONFIRMED DELETED" in done.stdout and HOW_TO_DELETE in done.stdout, text
    assert "schtasks /Query /TN stub-task" in done.stdout, text
    assert (out / "interactive-accepted.log").exists()


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
def test_a_scheduler_that_cannot_be_read_before_registering_registers_nothing(tmp_path, shell):
    done, out, calls = run_with_a_stand_in_scheduler(tmp_path, shell, query="always")
    text = plain(done.stdout + done.stderr)  # PowerShell 7 wraps its error text and can colour it
    assert done.returncode != 0 and "could not be read" in text and "Nothing was registered" in text, text
    assert calls == [], calls  # no /Create, no /Run: nothing reached the scheduler
    assert not (out / "interactive-accepted.log").exists()


# (what the run did and how the scheduler answered, the exit code that must win, the warning that must be printed)
COMBINED = [
    pytest.param(
        {"pytest_exit": 1, "delete_fails": True}, 3, "TASK NOT DELETED", id="rejected-run-and-the-task-is-still-there"
    ),
    pytest.param(
        {"pytest_exit": 1, "delete_fails": True, "query": "after-delete"},
        3,
        "TASK NOT CONFIRMED DELETED",
        id="rejected-run-and-the-scheduler-is-unreadable",
    ),
    pytest.param(
        {"state": "Running", "seconds": 1, "delete_fails": True},
        2,
        "TASK NOT DELETED",
        id="no-completion-evidence-and-the-task-is-still-there",
    ),
]


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
@pytest.mark.parametrize("settings, code, warning", COMBINED)
def test_a_run_that_failed_keeps_its_own_code_and_the_cleanup_warning_is_still_printed(
    tmp_path, shell, settings, code, warning
):
    """A run that is not accepted (3: rejected, 2: no completion evidence) and a task that is also still shown after the
    delete: the run's own code wins (the cleanup failure does not turn 3 into 4), and the warning about the task is
    printed anyway, with how to delete it. No accepted copy is made."""
    done, out, _ = run_with_a_stand_in_scheduler(tmp_path, shell, **settings)
    text = done.stdout + done.stderr
    assert done.returncode == code, text
    assert warning in done.stdout and HOW_TO_DELETE in done.stdout, text
    assert not (out / "interactive-accepted.log").exists()


STUB_NO_SUCH_TASK = """
function Get-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName)
    $record = New-Object Management.Automation.ErrorRecord (New-Object Exception 'no such task (stand-in)'),
        'CmdletizationQuery_NotFound_TaskName', ([Management.Automation.ErrorCategory]::ObjectNotFound), $TaskName
    $PSCmdlet.ThrowTerminatingError($record)
}
"""
STUB_COMMAND_MISSING = """
function Get-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName)
    $record = New-Object Management.Automation.ErrorRecord (New-Object Exception 'the term is not recognized'),
        'CommandNotFoundException', ([Management.Automation.ErrorCategory]::ObjectNotFound), 'Get-ScheduledTask'
    $PSCmdlet.ThrowTerminatingError($record)
}
"""
STUB_NO_ANSWER = """
function Get-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName)
}
"""
STUB_CANNOT_ASK = """
function Get-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName)
    $record = New-Object Management.Automation.ErrorRecord (New-Object Exception 'access is denied (stand-in)'), 'x',
        ([Management.Automation.ErrorCategory]::PermissionDenied), $TaskName
    $PSCmdlet.ThrowTerminatingError($record)
}
"""


def ask_for_facts(shell, *extra, name="stub-task"):
    done = subprocess.run(  # noqa: S603
        [shell, "-NoProfile", "-File", str(SCRIPT), "-Mode", "Facts", "-TaskName", name, *extra],
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    return done.returncode, done.stdout.strip()


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
@pytest.mark.parametrize(
    "stub, expected",
    [
        (STUB_NO_SUCH_TASK, "exists=false unreadable=false"),
        (STUB_CANNOT_ASK, "exists=false unreadable=true"),
        (STUB_COMMAND_MISSING, "exists=false unreadable=true"),
        (STUB_NO_ANSWER, "exists=false unreadable=true"),
    ],
    ids=[
        "no-such-task-is-a-confirmed-absence",
        "any-other-error-is-unreadable",
        "a-missing-command-has-the-same-category-and-is-unreadable",
        "no-error-and-no-answer-is-unreadable",
    ],
)
def test_the_two_kinds_of_no_from_the_scheduler_are_told_apart(tmp_path, shell, stub, expected):
    path = tmp_path / "scheduler-answer.ps1"
    path.write_text(stub, encoding="utf-8")
    assert ask_for_facts(shell, "-SchedulerStub", str(path)) == (0, expected)


# What the real scheduler says, asked on its own and read only (a task name that was never registered): the category,
# the thing that was not found and the error id, as PowerShell reports them. Nothing in agenttalk's script is used.
ASK_THE_SCHEDULER = (
    "try { Get-ScheduledTask -TaskName '{name}' -ErrorAction Stop | Out-Null; 'answered' } "
    "catch { 'category=' + $_.CategoryInfo.Category + ' target=' + $_.TargetObject "
    "+ ' fqid=' + $_.FullyQualifiedErrorId }"
)


def ask_the_real_scheduler(shell, name):
    done = subprocess.run(  # noqa: S603
        [shell, "-NoProfile", "-Command", ASK_THE_SCHEDULER.replace("{name}", name)],
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    return done.stdout.strip()


def the_scheduler_says_no_such_task(raw, name):
    """(True, the error id) when the raw answer is the scheduler saying that THIS task does not exist: the thing that
    was not found is the task (its name is the error's target), and it is not a missing command (whose target is the
    command). Judged without the id that agenttalk expects, so that a wrong expected id cannot hide itself. (False,
    why) otherwise."""
    found = re.fullmatch(r"category=(\S*) target=(.*) fqid=(.*)", raw, flags=re.DOTALL)
    if found is None:
        return False, raw or "no answer"
    category, target, fqid = found.groups()
    if category == "ObjectNotFound" and target == name:
        return True, fqid
    return False, raw


@pytest.mark.parametrize(
    "raw, says_no_such_task",
    [
        ("category=ObjectNotFound target=never fqid=CmdletizationQuery_NotFound_TaskName,Get-ScheduledTask", True),
        ("category=ObjectNotFound target=never fqid=SomeOtherId,Get-ScheduledTask", True),  # whatever its id is
        ("category=ObjectNotFound target=Get-ScheduledTask fqid=CommandNotFoundException", False),  # a missing command
        ("category=PermissionDenied target=never fqid=HRESULT 0x80070005,Get-ScheduledTask", False),
        ("answered", False),
        ("", False),
    ],
    ids=["real-answer", "another-id-is-still-a-no-such-task", "missing-command", "access-denied", "found", "no-answer"],
)
def test_the_independent_question_tells_no_such_task_from_a_scheduler_that_cannot_be_read(raw, says_no_such_task):
    assert the_scheduler_says_no_such_task(raw, "never")[0] is says_no_such_task


@pytest.mark.skipif(os.name != "nt", reason="the scheduled-task service is a Windows thing")
@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
def test_the_real_scheduler_confirms_the_absence_of_a_task_that_was_never_registered(shell):
    """Read only, nothing registered. The real scheduler is asked on its own first (the raw category, target and error
    id). The test is skipped ONLY when that independent question shows that the scheduler cannot be read on this
    machine (a sandbox that denies the query, a missing module): then nothing can be shown against it. Whenever the
    scheduler answers that there is no such task, `-Mode Facts` must say the same: absent, and readable. So a wrong
    error id, or any change that reads a real not-found answer as unreadable, FAILS here on a machine where the
    scheduler works."""
    name = "agenttalk-no-such-task-907r16"
    raw = ask_the_real_scheduler(shell, name)
    if raw == "answered":
        pytest.fail(f"a scheduled task named {name} exists on this machine; delete it or change the name of the test")
    says_no_such_task, evidence = the_scheduler_says_no_such_task(raw, name)
    if not says_no_such_task:
        pytest.skip(f"the scheduler cannot be read on this machine (its raw answer: {evidence})")
    expected = re.search(r"\$TaskNotFoundId = '([^']+)'", SCRIPT.read_text(encoding="utf-8")).group(1)
    assert evidence == expected, f"the real scheduler's not-found id is {evidence!r}; the script expects {expected!r}"
    code, text = ask_for_facts(shell, name=name)
    assert (code, text) == (0, "exists=false unreadable=false"), (text, raw)


# --- the scheduler command itself missing is not a task that is gone (round 15) --------------------------------

STUB_COMMAND_LEVEL = """
$script:stub = @{ exists = $false; deleted = $false }
function Get-ScheduledTask {
    # Only the command that the real Get-TaskFacts calls is replaced, so the real classification is what runs.
    # STUB_CMD: "ok" answers truthfully; "missing" is a Get-ScheduledTask that does not exist at all (the error that
    # PowerShell gives for a missing command or module: category ObjectNotFound, id CommandNotFoundException);
    # "missing-after-delete" is that, from the first delete on.
    [CmdletBinding()] param([string]$TaskName)
    $gone = ($env:STUB_CMD -eq 'missing') -or ($env:STUB_CMD -eq 'missing-after-delete' -and $script:stub.deleted)
    if ($gone) {
        $record = New-Object Management.Automation.ErrorRecord (New-Object Exception 'the term is not recognized'),
            'CommandNotFoundException', ([Management.Automation.ErrorCategory]::ObjectNotFound), 'Get-ScheduledTask'
        $PSCmdlet.ThrowTerminatingError($record)
    }
    if (-not $script:stub.exists) {
        $record = New-Object Management.Automation.ErrorRecord (New-Object Exception 'no such task (stand-in)'),
            'CmdletizationQuery_NotFound_TaskName', ([Management.Automation.ErrorCategory]::ObjectNotFound), $TaskName
        $PSCmdlet.ThrowTerminatingError($record)
    }
    return [pscustomobject]@{ State = 'Ready'; Principal = [pscustomobject]@{ LogonType = 'Interactive' } }
}
function Get-ScheduledTaskInfo {
    [CmdletBinding()] param([string]$TaskName)
    return [pscustomobject]@{ LastTaskResult = 0 }
}
function Invoke-Schtasks {
    param([string]$Line)
    if ($env:STUB_CALLS) { Add-Content -LiteralPath $env:STUB_CALLS -Value $Line }
    if ($Line -like '/Create*') { $script:stub.exists = $true; return 0 }
    if ($Line -like '/Run*') {
        $file = Get-ChildItem -LiteralPath $OutDir -Filter '*.args.json' |
            Sort-Object LastWriteTime | Select-Object -Last 1
        $saved = Read-ArgsFile -Path $file.FullName
        Set-Content -LiteralPath $saved.Log -Encoding utf8 -Value @(
            "run-id=$($saved.RunId)",
            ('  x.py:1: GitJobHostFacts: git-job-capability: {"windows": "made up", "session_id": 1, ' +
             '"pytest_process_in_a_job": true}'),
            'exit=0')
        return 0
    }
    if ($Line -like '/Delete*') {
        $script:stub.deleted = $true
        if ($env:STUB_DELETE_FAILS -eq '1') { return 1 }
        $script:stub.exists = $false
        return 0
    }
    return 0
}
"""


def run_with_a_command_level_stand_in(tmp_path, shell, *, command, delete_fails=False, wrapper=None):
    """`-Mode Run` against a stand-in that replaces only `Get-ScheduledTask` and `schtasks`: the real `Get-TaskFacts`
    classifies what the command answers. `command` is "ok", "missing" or "missing-after-delete" (see the stub). Returns
    (the finished process, the out folder, the schtasks commands the script issued). `wrapper` is PowerShell text
    that is run before the script, in the same process (used to force colour on)."""
    stub = tmp_path / "stub-command.ps1"
    stub.write_text(STUB_COMMAND_LEVEL, encoding="utf-8")
    out = tmp_path / "out"
    calls = tmp_path / "schtasks-calls.txt"
    env = dict(os.environ, STUB_CMD=command, STUB_DELETE_FAILS="1" if delete_fails else "0", STUB_CALLS=str(calls))
    script_args = [
        "-Mode", "Run", "-Runs", "Interactive", "-Python", "python", "-OutDir", str(out), "-User", "example-user",
        "-TaskName", "stub-task", "-TimeoutSeconds", "60", "-CommandLengthLimit", "5000", "-SchedulerStub", str(stub),
    ]  # fmt: skip
    if wrapper is None:
        argv = [shell, "-NoProfile", "-File", str(SCRIPT), *script_args]
    else:
        quoted = " ".join(  # the parameter names stay bare, the values are quoted
            part if part.startswith("-") and part[1:2].isalpha() else "'" + part.replace("'", "''") + "'"
            for part in script_args
        )
        argv = [shell, "-NoProfile", "-Command", f"{wrapper}; & '{SCRIPT}' {quoted}"]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=180, check=False, env=env)  # noqa: S603
    return done, out, calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
def test_a_missing_scheduler_command_before_registering_registers_nothing(tmp_path, shell):
    """PowerShell's own error for a command or module that is not there has the category ObjectNotFound too. It says
    nothing about the task: the run stops at the preflight, and nothing reaches the scheduler."""
    done, out, calls = run_with_a_command_level_stand_in(tmp_path, shell, command="missing")
    text = plain(done.stdout + done.stderr)
    assert done.returncode != 0 and "could not be read" in text and "Nothing was registered" in text, text
    assert calls == [], calls  # no /Create, no /Run
    assert not (out / "interactive-accepted.log").exists()


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
@pytest.mark.parametrize("delete_fails", [True, False], ids=["delete-failed", "delete-said-ok"])
def test_a_scheduler_command_that_goes_missing_after_the_delete_is_not_a_confirmed_cleanup(
    tmp_path, shell, delete_fails
):
    """The run is accepted, the task is deleted, and then the command that asks the scheduler is gone: the absence is
    not established. Exit code 4, TASK NOT CONFIRMED DELETED, the way to delete it by hand, the accepted log kept."""
    done, out, _ = run_with_a_command_level_stand_in(
        tmp_path, shell, command="missing-after-delete", delete_fails=delete_fails
    )
    text = plain(done.stdout + done.stderr)
    assert done.returncode == 4, text
    assert "task deleted: False" in text and "task deleted: True" not in text, text
    assert "TASK NOT CONFIRMED DELETED" in text and HOW_TO_DELETE in text, text
    assert (out / "interactive-accepted.log").exists()


def test_the_normalizer_removes_terminal_colour_and_the_error_frame():
    raw = "No\x1b[31;1mthing was\x1b[0m regis\x1b[1mtered\n | here"
    assert plain(raw) == "Nothing was registered here"


def test_the_refusal_is_found_whatever_the_terminal_colour_does_to_the_error_text(tmp_path):
    """PowerShell 7 wraps its error text and can put colour codes inside a word. Colour is forced on here (the same
    run as the preflight test): the words are split by codes in the raw text, and found in the normalized text."""
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is not on this machine")
    done, out, calls = run_with_a_command_level_stand_in(
        tmp_path, pwsh, command="missing", wrapper="$PSStyle.OutputRendering = 'Ansi'"
    )
    raw = done.stdout + done.stderr
    if "\x1b[" not in raw:
        pytest.skip("this PowerShell 7 wrote no colour codes even when asked to")
    text = plain(raw)
    assert "could not be read" in text and "Nothing was registered" in text, text
    assert calls == [], calls


@pytest.mark.parametrize("shell", SHELLS, ids=lambda path: Path(path).stem.lower())
def test_a_failed_delete_with_the_task_confirmed_absent_says_absent_not_deleted(tmp_path, shell):
    """The delete command failed, but the scheduler answers that there is no such task: the cleanup is confirmed (the
    exit code stays 0), and the output says "task absent (delete command failed ...)", never "task deleted: True"."""
    done, out, _ = run_with_a_stand_in_scheduler(tmp_path, shell, delete_fails="gone")
    text = plain(done.stdout + done.stderr)
    assert done.returncode == 0, text
    assert "task absent (delete command failed" in text and "task deleted: True" not in text, text
    assert (out / "interactive-accepted.log").exists()
