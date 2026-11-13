"""#761: the leg's evidence upload gets one bounded second run, and nothing else about the leg gets more forgiving.

A dev-gate leg runs the committed gate for hours, then uploads its evidence with the pinned upload action. One
transient failure of that upload used to turn a leg whose checks all passed red, and the whole run had to be repeated.
The rule these tests pin (zteam's lead accepted it, option B of the plan):

* the same pinned action runs a second time after a FAILED first upload, and only then: not after a success, and not
  after cancellation. Only the SECOND attempt (and its wait) is suppressed by a cancellation: the first upload and
  the final check use `always()` and still run, as the first upload always did, and a cancelled job stays cancelled;
* a permanent error (a missing file, a quota, a bad token) is simply tried twice and stays red;
* the gate command is never rerun and never forgiven: its step has no `continue-on-error` and exists once;
* a final step, which runs whatever happened, passes only if the OUTCOME of one attempt was `success`
  (`outcome` is what the action really did; `conclusion` would show a forgiven failure as a success).

The workflow is read as text (PyYAML is not a project dependency); its conditions are evaluated by a tiny evaluator
that only understands the few expressions the steps use, and the final step's script is run with bash.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import NamedTuple

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "tests.yml"
UPLOAD_ACTION = "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9"


def is_wsl_launcher(path: str) -> bool:
    """The `bash.exe` in the Windows system folder (or the Store apps folder) starts WSL: it is not a shell that can
    run these scripts, and it can refuse to start at all (E_ACCESSDENIED), which would read as a workflow failure.

    The path is read as a WINDOWS path on every host: `PureWindowsPath` splits on both `\\` and `/`, while `Path`
    takes the flavor of the machine running the test (on Linux or macOS `C:\\Windows\\System32\\bash.exe` is ONE
    component, so the folder names would never be seen)."""
    folders = {part.lower() for part in PureWindowsPath(path).parts}
    return bool({"system32", "syswow64", "sysnative", "windowsapps"} & folders)


def git_bash_candidates() -> list[str]:
    """Git for Windows keeps its bash next to git: <git root>/bin/bash.exe or <git root>/usr/bin/bash.exe."""
    git = shutil.which("git")
    if not git:
        return []
    found = []
    for parent in list(Path(git).resolve().parents)[:3]:
        found += [str(parent / rel) for rel in ("bin/bash.exe", "usr/bin/bash.exe") if (parent / rel).is_file()]
    return found


def bash_is_usable(path: str) -> bool:
    """Prove a bash can run a script before any workflow verdict depends on it."""
    try:
        done = subprocess.run([path, "-c", "echo agenttalk-bash-ok"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and done.stdout.strip() == "agenttalk-bash-ok"


def find_bash() -> str | None:
    """A bash that provably runs, preferring Git Bash; never the WSL launcher."""
    for candidate in [*git_bash_candidates(), shutil.which("bash")]:
        if candidate and not is_wsl_launcher(candidate) and bash_is_usable(candidate):
            return candidate
    return None


BASH = find_bash()
NO_BASH = "no usable bash here (Git Bash, or a Linux or macOS shell; the Windows WSL launcher is never used)"


def leg_job() -> str:
    text = WORKFLOW.read_text(encoding="utf-8").replace("\r\n", "\n")
    return text[text.index("  dev-gate-leg:\n") : text.index("  dev-gate-aggregate:\n")]


def steps() -> list[str]:
    """The text of each step of the leg job, from its `- ` line to the next step."""
    job = leg_job()
    body = job[job.index("    steps:\n") + len("    steps:\n") :]
    return [block for block in re.split(r"(?m)^(?=      - )", body) if block.strip()]


def step_by_id(step_id: str) -> str:
    found = [block for block in steps() if re.search(rf"(?m)^        id: {re.escape(step_id)}$", block)]
    assert len(found) == 1, f"expected exactly one step with id {step_id}, found {len(found)}"
    return found[0]


def step_name(block: str) -> str | None:
    match = re.match(r"      - name: (.*)$", block, re.MULTILINE)
    return match.group(1).strip() if match else None


def field(block: str, key: str) -> str | None:
    match = re.search(rf"(?m)^        {re.escape(key)}: (.*)$", block)
    return match.group(1).strip() if match else None


def inputs(block: str) -> dict[str, str]:
    """The `with:` inputs of a step."""
    match = re.search(r"(?ms)^        with:\n((?:          .*\n?)*)", block)
    pairs = re.findall(r"(?m)^          ([\w-]+): (.*)$", match.group(1)) if match else []
    return {key: value.strip() for key, value in pairs}


def script(block: str) -> str:
    """The `run: |` script of a step, dedented."""
    match = re.search(r"(?ms)^        run: \|\n((?:          .*\n|\n)*)", block)
    assert match, block
    return textwrap.dedent(match.group(1))


def evaluate(condition: str, outcomes: dict[str, str], cancelled: bool) -> bool:
    """Evaluate the small subset of GitHub's expression language the leg steps use. Anything else is refused."""
    python = condition.strip()
    if python.startswith("${{") and python.endswith("}}"):
        python = python[3:-2].strip()
    python = re.sub(r"steps\.(\w+)\.outcome", lambda m: repr(outcomes.get(m.group(1), "skipped")), python)
    python = python.replace("!cancelled()", "(not CANCELLED)").replace("cancelled()", "CANCELLED")
    python = python.replace("always()", "True").replace("&&", " and ").replace("||", " or ")
    if not re.fullmatch(r"[\w\s()'=]*", python.replace("!=", "==")) or "(" in python.replace("(not CANCELLED)", ""):
        raise AssertionError(f"the test's evaluator does not understand: {condition}")
    return bool(eval(python, {"__builtins__": {}}, {"CANCELLED": cancelled}))  # noqa: S307 - a vetted, tiny expression


