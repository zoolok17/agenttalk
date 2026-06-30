"""Tests for the install-skills bundled-copy mechanism."""

from __future__ import annotations

from pathlib import Path

from agenttalk import cli
from agenttalk.install_skills import SKILLS_ROOT, install

DEVKIT_SKILLS = [
    # dev-discipline pack
    "craft-code", "fix-ci", "qa-strategy", "refactor-code", "review-code", "review-docs", "test-coverage", "write-docs",
    # assurance review/test pack (P4) — emit P2/P3 close-compatible evidence
    "review-contract-drift", "review-failure-injection", "review-release-readiness",
    "system-review-protocol", "tester-qa",
    # shared reference-holder (Tier 0b): category=reference, not an invocable skill;
    # carries references/evidence.md + references/routing.md.
    "_shared",
]


def _bundled_devkit_file_count() -> int:
    """Every file under skills/devkit/ (SKILL.md per skill + nested references/)."""
    return sum(1 for p in (SKILLS_ROOT / "devkit").rglob("*") if p.is_file())


def _bundled_claude_count() -> int:
    return len(list((SKILLS_ROOT / "claude").glob("*.md")))


def _bundled_codex_count() -> int:
    return len([
        d for d in (SKILLS_ROOT / "codex").iterdir()
        if d.is_dir() and (d / "SKILL.md").is_file()
    ])


def _bundled_total() -> int:
    """Total bundled skill files. Derived from the source so adding a
    skill (e.g. agenttalk.propose) doesn't require touching a magic
    number in every count assertion — while still catching a real
    bundled→installed mismatch."""
    return _bundled_claude_count() + _bundled_codex_count()


