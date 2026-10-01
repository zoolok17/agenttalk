"""Tests for the install-skills bundled-copy mechanism."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from agenttalk import cli
from agenttalk import install_skills as iskl
from agenttalk.install_skills import SKILLS_ROOT, install

DEVKIT_SKILLS = [
    # dev-discipline pack
    "assurance-scan", "craft-code", "fix-ci", "qa-strategy", "refactor-code", "review-code", "review-docs",
    "test-coverage",
    "write-docs", "write-for-humans",
    # assurance review/test pack (P4) — emit P2/P3 close-compatible evidence
    "review-contract-drift", "review-failure-injection", "review-release-readiness",
    "system-review-protocol", "tester-qa", "test-docs", "test-integration",
    "test-performance", "test-security",
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
        "agenttalk.challenge.md",
        "agenttalk.consult.md",
        "agenttalk.handoff.md",
        "agenttalk.lead.md",
        "agenttalk.listen.md",
        "agenttalk.propose.md",
        "agenttalk.send.md",
    ]
    codex_subdirs = sorted(p.name for p in codex_dir.iterdir() if p.is_dir())
    assert codex_subdirs == [
        "agenttalk-challenge",
        "agenttalk-consult",
        "agenttalk-handoff",
        "agenttalk-lead",
        "agenttalk-listen",
        "agenttalk-propose",
        "agenttalk-send",
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


# ---------------------------------------------------- retired skills (sk-loop)
#
# Warning-only (PR #224 fix round 2): install-skills never deletes a
# retired skill's install destination. Two rounds of an automatic
# byte-identical delete each turned up a new correctness problem (a
# stationary directory junction, then a hash-to-unlink race, a fail-open
# lstat error, an unchecked root/ancestor, AND no cross-platform-safe
# definition of "byte-identical" since a CRLF checkout and an LF checkout
# hash the same shipped content differently) — so the feature is
# recast to detect-and-warn only. One test per vendor is enough: the
# leftover is reported, the file is untouched, and dry-run reports the
# same (there is nothing for --dry-run to preview, since neither path
# ever writes).

_FAKE_RETIRED_SKILL = {
    "name": "agenttalk.sk-loop.md",
    "claude_rel": "agenttalk.sk-loop.md",
    "codex_rel": "agenttalk-sk-loop/SKILL.md",
}


def test_real_retired_skills_table_matches_no_bundled_source() -> None:
    """The retired-skill table's whole point is that the source is GONE;
    guard against someone re-adding a same-named source without also
    dropping the (now stale) retired-skill entry."""
    assert not (SKILLS_ROOT / "claude" / "agenttalk.sk-loop.md").exists()
    assert not (SKILLS_ROOT / "codex" / "agenttalk-sk-loop").exists()


def test_fresh_install_reports_no_retired_skills(tmp_path: Path) -> None:
    """A fresh install has nothing to detect: no leftover retired-skill file
    exists at the destination, so `retired` stays empty."""
    claude_dir = tmp_path / "claude"
    codex_dir = tmp_path / "codex"
    res = install(claude_dir=claude_dir, codex_dir=codex_dir)
    assert res.retired == []


def test_existing_claude_install_warns_and_leaves_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leftover installed agenttalk.sk-loop.md is reported, by exact
    path, and never touched — regardless of its content."""
    monkeypatch.setattr(iskl, "_RETIRED_SKILLS", (_FAKE_RETIRED_SKILL,))
    claude_dir = tmp_path / "claude"
    codex_dir = tmp_path / "codex"
    claude_dir.mkdir(parents=True)
    target = claude_dir / "agenttalk.sk-loop.md"
    target.write_text("operator-authored mission notes, do not delete\n", encoding="utf-8")

    res = install(claude_dir=claude_dir, codex_dir=codex_dir)

    statuses = {str(r.path): r.status for r in res.retired}
    assert statuses[str(target)] == "warn"
    assert target.exists()
    assert target.read_text(encoding="utf-8") == (
        "operator-authored mission notes, do not delete\n"
    )


