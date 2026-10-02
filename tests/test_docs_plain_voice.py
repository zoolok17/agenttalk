"""The README and the new-user manual open in the plain-language voice, and the stale PDF
manual is no longer shipped (issue #260)."""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
README = REPO / "README.md"
MANUAL = REPO / "docs" / "AGENTTALK-NEW-USER-MANUAL.md"
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
