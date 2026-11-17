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


def test_manual_and_readme_pin_the_same_release() -> None:
    """A new user who follows the manual must install the same version the README names."""
    readme_pins = set(PIN.findall(README.read_text(encoding="utf-8")))
    manual_pins = set(PIN.findall(MANUAL.read_text(encoding="utf-8")))
    assert readme_pins and manual_pins
    assert manual_pins == readme_pins


def test_every_install_guide_pins_the_same_release() -> None:
    """The operator manual, like the README and the new-user manual, tells a reader to install
    the release this tree ships. It once kept an old pin because the test above named two files."""
    any_pin = re.compile(r"pip install\s+\"?git\+https://github\.com/zoolok17/agenttalk\.git@(v\d+\.\d+\.\d+)")
    readme_pins = set(any_pin.findall(README.read_text(encoding="utf-8")))
    assert readme_pins
    for path in (MANUAL, OPERATOR_MANUAL):
        pins = set(any_pin.findall(path.read_text(encoding="utf-8")))
        assert pins == readme_pins, f"{path.name} installs {sorted(pins)}, the README {sorted(readme_pins)}"
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
    assert pins and set(pins) == set(PIN.findall(MANUAL.read_text(encoding="utf-8")))
    assert commands[3] == invoke + " -m agenttalk install-skills"
    assert "& $env:AGENTTALK_PY -m agenttalk init --here --agents claude-dev,codex-rev" in started
    assert "& $env:AGENTTALK_PY -m agenttalk codex-config --enable" in started
    assert any(path_setup == line.strip() for block in blocks[1:] for line in block.splitlines())
    assert f"`{show_path}`" in started
    assert "`& $env:AGENTTALK_PY -m agenttalk doctor`" in started


def test_quick_setup_links_to_the_first_install_without_another_pip_recipe():
    quick = README.read_text(encoding="utf-8").split("\n## 2. Quick setup\n", 1)[1].split("\n## ", 1)[0]
    assert "[Get started](#get-started)" in quick
    assert "pip install" not in quick


def test_get_started_never_says_to_activate_a_project_environment_after_the_path_step():
    """Activating another venv would put its folder first on PATH and could run an older agenttalk."""
    started = " ".join(README.read_text(encoding="utf-8").split("## Get started\n", 1)[1].split("\n## ", 1)[0].split())
    assert "activate it after" not in started
    assert "do not activate it in a terminal where an assistant runs" in started
    assert "Call the project's Python by its full path instead" in started
