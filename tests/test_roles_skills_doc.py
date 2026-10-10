"""The roles, skills and procedures page names every shipped skill, and only shipped skills,
so its skill list cannot drift from src/agenttalk/skills/."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PAGE = REPO / "docs" / "ROLES-SKILLS-PROCEDURES.md"
SKILLS = REPO / "src" / "agenttalk" / "skills"


def _shipped() -> set[str]:
    """Every skill install_skills ships: Claude Code files, and Codex and devkit folders."""
    claude = {path.stem for path in (SKILLS / "claude").glob("*.md")}
    folders = {path.name for family in ("codex", "devkit") for path in (SKILLS / family).iterdir()
               if (path / "SKILL.md").is_file()}
    return claude | folders


def _listed() -> list[str]:
    """The skill names in the first column of each table in the page's Skills section."""
    text = PAGE.read_text(encoding="utf-8")
    section = text.split("\n## 3. Skills\n", 1)[1].split("\n## ", 1)[0]
    names = []
    for line in section.splitlines():
        if line.startswith("| `"):
            names += re.findall(r"`([^`]+)`", line.split("|")[1])
    return names


def test_every_shipped_skill_has_an_entry() -> None:
    missing = _shipped() - set(_listed())
    assert not missing, f"add a row in section 3 for: {sorted(missing)}"


def test_every_entry_names_a_shipped_skill() -> None:
    listed = _listed()
    unknown = set(listed) - _shipped()
    assert not unknown, f"section 3 names skills that do not ship: {sorted(unknown)}"
    assert len(listed) == len(set(listed)), "a skill has more than one row in section 3"
