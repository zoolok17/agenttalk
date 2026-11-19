"""The README and the new-user manual open in the plain-language voice, and the stale PDF
manual is no longer shipped (issue #260)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
MANUAL = REPO / "docs" / "AGENTTALK-NEW-USER-MANUAL.md"
OPERATOR_MANUAL = REPO / "docs" / "USER-MANUAL.md"
PDF = "docs/AGENTTALK-NEW-USER-MANUAL.pdf"
PIN = re.compile(r'pip install "git\+https://github\.com/zoolok17/agenttalk\.git@(v\d+\.\d+\.\d+)"')


def _opener(path: Path) -> str:
    """The "In plain words" paragraph right after the document's title."""
    text = path.read_text(encoding="utf-8")
    body = text.split("\n", 1)[1].lstrip("\n")
    assert body.startswith("**In plain words:**"), f"{path.name} must open with the summary"
    return " ".join(body.split("\n\n", 1)[0].split())


def _sentences(paragraph: str) -> int:
    return len(re.findall(r"[.!?](?=\s|$)", paragraph))


def test_readme_and_manual_open_with_a_plain_summary() -> None:
    for path in (README, MANUAL):
        assert 3 <= _sentences(_opener(path)) <= 6, path.name


ANY_PIN = re.compile(r"pip install\s+\"?git\+https://github\.com/zoolok17/agenttalk\.git@(v\d+\.\d+\.\d+)")
# the two places a manual teaches a first install: its own section, which now points to the README
INSTALL_SECTIONS = [
    pytest.param(OPERATOR_MANUAL, "## 2. Install and initialize", id="user-manual"),
    pytest.param(MANUAL, "## 3. Install and initialize", id="new-user-manual"),
]
# a line that starts a bare interpreter running pip install (the old recipe), and one that runs any bare `-m`
BARE_PIP_INSTALL = re.compile(r"(?m)^\s*(?:python3?|py)(?:\.exe)?\s+-m\s+pip\s+install")
BARE_MODULE_RUN = re.compile(r"(?m)^\s*(?:python3?|py)(?:\.exe)?\s+-m\b")


def _install_section(path: Path, heading: str) -> str:
    text = path.read_text(encoding="utf-8")
    assert f"\n{heading}\n" in text, f"{path.name} has no {heading!r}"
    return text.split(f"\n{heading}\n", 1)[1].split("\n## ", 1)[0]


def _fenced_lines(text: str) -> list[str]:
    """Every line inside a fenced code block (prose that quotes a command is not one)."""
    lines: list[str] = []
    for body in re.findall(r"```[^\n]*\n(.*?)```", text, re.S):
        lines.extend(body.splitlines())
    return lines


@pytest.mark.parametrize("path, heading", INSTALL_SECTIONS)
def test_a_manual_sends_the_reader_to_the_readme_install_steps(path: Path, heading: str) -> None:
    """The manuals no longer teach their own install: one recipe, in the README, and the manual says what it does."""
    section = " ".join(_install_section(path, heading).split())
    assert "](../README.md#get-started)" in section
    for words in ("AGENTTALK_PY", "first on PATH", "do not activate another Python environment"):
        assert words in section, f"{path.name}: the install section does not say {words!r}"
    # the warning covers every terminal where a person or an assistant runs agenttalk, and the way to check it
    assert "In any terminal where you or an assistant run agenttalk" in section
    assert "`(Get-Command agenttalk).Source`" in section and "`command -v agenttalk`" in section
    assert "pip install" not in section


# the lines of the README's Get started that mark where the do-it-yourself install starts and stops
README_INSTALL_START = "Give agenttalk its own Python environment"
README_PROJECT_SETUP = "Then, in your project's top folder:"


