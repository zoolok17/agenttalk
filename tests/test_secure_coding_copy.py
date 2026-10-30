"""The shipped copy of the secure-coding standard's general part (part 1).

docs/SECURE-CODING.md is the source. Its part 1, between two marker lines, is
copied into the skills so that anyone who installs them has the rules without
the docs folder. These tests keep the copy equal to the source and keep part 1
free of the project's own names, so that it can ship to any project.
"""

from __future__ import annotations

from pathlib import Path

from agenttalk.install_skills import SKILLS_ROOT, install

PAGE = Path("docs") / "SECURE-CODING.md"
COPY_REL = Path("_shared") / "references" / "secure-coding.md"
BEGIN = "<!-- secure-coding:general:begin -->"
END = "<!-- secure-coding:general:end -->"
VERSION_LINE = "Version: secure-coding@1"

# Words that belong to agenttalk's own part (part 2), not to the general rules.
PROJECT_WORDS = (
    "agenttalk", "roster", "validate_agent_name", "dashboard", "gateway", "liaison",
    "the bus", "knowledge store", "ROLES-SKILLS", "SECURITY.md", "CONTRIBUTING", "docs/", "src/agenttalk",
)


def _page() -> str:
    return PAGE.read_text(encoding="utf-8").replace("\r\n", "\n")


def _marked() -> str:
    page = _page()
    assert page.count(BEGIN) == 1 and page.count(END) == 1, "exactly one marker pair"
    before, rest = page.split(BEGIN)
    marked, _after = rest.split(END)
    assert marked.strip(), "the marked text is not empty"
    return marked.strip() + "\n"


def _copy() -> str:
    return (SKILLS_ROOT / "devkit" / COPY_REL).read_text(encoding="utf-8").replace("\r\n", "\n")


def test_marker_lines_come_once_and_in_order() -> None:
    page = _page()
    assert page.index(BEGIN) < page.index(END)
    _marked()


def test_the_shipped_copy_body_is_identical_to_the_marked_text() -> None:
    copy = _copy()
    assert copy.count(VERSION_LINE + "\n\n") == 1
    header, body = copy.split(VERSION_LINE + "\n\n", 1)
    assert body == _marked()
    assert header.startswith("---\nreviewed-against:") and "\n# " in header and BEGIN not in copy and END not in copy


def test_the_marked_text_and_its_copy_name_nothing_of_the_project() -> None:
    for name, text in (("marked text", _marked()), ("shipped copy", _copy())):
        found = [word for word in PROJECT_WORDS if word.lower() in text.lower()]
        assert not found, f"{name} names the project: {found}"


def test_the_version_in_the_copy_is_the_version_the_page_names() -> None:
    assert VERSION_LINE.split(": ")[1] in _page()


def test_the_copy_installs_and_can_be_read_beside_the_skills(tmp_path: Path) -> None:
    cl, cx = tmp_path / "cl", tmp_path / "cx"
    install(claude=False, codex=False, devkit=True, claude_skills_dir=cl, codex_skills_dir=cx)
    for root in (cl, cx):
        installed = root / COPY_REL
        assert installed.read_text(encoding="utf-8").replace("\r\n", "\n") == _copy()
        # the pointers in the skills use this relative path from a skill folder
        assert (root / "craft-code" / ".." / COPY_REL).resolve() == installed.resolve()
