"""The team-folder canary's own evidence rules (tests/support/team_folder_canary).

The canary is evidence for the #336 design, so its judgement calls are pinned
here: pip's cache folder comes only from a successful run's standard output, a
listing of the user's temp folder that fails or hits its limit is unknown, never an
empty result, a boundary run that did not complete makes the canary fail, ambient
switches are cleared from both modes, a run whose writes something else switched
off is labelled contaminated, a missing measurement or a crashed inner run makes the
run incomplete and still leaves a report, the pip query reads no pip configuration
even behind the filtered child boundaries, the canary writes no compiled files into
the checkout, and the design note states what the checked-in evidence recorded.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

CANARY_DIR = Path(__file__).parent / "support" / "team_folder_canary"
EVIDENCE_DIR = CANARY_DIR / "evidence"
DESIGN = Path(__file__).resolve().parents[1] / "docs" / "DESIGN-team-folder.md"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"team_folder_canary_{name}", CANARY_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(CANARY_DIR))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(CANARY_DIR))
    return module


probe = _load("probe")
canary = _load("canary")
ABSOLUTE = str(Path(__file__).resolve().parent / "cache" / "pip")


def _pip(returncode: int, stdout: str, stderr: str):
    done = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    with mock.patch.object(probe.subprocess, "run", return_value=done):
        return probe._pip_cache_dir()


def test_a_warning_on_stderr_never_becomes_pips_cache_folder():
    assert _pip(0, ABSOLUTE + "\n", "WARNING: unrelated diagnostic\n") == (ABSOLUTE, 1)


def test_pips_answer_must_be_exactly_one_absolute_path():
    assert _pip(0, ABSOLUTE + "\n" + ABSOLUTE + "\n", "") == (None, 0)
    assert _pip(0, "relative/cache\n", "") == (None, 0)
    assert _pip(0, "", "") == (None, 0)


def test_a_failed_pip_is_disabled_only_when_it_says_so():
    disabled = "ERROR: pip cache commands can not function since cache is disabled.\n"
    assert _pip(1, "", disabled) == ("pip-cache-disabled", 1)
    assert _pip(1, "", "ERROR: something else\n") == (None, 1)


def test_a_temp_listing_that_fails_is_unknown_not_empty(tmp_path):
    names, error = canary._names(tmp_path / "missing")
    assert names is None and error == "FileNotFoundError"
    summary = canary.user_temp_summary(None, {"a"}, [error])
    assert summary["status"] == "unknown"
    assert summary["surviving_new_top_level_names"] is None
    assert summary["surviving_new_names_like_the_canary_s"] is None


def test_only_new_names_still_present_at_the_end_are_counted():
    summary = canary.user_temp_summary({"a", "gone"}, {"a", "b", "team-canary-x"}, [])
    assert summary["status"] == "listed at start and end"
    assert summary["surviving_new_top_level_names"] == 2
    assert summary["surviving_new_names_like_the_canary_s"] == 1


def test_a_listing_past_its_entry_limit_is_unknown(tmp_path):
    for index in range(3):
        (tmp_path / f"entry-{index}").mkdir()
    assert canary._names(tmp_path, limit=3) == ({"entry-0", "entry-1", "entry-2"}, None)
    names, error = canary._names(tmp_path, limit=2)
    assert names is None and "2-entry limit" in error


def test_a_listing_past_its_time_limit_is_unknown(tmp_path):
    (tmp_path / "entry").mkdir()
    names, error = canary._names(tmp_path, seconds=-1.0)
    assert names is None and "second limit" in error


def _completed():
    child = {"label": "child"}
    return {
        "baseline/wrapper": {"turns": 1, "reply_landed": True, "child": child},
        "baseline/gateway": {"exit": 0, "child": child},
        "baseline/gate": {"external_base": "user-temp"},
        "baseline/comprehension": {"exit": 0, "child": child},
        "team/gate": {"run_root": "team-root/tmp", "exit": 0, "child": child},
    }


def test_every_boundary_that_did_not_complete_is_a_failure():
    assert canary.failures(_completed()) == []
    for key, change in [
        ("baseline/wrapper", {"reply_landed": False}),
        ("baseline/wrapper", {"turns": 0}),
        ("baseline/gateway", {"exit": 1}),
        ("baseline/comprehension", {"child": None}),
        ("team/gate", {"exit": 2}),
        ("team/gate", {"child": None}),
    ]:
        raw = _completed()
        raw[key] = {**raw[key], **change}
        assert [line.split(":")[0] for line in canary.failures(raw)] == [key], (key, change)


def test_an_inner_run_that_returned_no_record_is_a_failure():
    raw = _completed()
    raw["baseline/gateway"] = {"error": "the inner run exited 2"}
    raw["team/gate"] = {"external_base": "computed-only"}  # the team-mode gate must run
    raw["baseline/comprehension"] = []
    assert canary.failures(raw) == [
        "baseline/gateway: the inner run exited 2",
        "baseline/comprehension: the result is not a record",
        "team/gate: the child exited None",
        "team/gate: the child wrote no probe record",
    ]


def test_ambient_switches_are_cleared_from_both_modes_and_pip_reads_no_config():
    environ = {
        "PATH": "kept",
        "TEMP": "user-temp",
        "PYTHONDONTWRITEBYTECODE": "1",
        "pip_no_cache_dir": "1",
        "PIP_CONFIG_FILE": "user-pip.ini",
        "AGENTTALK_ROOT": "never-passed",
        "JAVA_HOME": "proposed-only",
    }
    base, cleared = canary.base_environment(environ, {"TEMP": "t", "JAVA_HOME": "j"})
    assert cleared == ["PIP_CONFIG_FILE", "PIP_NO_CACHE_DIR", "PYTHONDONTWRITEBYTECODE"]
    assert base["PATH"] == "kept" and base["TEMP"] == "user-temp"
    assert base["PIP_CONFIG_FILE"] == canary.os.devnull
    for name in ("PYTHONDONTWRITEBYTECODE", "pip_no_cache_dir", "AGENTTALK_ROOT", "JAVA_HOME"):
        assert name not in base


def test_a_child_whose_writes_were_switched_off_marks_the_run_contaminated():
    clean = {"label": "child", "dont_write_bytecode": False, "pip_cache_dir_resolved": "team-root/cache/pip"}
    raw = {
        "team/wrapper": {"process": dict(clean), "child": dict(clean)},
        "team/gate": {"child": {**clean, "pip_cache_dir_resolved": "pip-cache-disabled"}},
    }
    assert canary.contamination(raw) == []
    raw["team/wrapper"]["child"]["dont_write_bytecode"] = True
    raw["team/wrapper"]["process"]["pip_cache_dir_resolved"] = "pip-cache-disabled"
    assert canary.contamination(raw) == [
        "team/wrapper process: pip's cache was switched off",
        "team/wrapper child: bytecode writing was switched off",
    ]


def test_per_project_folders_differ_for_the_same_seat_and_caches_are_shared(tmp_path):
    one = canary.team_variables(tmp_path, "a" * 64)
    two = canary.team_variables(tmp_path, "b" * 64)
    for name in ("TEMP", "TMP", "TMPDIR", "AGENTTALK_SCRATCH", "AGENTTALK_TURN_EVENTS_DIR"):
        assert one[name] != two[name], name
    for name in ("PIP_CACHE_DIR", "npm_config_cache", "PYTHONPYCACHEPREFIX", "XDG_CACHE_HOME"):
        assert one[name] == two[name], name


def _probe_record(**over):
    record = {
        "env": {}, "python_tempdir": "t", "pycache_resolved": "p", "pycache_resolved_for_installed_code": "i",
        "pip_cache_dir_resolved": "c", "dont_write_bytecode": False, "temp_file_written": "w",
        "pycache_written": "pw", "pytest_tmp_path": "pt",
    }
    record.update(over)
    return record


def _complete_raw():
    raw = {}
    for mode in ("baseline", "team"):
        raw[f"{mode}/wrapper"] = {"process": _probe_record(), "child": _probe_record()}
        raw[f"{mode}/gateway"] = {"child": _probe_record()}
        raw[f"{mode}/comprehension"] = {"child": _probe_record()}
    raw["baseline/gate"] = {"external_base": "user-temp"}
    raw["team/gate"] = {"run_root": "r", "child": _probe_record(pip_cache_dir_resolved="pip-cache-disabled")}
    return raw


def test_a_complete_measurement_has_nothing_missing():
    assert canary.incomplete(_complete_raw()) == []


def test_every_expected_boundary_must_be_there_with_a_result_of_its_own():
    assert canary.incomplete({}) == [f"{key}: no result" for key in canary.EXPECTED]
    raw = _complete_raw()
    raw["team/gate"] = {"external_base": "computed-only"}
    assert canary.incomplete(raw) == ["team/gate: the gate did not run", "team/gate child: no probe record"]
    raw = _complete_raw()
    raw["baseline/gate"] = {}
    assert canary.incomplete(raw) == ["baseline/gate: the gate's run folder was not computed"]
    raw = _complete_raw()
    raw["team/gateway"] = {"error": "the inner run timed out after 500 seconds"}
    raw["team/comprehension"] = "not a record"
    raw["team/elsewhere"] = {}
    assert canary.incomplete(raw) == [
        "team/gateway: no result", "team/comprehension: no result", "team/elsewhere: not a boundary the canary runs"]


def test_a_missing_pip_answer_or_an_empty_record_makes_the_measurement_incomplete():
    for key, part, change, expected in [
        ("team/gateway", "child", {"pip_cache_dir_resolved": None},
         "team/gateway child: pip_cache_dir_resolved missing"),
        ("team/gateway", "child", {"temp_file_written": None}, "team/gateway child: temp_file_written missing"),
        ("baseline/wrapper", "process", {"python_tempdir": None}, "baseline/wrapper process: python_tempdir missing"),
        ("team/gate", "child", {"pytest_tmp_path": None}, "team/gate child: pytest_tmp_path missing"),
    ]:
        raw = _complete_raw()
        raw[key][part] = {**raw[key][part], **change}
        assert expected in canary.incomplete(raw), (key, change)
    raw = _complete_raw()
    raw["team/gateway"]["child"] = {}
    assert "team/gateway child: env missing" in canary.incomplete(raw)
    raw = _complete_raw()
    raw["baseline/wrapper"]["child"] = None
    assert canary.incomplete(raw) == ["baseline/wrapper child: no probe record"]


def test_an_inner_run_that_crashes_still_leaves_an_incomplete_report(tmp_path, monkeypatch):
    """Each way an inner run can fail before it returns a record: the report is still written."""
    outcomes = iter(["timeout", "exit", "malformed", "not-a-record"] * 2)

    def inner_run(argv, env, timeout):
        how = next(outcomes)
        raw_out = Path(argv[argv.index("--raw-out") + 1])
        if how == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout)
        if how == "malformed":
            raw_out.write_text("{not json", encoding="utf-8")
        if how == "not-a-record":
            raw_out.write_text("[]", encoding="utf-8")
        return SimpleNamespace(returncode=2 if how == "exit" else 0)

    monkeypatch.setattr(canary.subprocess, "run", inner_run)
    monkeypatch.setattr(canary, "_names", lambda folder: (set(), None))
    # The checkout also gained a compiled file during the run.
    listings = iter([({}, None), ({"gained.pyc": (1, 1)}, None)])
    monkeypatch.setattr(canary, "_compiled_files", lambda root, skip: next(listings))
    out = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["canary", "--team-root", str(tmp_path / "team"), "--out", str(out)])
    assert canary.main() == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["measurement"] == "incomplete" and report["comparison"] == "unknown"
    assert report["missing"] == [f"{key}: no result" for key in canary.EXPECTED]
    assert report["checkout"]["new_or_changed_compiled_files"] == 1
    assert report["failures"][-1] == "the checkout gained or changed 1 compiled files"
    timed_out = f"the inner run timed out after {canary.INNER_SECONDS} seconds"
    assert {line.split(": ", 1)[1] for line in report["failures"][:-1]} == {
        timed_out,
        "the inner run exited 2",
        "the inner run's result is unreadable (JSONDecodeError)",
        "the inner run's result is not a record",
    }
    assert report["results"]["team/wrapper"] == {"error": timed_out}


def test_the_whole_interpreter_option_prefix_is_kept_even_with_an_added_option():
    assert canary.interpreter_prefix(["py", "-s", "-S", "-m", "mod"]) == ["py", "-s", "-S"]
    assert canary.interpreter_prefix(["py", "-s", "-S", "-B", "-m", "mod"]) == ["py", "-s", "-S", "-B"]
    assert canary.interpreter_prefix(["py", "-B", "-s", "-S", "-m", "mod"]) == ["py", "-B", "-s", "-S"]


def test_a_location_inside_a_property_is_classified_and_the_property_kept(tmp_path):
    anchors = [("team-root", Path(os.path.normcase(str(tmp_path))), 2)]
    value = "-Dmaven.repo.local=" + str(tmp_path / "cache" / "maven" / "x")
    assert canary.classify(value, anchors) == "-Dmaven.repo.local=team-root/cache/maven/..."


def test_the_pip_query_sets_its_own_no_config_control_without_touching_the_process(monkeypatch):
    done = SimpleNamespace(returncode=0, stdout=ABSOLUTE + "\n", stderr="")
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)  # the control must not come from outside
    before = os.environ.get("PIP_CONFIG_FILE")
    with mock.patch.object(probe.subprocess, "run", return_value=done) as run:
        probe._pip_cache_dir()
    assert run.call_args.kwargs["env"]["PIP_CONFIG_FILE"] == os.devnull
    assert run.call_args.kwargs["env"]["PYTHONDONTWRITEBYTECODE"] == "1"  # pip writes no compiled files
    assert os.environ.get("PIP_CONFIG_FILE") == before  # the probe's own process is untouched


def _bytecode_on() -> dict:
    """This environment with ordinary bytecode writing and none of the canary's own settings."""
    return {key: value for key, value in os.environ.items()
            if key.upper() not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")
            and not key.upper().startswith("AGENTTALK_")}


