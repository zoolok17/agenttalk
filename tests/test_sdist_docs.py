"""Every file under docs/ that a test opens ships in the source package (sdist), so the tests
inside an unpacked sdist find it. A file only cited in a comment or a sentence does not count.
Inside an unpacked sdist this passes by construction: only shipped docs are there."""

from __future__ import annotations

import ast
import posixpath
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _file_names(source: str) -> set[str]:
    """The last path part of every string in the code; comments are not code."""
    return {node.value.replace("\\", "/").rsplit("/", 1)[-1] for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)}


def test_a_path_in_code_counts_but_a_comment_or_a_sentence_does_not() -> None:
    source = 'open("docs\\\\ops\\\\a.md")\nopen(REPO / "docs" / "b.json")  # c.md\nprint("see d.md first")\n'
    assert {"a.md", "b.json"} <= _file_names(source)
    assert not {"c.md", "d.md"} & _file_names(source)


def test_every_docs_file_a_test_opens_is_in_the_sdist() -> None:
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    # Each entry is one file or folder; none is a pattern today.
    include = [entry.strip("/") for entry in pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]]
    named = set().union(*(_file_names(test.read_text(encoding="utf-8")) for test in (REPO / "tests").rglob("*.py")))
    docs = [path.relative_to(REPO).as_posix() for path in (REPO / "docs").rglob("*") if path.is_file()]
    missing = sorted(path for path in docs if path.rsplit("/", 1)[-1] in named
                     and not any(path == entry or path.startswith(entry + "/") for entry in include))
    assert missing == [], f"add to [tool.hatch.build.targets.sdist] include in pyproject.toml: {missing}"


def _local_links(name: str) -> set[str]:
    """Relative link targets in a Markdown file, as written (no address, no anchor)."""
    text = (REPO / name).read_text(encoding="utf-8")
    links = set(re.findall(r"\]\(([^)\s#]+)(?:#[^)]*)?\)", text))
    return {link for link in links if not re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", link)}


def test_the_links_in_the_readme_and_the_contributing_guide_find_their_files() -> None:
    """Both files ship in the sdist, so each link must point at a file that exists and that the sdist ships
    too (a checkout always has it, which hides the gap). A docs/ page is linked by its web address instead,
    which also works on the PyPI project page: PyPI shows the README there, and no relative link works."""
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    include = [entry.strip("/") for entry in pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]]
    shipped = "LICENSE"  # the build adds the licence file by itself
    readme_links, guide_links = _local_links("README.md"), _local_links("CONTRIBUTING.md")
    assert {"CHANGELOG.md", "SECURITY.md", "CONTRIBUTING.md"} <= readme_links
    assert {"README.md", "SECURITY.md", "LICENSE"} <= guide_links
    # A page under docs/ may not be in the source package, so both link to it by its web address.
    assert not [link for link in readme_links | guide_links if link.startswith("docs/")]
    nowhere = sorted(link for link in readme_links | guide_links if not (REPO / link).exists())
    assert nowhere == [], f"these links point at files that do not exist: {nowhere}"
    missing = sorted(link for link in readme_links | guide_links if link != shipped
                     and not any(link == entry or link.startswith(entry + "/") for entry in include))
    assert missing == [], f"add to [tool.hatch.build.targets.sdist] include in pyproject.toml: {missing}"


# Each shipped docs page the next test reads is named here, so that CI runs the full checks when one of them changes
# (scripts/ci_scope.py looks for page names in the tests). The roadmap is left out on purpose, so that a change to the
# roadmap alone keeps the lighter documentation checks in CI; it is named without ".md" so the scan does not count it.
UNCHECKED_PAGE = "docs/ROADMAP"
SHIPPED_DOCS_PAGES = (
    "docs/AGENT-MANUAL.md", "docs/AGENTTALK-NEW-USER-MANUAL.md", "docs/ASSURANCE.md", "docs/BUDGET-FEED.md",
    "docs/DANGER-AREAS.md", "docs/DASHBOARD-CONTROL-PLANE-DESIGN-HISTORY.md", "docs/DESIGN.md", "docs/DEV-GATE.md",
    "docs/DashboardDesign/BUILD-SPEC.md", "docs/DashboardDesign/design_handoff_team_console/README.md",
    "docs/ISSUES.md", "docs/MIGRATION-PLAYBOOK.md", "docs/OPERATIONS.md", "docs/OVERVIEW.md",
    "docs/ROADMAP-ARCHIVE-2026-06.md", "docs/ROLES-SKILLS-PROCEDURES.md", "docs/SECURE-CODING.md", "docs/USER-MANUAL.md",
    "docs/agenttalk-delivery-os-field-notes.md", "docs/audit-2026-06-28.md",
    "docs/bug-report-2026-07-11-taskkill-windows-0xc0000142.md", "docs/examples/write-for-humans-samples.md",
    "docs/ops/lessons-lookup.md", "docs/ops/scratch-hygiene.md", "docs/research/DOD-REPLAY-2026-10.md",
    "docs/reviews/v0.2.0.md", "docs/reviews/v0.74.0-remediation.md", "docs/rfc-identity-authz.md",
    "docs/skill-devkit-evolution-design.md", "docs/supervisor-hosting.md", "docs/supervisor-liveness-design.md",
    "docs/supervisor-tutorial.md",
)


def test_the_links_in_the_shipped_docs_pages_find_shipped_files() -> None:
    """A shipped docs page may link to another file by a relative path only when that file ships too;
    otherwise it links by web address, as the README does."""
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    include = [entry.strip("/") for entry in pyproject["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]]

    def ships(path: str) -> bool:
        # the build adds the licence file by itself
        return path == "LICENSE" or any(path == entry or path.startswith(entry + "/") for entry in include)

    pages = sorted(path.relative_to(REPO).as_posix() for path in (REPO / "docs").rglob("*.md"))
    pages = [page for page in pages if ships(page) and page.removesuffix(".md") != UNCHECKED_PAGE]
    assert pages == sorted(SHIPPED_DOCS_PAGES), "name every shipped docs page in SHIPPED_DOCS_PAGES, and only those"
    broken = []
    for page in pages:
        for link in sorted(_local_links(page)):
            target = posixpath.normpath(posixpath.join(posixpath.dirname(page), link))
            if not (REPO / target).exists() or not ships(target):
                broken.append(f"{page} -> {link}")
    assert broken == [], f"ship these files, or link them by their github.com/zoolok17/agenttalk address: {broken}"
