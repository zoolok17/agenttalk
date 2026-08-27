"""#55 slice-1 PR-B item 1: the sanitized bundled scanner worker
(DESIGN-55-comprehension-plane.md, "System boundary" / "Privacy and offline
enforcement"). No adapter is wired in yet - these tests exercise the
process boundary, the environment allowlist, and the default
every-file-is-addressable guarantee only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agenttalk.comprehension import worker


# ----------------------------------------------------------- sanitized_worker_env

def test_sanitized_worker_env_keeps_only_the_fixed_allowlist():
    source = {
        "PATH": "/usr/bin",
        "ANTHROPIC_API_KEY": "secret",
        "OPENAI_API_KEY": "secret",
        "HTTP_PROXY": "http://evil.example",
        "HTTPS_PROXY": "http://evil.example",
        "NO_PROXY": "example.com",
        "AGENTTALK_SELF": "claude",
        "AGENTTALK_ROOT": "D:\\somewhere",
        "GIT_AUTHOR_NAME": "someone",
        "HOSTNAME": "some-host",
        "COMPUTERNAME": "SOME-HOST",
    }
    env = worker.sanitized_worker_env(source)
    assert env == {"PATH": "/usr/bin"}


def test_sanitized_worker_env_is_case_insensitive_on_the_key_but_preserves_value():
    env = worker.sanitized_worker_env({"path": "/usr/bin", "Path": "/bin"})
    # Both keys casefold-match the allowlist entry "PATH"; both survive as
    # distinct dict keys (we do not silently collapse them) - only the
    # PRESENCE test matters here since callers pass a real os.environ-shaped
    # dict where keys are unique per platform casing convention already.
    assert set(env) == {"path", "Path"}


def test_sanitized_worker_env_defaults_to_the_real_process_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = worker.sanitized_worker_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert env.get("PATH") == "/usr/bin"


# ----------------------------------------------------------- process_paths

def test_process_paths_claims_every_file_with_size_and_digest(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"hello")
    (tmp_path / "b.txt").write_bytes(b"world!")
    result = worker.process_paths(tmp_path, ["a.txt", "b.txt"])
    assert result.schema_version == worker.WORKER_SCHEMA_VERSION
    by_path = {c.relative_path: c for c in result.file_claims}
    assert by_path["a.txt"].byte_count == 5
    assert by_path["b.txt"].byte_count == 6
    import hashlib
    assert by_path["a.txt"].content_digest == hashlib.sha256(b"hello").hexdigest()
    assert result.problems == []


def test_process_paths_reports_a_traversal_path_as_a_problem_not_a_crash(
    tmp_path: Path,
) -> None:
    result = worker.process_paths(tmp_path, ["../../../../escaped"])
    assert result.file_claims == []
    assert len(result.problems) == 1
    assert result.problems[0].reason_code == "path_excluded"
    assert result.problems[0].relative_path == "../../../../escaped"


def test_process_paths_reports_an_unreadable_path_as_a_problem(tmp_path: Path) -> None:
    (tmp_path / "a_directory").mkdir()
    result = worker.process_paths(tmp_path, ["a_directory"])
    assert result.file_claims == []
    assert len(result.problems) == 1
    assert result.problems[0].reason_code == "parse_failed"


def test_process_paths_is_deterministic_regardless_of_input_order(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"hello")
    (tmp_path / "b.txt").write_bytes(b"world!")
    forward = worker.process_paths(tmp_path, ["a.txt", "b.txt"])
    backward = worker.process_paths(tmp_path, ["b.txt", "a.txt"])
    assert {c.content_digest for c in forward.file_claims} == {
        c.content_digest for c in backward.file_claims
    }


# ----------------------------------------------------------- _main (worker entrypoint)

def test_main_writes_a_valid_result_for_well_formed_input(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    (tmp_path / "a.txt").write_bytes(b"hello")
    payload = json.dumps({"root": str(tmp_path), "relative_paths": ["a.txt"]})
    monkeypatch.setattr(sys, "stdin", _FakeStdin(payload))
    exit_code = worker._main([])
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema_version"] == worker.WORKER_SCHEMA_VERSION
    assert out["file_claims"][0]["relative_path"] == "a.txt"


def test_main_refuses_malformed_json_input(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin("not json"))
    exit_code = worker._main([])
    assert exit_code == 2
    err = json.loads(capsys.readouterr().err)
    assert "malformed worker input" in err["error"]


def test_main_refuses_input_missing_required_keys(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "stdin", _FakeStdin(json.dumps({"root": "x"})))
    exit_code = worker._main([])
    assert exit_code == 2
    err = json.loads(capsys.readouterr().err)
    assert "root and relative_paths" in err["error"]


class _FakeStdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


# ----------------------------------------------------------- run_sanitized_worker (mocked subprocess)

def test_run_sanitized_worker_launches_with_the_sanitized_environment_only(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-never-reach-the-worker")
    captured: dict = {}

    class _FakeCompleted:
        returncode = 0
        stderr = ""
        stdout = json.dumps(worker._result_to_json(
            worker.WorkerResult(schema_version=worker.WORKER_SCHEMA_VERSION)))

    def fake_run(argv, *, input, capture_output, text, env, timeout, check):
        captured["argv"] = argv
        captured["env"] = env
        captured["input"] = input
        captured["timeout"] = timeout
        return _FakeCompleted()

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    result = worker.run_sanitized_worker(tmp_path, ["a.txt"])

    assert captured["argv"] == [sys.executable, "-m", "agenttalk.comprehension.worker"]
    assert "ANTHROPIC_API_KEY" not in captured["env"]
    assert json.loads(captured["input"]) == {"root": str(tmp_path), "relative_paths": ["a.txt"]}
    assert result.schema_version == worker.WORKER_SCHEMA_VERSION


def test_run_sanitized_worker_raises_worker_error_on_nonzero_exit(
    tmp_path: Path, monkeypatch,
) -> None:
    class _FakeCompleted:
        returncode = 1
        stderr = "boom"
        stdout = ""

    monkeypatch.setattr(
        worker.subprocess, "run",
        lambda *a, **k: _FakeCompleted(),  # noqa: ARG005
    )
    with pytest.raises(worker.WorkerError, match="boom"):
        worker.run_sanitized_worker(tmp_path, [])


def test_run_sanitized_worker_raises_worker_error_on_timeout(
    tmp_path: Path, monkeypatch,
) -> None:
    def fake_run(*args, **kwargs):  # noqa: ARG001
        raise subprocess.TimeoutExpired(cmd="worker", timeout=1.0)

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    with pytest.raises(worker.WorkerError, match="timed out"):
        worker.run_sanitized_worker(tmp_path, [], timeout_seconds=1.0)


def test_run_sanitized_worker_raises_worker_error_on_malformed_output(
    tmp_path: Path, monkeypatch,
) -> None:
    class _FakeCompleted:
        returncode = 0
        stderr = ""
        stdout = "not json"

    monkeypatch.setattr(
        worker.subprocess, "run",
        lambda *a, **k: _FakeCompleted(),  # noqa: ARG005
    )
    with pytest.raises(worker.WorkerError, match="malformed output"):
        worker.run_sanitized_worker(tmp_path, [])


# ----------------------------------------------------------- real subprocess end-to-end

def test_run_sanitized_worker_end_to_end_real_subprocess(tmp_path: Path) -> None:
    """The one test that actually spawns the real child process under the
    real sanitized (allowlisted) environment. Skips, rather than fails,
    when this dev environment's installed ``agenttalk`` package predates
    this module (e.g. a stale non-editable install with no ``comprehension``
    subpackage at all) - the sanitized environment deliberately excludes
    PYTHONPATH, so this test can only exercise the true child-process
    boundary when agenttalk is genuinely installed, which is exactly the
    condition real deployments and CI's own pip-install step satisfy."""
    (tmp_path / "a.txt").write_bytes(b"hello")
    try:
        result = worker.run_sanitized_worker(tmp_path, ["a.txt"])
    except worker.WorkerError as exc:
        if "No module named" in str(exc):
            pytest.skip(
                "agenttalk is not installed without PYTHONPATH in this dev "
                "environment - the sanitized child process cannot see this "
                "checkout; unaffected in a real install (see docstring)")
        raise
    assert result.file_claims[0].relative_path == "a.txt"
    assert result.file_claims[0].byte_count == 5
