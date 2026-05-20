"""Copy bundled skill files out to the global locations Claude Code and
Codex actually scan.

Claude Code reads slash commands from ``~/.claude/commands/*.md``.
Codex reads skills from ``~/.codex/skills/<name>/SKILL.md``.

We ship canonical copies inside the package at
``src/agenttalk/skills/{claude,codex}/...`` and this module copies them
out on demand.
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


@dataclass
class FileAction:
    src: Path
    dst: Path
    status: str  # "would-copy" | "copied" | "unchanged" | "skipped" | "would-overwrite"


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


def install(
    *,
    claude: bool = True,
    codex: bool = True,
    claude_dir: Path | None = None,
    codex_dir: Path | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> InstallResult:
    """Copy skill files to their global locations.

    Idempotent: if a target file is byte-identical to the source it's
    reported as ``unchanged``. If the target exists but differs and
    ``force`` is False, it's ``skipped`` (the user's edits are
    preserved). With ``force``, the differing file is overwritten.
    """
    result = InstallResult()
    claude_dir = (claude_dir or default_claude_dir()).expanduser()
    codex_dir = (codex_dir or default_codex_dir()).expanduser()

    pairs: list[tuple[Path, Path]] = []
    if claude:
        pairs.extend(_claude_pairs(claude_dir))
    if codex:
        pairs.extend(_codex_pairs(codex_dir))

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
            return FileAction(src=src, dst=dst, status="skipped")
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
