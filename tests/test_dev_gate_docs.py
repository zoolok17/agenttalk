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


def test_release_candidate_docs_preserve_read_only_authority_and_distinct_refusals() -> None:
    reference = Path("docs/DEV-GATE.md").read_text(encoding="utf-8")
    manual = Path("docs/AGENT-MANUAL.md").read_text(encoding="utf-8")
    design = Path("docs/DESIGN.md").read_text(encoding="utf-8")

    for refusal in (
        "release_evidence_missing",
        "release_evidence_stale",
        "release_evidence_sha_mismatch",
    ):
        assert refusal in reference
    assert "exact wheel and sdist" in reference
    assert "90-day retention is a requested ceiling" in reference
    assert "creates no tag, GitHub Release, or package publication" in reference
    assert "AGENTTALK-NEW-USER-MANUAL.pdf" in reference
    assert "carrier digest mismatch" in reference
    assert "artifact file set differs from SHA256SUMS" in reference
    assert "$ErrorActionPreference = 'Stop'" in reference
    assert "security-events: write" in reference
    assert "release-provenance.yml" in manual
    assert "manually tagging" in manual
    assert "temporal double-check" in design
    assert "not separation of duties or two-party control" in design