def _fresh_checkout(tmp_path: Path) -> Path:
    """A miniature checkout holding only the canary's three scripts, with no compiled files."""
    folder = tmp_path / "checkout" / "tests" / "support" / "team_folder_canary"
    folder.mkdir(parents=True)
    for name in ("canary.py", "probe.py", "child.py"):
        shutil.copyfile(CANARY_DIR / name, folder / name)
    return folder


def test_the_canary_command_writes_no_compiled_files_into_the_checkout(tmp_path):
    folder = _fresh_checkout(tmp_path)
    done = subprocess.run([sys.executable, str(folder / "canary.py"), "--help"], env=_bytecode_on(),
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-500:]
    assert list((tmp_path / "checkout").rglob("*.pyc")) == []
    # The control: in this environment an ordinary import of the probe does write one.
    subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import probe", str(folder)],
                   env=_bytecode_on(), check=True, timeout=120)
    assert [path.name.split(".")[0] for path in (tmp_path / "checkout").rglob("*.pyc")] == ["probe"]


def test_a_childs_probe_import_writes_no_compiled_file_and_its_measurement_is_unchanged(tmp_path):
    folder = _fresh_checkout(tmp_path)
    work, out, temp = tmp_path / "work", tmp_path / "record.json", tmp_path / "temp"
    temp.mkdir()
    env = {**_bytecode_on(), "TEMP": str(temp), "TMP": str(temp), "TMPDIR": str(temp)}
    done = subprocess.run([sys.executable, str(folder / "child.py"), "--canary", str(out), str(work), "child", "1"],
                          env=env, cwd=str(tmp_path), capture_output=True, text=True, timeout=240)
    assert done.returncode == 0, done.stderr[-500:]
    assert list((tmp_path / "checkout").rglob("*.pyc")) == []
    record = json.loads(out.read_text(encoding="utf-8"))
    assert record["dont_write_bytecode"] is False  # measured as the process started
    assert record["pycache_written"] and Path(record["pycache_written"]).is_relative_to(work)


