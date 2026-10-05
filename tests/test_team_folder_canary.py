"""The team-folder canary's own evidence rules (tests/support/team_folder_canary).

The canary is evidence for the #336 design, so its judgement calls are pinned
here: pip's cache folder comes only from a successful run's standard output, a
listing of the user's temp folder that fails or hits its limit is unknown, never an
empty result, a boundary run that did not complete makes the canary fail, ambient
switches are cleared from both modes, a run whose writes something else switched
off is labelled contaminated, a missing measurement makes the run incomplete, and
the pip query reads no pip configuration even behind the filtered child boundaries.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

CANARY_DIR = Path(__file__).parent / "support" / "team_folder_canary"


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
    return {
        "baseline/wrapper": {"process": _probe_record(), "child": _probe_record()},
        "baseline/gate": {"external_base": "user-temp"},
        "team/gateway": {"child": _probe_record()},
        "team/gate": {"run_root": "r", "child": _probe_record(pip_cache_dir_resolved="pip-cache-disabled")},
    }


def test_a_complete_measurement_has_nothing_missing():
    assert canary.incomplete(_complete_raw()) == []


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


def test_the_whole_interpreter_option_prefix_is_kept_even_with_an_added_option():
    assert canary.interpreter_prefix(["py", "-s", "-S", "-m", "mod"]) == ["py", "-s", "-S"]
    assert canary.interpreter_prefix(["py", "-s", "-S", "-B", "-m", "mod"]) == ["py", "-s", "-S", "-B"]
    assert canary.interpreter_prefix(["py", "-B", "-s", "-S", "-m", "mod"]) == ["py", "-B", "-s", "-S"]


def test_a_location_inside_a_property_is_classified_and_the_property_kept(tmp_path):
    anchors = [("team-root", Path(os.path.normcase(str(tmp_path))), 2)]
    value = "-Dmaven.repo.local=" + str(tmp_path / "cache" / "maven" / "x")
    assert canary.classify(value, anchors) == "-Dmaven.repo.local=team-root/cache/maven/..."


def test_the_pip_query_sets_its_own_no_config_control_without_touching_the_process():
    done = SimpleNamespace(returncode=0, stdout=ABSOLUTE + "\n", stderr="")
    before = os.environ.get("PIP_CONFIG_FILE")
    with mock.patch.object(probe.subprocess, "run", return_value=done) as run:
        probe._pip_cache_dir()
    assert run.call_args.kwargs["env"]["PIP_CONFIG_FILE"] == os.devnull
    assert os.environ.get("PIP_CONFIG_FILE") == before  # the probe's own process is untouched


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
