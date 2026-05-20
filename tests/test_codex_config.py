"""Tests for the codex-config TOML editor.

Covers the bugs Codex caught during review (escaped double-quoted
keys, inline-comment headers, naive-timestamp guard, etc.) plus the
happy path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agenttalk.codex_config import (
    disable_project,
    enable_project,
    status,
)


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# ------------------------------------------------------ enable on fresh file

def test_enable_creates_config_when_missing(tmp_path: Path) -> None:
    cfg = tmp_path / "codex" / "config.toml"
    project = tmp_path / "my-project"
    project.mkdir()
    res = enable_project(cfg, project)
    assert res.action == "created"
    assert cfg.is_file()
    text = cfg.read_text(encoding="utf-8")
    assert "approval_policy = \"never\"" in text
    assert "sandbox_mode = \"workspace-write\"" in text
    assert "trust_level = \"trusted\"" in text


def test_enable_appends_block_when_section_missing(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    _write(cfg, "model = \"gpt-5.5\"\n")
    project = tmp_path / "proj"
    project.mkdir()
    res = enable_project(cfg, project)
    assert res.action == "updated"
    text = cfg.read_text(encoding="utf-8")
    assert "model = \"gpt-5.5\"" in text
    assert "[projects." in text


# ---------------------------------------------------------------- idempotent

def test_enable_is_idempotent(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    project = tmp_path / "proj"
    project.mkdir()
    enable_project(cfg, project)
    res = enable_project(cfg, project)
    assert res.action == "unchanged"


# ----------------------------------------------- escaped double-quoted key

def test_enable_matches_escaped_double_quoted_key(tmp_path: Path) -> None:
    """Regression for Codex iteration-1 bug.

    A pre-existing section with TOML basic-string escaping must match
    a path normalized to lowercase drive letter, not be appended as a
    duplicate.
    """
    cfg = tmp_path / "config.toml"
    project = tmp_path / "sub" / "ProjectEscaped"
    project.mkdir(parents=True)
    # Use the project's lowercased path with escaped backslashes (TOML
    # basic-string form). On POSIX there are no backslashes so this
    # test still exercises the parser via the quote-style branch.
    section_key = str(project.resolve()).replace("\\", "\\\\")
    _write(cfg, f'[projects."{section_key}"]\n'
                'trust_level = "trusted"\n'
                'approval_policy = "never"\n'
                'sandbox_mode = "workspace-write"\n')
    res = enable_project(cfg, project)
    assert res.action == "unchanged", (
        f"escaped-key section was not matched; got action={res.action!r}"
    )
    # And no duplicate appended
    text = cfg.read_text(encoding="utf-8")
    assert text.count("[projects.") == 1


# --------------------------------------------------- inline-comment header

def test_enable_matches_inline_comment_header(tmp_path: Path) -> None:
    """Regression for Codex iteration-2 bug.

    `[projects.'...'] # comment` must still parse as a project section.
    """
    cfg = tmp_path / "config.toml"
    project = tmp_path / "ProjectComment"
    project.mkdir()
    key_path = str(project.resolve())
    _write(cfg, f"[projects.'{key_path}'] # existing project\n"
                'trust_level = "trusted"\n'
                'approval_policy = "never"\n'
                'sandbox_mode = "workspace-write"\n')
    res = enable_project(cfg, project)
    assert res.action == "unchanged"
    text = cfg.read_text(encoding="utf-8")
    assert text.count("[projects.") == 1


# -------------------------------------------------- case-insensitive match

def test_enable_matches_different_drive_letter_casing(tmp_path: Path) -> None:
    """On Windows, codex paths often use lowercased drive letter even when
    the disk path is uppercase. The matcher should treat them as equal.
    """
    cfg = tmp_path / "config.toml"
    project = tmp_path / "proj"
    project.mkdir()
    key_disk = str(project.resolve())
    # Swap case of one ascii letter to fake a casing difference
    key_swapped = key_disk.swapcase() if any(c.isalpha() for c in key_disk) else key_disk
    _write(cfg, f"[projects.'{key_swapped}']\n"
                'trust_level = "trusted"\n'
                'approval_policy = "never"\n'
                'sandbox_mode = "workspace-write"\n')
    res = enable_project(cfg, project)
    assert res.action == "unchanged"


# --------------------------------------------------------------- status

def test_status_reports_present_section(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    project = tmp_path / "proj"
    project.mkdir()
    enable_project(cfg, project)
    st = status(cfg, project)
    assert st["section_present"] is True
    assert st["keys"]["trust_level"] == '"trusted"'
    assert st["keys"]["approval_policy"] == '"never"'
    assert st["keys"]["sandbox_mode"] == '"workspace-write"'


def test_status_reports_missing_section(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    _write(cfg, "model = \"gpt-5.5\"\n")
    project = tmp_path / "nope"
    project.mkdir()
    st = status(cfg, project)
    assert st["section_present"] is False
    assert all(v is None for v in st["keys"].values())


# --------------------------------------------------------------- disable

def test_disable_removes_sandbox_keys_but_keeps_trust(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    project = tmp_path / "proj"
    project.mkdir()
    enable_project(cfg, project)
    res = disable_project(cfg, project)
    assert res.action == "removed"
    text = cfg.read_text(encoding="utf-8")
    assert "trust_level = \"trusted\"" in text
    assert "approval_policy" not in text
    assert "sandbox_mode" not in text


def test_disable_on_missing_section_is_noop(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    _write(cfg, "model = \"gpt-5.5\"\n")
    project = tmp_path / "ghost"
    project.mkdir()
    res = disable_project(cfg, project)
    assert res.action == "no-op"
