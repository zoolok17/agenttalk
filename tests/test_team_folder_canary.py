"""The team-folder canary's own evidence rules (tests/support/team_folder_canary).

The canary is evidence for the #336 design, so its judgement calls are pinned
here: pip's cache folder comes only from a successful run's standard output, a
listing of the user's temp folder that fails or hits its limit is unknown, never an
empty result, and a boundary run that did not complete makes the canary fail.
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