def test_existing_codex_install_warns_and_leaves_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same as the Claude case, for the folder-per-skill Codex layout."""
    monkeypatch.setattr(iskl, "_RETIRED_SKILLS", (_FAKE_RETIRED_SKILL,))
    claude_dir = tmp_path / "claude"
    codex_dir = tmp_path / "codex"
    (codex_dir / "agenttalk-sk-loop").mkdir(parents=True)
    target = codex_dir / "agenttalk-sk-loop" / "SKILL.md"
    target.write_text("operator-authored mission notes, do not delete\n", encoding="utf-8")

    res = install(claude_dir=claude_dir, codex_dir=codex_dir)

    statuses = {str(r.path): r.status for r in res.retired}
    assert statuses[str(target)] == "warn"
    assert target.exists()
    assert target.read_text(encoding="utf-8") == (
        "operator-authored mission notes, do not delete\n"
    )


def test_dry_run_reports_the_same_warning_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(iskl, "_RETIRED_SKILLS", (_FAKE_RETIRED_SKILL,))
    claude_dir = tmp_path / "claude"
    codex_dir = tmp_path / "codex"
    claude_dir.mkdir(parents=True)
    target = claude_dir / "agenttalk.sk-loop.md"
    target.write_text("still installed\n", encoding="utf-8")

    res = install(claude_dir=claude_dir, codex_dir=codex_dir, dry_run=True)

    statuses = {str(r.path): r.status for r in res.retired}
    assert statuses[str(target)] == "warn"
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "still installed\n"


def test_claude_only_does_not_check_codex_retired_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Honour the same --claude-only/--codex-only scoping the rest of
    install-skills respects."""
    monkeypatch.setattr(iskl, "_RETIRED_SKILLS", (_FAKE_RETIRED_SKILL,))
    claude_dir = tmp_path / "claude"
    codex_dir = tmp_path / "codex"
    claude_dir.mkdir(parents=True)
    (codex_dir / "agenttalk-sk-loop").mkdir(parents=True)
    (codex_dir / "agenttalk-sk-loop" / "SKILL.md").write_text("x", encoding="utf-8")

    res = install(claude=True, codex=False, claude_dir=claude_dir, codex_dir=codex_dir)

    assert res.retired == []
    assert (codex_dir / "agenttalk-sk-loop" / "SKILL.md").exists()


def test_cli_install_skills_warns_with_migration_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    """The CLI surface prints the exact path plus the manual migration
    recipe (remove both installed sk-loop paths, refresh with --force)."""
    monkeypatch.setattr(iskl, "_RETIRED_SKILLS", (_FAKE_RETIRED_SKILL,))
    claude_dir = tmp_path / "claude"
    codex_dir = tmp_path / "codex"
    claude_dir.mkdir(parents=True)
    target = claude_dir / "agenttalk.sk-loop.md"
    target.write_text("still installed\n", encoding="utf-8")

    rc = cli.main([
        "install-skills", "--no-devkit",
        "--claude-dir", str(claude_dir), "--codex-dir", str(codex_dir),
    ])
    out = capsys.readouterr().out

    assert rc == 0
    assert str(target) in out
    assert "install-skills --force" in out
    assert target.exists()


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


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_lead_skills_carry_the_writing_rules_without_the_devkit(tmp_path: Path) -> None:
    """A bus-only install (--no-devkit) has no write-for-humans skill, so the lead
    skills must carry its core rules themselves, in identical words."""
    claude_dir = tmp_path / "claude"
    codex_dir = tmp_path / "codex"
    install(claude_dir=claude_dir, codex_dir=codex_dir)
    claude_lead = _flat((claude_dir / "agenttalk.lead.md").read_text(encoding="utf-8"))
    codex_lead = _flat((codex_dir / "agenttalk-lead" / "SKILL.md").read_text(encoding="utf-8"))
    rule = (
        "use the write-for-humans skill if it is installed. If it is not, follow its "
        "three core rules: (1) to describe a change, write in this order: what changed, "
        "why it matters, what they will notice, what they need to do; (2) use plain "
        "words that are true for this case, never invent a number, name or place, and "
        "explain any technical term you cannot avoid the first time; (3) keep "
        "every fact, with file names, IDs and test names in a Technical details section "
        "at the end, but never include secrets, private data or internal-only addresses, "
        "not even there. A review comment keeps the review's own format and severity tag."
    )
    assert rule in claude_lead
    assert rule in codex_lead


def test_write_for_humans_never_publishes_sensitive_data() -> None:
    """'Keep every fact' must carry its sensitive-data exception right beside it."""
    text = (SKILLS_ROOT / "devkit" / "write-for-humans" / "SKILL.md").read_text(encoding="utf-8")
    section = _flat(text.split("## PRECISION WITHOUT CLUTTER", 1)[1].split("\n## ", 1)[0])
    keep = section.index("**Keep every fact.**")
    exception = section.index("**Except anything sensitive, which this rule never covers.**")
    assert keep < exception < section.index("The main text must make sense")
    assert "not even in Technical details" in section
    assert "Moving them to the end does not make them safe" in section