# ----------------------------------------------------------------------------------------------- the steps


def test_the_leg_has_the_gate_the_first_upload_the_wait_the_second_upload_and_the_final_check_in_order() -> None:
    ids = [field(block, "id") for block in steps()]
    assert [i for i in ids if i] == ["gate", "upload_1", "upload_wait", "upload_2", "upload_check"]
    assert len({field(block, "id") for block in steps() if field(block, "id")}) == 5


def test_the_gate_runs_once_and_is_never_forgiven() -> None:
    gate = step_by_id("gate")
    assert field(gate, "continue-on-error") is None and field(gate, "if") is None
    assert "python -I -m agenttalk dev-gate" in gate and "--profile release" in gate
    assert "--evidence" in gate and "dev-gate-evidence.json" in gate
    assert sum(block.count("agenttalk dev-gate") for block in steps()) == 1  # no rerun, anywhere in the leg
    job = leg_job()
    assert not re.search(r"(?m)^    continue-on-error:", job)  # not forgiven at job level either
    forgiving = [step_name(block) for block in steps() if field(block, "continue-on-error")]
    assert sorted(forgiving) == ["Upload leg evidence", "Upload leg evidence (second attempt)"]


def test_the_first_upload_is_unchanged_apart_from_its_id_its_cap_and_its_outcome_being_recorded() -> None:
    first = step_by_id("upload_1")
    assert field(first, "uses") and field(first, "uses").split()[0] == UPLOAD_ACTION
    assert field(first, "if") == "always()"  # a failed gate still leaves its evidence
    assert field(first, "continue-on-error") == "true" and field(first, "timeout-minutes") == "3"
    assert inputs(first) == {
        "name": "dev-gate-leg-${{ matrix.os.id }}-${{ matrix.python-version }}",
        "path": "${{ runner.temp }}/dev-gate-evidence/",
        "if-no-files-found": "error",
        "retention-days": "14",
    }