def test_compiled_files_the_checkout_gains_or_changes_are_counted_and_a_failed_listing_is_unknown(tmp_path):
    files, error = canary._compiled_files(tmp_path / "missing", tmp_path / "team")
    assert files is None and error == "FileNotFoundError"
    assert canary.checkout_summary(None, {}, [error]) == {
        "status": "unknown", "errors": ["FileNotFoundError"], "new_or_changed_compiled_files": None}
    root, team = tmp_path / "checkout", tmp_path / "checkout" / "team"
    cache = root / "pkg" / "__pycache__"
    for folder in (cache, root / ".git", team):
        folder.mkdir(parents=True)
    (root / ".git" / "not-the-checkout.pyc").write_bytes(b"x")
    (team / "the-team-root.pyc").write_bytes(b"x")
    old = cache / "old.pyc"
    old.write_bytes(b"1")
    before, error = canary._compiled_files(root, team)
    assert error is None and list(before) == [str(old)]
    old.write_bytes(b"22")
    (cache / "new.pyc").write_bytes(b"3")
    after, _error = canary._compiled_files(root, team)
    assert canary.checkout_summary(before, after, [])["new_or_changed_compiled_files"] == 2
    assert canary.checkout_summary(before, before, [])["new_or_changed_compiled_files"] == 0
    assert canary._compiled_files(root, team, limit=1) == (None, "stopped at the 1-entry limit")


