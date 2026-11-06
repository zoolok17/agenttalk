"""The lead documents ship inside the wheel (#675). hatch_build.py copies each page to agenttalk/docs/ and
rewrites its links, either to another shipped file or to the same file at this version's tag on GitHub. An
installed lead finds the folder through `agenttalk doctor`, as both lead skills say."""

from __future__ import annotations

import importlib.util
import json
import os
import posixpath
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
# Named literally, so CI runs these tests whenever one of the pages changes (scripts/ci_scope.py).
SHIPPED_PAGES = (
    "docs/LEAD-SETUP-BRIEF.md",
    "docs/LEAD-GUIDE.md",
    "docs/playbook/index.md",
    "docs/playbook/build-and-test.md",
    "docs/playbook/challenges.md",
    "docs/playbook/dispatch.md",
    "docs/playbook/lead-conduct.md",
    "docs/playbook/learning.md",
    "docs/playbook/machine-safety.md",
    "docs/playbook/merge-gate.md",
    "docs/playbook/review.md",
    "docs/playbook/writing-for-people.md",
    "docs/supervisor-tutorial.md",
    "docs/supervisor-hosting.md",
    "docs/USER-MANUAL.md",
)
LEAD_PAGES = SHIPPED_PAGES[:12]  # the brief, the guide and the playbook; the rest are where they send a lead
LEAD_SKILLS = ("skills/claude/agenttalk.lead.md", "skills/codex/agenttalk-lead/SKILL.md")
VERSION = re.search(r'^version = "([^"]+)"', (REPO / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
TAG = f"https://github.com/zoolok17/agenttalk/blob/v{VERSION}/"
DOCS_PATH = re.compile(r"`(docs/[\w./-]+\.md)`")


def _load_hook():
    spec = importlib.util.spec_from_file_location("hatch_build", REPO / "hatch_build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hatch_build = _load_hook()


def _shipped_text(page: str) -> bytes:
    return hatch_build.rewrite_links(page, (REPO / page).read_text(encoding="utf-8"), VERSION).encode("utf-8")


def _lead_documents_section(skill: str) -> str:
    return skill.replace("\r\n", "\n").split("\n## Lead documents\n", 1)[1].split("\n## ", 1)[0]


def test_the_wheel_carries_the_lead_pages_and_leaves_agenttalks_own_pages_out() -> None:
    """The roles page and the agent manual carry agenttalk's own merge and release steps, and the
    secure-coding standard's own part is agenttalk's; the wheel links to them at the tag instead."""
    assert hatch_build.LEAD_DOCS == SHIPPED_PAGES
    playbook = {path.relative_to(REPO).as_posix() for path in (REPO / "docs" / "playbook").glob("*.md")}
    assert playbook <= set(SHIPPED_PAGES), "a new playbook page ships with the others: add it to hatch_build.py"
    assert not {"docs/ROLES-SKILLS-PROCEDURES.md", "docs/AGENT-MANUAL.md", "docs/SECURE-CODING.md"} & set(SHIPPED_PAGES)


@pytest.mark.parametrize("page", SHIPPED_PAGES)
def test_every_link_in_a_shipped_page_reaches_a_shipped_file_or_this_versions_tag(page: str) -> None:
    text = (REPO / page).read_text(encoding="utf-8")
    # The hook rewrites inline links only; a titled or a reference-style link would keep its old target.
    assert not re.search(r"\]\([^)\s]+\s+[\"']", text), f"{page}: drop the link title, the hook skips titled links"
    assert not re.search(r"(?m)^ {0,3}\[[^\]]+\]:\s", text), f"{page}: use an inline link, not a reference"
    shipped = _shipped_text(page).decode("utf-8")
    assert hatch_build.LINK.sub("", shipped) == hatch_build.LINK.sub("", text), "the hook changes link targets only"
    installed_page = "agenttalk/" + page
    for link in hatch_build.LINK.findall(shipped):
        target = link.split("#", 1)[0]
        if target.startswith("https://github.com/zoolok17/agenttalk/"):
            if "/blob/" in target or "/tree/" in target:
                assert target.startswith(TAG), f"{page}: {link} must name the tag v{VERSION}"
                assert (REPO / target.removeprefix(TAG)).exists(), f"{page}: {link} names no file"
        elif target and not re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", target):
            installed = posixpath.normpath(posixpath.join(posixpath.dirname(installed_page), target))
            if installed.startswith("agenttalk/docs/"):
                assert installed.removeprefix("agenttalk/") in SHIPPED_PAGES, f"{page}: {link} is not shipped"
            else:
                assert installed.startswith("agenttalk/"), f"{page}: {link} leaves the package"
                assert (REPO / "src" / installed).is_file(), f"{page}: {link} reaches no package file"


@pytest.mark.parametrize("page", LEAD_PAGES)
def test_a_lead_page_names_in_its_text_only_pages_that_ship(page: str) -> None:
    """A page named in plain text is not a link, so the hook cannot point it at the tag: it must ship."""
    text = (REPO / page).read_text(encoding="utf-8")
    unshipped = set(re.findall(r"(?<!\[)`(docs/[\w./-]+\.md)`(?!\]\()", text)) - set(SHIPPED_PAGES)
    assert not unshipped, f"{page}: link these instead, or ship them: {sorted(unshipped)}"


def test_both_lead_skills_send_a_lead_to_the_installed_pages_the_same_way() -> None:
    claude, codex = ((REPO / "src" / "agenttalk" / skill).read_text(encoding="utf-8") for skill in LEAD_SKILLS)
    section = _lead_documents_section(claude)
    assert section == _lead_documents_section(codex)
    assert "`agenttalk doctor`" in section and "`module path`" in section and "`agenttalk_module_path`" in section
    named = set(DOCS_PATH.findall(section))
    assert "docs/LEAD-GUIDE.md" in named and named <= set(SHIPPED_PAGES)
    for skill in (claude, codex):
        # The secure-coding standard's own part is a rule for agenttalk's own repository only (#675).
        assert "(in agenttalk's own repository: `docs/SECURE-CODING.md`, sections 7 and 8," in " ".join(skill.split())
        assert set(DOCS_PATH.findall(skill)) - set(SHIPPED_PAGES) == {"docs/SECURE-CODING.md"}


def _require_build_tools() -> None:
    """Fail where a dev-gate leg promises build and hatchling (AGENTTALK_TEST_BUILD_TOOLS=1); skip elsewhere."""
    import agenttalk

    if Path(agenttalk.__file__).resolve().parent != (REPO / "src" / "agenttalk").resolve():
        pytest.skip("this run tests an installed wheel; the wheel check runs against the source")
    missing = [name for name in ("build", "hatchling") if importlib.util.find_spec(name) is None]
    if missing:
        reason = f"needs {' and '.join(missing)}: python -m pip install -r dev-gate-requirements.txt"
        if os.environ.get("AGENTTALK_TEST_BUILD_TOOLS") == "1":
            pytest.fail(f"{reason} (this dev-gate leg installs them, so they must be here)", pytrace=False)
        pytest.skip(reason)


@pytest.mark.parametrize("promised", [True, False])
def test_missing_build_tools_fail_where_a_dev_gate_leg_promises_them_and_skip_elsewhere(
    monkeypatch: pytest.MonkeyPatch, promised: bool
) -> None:
    find_spec = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None if name == "build" else find_spec(name))
    monkeypatch.setitem(sys.modules, "agenttalk", type(sys)("agenttalk"))
    sys.modules["agenttalk"].__file__ = str(REPO / "src" / "agenttalk" / "__init__.py")
    if promised:
        monkeypatch.setenv("AGENTTALK_TEST_BUILD_TOOLS", "1")
    else:
        monkeypatch.delenv("AGENTTALK_TEST_BUILD_TOOLS", raising=False)
    # Catch both outcomes: a skip escaping pytest.raises would mark this test skipped, not failed.
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception), match="needs build") as outcome:
        _require_build_tools()
    assert outcome.type is (pytest.fail.Exception if promised else pytest.skip.Exception)