def test_the_second_upload_is_the_same_pinned_action_with_the_same_inputs_and_may_replace_its_own_artifact() -> None:
    first, second = step_by_id("upload_1"), step_by_id("upload_2")
    assert field(second, "uses").split()[0] == UPLOAD_ACTION == field(first, "uses").split()[0]
    assert inputs(second) == {**inputs(first), "overwrite": "true"}  # only its own exact name, never a pattern
    assert field(second, "continue-on-error") == "true" and field(second, "timeout-minutes") == "3"


def test_the_wait_is_thirty_seconds_under_the_same_condition_and_a_one_minute_cap() -> None:
    wait, second = step_by_id("upload_wait"), step_by_id("upload_2")
    assert field(wait, "if") == field(second, "if")
    assert field(wait, "timeout-minutes") == "1" and field(wait, "shell") == "bash"
    assert re.search(r"(?m)^          sleep 30$", wait)


def test_the_final_check_runs_whatever_happened_and_cannot_be_forgiven() -> None:
    check = step_by_id("upload_check")
    assert field(check, "if") == "always()" and field(check, "continue-on-error") is None
    assert field(check, "timeout-minutes") == "1" and field(check, "shell") == "bash"
    env = inputs_of_env(check)
    # the OUTCOME of each attempt (what the action did), never its conclusion (which a forgiven failure turns green)
    assert env == {
        "GATE": "${{ steps.gate.outcome }}",
        "UPLOAD_1": "${{ steps.upload_1.outcome }}",
        "UPLOAD_2": "${{ steps.upload_2.outcome }}",
    }
    assert "conclusion" not in check


def inputs_of_env(block: str) -> dict[str, str]:
    match = re.search(r"(?ms)^        env:\n((?:          .*\n?)*)", block)
    pairs = re.findall(r"(?m)^          (\w+): (.*)$", match.group(1)) if match else []
    return {key: value.strip() for key, value in pairs}


# ----------------------------------------------------------------------------------- what the conditions do


@pytest.mark.parametrize("cancelled", [False, True], ids=["running", "cancelled"])
@pytest.mark.parametrize("first", ["success", "failure", "cancelled", "skipped"])
def test_a_second_attempt_happens_only_after_a_failed_first_one_and_never_after_cancellation(first, cancelled) -> None:
    outcomes = {"upload_1": first}
    expected = first == "failure" and not cancelled
    for step_id in ("upload_wait", "upload_2"):
        assert evaluate(field(step_by_id(step_id), "if"), outcomes, cancelled) is expected
    assert evaluate(field(step_by_id("upload_1"), "if"), {}, cancelled) is True  # the first always runs


@pytest.mark.parametrize("step_id", ["upload_1", "upload_wait", "upload_2", "upload_check"])
def test_every_condition_names_a_status_function_so_a_failed_gate_does_not_skip_the_steps(step_id) -> None:
    """Without one GitHub adds an implicit `success()`, and a failed gate would skip the upload of its evidence."""
    assert re.search(r"\b(always|cancelled|failure|success)\(\)", field(step_by_id(step_id), "if"))


# --------------------------------------------------------------------- the final check, run for real with bash

OUTCOMES = ["success", "failure", "cancelled", "skipped"]


class CheckRun(NamedTuple):
    """What running the final check's script gave. `why()` goes into every assertion message, so a problem with the
    shell itself (it printed an error and never ran the script) is read as that and not as a workflow verdict."""

    code: int
    summary: str
    stdout: str
    stderr: str
    inputs: str

    def why(self) -> str:
        return (
            f"{self.inputs}: exit {self.code}; stdout={self.stdout!r}; stderr={self.stderr!r}; summary={self.summary!r}"
        )