def test_write_for_humans_defers_to_the_calling_skills_evidence() -> None:
    from agenttalk.skill_currency import _parse_skill_stub

    text = (SKILLS_ROOT / "devkit" / "write-for-humans" / "SKILL.md").read_text(encoding="utf-8")
    evidence = _flat(text.split("\n## Evidence", 1)[1])
    assert "emit THAT skill's evidence, unchanged" in evidence
    assert "`review-result` fields" in evidence
    whole_job = evidence.split("Only when this skill is the whole job", 1)[1].split("emit the", 1)[0]
    assert "an issue comment" in whole_job
    # the standalone stub stays production-handoff, so the parity check still applies
    assert _parse_skill_stub(text)[0] == "production-handoff"


def test_write_for_humans_review_comments_keep_the_severity_tag() -> None:
    text = (SKILLS_ROOT / "devkit" / "write-for-humans" / "SKILL.md").read_text(encoding="utf-8")
    comments = text.split("### Issue and review comments", 1)[1].split("\n### ", 1)[0]
    assert "**<severity tag, unchanged> What I found:**" in comments
    assert "it never replaces it" in _flat(comments)
    assert "**P1. What I found:**" in comments


def test_write_for_humans_keeps_the_calling_review_format() -> None:
    text = (SKILLS_ROOT / "devkit" / "write-for-humans" / "SKILL.md").read_text(encoding="utf-8")
    comments = _flat(text.split("### Issue and review comments", 1)[1].split("\n### ", 1)[0])
    own_format = comments.index("keep that format exactly: the same fields, labels, severity "
                                "tags and order")
    assert own_format < comments.index("When no format is set, use the template below")
    assert "This skill only changes the words inside it" in comments


def _skill() -> str:
    return (SKILLS_ROOT / "devkit" / "write-for-humans" / "SKILL.md").read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    return text.split(f"\n## {heading}", 1)[1].split("\n## ", 1)[0]


def test_write_for_humans_explains_terms_for_this_case() -> None:
    terms = _flat(_section(_skill(), "TERMS"))
    assert "**The plain words must be true for this case, not in general.**" in terms
    assert "ask what your plain version now claims that the original did not" in terms
    assert "Never invent a number, a name or a place that the source does not give." in terms


def test_write_for_humans_has_no_substitution_table() -> None:
    """A fixed 'say this instead' table is false in some context, so the skill
    carries no markdown table at all."""
    separator = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")
    assert not [ln for ln in _skill().splitlines() if separator.match(ln)]


def test_write_for_humans_examples_are_labelled_and_at_most_three() -> None:
    terms = _section(_skill(), "TERMS")
    assert "They show the method; they are not words to copy." in _flat(terms)
    examples = re.findall(r'\*\*Example (\d+), "([^"]+)"\.\*\*', terms)
    assert examples == [("1", "merged"), ("2", "timeout"), ("3", "race condition")]
    for example in re.split(r"\*\*Example \d+, ", _flat(terms))[1:]:
        assert "Wrong:" in example and "Why it is wrong:" in example and "Right:" in example


def test_write_for_humans_keeps_release_and_deploy_apart() -> None:
    terms = _flat(_section(_skill(), "TERMS"))
    assert ('"Released" means a new version can be installed; it does not mean anyone '
            'is using it yet.') in terms
    assert ('"Deployed" means a running system was switched over to it, so people are '
            'using it now.') in terms


def test_write_for_humans_keeps_null_absent_and_empty_apart() -> None:
    terms = _flat(_section(_skill(), "TERMS"))
    assert "the field is left out entirely (absent);" in terms
    assert 'the field is there but set to "no value" (null);' in terms
    assert "`{field: undefined}` still has the field, while `{}` does not" in terms
    assert "the field is there but empty (an empty text or an empty list)" in terms
    assert "absent or undefined" not in terms


def test_write_for_humans_never_invents_numbers_from_a_percentage() -> None:
    text = _flat((SKILLS_ROOT / "devkit" / "write-for-humans" / "SKILL.md").read_text(encoding="utf-8"))
    assert "1 second instead of 6" not in text
    assert "only when the source measured them; never work them out from a percentage" in text
    assert "(the p95) dropped by 83%" in text


