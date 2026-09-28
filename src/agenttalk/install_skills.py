"""Copy bundled skill files out to the global locations Claude Code and
Codex actually scan.

Two skill families ship in the package:

* **Bus skills** (the agenttalk collaboration commands). Claude reads these
  as slash commands from ``~/.claude/commands/*.md``; Codex reads them from
  ``~/.codex/skills/<name>/SKILL.md``. The two sides differ in format, so they
  have separate sources under ``src/agenttalk/skills/{claude,codex}/``.
* **Devkit skills** (the dev-discipline pack: craft-code, test-coverage,
  review-code, write-docs, review-docs). These are byte-identical
  Agent-Skills ``SKILL.md`` folders for BOTH agents, so a single source under
  ``src/agenttalk/skills/devkit/<name>/`` installs to BOTH
  ``~/.claude/skills/<name>/`` and ``~/.codex/skills/<name>/``.

This module copies them out on demand.
"""

from __future__ import annotations

import filecmp
import hashlib
import os
import shutil
import stat
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
    retired: list["RetiredSkillAction"] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.actions:
            out[a.status] = out.get(a.status, 0) + 1
        return out


@dataclass
class RetiredSkillAction:
    path: Path
    # "removed"       — byte-identical to the last-shipped version, deleted
    # "would-remove"  — dry-run: would have deleted (byte-identical)
    # "warn-modified" — present, does NOT match the last-shipped bytes; left
    #                   alone (never destroy a file that might be modified)
    # "warn-linked"   — a path component between the install root and the
    #                   file is a symlink/junction/reparse point; left alone
    #                   even if the bytes match (content identity does not
    #                   prove this is an installer-owned copy safe to delete)
    status: str
    linked_at: Path | None = None


# Skills retired from the bundled tree. Deleting the SOURCE does not remove
# an already-installed copy (fresh-install pairs only enumerate surviving
# sources), so on every install-skills run we separately check each retired
# skill's install DESTINATION: byte-identical to the last-shipped content
# means it was never modified, so it is safe to delete; anything else is
# left alone with a warning naming the exact path. This is deliberately a
# single hardcoded table, not a general uninstall framework.
_RETIRED_SKILLS: tuple[dict, ...] = (
    {
        "name": "agenttalk.sk-loop.md",
        "claude_rel": "agenttalk.sk-loop.md",
        "codex_rel": "agenttalk-sk-loop/SKILL.md",
        "claude_sha256": "4e65e65fd189cb662dce755c5342165b9a4347e166f11e968e61e107f3b6ffef",
        "codex_sha256": "8b7b6f990d1cb1247b0d5f79c82aa5ba05dbfed55f4a69408455770050d219f8",
    },
)


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _is_link_like(path: Path) -> bool:
    """True if `path` ITSELF (not what it resolves to) is a symlink, a
    Windows junction, or carries any other reparse point. Uses an
    un-followed stat (lstat) so it never resolves through a link to judge
    the link by its target's properties."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    # Windows junctions (dir "mount points") are reparse points but are
    # NOT reported by S_ISLNK on every Python build; st_file_attributes
    # (Windows-only os.stat_result field) catches those and any other
    # reparse-point tag directly.
    attrs = getattr(st, "st_file_attributes", None)
    return attrs is not None and bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _linked_component(root: Path, rel: str) -> Path | None:
    """Walk `root / rel` component by component; return the first path
    (strictly beneath `root`) that is itself a symlink/junction/reparse
    point, or None if every component down to the file is a plain
    directory/file. A regular file (e.g. SKILL.md) sitting beneath a
    linked PARENT directory is still caught, because the parent
    directory component is checked on the way down."""
    current = root
    for part in Path(rel).parts:
        current = current / part
        if _is_link_like(current):
            return current
    return None


def check_retired_skills(
    *,
    claude: bool,
    codex: bool,
    claude_dir: Path,
    codex_dir: Path,
    dry_run: bool,
) -> list[RetiredSkillAction]:
    """Detect retired-skill leftovers at install destinations.

    Only acts on a destination that still exists. Before hashing or
    deleting anything, every path component between the configured
    install root and the file is checked for being a symlink/junction/
    reparse point — matching bytes through a link only proves the
    content is familiar, never that this is an installer-owned copy safe
    to delete, so a linked component always wins over a byte match and is
    reported ``warn-linked`` without ever being hashed or touched.
    Otherwise, a byte-identical match against the last-shipped content is
    deleted (or reported as ``would-remove`` under ``--dry-run``);
    anything else — including a file this table doesn't know how to
    verify — is reported as ``warn-modified`` and never touched.
    """
    out: list[RetiredSkillAction] = []
    for retired in _RETIRED_SKILLS:
        candidates: list[tuple[Path, str, str]] = []
        if claude:
            candidates.append((claude_dir, retired["claude_rel"], retired["claude_sha256"]))
        if codex:
            candidates.append((codex_dir, retired["codex_rel"], retired["codex_sha256"]))
        for root, rel, expected_sha256 in candidates:
            dst = root / rel
            if not dst.exists():
                continue
            linked = _linked_component(root, rel)
            if linked is not None:
                out.append(RetiredSkillAction(path=dst, status="warn-linked", linked_at=linked))
                continue
            if _sha256(dst) == expected_sha256:
                if dry_run:
                    out.append(RetiredSkillAction(path=dst, status="would-remove"))
                else:
                    dst.unlink()
                    out.append(RetiredSkillAction(path=dst, status="removed"))
            else:
                out.append(RetiredSkillAction(path=dst, status="warn-modified"))
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

    if claude or codex:
        result.retired = check_retired_skills(
            claude=claude,
            codex=codex,
            claude_dir=claude_dir,
            codex_dir=codex_dir,
            dry_run=dry_run,
        )

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