def run_check(tmp_path: Path, gate: str, upload_1: str, upload_2: str) -> CheckRun:
    summary = tmp_path / "summary.md"
    summary.write_text("", encoding="utf-8")
    env = {"GATE": gate, "UPLOAD_1": upload_1, "UPLOAD_2": upload_2, "GITHUB_STEP_SUMMARY": str(summary)}
    done = subprocess.run(
        [BASH, "-eo", "pipefail", "-c", script(step_by_id("upload_check"))],
        env={**env, "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
    )
    inputs = f"gate={gate} upload_1={upload_1} upload_2={upload_2} (bash: {BASH})"
    return CheckRun(done.returncode, summary.read_text(encoding="utf-8"), done.stdout, done.stderr, inputs)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (r"C:\Windows\System32\bash.exe", True),
        (r"c:\windows\sysnative\BASH.EXE", True),
        (r"C:\Users\x\AppData\Local\Microsoft\WindowsApps\bash.exe", True),
        (r"C:\Program Files\Git\bin\bash.exe", False),
        (r"C:\Program Files\Git\usr\bin\bash.exe", False),
        ("C:/Windows/System32/bash.exe", True),
        ("/c/Windows/System32/bash.exe", True),
        ("/mnt/c/Windows/Sysnative/bash.exe", True),
        ("/c/Program Files/Git/usr/bin/bash.exe", False),
        ("/usr/bin/bash", False),
        ("/opt/homebrew/bin/bash", False),
    ],
)
def test_the_windows_wsl_launcher_is_never_chosen_as_the_shell(path, expected) -> None:
    assert is_wsl_launcher(path) is expected


@pytest.mark.parametrize("flavor", [PurePosixPath, PureWindowsPath], ids=["posix-host", "windows-host"])
def test_the_launcher_check_gives_the_same_answer_on_every_host_flavor(monkeypatch, flavor) -> None:
    """On Windows `Path` is a Windows path, on Linux and macOS a POSIX one. Bind the module's `Path` to each flavor in
    turn: the answers must not change, so a check that quietly relies on the host's flavor cannot hide on Windows."""
    monkeypatch.setitem(globals(), "Path", flavor)
    assert is_wsl_launcher(r"C:\Windows\System32\bash.exe") is True
    assert is_wsl_launcher(r"c:\windows\sysnative\BASH.EXE") is True
    assert is_wsl_launcher(r"C:\Users\x\AppData\Local\Microsoft\WindowsApps\bash.exe") is True
    assert is_wsl_launcher(r"C:\Program Files\Git\bin\bash.exe") is False
    assert is_wsl_launcher("/usr/bin/bash") is False


def test_a_bash_is_chosen_only_after_it_provably_runs_a_script(tmp_path) -> None:
    assert not bash_is_usable(str(tmp_path / "no-such-bash"))
    if BASH is not None:
        assert bash_is_usable(BASH) and not is_wsl_launcher(BASH)


def test_a_shell_that_cannot_start_is_not_usable_and_says_why_in_assertions(tmp_path) -> None:
    refuses = tmp_path / "refuses.cmd"
    refuses.write_text("@echo Access is denied 1>&2\n@exit /b 1\n" if os.name == "nt" else "#!/bin/sh\nexit 1\n")
    refuses.chmod(0o755)
    assert not bash_is_usable(str(refuses))
    run = CheckRun(1, "", "", "Bash/Service/E_ACCESSDENIED", "gate=success upload_1=success upload_2=skipped")
    assert "E_ACCESSDENIED" in run.why() and "gate=success" in run.why()


@pytest.mark.skipif(BASH is None, reason=NO_BASH)
@pytest.mark.parametrize("upload_2", OUTCOMES)
@pytest.mark.parametrize("upload_1", OUTCOMES)
def test_the_final_check_passes_only_if_one_attempts_outcome_was_success(tmp_path, upload_1, upload_2) -> None:
    run = run_check(tmp_path, "success", upload_1, upload_2)
    assert (run.code == 0) == ("success" in (upload_1, upload_2)), run.why()


@pytest.mark.skipif(BASH is None, reason=NO_BASH)
@pytest.mark.parametrize(
    ("gate", "upload_1", "upload_2", "words"),
    [
        ("success", "success", "skipped", "Gate checks passed; evidence uploaded."),
        ("success", "failure", "success", "Gate checks passed; evidence uploaded."),
        ("success", "failure", "failure", "evidence upload failed"),
        ("failure", "success", "skipped", "Tests or other gate checks failed; evidence uploaded."),
        ("failure", "failure", "failure", "Gate checks failed and evidence upload failed."),
    ],
)
def test_the_final_check_says_in_plain_words_what_happened(tmp_path, gate, upload_1, upload_2, words) -> None:
    run = run_check(tmp_path, gate, upload_1, upload_2)
    assert words in run.summary, run.why()


