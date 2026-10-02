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
