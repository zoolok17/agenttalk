"""Tests for the install-skills bundled-copy mechanism."""

from __future__ import annotations

from pathlib import Path

from agenttalk.install_skills import SKILLS_ROOT, install


def test_bundled_skills_exist_in_package() -> None:
    """The bundled source dir must contain the canonical skill files;
    without them install-skills is a no-op and the README lies."""
    claude_dir = SKILLS_ROOT / "claude"
    codex_dir = SKILLS_ROOT / "codex"
    assert claude_dir.is_dir()
    assert codex_dir.is_dir()
    claude_files = sorted(p.name for p in claude_dir.glob("*.md"))
    assert claude_files == [
        "agenttalk.handoff.md",
        "agenttalk.listen.md",
        "agenttalk.send.md",
        "agenttalk.sk-loop.md",
    ]
    codex_subdirs = sorted(p.name for p in codex_dir.iterdir() if p.is_dir())
    assert codex_subdirs == [
        "agenttalk-handoff",
        "agenttalk-listen",
        "agenttalk-send",
        "agenttalk-sk-loop",
    ]
    for sub in codex_subdirs:
        assert (codex_dir / sub / "SKILL.md").is_file()


def test_fresh_install_copies_all_files(tmp_path: Path) -> None:
    claude_dir = tmp_path / "claude"
    codex_dir = tmp_path / "codex"
    res = install(claude_dir=claude_dir, codex_dir=codex_dir)
    counts = res.counts()
    assert counts.get("copied") == 8
    # Layout
    assert (claude_dir / "agenttalk.send.md").is_file()
    assert (codex_dir / "agenttalk-send" / "SKILL.md").is_file()


def test_second_install_is_unchanged(tmp_path: Path) -> None:
    claude_dir = tmp_path / "claude"
    codex_dir = tmp_path / "codex"
    install(claude_dir=claude_dir, codex_dir=codex_dir)
    res = install(claude_dir=claude_dir, codex_dir=codex_dir)
    assert res.counts().get("unchanged") == 8


def test_existing_modified_target_is_skipped_without_force(tmp_path: Path) -> None:
    claude_dir = tmp_path / "claude"
    codex_dir = tmp_path / "codex"
    install(claude_dir=claude_dir, codex_dir=codex_dir)
    # Mutate one target
    target = claude_dir / "agenttalk.send.md"
    target.write_text("user local edits\n", encoding="utf-8")
    res = install(claude_dir=claude_dir, codex_dir=codex_dir)
    counts = res.counts()
    assert counts.get("skipped") == 1
    assert counts.get("unchanged") == 7
    # Confirm we didn't overwrite the user's edit
    assert target.read_text(encoding="utf-8") == "user local edits\n"


def test_force_overwrites_differing_targets(tmp_path: Path) -> None:
    claude_dir = tmp_path / "claude"
    codex_dir = tmp_path / "codex"
    install(claude_dir=claude_dir, codex_dir=codex_dir)
    target = claude_dir / "agenttalk.send.md"
    target.write_text("user local edits\n", encoding="utf-8")
    res = install(claude_dir=claude_dir, codex_dir=codex_dir, force=True)
    assert res.counts().get("copied") == 1
    assert res.counts().get("unchanged") == 7


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    claude_dir = tmp_path / "claude"
    codex_dir = tmp_path / "codex"
    res = install(claude_dir=claude_dir, codex_dir=codex_dir, dry_run=True)
    assert res.counts().get("would-copy") == 8
    assert not claude_dir.exists()
    assert not codex_dir.exists()


def test_claude_only_skips_codex(tmp_path: Path) -> None:
    claude_dir = tmp_path / "claude"
    codex_dir = tmp_path / "codex"
    res = install(claude=True, codex=False, claude_dir=claude_dir, codex_dir=codex_dir)
    counts = res.counts()
    assert counts.get("copied") == 4
    assert claude_dir.is_dir()
    assert not codex_dir.exists()