@pytest.mark.parametrize("path, heading", INSTALL_SECTIONS)
def test_a_manual_points_to_the_readme_install_steps_only_and_stops_before_its_project_setup(
    path: Path, heading: str
) -> None:
    """The README goes on, after the install, to set up a project with its own team names; a reader who goes on there
    and then follows the manual's own init finds the first team list kept (a second init without --force keeps it)."""
    started = README.read_text(encoding="utf-8").split("## Get started\n", 1)[1].split("\n## ", 1)[0]
    assert README_INSTALL_START in started and started.count(README_PROJECT_SETUP) == 1  # the stop point is real
    assert started.index(README_INSTALL_START) < started.index(README_PROJECT_SETUP)
    section = " ".join(_install_section(path, heading).split())
    assert "the README's do-it-yourself steps in [Get started](../README.md#get-started)" in section
    assert f'start at "{README_INSTALL_START}" and stop before the line "{README_PROJECT_SETUP}"' in section
    assert "Do not follow the README's project setup after that line" in section
    assert "a second `init` without `--force` keeps the team list of the first" in section
    # the setup brief is a different route with its own version: if it is named, it is named as that, never linked
    assert "LEAD-SETUP-BRIEF" not in section
    assert "different route, with its own version" in section
    # the pointer never again sends the reader through the whole section
    assert "with the steps in the README's [Get started]" not in section


def test_the_new_user_manual_claims_one_version_only_for_the_steps_that_install_it() -> None:
    section = " ".join(_install_section(MANUAL, "## 3. Install and initialize").split())
    assert "The README's steps install a released version" not in section
    assert ("Those steps install the one released version the README names, not a branch, so every agent that "
            "follows them runs the same version") in section


@pytest.mark.parametrize("path, heading", INSTALL_SECTIONS)
def test_no_fenced_line_in_an_install_section_starts_with_a_bare_python_m(path: Path, heading: str) -> None:
    section = _install_section(path, heading)
    offenders = [line for line in _fenced_lines(section) if BARE_MODULE_RUN.match(line)]
    assert offenders == [], f"{path.name}: use the environment's Python (AGENTTALK_PY), not a bare one: {offenders}"


@pytest.mark.parametrize("path", [MANUAL, OPERATOR_MANUAL], ids=["new-user-manual", "user-manual"])
def test_no_fenced_line_anywhere_in_a_manual_is_the_old_bare_pip_install(path: Path) -> None:
    """The guard against the old recipe coming back. Only fenced blocks are searched, so prose that quotes a command
    (the user manual mentions `python -m agenttalk` in a sentence about searching) cannot trip it."""
    offenders = [line for line in _fenced_lines(path.read_text(encoding="utf-8")) if BARE_PIP_INSTALL.match(line)]
    assert offenders == [], f"{path.name}: {offenders}"


def test_the_manuals_name_no_install_pin_only_the_readme_does() -> None:
    """A pin in a manual is a second version to bump at every release; the manuals point to the README instead."""
    for path in (MANUAL, OPERATOR_MANUAL):
        assert ANY_PIN.findall(path.read_text(encoding="utf-8")) == [], path.name