def test_write_for_humans_before_and_now_only_for_changes() -> None:
    text = (SKILLS_ROOT / "devkit" / "write-for-humans" / "SKILL.md").read_text(encoding="utf-8")
    order = _flat(text.split("## ORDER", 1)[1].split("\n## ", 1)[0])
    assert order.index("This order is for text that describes a change") < order.index("1. ")
    assert "Review comments and status reports follow their own templates" in order
    check = _flat(text.split("## SELF-CHECK", 1)[1].split("\n## ", 1)[0])
    assert ("For a release note, changelog entry or pull request description: is \"what "
            "you will notice\" there") in check


def test_write_for_humans_triggers_only_on_its_artifacts() -> None:
    text = (SKILLS_ROOT / "devkit" / "write-for-humans" / "SKILL.md").read_text(encoding="utf-8")
    description = _flat(text.split("description: >-", 1)[1].split("\nreviewed-against:", 1)[0])
    assert "anything a person will read" not in description
    assert "whenever the reader may not be a developer" not in description
    assert description.startswith(
        "Write release notes, changelog entries, pull request descriptions, issue and "
        "review comments, and status reports")
    assert "Use only for those kinds of text." in description
    assert "Do NOT use for answering questions, plans or design discussions" in description


def test_write_for_humans_samples_follow_the_skill() -> None:
    """The reference rewrites must pass the skill's own checks: no issue numbers in the
    reader-facing text, the PR template's sections, and no overstated freshness."""
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "docs" / "examples" / "write-for-humans-samples.md").read_text(encoding="utf-8")
    afters = [a.split("## Sample 2", 1)[0] for a in re.split(r"(?m)^### After$", text)[1:]]
    assert len(afters) == 2
    for after in afters:
        main = re.split(r"(?m)^#{4,5} Technical details$", after)[0]
        assert not re.findall(r"#\d+", main)
    pr_sections = re.findall(r"(?m)^#### (.*)$", afters[1])
    for required in ("What this changes", "What you will notice",
                     "What reviewers and users need to do", "How we checked it",
                     "Technical details"):
        assert required in pr_sections
    assert "never out of date" not in _flat(afters[1])


def test_write_for_humans_samples_ship_in_the_sdist() -> None:
    """The CHANGELOG points readers at the samples, so the sdist must carry them."""
    repo = Path(__file__).resolve().parents[1]
    pyproject = (repo / "pyproject.toml").read_text(encoding="utf-8")
    sdist = pyproject.split("[tool.hatch.build.targets.sdist]", 1)[1].split("\n[", 1)[0]
    assert '"/docs/examples/write-for-humans-samples.md"' in sdist
    assert (repo / "docs" / "examples" / "write-for-humans-samples.md").is_file()


# ---------------------------------------------- the shared plain-language voice

_VOICE_REL = Path("_shared") / "references" / "plain-language.md"


def _voice() -> str:
    return (SKILLS_ROOT / "devkit" / _VOICE_REL).read_text(encoding="utf-8")


def _kind(letter: str) -> str:
    return _voice().split(f"\n### {letter}. ", 1)[1].split("\n### ", 1)[0].split("\n## ", 1)[0]


def test_plain_language_reference_ships_and_installs(tmp_path: Path) -> None:
    assert (SKILLS_ROOT / "devkit" / _VOICE_REL).is_file()
    cl, cx = tmp_path / "cl", tmp_path / "cx"
    install(claude=False, codex=False, devkit=True, claude_skills_dir=cl, codex_skills_dir=cx)
    assert (cl / _VOICE_REL).is_file() and (cx / _VOICE_REL).is_file()
    assert "references/plain-language.md" in (SKILLS_ROOT / "devkit" / "_shared" / "SKILL.md").read_text(
        encoding="utf-8")


@pytest.mark.parametrize("skill, kind", [
    ("write-for-humans", "kind A"),
    ("write-docs", "kind B"),
    ("review-docs", "kind B"),
    ("craft-code", "kind C"),
    ("craft-code", "kind D"),
])
def test_writing_skills_point_to_the_plain_language_voice(skill: str, kind: str) -> None:
    text = _flat((SKILLS_ROOT / "devkit" / skill / "SKILL.md").read_text(encoding="utf-8"))
    assert "../_shared/references/plain-language.md" in text
    assert kind in text


def test_write_for_humans_trigger_is_not_widened_by_the_shared_voice() -> None:
    description = _flat(_skill().split("description: >-", 1)[1].split("\nreviewed-against:", 1)[0])
    positive = description.split("Do NOT use", 1)[0]
    for wider in ("documentation", "code comment", "interface", "README", "dashboard", "anything"):
        assert wider not in positive
    assert "Use only for those kinds of text." in positive


