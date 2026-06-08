"""Copy bundled skill files out to the global locations Claude Code and
Codex actually scan.

Two skill families ship in the package:

* **Bus skills** (the agenttalk collaboration commands). Claude reads these
  as slash commands from ``~/.claude/commands/*.md``; Codex reads them from
  ``~/.codex/skills/<name>/SKILL.md``. The two sides differ in format, so they
  have separate sources under ``src/agenttalk/skills/{claude,codex}/``.
* **Devkit skills** (the dev-discipline pack: craft-code, test-coverage,
  review-code, write-docs, review-docs — a non-spec-kitty fallback). These are
  byte-identical Agent-Skills ``SKILL.md`` folders for BOTH agents, so a single
  source under ``src/agenttalk/skills/devkit/<name>/`` installs to BOTH
  ``~/.claude/skills/<name>/`` and ``~/.codex/skills/<name>/``.

This module copies them out on demand.
"""

from __future__ import annotations

import filecmp
import shutil
from dataclasses import dataclass, field
from pathlib import Path


SKILLS_ROOT = Path(__file__).parent / "skills"


def default_claude_dir() -> Path:
    return Path.home() / ".claude" / "commands"


def default_codex_dir() -> Path:
    return Path.home() / ".codex" / "skills"


def default_claude_skills_dir() -> Path:
    """Claude Agent-Skills dir — where the devkit installs (auto-invocable +
    gives the /<name> command). Distinct from the bus-command dir."""
    return Path.home() / ".claude" / "skills"


def default_codex_skills_dir() -> Path:
    return Path.home() / ".codex" / "skills"


@dataclass
class FileAction:
    src: Path
    dst: Path
    # "copied"          — wrote a new or overwritten file
    # "unchanged"       — target byte-identical to source, no write
    # "skipped"         — target differs, --force not set, no write
    # "would-copy"      — dry-run: target absent, would write
    # "would-overwrite" — dry-run: target differs AND --force, would write
    # "would-skip"      — dry-run: target differs, --force NOT set, would NOT write
    status: str


@dataclass
class InstallResult:
    actions: list[FileAction] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.actions:
            out[a.status] = out.get(a.status, 0) + 1
        return out


def _claude_pairs(claude_dir: Path) -> list[tuple[Path, Path]]:
    """Source .md files for Claude side, paired with their target paths."""
    src_dir = SKILLS_ROOT / "claude"
    pairs: list[tuple[Path, Path]] = []
    for src in sorted(src_dir.glob("*.md")):
        pairs.append((src, claude_dir / src.name))
    return pairs


def _codex_pairs(codex_dir: Path) -> list[tuple[Path, Path]]:
    """Source SKILL.md files for Codex side, paired with their target paths.

    The Codex layout is folder-per-skill: src/agenttalk/skills/codex/<name>/SKILL.md
    maps to <codex_dir>/<name>/SKILL.md.
    """
    src_dir = SKILLS_ROOT / "codex"
    pairs: list[tuple[Path, Path]] = []
    if not src_dir.exists():
        return pairs
    for skill_dir in sorted(src_dir.iterdir()):
        if not skill_dir.is_dir():
            continue
        src = skill_dir / "SKILL.md"
        if not src.exists():
            continue
        dst = codex_dir / skill_dir.name / "SKILL.md"
        pairs.append((src, dst))
    return pairs


def _devkit_pairs(claude_skills_dir: Path, codex_skills_dir: Path) -> list[tuple[Path, Path]]:
    """Dev-discipline pack, paired with BOTH agents' Agent-Skills dirs.

    The devkit is format-identical for both agents, so one bundled source under
    ``skills/devkit/<name>/`` maps to ``<claude_skills_dir>/<name>/...`` AND
    ``<codex_skills_dir>/<name>/...``. Each skill folder is copied whole, so a
    nested ``references/`` file (e.g. review-code/references/security.md) goes
    along with the SKILL.md.
    """
    src_dir = SKILLS_ROOT / "devkit"
    pairs: list[tuple[Path, Path]] = []
    if not src_dir.exists():
        return pairs
    for src in sorted(src_dir.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(src_dir)  # e.g. review-code/references/security.md
        pairs.append((src, claude_skills_dir / rel))
        pairs.append((src, codex_skills_dir / rel))
    return pairs


def install(
    *,
    claude: bool = True,
    codex: bool = True,
    devkit: bool = False,
    claude_dir: Path | None = None,
    codex_dir: Path | None = None,
    claude_skills_dir: Path | None = None,
    codex_skills_dir: Path | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> InstallResult:
    """Copy skill files to their global locations.

    ``claude`` / ``codex`` gate the bus skills; ``devkit`` gates the
    dev-discipline pack (installed to BOTH agents' Agent-Skills dirs).
    ``devkit`` defaults False here so library callers (and tests) never write
    to a real HOME implicitly — the CLI turns it on by default.

    Idempotent: if a target file is byte-identical to the source it's
    reported as ``unchanged``. If the target exists but differs and
    ``force`` is False, it's ``skipped`` (the user's edits are
    preserved). With ``force``, the differing file is overwritten.
    """
    result = InstallResult()
    claude_dir = (claude_dir or default_claude_dir()).expanduser()
    codex_dir = (codex_dir or default_codex_dir()).expanduser()
    claude_skills_dir = (claude_skills_dir or default_claude_skills_dir()).expanduser()
    codex_skills_dir = (codex_skills_dir or default_codex_skills_dir()).expanduser()

    pairs: list[tuple[Path, Path]] = []
    if claude:
        pairs.extend(_claude_pairs(claude_dir))
    if codex:
        pairs.extend(_codex_pairs(codex_dir))
    if devkit:
        pairs.extend(_devkit_pairs(claude_skills_dir, codex_skills_dir))

    for src, dst in pairs:
        action = _plan_one(src, dst, force=force, dry_run=dry_run)
        result.actions.append(action)

    return result


def _plan_one(src: Path, dst: Path, *, force: bool, dry_run: bool) -> FileAction:
    if dst.exists():
        # Compare contents to decide unchanged vs differs.
        try:
            same = filecmp.cmp(src, dst, shallow=False)
        except OSError:
            same = False
        if same:
            return FileAction(src=src, dst=dst, status="unchanged")
        if not force:
            # Target differs but we're not allowed to overwrite. In a
            # real run this is `skipped` (nothing happens); in a dry
            # run report it as `would-skip` so the output is visibly
            # different from a non-dry-run — the previous code
            # collapsed both to "skipped" which made `--dry-run` look
            # broken (identical output to a normal run).
            return FileAction(
                src=src, dst=dst,
                status="would-skip" if dry_run else "skipped",
            )
        if dry_run:
            return FileAction(src=src, dst=dst, status="would-overwrite")
        _copy(src, dst)
        return FileAction(src=src, dst=dst, status="copied")

    # Target doesn't exist — fresh install path
    if dry_run:
        return FileAction(src=src, dst=dst, status="would-copy")
    _copy(src, dst)
    return FileAction(src=src, dst=dst, status="copied")


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