def test_the_readme_pins_the_release_this_tree_ships() -> None:
    """The one pin that must keep passing: the README's install line equals pyproject.toml's version. (The manuals
    no longer carry a pin: see test_the_manuals_name_no_install_pin_only_the_readme_does.)"""
    readme_pins = set(ANY_PIN.findall(README.read_text(encoding="utf-8")))
    assert readme_pins
    version = re.search(r'^version = "([^"]+)"', (REPO / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
    assert readme_pins == {f"v{version}"}


def test_readme_links_the_lead_setup_brief_at_the_release_it_installs() -> None:
    """#675: the brief a new user hands their assistant matches the version the README installs,
    not the newest text on master."""
    readme = README.read_text(encoding="utf-8")
    version = re.search(r'^version = "([^"]+)"', (REPO / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
    assert f"https://github.com/zoolok17/agenttalk/blob/v{version}/docs/LEAD-SETUP-BRIEF.md" in readme
    assert "blob/master/docs/LEAD-SETUP-BRIEF.md" not in readme


def test_stale_pdf_manual_is_not_shipped() -> None:
    assert not (REPO / PDF).exists()
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    sdist = pyproject.split("[tool.hatch.build.targets.sdist]", 1)[1].split("\n[", 1)[0]
    assert '"/docs/AGENTTALK-NEW-USER-MANUAL.md"' in sdist
    assert "AGENTTALK-NEW-USER-MANUAL.pdf" not in sdist
    manifest = json.loads((REPO / "dev-gate.json").read_text(encoding="utf-8"))
    required = manifest["checks"]["package-build"]["required_sdist_paths"]
    assert "docs/AGENTTALK-NEW-USER-MANUAL.md" in required
    assert PDF not in required
    assert "the old PDF copy was out of date and is no longer shipped" in _opener(MANUAL)


def _section(text: str, heading: str) -> str:
    return text.split(f"\n### {heading}\n", 1)[1].split("\n### ", 1)[0]


def test_readme_lists_the_built_in_network_integrations() -> None:
    """The gateway address, the package index and the Semgrep rule sets named here are
    compared with the values in the code and the gate manifest. This does not prove the
    list is complete; it only catches drift in the values it names."""
    from agenttalk import ovh_gateway, ovh_gateway_service

    network = " ".join(_section(README.read_text(encoding="utf-8"),
                                "Local-first: what reaches the network").split())
    assert ("off by default; each runs only when the scan profile's `network_allowed` setting "
            "lists that tool") in network
    assert ("That build is not gated by `network_allowed`: it can download the project's build "
            "backend from the Python package index even when `network_allowed` allows nothing") in network
    changelog = " ".join((REPO / "CHANGELOG.md").read_text(encoding="utf-8").split("\n## [0.95.0]", 1)[0].split())
    assert "release-profile build is not gated by `network_allowed`" in changelog
    assert "**DEFAULT: none.**" in network
    assert ovh_gateway_service.DEFAULT_API_BASE in network
    manifest = json.loads((REPO / "dev-gate.json").read_text(encoding="utf-8"))
    assert manifest["checks"]["wheel-contract"]["dependency_index"] in network
    registry_rules = [c for c in manifest["checks"]["semgrep"]["configs"] if c.startswith("p/")]
    assert registry_rules
    for rule_set in registry_rules:
        assert f"`{rule_set}`" in network
    for tool in ("osv-scanner", "pip-audit", "network_allowed", "python -m build"):
        assert tool in network
    assert network.count("**OPT-IN") == 3
    assert ovh_gateway.MODEL_ALIAS in README.read_text(encoding="utf-8")
    assert "no egress" not in README.read_text(encoding="utf-8")


def test_readme_names_the_files_kept_outside_the_project() -> None:
    from agenttalk import ovh_gateway

    files = " ".join(_section(README.read_text(encoding="utf-8"), "Where agenttalk keeps files").split())
    for where in (r"agenttalk\keys" "\\", r"agenttalk\recovery" "\\", "AGENTTALK_RECOVERY_DIR",
                  r"agenttalk\wrapper-logs" "\\", ovh_gateway.DEFAULT_LOCAL_DIRNAME + "\\",
                  ovh_gateway.DEFAULT_SPEND_DIRNAME + "\\", "~/.codex/config.toml"):
        assert where in files, where
    assert "`$XDG_STATE_HOME/agenttalk/wrapper-logs/`" in files
    assert "`agenttalk codex-config --enable` adds a block" in files
    assert "`agenttalk backup` copies only the coordination store" in files
    assert "full backup" not in files and "fully" not in files
    manual = " ".join(MANUAL.read_text(encoding="utf-8").split())
    assert "Everything it records is kept in files inside your project" not in manual
    assert "A few things live in per-user folders outside the project" in manual


def test_readme_describes_the_install_flags_exactly() -> None:
    readme = " ".join(README.read_text(encoding="utf-8").split())
    assert ("`--claude-only` and `--codex-only` choose which side gets the bus commands; the devkit "
            "still goes to both sides unless you add `--no-devkit`") in readme
    assert "to install just one side" not in readme


@pytest.mark.parametrize(
    "shell, create, capture, invoke, path_setup, show_path",
    [
        (
            "powershell",
            "python -m venv agenttalk-env",
            r"$env:AGENTTALK_PY = (Resolve-Path .\agenttalk-env\Scripts\python.exe).Path",
            "& $env:AGENTTALK_PY",
            "$env:PATH = (Split-Path $env:AGENTTALK_PY) + ';' + $env:PATH",
            "$env:AGENTTALK_PY",
        ),
        (
            "sh",
            "python3 -m venv agenttalk-env",
            'export AGENTTALK_PY="$(pwd)/agenttalk-env/bin/python"',
            '"$AGENTTALK_PY"',
            'export PATH="$(dirname "$AGENTTALK_PY"):$PATH"',
            'echo "$AGENTTALK_PY"',
        ),
    ],
    ids=["powershell", "sh"],
)
def test_quick_start_uses_the_environment_python(shell, create, capture, invoke, path_setup, show_path):
    """The first install block uses one Python; assistant terminals put its commands first on PATH."""
    started = README.read_text(encoding="utf-8").split("## Get started\n", 1)[1].split("\n## ", 1)[0]
    blocks = [
        body for language, body in re.findall(r"```(powershell|sh)\n(.*?)```", started, re.S) if language == shell
    ]
    assert blocks, f"Get started needs the {shell} environment recipe"
    commands = [line.strip() for line in blocks[0].splitlines() if line.strip()]
    assert len(commands) == 4
    assert commands[:2] == [create, capture]
    assert commands[2].startswith(invoke + " -m pip install ")
    pins = PIN.findall(commands[2])
    version = re.search(r'^version = "([^"]+)"', (REPO / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
    assert pins and set(pins) == {f"v{version}"}
    assert commands[3] == invoke + " -m agenttalk install-skills"
    assert "& $env:AGENTTALK_PY -m agenttalk init --here --agents claude-dev,codex-rev" in started
    assert "& $env:AGENTTALK_PY -m agenttalk codex-config --enable" in started
    assert any(path_setup == line.strip() for block in blocks[1:] for line in block.splitlines())
    assert f"`{show_path}`" in started
    assert "`& $env:AGENTTALK_PY -m agenttalk doctor`" in started
    # the doctor line before the recipe serves both platforms
    assert '`"$AGENTTALK_PY" -m agenttalk doctor`' in started
    # that line comes before the recipe, so it says where AGENTTALK_PY is set
    early = started.split("`& $env:AGENTTALK_PY -m agenttalk doctor`", 1)[0]
    assert "After the steps below, which set `AGENTTALK_PY`" in " ".join(early.split())


def test_quick_setup_links_to_the_first_install_without_another_pip_recipe():
    quick = README.read_text(encoding="utf-8").split("\n## 2. Quick setup\n", 1)[1].split("\n## ", 1)[0]
    assert "[Get started](#get-started)" in quick
    assert "pip install" not in quick


def test_get_started_never_says_to_activate_a_project_environment_after_the_path_step():
    """Activating another venv would put its folder first on PATH and could run an older agenttalk."""
    started = " ".join(README.read_text(encoding="utf-8").split("## Get started\n", 1)[1].split("\n## ", 1)[0].split())
    assert "activate it after" not in started
    assert "do not activate it in any terminal where you or an assistant run agenttalk" in started
    assert "Call the project's Python by its full path instead" in started
    # a full path with spaces must still be one command in PowerShell
    assert r'`& "<project-env>\Scripts\python.exe"`' in started
    assert "`(Get-Command agenttalk).Source`" in started and "`command -v agenttalk`" in started
