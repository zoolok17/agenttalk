import json
from pathlib import Path

from agenttalk.cli import build_parser


def _dev_gate_parser():
    parser = build_parser()
    subparsers = next(action for action in parser._actions if action.dest == "cmd")
    return subparsers.choices["dev-gate"]


def test_dev_gate_reference_tracks_the_public_command_surface() -> None:
    reference = Path("docs/DEV-GATE.md").read_text(encoding="utf-8")
    parser = _dev_gate_parser()
    documented = {
        "--profile",
        "--ci-leg",
        "--aggregate",
        "--evidence",
        "--temp-root",
        "--python",
    }
    exposed = {
        option
        for action in parser._actions
        for option in action.option_strings
        if option not in {"-h", "--help"}
    }

    assert exposed == documented
    assert all(option in reference for option in documented)
    assert "--skip" not in reference


def test_dev_gate_reference_tracks_the_committed_authority_split() -> None:
    manifest = json.loads(Path("dev-gate.json").read_text(encoding="utf-8"))
    profile = manifest["profiles"]["release"]
    reference = Path("docs/DEV-GATE.md").read_text(encoding="utf-8")

    assert profile["local"]["python_minors"] == ["3.10", "3.14"]
    assert profile["ci"]["python_minors"] == ["3.10", "3.11", "3.12", "3.13"]
    assert profile["ci"]["oses"] == ["linux", "windows", "macos"]
    assert profile["ci"]["canonical_static_leg"] == "linux/3.12"
    assert "Python 3.10 and 3.14" in reference
    assert "Python 3.10 through 3.13" in reference
    assert "exact 12-leg set" in " ".join(reference.split())
    assert "`linux/3.12`" in reference


def test_dev_gate_reference_and_manifest_are_shipped_in_the_sdist() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")

    assert '"/docs/DEV-GATE.md"' in pyproject
    assert '"/dev-gate.json"' in pyproject
    assert '"/dev-gate-requirements.txt"' in pyproject
    assert Path("dev-gate.json").is_file()
    assert Path("dev-gate-requirements.txt").is_file()
    assert Path("docs/DEV-GATE.md").is_file()
