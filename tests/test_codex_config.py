"""Tests for the codex-config TOML editor.

Covers the bugs Codex caught during review (escaped double-quoted
keys, inline-comment headers, naive-timestamp guard, etc.) plus the
happy path.
"""

from __future__ import annotations

from pathlib import Path


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


def test_enable_preserves_crlf_line_endings(tmp_path: Path) -> None:
    """A CRLF config (Notepad default on Windows) must keep its line
    endings — the old read_text() folded \\r\\n to \\n and silently
    rewrote the user's whole file to LF (review M4a)."""
    cfg = tmp_path / "config.toml"
    cfg.write_bytes(b'model = "gpt-5.5"\r\nother = 1\r\n')
    project = tmp_path / "proj"
    project.mkdir()
    enable_project(cfg, project)
    raw = cfg.read_bytes()
    assert b"\r\n" in raw                              # CRLF preserved
    assert b"\r\r" not in raw                          # no double-translation
    assert raw.count(b"\n") == raw.count(b"\r\n")      # every \n is part of \r\n


def test_disable_preserves_crlf_line_endings(tmp_path: Path) -> None:
    cfg = tmp_path / "config.toml"
    project = tmp_path / "proj"
    project.mkdir()
    enable_project(cfg, project)
    # rewrite to CRLF, then disable, and assert endings survive
    cfg.write_bytes(cfg.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8"))
    disable_project(cfg, project)
    raw = cfg.read_bytes()
    assert b"\r\n" in raw
    assert b"\r\r" not in raw
    assert raw.count(b"\n") == raw.count(b"\r\n")


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


# -------------------------------------------------- TOML quoting / escaping

def test_enable_quotes_apostrophe_path_as_basic_string(tmp_path: Path) -> None:
    """Regression: paths containing a single quote (e.g. `Bob's Repo`)
    were written as `[projects.'.../Bob's Repo']`, producing invalid
    TOML. Now they must be emitted as basic strings with proper
    escapes.
    """
    cfg = tmp_path / "config.toml"
    project = tmp_path / "Bob's Repo"
    project.mkdir()
    res = enable_project(cfg, project)
    assert res.action == "created"
    text = cfg.read_text(encoding="utf-8")
    # Must use double-quoted basic string, not invalid literal
    assert "[projects.\"" in text
    assert "Bob's Repo" in text
    # And re-enabling must be idempotent (the parser must round-trip).
    # Re-read the file after the second call before counting, since
    # the on-disk content is what users (and Codex) actually see.
    res2 = enable_project(cfg, project)
    assert res2.action == "unchanged"
    text_after = cfg.read_text(encoding="utf-8")
    assert text_after.count("[projects.") == 1


def test_enable_escapes_backslashes_in_basic_string_paths(tmp_path: Path) -> None:
    """When emitting a basic string (because the path contains a
    single quote), backslashes must be escaped per TOML basic-string
    rules so re-parsing recovers the original path.
    """
    cfg = tmp_path / "config.toml"
    project = tmp_path / "weird's-dir"
    project.mkdir()
    enable_project(cfg, project)
    text = cfg.read_text(encoding="utf-8")
    # On Windows the path contains backslashes; on POSIX it doesn't.
    # Either way, idempotent re-enable must round-trip cleanly.
    res2 = enable_project(cfg, project)
    assert res2.action == "unchanged", (
        f"non-idempotent — text was {text!r}"
    )


# -------------------------------------------------- atomic write contract

def test_all_write_sites_use_atomic_write(tmp_path: Path, monkeypatch) -> None:
    """codex_config writes the user's GLOBAL Codex config; a crash
    must never leave a half-written file. After the fix, ALL three
    write paths (create, update, disable) must go through
    agenttalk._atomic.write_text (temp-file + os.replace).
    """
    from agenttalk import codex_config
    calls: list[tuple[str, str]] = []  # (action, path)
    original = codex_config._atomic_write_text

    def spy(path, text, **kwargs):
        calls.append((str(path), text[:40]))
        return original(path, text, **kwargs)

    monkeypatch.setattr(codex_config, "_atomic_write_text", spy)
    cfg = tmp_path / "config.toml"
    project = tmp_path / "proj"
    project.mkdir()

    # Path 1: enable on missing config (create)
    enable_project(cfg, project)
    create_calls = len(calls)
    assert create_calls >= 1, "enable_project (create) must call atomic write"

    # Path 2: enable that mutates an existing section (update)
    # Pre-populate a section that's missing one managed key so enable
    # has work to do, forcing the update write path.
    cfg.write_text(
        "[projects.'" + str(project.resolve()).replace('\\', '\\\\') + "']\n"
        "trust_level = \"trusted\"\n",
        encoding="utf-8",
    )
    calls.clear()
    enable_project(cfg, project)
    assert len(calls) >= 1, "enable_project (update) must call atomic write"

    # Path 3: disable that removes managed keys
    calls.clear()
    disable_project(cfg, project)
    assert len(calls) >= 1, "disable_project must call atomic write"