# ------------------------------------------------- the whole leg, as a table of what each step does and the verdict


class LegRun(NamedTuple):
    ran: list[str]  # which upload attempts ran
    check: CheckRun  # the final check's own outcome (its exit status), kept apart from the job's result
    job: str  # what the job ends as: success, failure or cancelled


def leg_run(tmp_path: Path, gate: str, first: str, second_if_run: str, cancelled: bool) -> LegRun:
    """Which upload attempts run, what the final check says, and how the JOB ends, from each step's own condition and
    script. The final check always runs (`always()`), also after a cancellation, so its exit status alone says
    nothing about the job: a job that was cancelled stays cancelled whatever the check returned."""
    outcomes = {"gate": gate, "upload_1": first}
    ran = ["upload_1"]
    if evaluate(field(step_by_id("upload_2"), "if"), outcomes, cancelled):
        ran.append("upload_2")
        outcomes["upload_2"] = second_if_run
    check = run_check(tmp_path, gate, first, outcomes.get("upload_2", "skipped"))
    if cancelled or "cancelled" in outcomes.values():
        job = "cancelled"
    elif gate != "success" or check.code != 0:  # the gate and the final check are never forgiven; the uploads are
        job = "failure"
    else:
        job = "success"
    return LegRun(ran, check, job)


@pytest.mark.skipif(BASH is None, reason=NO_BASH)
@pytest.mark.parametrize(
    ("gate", "first", "second", "cancelled", "attempts", "check_passes", "job"),
    [
        ("success", "success", "success", False, 1, True, "success"),  # nothing to retry
        ("success", "failure", "success", False, 2, True, "success"),  # the transient case this is for
        ("success", "failure", "failure", False, 2, False, "failure"),  # a permanent error: tried twice, stays red
        ("success", "failure", "success", True, 1, False, "cancelled"),  # cancelled: no second run, never green
        ("success", "success", "success", True, 1, True, "cancelled"),  # cancelled after a good upload: still not green
        ("success", "cancelled", "success", False, 1, False, "cancelled"),  # a cancelled first attempt: no retry
        ("failure", "success", "success", False, 1, True, "failure"),  # the gate failed: red, whatever the upload did
        ("failure", "failure", "success", False, 2, True, "failure"),  # the gate failed and the upload needed a retry
    ],
)
def test_the_whole_leg(tmp_path, gate, first, second, cancelled, attempts, check_passes, job) -> None:
    run = leg_run(tmp_path, gate, first, second, cancelled)
    assert len(run.ran) == attempts, run.check.why()
    assert (run.check.code == 0) is check_passes, run.check.why()  # the final check's own outcome
    assert run.job == job, run.check.why()  # and the job's result, which a cancellation keeps from being green


# ----------------------------------------------------------------------------- the scanner and evaluator themselves


def test_the_evaluator_refuses_an_expression_it_does_not_understand() -> None:
    with pytest.raises(AssertionError, match="does not understand"):
        evaluate("contains(github.ref, 'x')", {}, False)


@pytest.mark.parametrize(
    ("condition", "outcomes", "cancelled", "expected"),
    [
        ("always()", {}, True, True),
        ("!cancelled() && steps.a.outcome == 'failure'", {"a": "failure"}, False, True),
        ("!cancelled() && steps.a.outcome == 'failure'", {"a": "failure"}, True, False),
        ("!cancelled() && steps.a.outcome == 'failure'", {"a": "success"}, False, False),
        ("steps.a.outcome != 'success'", {"a": "skipped"}, False, True),
    ],
)
def test_the_evaluator_agrees_with_how_these_conditions_read(condition, outcomes, cancelled, expected) -> None:
    assert evaluate(condition, outcomes, cancelled) is expected