def test_plain_language_kinds_carry_their_length_and_structure_rules() -> None:
    kinds = {"A": "Release notes", "B": "Documentation", "C": "Code comments", "D": "Interface text"}
    for letter, title in kinds.items():
        section = _flat(_kind(letter))
        assert section.startswith(title)
        assert "- Length:" in section and "- Structure:" in section
    assert 'opens with an **"In plain words"** summary' in _flat(_kind("B"))
    assert "about 3 to 6 sentences" in _flat(_kind("B"))
    assert "one or two sentences. Never an essay." in _flat(_kind("C"))
    assert 'No "before" and "now".' in _flat(_kind("C"))
    assert "as short as possible" in _flat(_kind("D"))
    assert "never blames the reader" in _flat(_kind("D"))


def test_plain_language_leaves_agent_messages_compact() -> None:
    out_of_scope = _flat(_voice().split("\n## Out of scope: text for agents", 1)[1])
    assert "Messages between agents, task briefs, bus traffic" in out_of_scope
    assert "stay compact and precise" in out_of_scope
    assert "every extra word costs input" in out_of_scope


@pytest.mark.parametrize("rule", [
    "**The plain words must be true for this case, not in general.**",
    "ask what your plain version now claims that the original did not: where, which branch "
    "or version, who, how many, how long, in what order.",
    "Never invent a number, a name or a place that the source does not give.",
    '"Released" means a new version can be installed; it does not mean anyone is using it yet.',
    "Wherever the language or the data format tells them apart, they are different facts, so "
    "say which one it is: the field is left out entirely (absent); the field is there but set "
    'to "no value" (null); the field is there but holds "undefined" (in JavaScript, '
    "`{field: undefined}` still has the field, while `{}` does not); or the field is there "
    "but empty (an empty text or an empty list).",
])
def test_write_for_humans_and_the_shared_voice_state_the_core_rules_alike(rule: str) -> None:
    """write-for-humans keeps its own copy (a loader may show only SKILL.md), so the
    shared voice and the skill must not drift apart."""
    assert rule in _flat(_voice())
    assert rule in _flat(_skill())


def test_design_documents_may_describe_labelled_proposals() -> None:
    """Specs and design docs are in write-docs' scope, so the shipped-behavior rule must
    not forbid their main content; a proposal is allowed only when labelled."""
    docs = _flat((SKILLS_ROOT / "devkit" / "write-docs" / "SKILL.md").read_text(encoding="utf-8"))
    assert "For **current product documentation**, update docs in the **same change as the code**" in docs
    assert "A **design or specification document** may describe what is not built yet" in docs
    assert "`Status: proposed`" in docs
    assert "never present an unimplemented proposal as existing behavior" in docs
    review = _flat((SKILLS_ROOT / "devkit" / "review-docs" / "SKILL.md").read_text(encoding="utf-8"))
    assert "In a **design or specification document**, proposals are allowed" in review
    assert "An unimplemented proposal presented as existing behavior is **BLOCKING**" in review
    kind_b = _flat(_kind("B"))
    assert "Proposals: a specification or a design document may describe what is not built yet" in kind_b
    assert "Never present an unimplemented proposal as something the software already does." in kind_b


def test_internal_paths_are_hidden_by_audience_and_secrets_everywhere() -> None:
    """Secrets are never shown; internal paths are hidden only from public or unauthorized
    readers, so a local diagnostic keeps the detail that lets the person fix the problem."""
    core = _flat(_voice().split("\n## Core rules", 1)[1].split("\n## ", 1)[0])
    assert "secrets (passwords, keys, tokens) and private or customer data are removed from every text" in core
    assert "removed from anything a public or unauthorized audience sees" in core
    assert "Local diagnostics and private run guides keep the path" in core
    assert "never forces a choice between saying what to do next and keeping something private" in core
    kind_d = _flat(_kind("D"))
    assert "never show a secret (a password, a key, a token), anywhere" in kind_d
    assert "Hide internal paths and hostnames only from people who should not see them" in kind_d
    assert "A local diagnostic keeps the path" in kind_d
    assert "an internal path, including in error details" not in kind_d
    craft = _flat((SKILLS_ROOT / "devkit" / "craft-code" / "SKILL.md").read_text(encoding="utf-8"))
    assert "Hide internal paths only from public or unauthorized readers" in craft
    assert "a local diagnostic keeps the path" in craft