def test_bundled_skills_exist_in_package() -> None:
    """The bundled source dir must contain the canonical skill files;
    without them install-skills is a no-op and the README lies."""
    claude_dir = SKILLS_ROOT / "claude"
    codex_dir = SKILLS_ROOT / "codex"
    assert claude_dir.is_dir()
    assert codex_dir.is_dir()
    claude_files = sorted(p.name for p in claude_dir.glob("*.md"))
    assert claude_files == [
        "agenttalk.consult.md",
        "agenttalk.handoff.md",
        "agenttalk.lead.md",
        "agenttalk.listen.md",
        "agenttalk.propose.md",
        "agenttalk.send.md",
        "agenttalk.sk-loop.md",
    ]
    codex_subdirs = sorted(p.name for p in codex_dir.iterdir() if p.is_dir())
    assert codex_subdirs == [
        "agenttalk-consult",
        "agenttalk-handoff",
        "agenttalk-lead",
        "agenttalk-listen",
        "agenttalk-propose",
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
    assert counts.get("copied") == _bundled_total()
    # Layout
    assert (claude_dir / "agenttalk.send.md").is_file()
    assert (claude_dir / "agenttalk.consult.md").is_file()
    assert (codex_dir / "agenttalk-send" / "SKILL.md").is_file()
    assert (codex_dir / "agenttalk-consult" / "SKILL.md").is_file()


def test_second_install_is_unchanged(tmp_path: Path) -> None:
    claude_dir = tmp_path / "claude"
    codex_dir = tmp_path / "codex"
    install(claude_dir=claude_dir, codex_dir=codex_dir)
    res = install(claude_dir=claude_dir, codex_dir=codex_dir)
    assert res.counts().get("unchanged") == _bundled_total()


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
    assert counts.get("unchanged") == _bundled_total() - 1
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
    assert res.counts().get("unchanged") == _bundled_total() - 1


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    claude_dir = tmp_path / "claude"
    codex_dir = tmp_path / "codex"
    res = install(claude_dir=claude_dir, codex_dir=codex_dir, dry_run=True)
    assert res.counts().get("would-copy") == _bundled_total()
    assert not claude_dir.exists()
    assert not codex_dir.exists()


def test_dry_run_reports_would_skip_when_target_differs_no_force(
    tmp_path: Path,
) -> None:
    """v0.7.2 regression: --dry-run used to collapse to "skipped"
    for differing targets, which made --dry-run output identical
    to a real run and looked like a broken flag. It must now
    surface as "would-skip" so users can see at a glance that
    --dry-run did report intent."""
    claude_dir = tmp_path / "claude"
    codex_dir = tmp_path / "codex"
    install(claude_dir=claude_dir, codex_dir=codex_dir)
    target = claude_dir / "agenttalk.send.md"
    target.write_text("user local edits\n", encoding="utf-8")
    res = install(claude_dir=claude_dir, codex_dir=codex_dir, dry_run=True)
    counts = res.counts()
    assert counts.get("would-skip") == 1
    assert counts.get("unchanged") == _bundled_total() - 1
    # And the file is untouched, of course.
    assert target.read_text(encoding="utf-8") == "user local edits\n"


def test_dry_run_with_force_reports_would_overwrite_no_writes(
    tmp_path: Path,
) -> None:
    """v0.7.2: --dry-run --force previews exactly what --force
    would do without writing. The user's recommended path is
    "dry-run --force first, then --force"."""
    claude_dir = tmp_path / "claude"
    codex_dir = tmp_path / "codex"
    install(claude_dir=claude_dir, codex_dir=codex_dir)
    target = claude_dir / "agenttalk.send.md"
    target.write_text("user local edits\n", encoding="utf-8")
    res = install(
        claude_dir=claude_dir, codex_dir=codex_dir,
        force=True, dry_run=True,
    )
    counts = res.counts()
    assert counts.get("would-overwrite") == 1
    assert counts.get("unchanged") == _bundled_total() - 1
    assert target.read_text(encoding="utf-8") == "user local edits\n", (
        "--dry-run --force must not write anything"
    )


def test_claude_only_skips_codex(tmp_path: Path) -> None:
    claude_dir = tmp_path / "claude"
    codex_dir = tmp_path / "codex"
    res = install(claude=True, codex=False, claude_dir=claude_dir, codex_dir=codex_dir)
    counts = res.counts()
    assert counts.get("copied") == _bundled_claude_count()
    assert claude_dir.is_dir()
    assert not codex_dir.exists()


# ---------------------------------------------------- devkit (dev-discipline pack)

def test_bundled_devkit_skills_exist_in_package() -> None:
    """The dev-discipline pack ships in the package as Agent-Skills folders."""
    root = SKILLS_ROOT / "devkit"
    assert root.is_dir()
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == sorted(DEVKIT_SKILLS)
    for s in DEVKIT_SKILLS:
        assert (root / s / "SKILL.md").is_file()
    # review-code carries a nested progressive-disclosure reference file
    assert (root / "review-code" / "references" / "security.md").is_file()


def test_devkit_is_off_by_default_in_install_function(tmp_path: Path) -> None:
    """Library default keeps devkit OFF so callers/tests never touch a real
    HOME implicitly — a plain install writes only the bus skills."""
    cl_skills = tmp_path / "clskills"
    cx_skills = tmp_path / "cxskills"
    res = install(
        claude_dir=tmp_path / "claude", codex_dir=tmp_path / "codex",
        claude_skills_dir=cl_skills, codex_skills_dir=cx_skills,
    )
    assert res.counts().get("copied") == _bundled_total()
    assert not cl_skills.exists() and not cx_skills.exists()  # devkit untouched


def test_devkit_installs_to_both_scopes(tmp_path: Path) -> None:
    """One bundled source → BOTH ~/.claude/skills and ~/.codex/skills, whole
    folders (nested references/ travels with the SKILL.md)."""
    cl = tmp_path / "cl"
    cx = tmp_path / "cx"
    res = install(claude=False, codex=False, devkit=True,
                  claude_skills_dir=cl, codex_skills_dir=cx)
    assert res.counts().get("copied") == _bundled_devkit_file_count() * 2
    for s in DEVKIT_SKILLS:
        assert (cl / s / "SKILL.md").is_file()
        assert (cx / s / "SKILL.md").is_file()
    assert (cl / "review-code" / "references" / "security.md").is_file()
    assert (cx / "review-code" / "references" / "security.md").is_file()


def test_devkit_second_install_unchanged(tmp_path: Path) -> None:
    cl = tmp_path / "cl"
    cx = tmp_path / "cx"
    install(claude=False, codex=False, devkit=True, claude_skills_dir=cl, codex_skills_dir=cx)
    res = install(claude=False, codex=False, devkit=True, claude_skills_dir=cl, codex_skills_dir=cx)
    assert res.counts().get("unchanged") == _bundled_devkit_file_count() * 2


def test_devkit_skill_frontmatter_is_well_formed() -> None:
    """Each devkit SKILL.md needs name==dir, a description, and the
    'Do NOT use' disambiguation clause that makes auto-invocation precise."""
    for s in DEVKIT_SKILLS:
        text = (SKILLS_ROOT / "devkit" / s / "SKILL.md").read_text(encoding="utf-8")
        assert f"name: {s}" in text, f"{s}: name must equal dir"
        assert "description:" in text, f"{s}: missing description"
        if s == "_shared":
            # the reference-holder (category=reference, Tier 0b) is not an auto-invocable
            # capability, so it carries a do-not-invoke clause rather than the capability
            # 'Do NOT use ... (use X)' disambiguation.
            assert "category: reference" in text, "_shared must be category=reference"
            assert "do not invoke" in text.lower(), "_shared must say do-not-invoke"
        else:
            assert "Do NOT use" in text, f"{s}: missing 'Do NOT use' disambiguation"


def test_cli_install_devkit_only(tmp_path: Path) -> None:
    """`install-skills --devkit-only` writes only the pack (no bus dirs)."""
    cl = tmp_path / "cl"
    cx = tmp_path / "cx"
    bus_cl = tmp_path / "buscl"
    bus_cx = tmp_path / "buscx"
    rc = cli.main([
        "install-skills", "--devkit-only",
        "--claude-dir", str(bus_cl), "--codex-dir", str(bus_cx),
        "--claude-skills-dir", str(cl), "--codex-skills-dir", str(cx),
    ])
    assert rc == 0
    assert (cl / "craft-code" / "SKILL.md").is_file()
    assert (cx / "review-code" / "references" / "security.md").is_file()
    assert not bus_cl.exists() and not bus_cx.exists()  # bus skills skipped


def test_cli_default_installs_bus_and_devkit(tmp_path: Path) -> None:
    """A plain `install-skills` (all dirs overridden) writes bus + devkit; the
    devkit lands in the Agent-Skills dirs, distinct from the bus-command dir."""
    bus_cl = tmp_path / "buscl"
    bus_cx = tmp_path / "buscx"
    cl = tmp_path / "cl"
    cx = tmp_path / "cx"
    rc = cli.main([
        "install-skills",
        "--claude-dir", str(bus_cl), "--codex-dir", str(bus_cx),
        "--claude-skills-dir", str(cl), "--codex-skills-dir", str(cx),
    ])
    assert rc == 0
    assert (bus_cl / "agenttalk.listen.md").is_file()       # bus skill
    assert (cl / "craft-code" / "SKILL.md").is_file()       # devkit
    assert (cx / "test-coverage" / "SKILL.md").is_file()


def test_cli_no_devkit_skips_pack(tmp_path: Path) -> None:
    bus_cl = tmp_path / "buscl"
    bus_cx = tmp_path / "buscx"
    cl = tmp_path / "cl"
    cx = tmp_path / "cx"
    rc = cli.main([
        "install-skills", "--no-devkit",
        "--claude-dir", str(bus_cl), "--codex-dir", str(bus_cx),
        "--claude-skills-dir", str(cl), "--codex-skills-dir", str(cx),
    ])
    assert rc == 0
    assert (bus_cl / "agenttalk.listen.md").is_file()
    assert not cl.exists() and not cx.exists()  # devkit skipped


def test_listen_skills_contain_consult_handling(tmp_path: Path) -> None:
    """The listen skill bodies must route `meta consult=true` messages
    via the consult-handling section, not the generic question path."""
    claude_dir = tmp_path / "claude"
    codex_dir = tmp_path / "codex"
    install(claude_dir=claude_dir, codex_dir=codex_dir)
    claude_listen = (claude_dir / "agenttalk.listen.md").read_text(encoding="utf-8")
    codex_listen = (codex_dir / "agenttalk-listen" / "SKILL.md").read_text(encoding="utf-8")
    for body in (claude_listen, codex_listen):
        assert "Consult handling" in body
        assert "meta.consult=true" in body
        assert "Do NOT modify project files" in body
        assert "Do NOT answer the user directly" in body
        assert "Do NOT start your own consult in return" in body
