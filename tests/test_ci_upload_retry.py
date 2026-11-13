"""#761: the leg's evidence upload gets one bounded second run, and nothing else about the leg gets more forgiving.

A dev-gate leg runs the committed gate for hours, then uploads its evidence with the pinned upload action. One
transient failure of that upload used to turn a leg whose checks all passed red, and the whole run had to be repeated.
The rule these tests pin (zteam's lead accepted it, option B of the plan):

* the same pinned action runs a second time after a FAILED first upload, and only then: not after a success, and not
  after cancellation (a cancelled run must never start more work);
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
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "tests.yml"
UPLOAD_ACTION = "actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9"
BASH = shutil.which("bash")


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


def run_check(tmp_path: Path, gate: str, upload_1: str, upload_2: str) -> tuple[int, str]:
    summary = tmp_path / "summary.md"
    summary.write_text("", encoding="utf-8")
    env = {"GATE": gate, "UPLOAD_1": upload_1, "UPLOAD_2": upload_2, "GITHUB_STEP_SUMMARY": str(summary)}
    done = subprocess.run(
        [BASH, "-eo", "pipefail", "-c", script(step_by_id("upload_check"))],
        env={**env, "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
    )
    return done.returncode, summary.read_text(encoding="utf-8")


@pytest.mark.skipif(BASH is None, reason="no bash on this machine")
@pytest.mark.parametrize("upload_2", OUTCOMES)
@pytest.mark.parametrize("upload_1", OUTCOMES)
def test_the_final_check_passes_only_if_one_attempts_outcome_was_success(tmp_path, upload_1, upload_2) -> None:
    code, _summary = run_check(tmp_path, "success", upload_1, upload_2)
    assert (code == 0) == ("success" in (upload_1, upload_2))


@pytest.mark.skipif(BASH is None, reason="no bash on this machine")
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
    _code, summary = run_check(tmp_path, gate, upload_1, upload_2)
    assert words in summary


# ------------------------------------------------- the whole leg, as a table of what each step does and the verdict


def leg_verdict(tmp_path: Path, gate: str, first: str, second_if_run: str, cancelled: bool) -> tuple[list[str], bool]:
    """Which upload attempts run and whether the leg is green, from each step's own condition and script."""
    outcomes = {"gate": gate, "upload_1": first}
    ran = ["upload_1"]
    if evaluate(field(step_by_id("upload_2"), "if"), outcomes, cancelled):
        ran.append("upload_2")
        outcomes["upload_2"] = second_if_run
    code, _ = run_check(tmp_path, gate, first, outcomes.get("upload_2", "skipped"))
    # a step fails the job unless it is forgiven: the gate and the final check are not, the two uploads are
    green = gate == "success" and code == 0
    return ran, green


@pytest.mark.skipif(BASH is None, reason="no bash on this machine")
@pytest.mark.parametrize(
    ("gate", "first", "second", "cancelled", "attempts", "green"),
    [
        ("success", "success", "success", False, 1, True),  # nothing to retry
        ("success", "failure", "success", False, 2, True),  # the transient case this is for
        ("success", "failure", "failure", False, 2, False),  # a permanent error: tried twice, stays red
        ("success", "failure", "success", True, 1, False),  # cancelled: no second run, never green
        ("success", "cancelled", "success", False, 1, False),  # a cancelled first attempt is not retried either
        ("failure", "success", "success", False, 1, False),  # the gate failed: red, whatever the upload did
        ("failure", "failure", "success", False, 2, False),  # the gate failed and the evidence needed a retry
    ],
)
def test_the_whole_leg(tmp_path, gate, first, second, cancelled, attempts, green) -> None:
    ran, is_green = leg_verdict(tmp_path, gate, first, second, cancelled)
    assert len(ran) == attempts and is_green is green


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