@pytest.mark.subprocess
def test_a_wheel_built_from_the_sdist_carries_the_pages_and_doctor_finds_them_offline(tmp_path: Path) -> None:
    """The challenge's acceptance (#675): build the wheel directly and from the sdist, install it away from the
    checkout without an index, and follow both lead skills to the same pages through `agenttalk doctor`."""
    _require_build_tools()
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("AGENTTALK_")}

    def run(*argv: str, cwd: Path = tmp_path, **extra: str) -> subprocess.CompletedProcess:
        result = subprocess.run([sys.executable, *argv], cwd=cwd, env={**env, **extra},
                                capture_output=True, text=True, timeout=600)
        assert result.returncode == 0, f"{argv[:2]} exit {result.returncode}:\n{result.stdout}\n{result.stderr}"
        return result

    run("-m", "build", "--no-isolation", "--outdir", str(tmp_path / "from-sdist"), str(REPO))  # sdist, then its wheel
    run("-m", "build", "--no-isolation", "--wheel", "--outdir", str(tmp_path / "direct"), str(REPO))
    (wheel,) = (tmp_path / "from-sdist").glob("*.whl")
    (direct,) = (tmp_path / "direct").glob("*.whl")
    expected = {"agenttalk/" + page: _shipped_text(page) for page in SHIPPED_PAGES}
    assert not any(b"\r" in text for text in expected.values())
    for built in (wheel, direct):
        with zipfile.ZipFile(built) as archive:
            docs = {name: archive.read(name) for name in archive.namelist() if name.startswith("agenttalk/docs/")}
        assert docs == expected, f"the lead pages in the {built.parent.name} wheel differ from the hook's output"

    site = tmp_path / "site"
    run("-m", "pip", "install", "--no-index", "--no-deps", "--target", str(site), str(wheel))
    project = tmp_path / "project"
    project.mkdir()
    # Outside a store doctor exits non-zero, but it still prints the module path.
    doctor = subprocess.run([sys.executable, "-m", "agenttalk", "doctor", "--json"], cwd=project,
                            env={**env, "PYTHONPATH": str(site)}, capture_output=True, text=True, timeout=300)
    module = Path(json.loads(doctor.stdout)["agenttalk_module_path"])
    assert module.resolve() == (site / "agenttalk").resolve()
    sections = [_lead_documents_section((module / skill).read_text(encoding="utf-8")) for skill in LEAD_SKILLS]
    assert sections[0] == sections[1]
    for page in sorted(set(DOCS_PATH.findall(sections[0]))):
        assert (module / page).read_bytes() == expected["agenttalk/" + page], page
    assert not (module / "docs" / "SECURE-CODING.md").exists()
