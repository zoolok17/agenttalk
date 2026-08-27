"""#55 slice-1 PR-B item 9: `agenttalk comprehension scan|status|report|
validate` CLI wiring. Exercises the real argparse parser and `cli.main`,
per this codebase's own test_cli.py convention ("invoke main(argv) rather
than subprocess-ing to keep tests fast").

The sanitized worker's subprocess boundary is monkeypatched to an
in-process call for the same reason test_comprehension_scan_pipeline.py
does - these tests are about CLI/pipeline wiring, not re-proving the
worker boundary (already covered in test_comprehension_worker.py).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agenttalk import cli
from agenttalk.comprehension import scan_pipeline
from agenttalk.comprehension import worker as workermod


@pytest.fixture(autouse=True)
def _inprocess_worker(monkeypatch):
    monkeypatch.setattr(
        scan_pipeline.worker, "run_sanitized_worker",
        lambda root, relative_paths, **_kwargs: workermod.process_paths(root, relative_paths),
    )


@pytest.fixture(autouse=True)
def _no_agent_identity(monkeypatch):
    monkeypatch.delenv("AGENTTALK_SELF", raising=False)


def _init_git_repo(root: Path, *, ignore_agenttalk: bool = True) -> None:
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)  # noqa: S603,S607  # nosec B603 B607
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(root), "config", "user.email", "t@t"], check=True)
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(root), "config", "user.name", "t"], check=True)
    pattern = ".agenttalk/\n" if ignore_agenttalk else "build/\n"
    (root / ".gitignore").write_text(pattern, encoding="utf-8")


def _write_sample_java_project(root: Path) -> None:
    app_dir = root / "src" / "main" / "java" / "p"
    app_dir.mkdir(parents=True)
    (app_dir / "App.java").write_text(
        "package p;\nclass App {\n  public static void main(String[] args) {}\n}\n",
        encoding="utf-8",
    )


@pytest.fixture()
def java_repo(tmp_path: Path) -> Path:
    _init_git_repo(tmp_path)
    _write_sample_java_project(tmp_path)
    return tmp_path


def _run(argv: list[str], root: Path) -> int:
    return cli.main(["--root", str(root), *argv])


# ----------------------------------------------------------- scan

def test_scan_publishes_and_prints_json(java_repo: Path, capsys) -> None:
    exit_code = _run(["comprehension", "scan", "--json"], java_repo)
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "complete"
    assert payload["scan_id"]


def test_scan_human_output(java_repo: Path, capsys) -> None:
    exit_code = _run(["comprehension", "scan"], java_repo)
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "scan_id:" in out
    assert "status:  complete" in out


def test_scan_without_privacy_proof_refuses(tmp_path: Path, capsys) -> None:
    _init_git_repo(tmp_path, ignore_agenttalk=False)
    _write_sample_java_project(tmp_path)
    exit_code = _run(["comprehension", "scan"], tmp_path)
    assert exit_code == 2
    assert "vcs_privacy_refused" in capsys.readouterr().err
    assert not (tmp_path / ".agenttalk").exists()


def test_acknowledge_without_work_id_refuses(tmp_path: Path, capsys) -> None:
    _init_git_repo(tmp_path, ignore_agenttalk=False)
    _write_sample_java_project(tmp_path)
    exit_code = _run(
        ["comprehension", "scan", "--acknowledge-unignored-private-store"], tmp_path)
    assert exit_code == 2
    assert "--work-id" in capsys.readouterr().err


def test_acknowledge_headless_without_agent_identity_reports_and_refuses(
    tmp_path: Path, capsys,
) -> None:
    """No interactive terminal in a pytest run (stdin/stdout are not a real
    tty) and no AGENTTALK_SELF set - the CLI can neither prompt nor
    escalate, so it must refuse loudly, never silently proceed."""
    _init_git_repo(tmp_path, ignore_agenttalk=False)
    _write_sample_java_project(tmp_path)
    exit_code = _run(
        ["comprehension", "scan", "--acknowledge-unignored-private-store",
         "--work-id", "migrate-app"],
        tmp_path,
    )
    assert exit_code == 2
    err = capsys.readouterr().err
    assert "AGENTTALK_SELF" in err
    assert not (tmp_path / ".agenttalk").exists()


# ----------------------------------------------------------- status

def test_status_before_any_scan(tmp_path: Path, capsys) -> None:
    exit_code = _run(["comprehension", "status", "--json"], tmp_path)
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {"status": "not_scanned"}


def test_status_after_a_scan(java_repo: Path, capsys) -> None:
    _run(["comprehension", "scan", "--json"], java_repo)
    capsys.readouterr()
    exit_code = _run(["comprehension", "status", "--json"], java_repo)
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "complete"
    assert payload["latest_scan_id"]


# ----------------------------------------------------------- report

def test_report_after_a_scan(java_repo: Path, capsys) -> None:
    _run(["comprehension", "scan", "--json"], java_repo)
    capsys.readouterr()
    exit_code = _run(["comprehension", "report", "--json"], java_repo)
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["units"] > 0


def test_report_before_any_scan_refuses(tmp_path: Path, capsys) -> None:
    exit_code = _run(["comprehension", "report", "--json"], tmp_path)
    assert exit_code == 2
    assert "no comprehension run has ever been published" in capsys.readouterr().err


# ----------------------------------------------------------- validate

def test_validate_after_a_scan(java_repo: Path, capsys) -> None:
    _run(["comprehension", "scan", "--json"], java_repo)
    capsys.readouterr()
    exit_code = _run(["comprehension", "validate", "--json"], java_repo)
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True


# ----------------------------------------------------------- bare subcommand

def test_bare_comprehension_with_no_subcommand_refuses(tmp_path: Path, capsys) -> None:
    exit_code = _run(["comprehension"], tmp_path)
    assert exit_code == 2
    assert "subcommand" in capsys.readouterr().err
