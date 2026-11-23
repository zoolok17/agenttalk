"""#907: the scheduled-task host check (`scripts/check_git_job_scheduled_task.ps1`), tested without a scheduler.

The script registers a task, waits for it and judges its log. Registering needs a person (the second run asks for a
password), so the tests here cover the two parts that need nothing: the verifier, which judges a made-up log once, and
`-DryRun`, which prints every command it would run and registers nothing. They run on every machine that has a
PowerShell (Windows PowerShell 5.1 and PowerShell 7 are both tried when both are there).
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

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
    assert "run: schtasks /Delete /TN" in done.stdout and "neither Running nor Queued" in done.stdout
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
