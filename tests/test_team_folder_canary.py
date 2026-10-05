"""The team-folder canary's own evidence rules (tests/support/team_folder_canary).

The canary is evidence for the #336 design, so its judgement calls are pinned
here: pip's cache folder comes only from a successful run's standard output, a
listing of the user's temp folder that fails or hits its limit is unknown, never an
empty result, a boundary run that did not complete makes the canary fail, ambient
switches are cleared from both modes, and a run whose writes something else switched
off is labelled contaminated.
"""

from __future__ import annotations

import importlib.util
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
