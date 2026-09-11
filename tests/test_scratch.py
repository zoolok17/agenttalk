"""Tests for scratch.py - per-seat scratch root resolution (#148)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenttalk import scratch


def _init_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / ".agenttalk").mkdir(parents=True)
    return project


def test_default_root_is_sibling_atk_scratch(tmp_path):
    project = _init_project(tmp_path)
    assert scratch.resolve_scratch_root(project) == tmp_path / "atk-scratch"


def test_no_config_file_uses_default(tmp_path):
    # No .agenttalk/ at all - resolution must not raise.
    project = tmp_path / "unconfigured"
    project.mkdir()
    assert scratch.resolve_scratch_root(project) == tmp_path / "atk-scratch"


def test_corrupt_config_falls_back_to_default(tmp_path):
    project = _init_project(tmp_path)
    (project / ".agenttalk" / "config.json").write_text("{not json", encoding="utf-8")
    assert scratch.resolve_scratch_root(project) == tmp_path / "atk-scratch"


def test_short_form_string_config(tmp_path):
    project = _init_project(tmp_path)
    custom = tmp_path / "custom-scratch"
    (project / ".agenttalk" / "config.json").write_text(
        json.dumps({"scratch": str(custom)}), encoding="utf-8"
    )
    assert scratch.resolve_scratch_root(project) == custom.resolve()


def test_long_form_dict_config(tmp_path):
    project = _init_project(tmp_path)
    custom = tmp_path / "custom-scratch"
    (project / ".agenttalk" / "config.json").write_text(
        json.dumps({"scratch": {"root": str(custom), "keep_days": 7}}), encoding="utf-8"
    )
    assert scratch.resolve_scratch_root(project) == custom.resolve()
    assert scratch.resolve_keep_days(project) == 7


def test_keep_days_defaults_when_absent(tmp_path):
    project = _init_project(tmp_path)
    assert scratch.resolve_keep_days(project) == scratch.DEFAULT_KEEP_DAYS


def test_task_scratch_dir_creates_agent_and_task_dirs(tmp_path):
    project = _init_project(tmp_path)
    path = scratch.task_scratch_dir(project, "dev-2", "task148")
    assert path == tmp_path / "atk-scratch" / "dev-2" / "task148"
    assert path.is_dir()


def test_task_scratch_dir_defaults_task_to_default(tmp_path):
    project = _init_project(tmp_path)
    path = scratch.task_scratch_dir(project, "dev-2", None)
    assert path.name == "default"


def test_agent_scratch_dir_without_create(tmp_path):
    project = _init_project(tmp_path)
    path = scratch.agent_scratch_dir(project, "dev-2", create=False)
    assert not path.exists()
    assert path == tmp_path / "atk-scratch" / "dev-2"


@pytest.mark.parametrize("bad", ["", "..", "a/b", "a\\b", "-leading-dash", ".leading-dot"])
def test_validate_segment_rejects_unsafe_names(bad):
    with pytest.raises(ValueError):
        scratch.validate_segment(bad, label="agent name")


def test_validate_segment_accepts_safe_names():
    assert scratch.validate_segment("dev-2_task.148", label="agent name") == "dev-2_task.148"