_HOSTILE = "[global]\nno-cache-dir = true\n"


def _pip_config_files(env: dict, cwd: Path) -> list[Path]:
    """Every configuration file pip says it reads in this environment (pip config debug)."""
    done = subprocess.run([sys.executable, "-m", "pip", "config", "debug"], env=env, cwd=str(cwd),
                          capture_output=True, text=True, timeout=240)
    assert done.returncode == 0, done.stderr[-500:]
    files = []
    for line in done.stdout.splitlines():
        if ", exists:" in line:
            files.append((cwd / line.split(", exists:")[0].strip()).resolve())
    return files


def _plant_hostile_pip_config(env: dict, folder: Path) -> list[Path]:
    """A config that switches pip's cache off, planted at each place pip reads that lies
    inside this test's own folder; never anywhere else."""
    planted = []
    for path in _pip_config_files(env, folder):
        if folder.resolve() in path.parents:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_HOSTILE, encoding="ascii")
            planted.append(path)
    return planted


def _ask_pip(env: dict, cwd: Path, *, through_probe: bool) -> object:
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); import probe; "
            "print(json.dumps(probe._pip_cache_dir()))") if through_probe else (
           "import json, subprocess, sys; done = subprocess.run([sys.executable, '-m', 'pip', 'cache', 'dir'], "
           "capture_output=True, text=True); print(json.dumps([done.returncode, 'cache is disabled' in done.stderr]))")
    done = subprocess.run([sys.executable, "-c", code, str(CANARY_DIR)], env=env, cwd=str(cwd),
                          capture_output=True, text=True, timeout=240)
    assert done.returncode == 0, done.stderr[-500:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def _boundary_environments(tmp_path: Path) -> dict:
    from agenttalk.comprehension import worker
    from agenttalk.wrapper import run

    project = tmp_path / "project"
    project.mkdir()
    # The gateway-backed child moves its home into the project folder.
    gateway = run._child_env(project, backend_profile="ovh-qwen", profile_env={
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000", "ANTHROPIC_AUTH_TOKEN": "canary-placeholder"})
    # The comprehension worker keeps the caller's home folders: point them into the test folder.
    appdata, home = tmp_path / "appdata", tmp_path / "home"
    appdata.mkdir()
    home.mkdir()
    source = {key: value for key, value in os.environ.items() if not key.upper().startswith("PIP_")}
    source.update(APPDATA=str(appdata), HOME=str(home), USERPROFILE=str(home))
    source.pop("XDG_CONFIG_HOME", None)
    comprehension = worker.sanitized_worker_env(source)
    return {"gateway": gateway, "comprehension": comprehension}


def test_a_hostile_pip_config_behind_both_filtered_boundaries_cannot_switch_the_query_off(tmp_path):
    for name, env in _boundary_environments(tmp_path).items():
        assert "PIP_CONFIG_FILE" not in env, name  # the boundary drops the outer control
        planted = _plant_hostile_pip_config(env, tmp_path)
        assert planted, (name, "pip reads no config file inside the test folder in this environment")
        _exit_code, disabled = _ask_pip(env, tmp_path, through_probe=False)
        assert disabled, (name, "the planted config must switch an uncontrolled pip query off")
        answer, _warnings = _ask_pip(env, tmp_path, through_probe=True)
        assert answer not in (None, "pip-cache-disabled") and os.path.isabs(answer), (name, answer)


def _evidence_rows(text: str) -> dict[str, list[str]]:
    """The design note's evidence table: file name -> its cells after the name."""
    rows = {}
    for line in text.splitlines():
        match = re.match(r"^\| `(windows-[^`]+\.json)` \|(.*)\|$", line)
        if match:
            rows[match.group(1)] = [cell.strip() for cell in match.group(2).split("|")]
    return rows


def test_the_design_note_states_what_each_evidence_file_recorded():
    text = DESIGN.read_text(encoding="utf-8")
    rows = _evidence_rows(text)
    assert sorted(rows) == sorted(path.name for path in EVIDENCE_DIR.glob("*.json"))
    for name, cells in rows.items():
        report = json.loads((EVIDENCE_DIR / name).read_text(encoding="utf-8"))
        assert cells == [
            report["python"],
            "; ".join(report["failures"]) or "none",
            report["measurement"],
            report["comparison"],
            str(report["user_temp"]["surviving_new_top_level_names"]),
            str(report["user_temp"]["surviving_new_names_like_the_canary_s"]),
            str(report["checkout"]["new_or_changed_compiled_files"]),
        ], name
    # The summary sentences say no more than the table holds.
    all_clean = len(rows) == 3 and all(cells[1:4] == ["none", "complete", "clean"] for cells in rows.values())
    assert ("All three runs had no failures, a complete measurement and a clean comparison." in text) == all_clean
    none_left = all(cells[4] == "0" for cells in rows.values())
    assert ("no new top-level name remained in that run's own temp folder" in " ".join(text.split())) == none_left
