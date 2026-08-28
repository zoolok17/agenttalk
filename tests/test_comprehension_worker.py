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

def test_process_paths_claims_every_file_with_its_size(tmp_path: Path) -> None:
    """N3 (fourth cold read, fix round 6): WorkerFileClaim used to also
    carry a content_digest - a second hash of every file's bytes, on top
    of the one discovery.py already computes for the whole-scope
    fingerprint - with zero consumers outside this module. Dropped along
    with the hashing that produced it; byte_count (still genuinely used
    to prove "this file is still addressable") remains."""
    (tmp_path / "a.txt").write_bytes(b"hello")
    (tmp_path / "b.txt").write_bytes(b"world!")
    result = worker.process_paths(tmp_path, ["a.txt", "b.txt"])
    assert result.schema_version == worker.WORKER_SCHEMA_VERSION
    by_path = {c.relative_path: c for c in result.file_claims}
    assert by_path["a.txt"].byte_count == 5
    assert by_path["b.txt"].byte_count == 6
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


def test_process_paths_caps_adapter_work_and_degrades_instead_of_aborting(
    tmp_path: Path, monkeypatch,
) -> None:
    """M11 (cold-read, PR-B fix round 3): the design lists "adapter work"
    among the resource caps, but none existed - the file still gets its
    base WorkerFileClaim (still addressable), and the scan degrades via a
    bounded resource_limit problem, instead of the only prior option (the
    whole-worker timeout aborting the entire scan with no published run
    at all)."""
    monkeypatch.setattr(worker, "_MAX_ADAPTER_INPUT_BYTES", 10)
    (tmp_path / "Big.java").write_text(
        "package p;\nclass Big {\n  void run() { Foo.bar(); }\n}\n", encoding="utf-8")
    result = worker.process_paths(tmp_path, ["Big.java"])
    assert result.file_claims[0].relative_path == "Big.java"  # still addressable
    assert "Big.java" not in result.java_results  # adapter analysis skipped
    assert len(result.problems) == 1
    assert result.problems[0].reason_code == "resource_limit"
    assert result.problems[0].relative_path == "Big.java"


def test_process_paths_dispatch_is_not_extension_case_sensitive(tmp_path: Path) -> None:
    """Note 10 (second cold read, fix round 4): Windows and default macOS
    filesystems are case-insensitive/case-preserving - `Foo.JAVA` and
    `POM.XML` are perfectly reachable real files there, and a case-
    sensitive dispatch check would silently skip adapter dispatch for
    them."""
    (tmp_path / "Foo.JAVA").write_text("package p;\nclass Foo {}\n", encoding="utf-8")
    (tmp_path / "POM.XML").write_text(
        "<project><dependencies><dependency>"
        "<groupId>g</groupId><artifactId>a</artifactId>"
        "</dependency></dependencies></project>",
        encoding="utf-8",
    )
    result = worker.process_paths(tmp_path, ["Foo.JAVA", "POM.XML"])
    assert result.problems == []
    assert "Foo.JAVA" in result.java_results
    assert result.java_results["Foo.JAVA"]["units"][0]["qualified_name"] == "p.Foo"
    assert "POM.XML" in result.java_results
    assert result.java_results["POM.XML"]["edges"][0]["target"] == "g:a"


def test_process_paths_dispatches_pom_xml_through_the_java_results_channel(
    tmp_path: Path,
) -> None:
    """B-3 (reviewer-3, PR-B delta review): pom.xml build-relation
    extraction must happen INSIDE this worker, on the same bytes already
    read here - never a second, separate read in the parent process."""
    (tmp_path / "pom.xml").write_text(
        "<project><dependencies><dependency>"
        "<groupId>org.springframework</groupId><artifactId>spring-core</artifactId>"
        "</dependency></dependencies></project>",
        encoding="utf-8",
    )
    result = worker.process_paths(tmp_path, ["pom.xml"])
    assert result.problems == []
    assert "pom.xml" in result.java_results
    edges = result.java_results["pom.xml"]["edges"]
    assert edges and edges[0]["target"] == "org.springframework:spring-core"
    assert edges[0]["relation"] == "build"


def test_process_paths_dispatches_web_xml_through_the_java_results_channel(
    tmp_path: Path,
) -> None:
    """M9 (cold-read, PR-B fix round 3): parse_web_xml existed with its
    own passing unit tests but no dispatch anywhere in the pipeline - a
    valid servlet-mapping web.xml produced no route at all. Wired in the
    same shape as pom.xml's build edges: same already-read bytes, same
    java_results channel."""
    (tmp_path / "web.xml").write_text(
        "<web-app>\n"
        "  <servlet-mapping>\n"
        "    <servlet-name>dispatcher</servlet-name>\n"
        "    <url-pattern>/api/*</url-pattern>\n"
        "  </servlet-mapping>\n"
        "</web-app>\n",
        encoding="utf-8",
    )
    result = worker.process_paths(tmp_path, ["web.xml"])
    assert result.problems == []
    assert "web.xml" in result.java_results
    entry_points = result.java_results["web.xml"]["entry_points"]
    assert entry_points and entry_points[0]["name"] == "/api/*"
    assert entry_points[0]["kind"] == "http_route"


def test_process_paths_flags_a_java_file_that_parses_but_extracts_no_types(
    tmp_path: Path,
) -> None:
    """BLOCKER 1b (fifth cold read, fix round 8): a .java file whose
    parse SUCCEEDS but extracts ZERO declared types used to count as
    positive adapter evidence with no problem recorded at all -
    readiness then reported source_understood satisfied for a file this
    adapter never actually understood. Genuinely unrecognized top-level
    content (not a comment, not a package/import statement, not any
    known declaration keyword) must now be a named, explicit problem."""
    (tmp_path / "Garbage.java").write_text("package p;\nfoo bar baz;\n", encoding="utf-8")
    result = worker.process_paths(tmp_path, ["Garbage.java"])
    assert "Garbage.java" in result.java_results
    assert [p.reason_code for p in result.problems] == ["no_types_extracted"]
    assert result.problems[0].relative_path == "Garbage.java"


def test_process_paths_does_not_flag_package_info_java(tmp_path: Path) -> None:
    """package-info.java legitimately declares no class/interface/enum/
    record at all - even carrying its own package-level annotation
    (a common real-world shape) - and must never be flagged as an
    unrecognized header."""
    (tmp_path / "package-info.java").write_text(
        "/**\n * Javadoc.\n */\n@Deprecated\npackage p;\n", encoding="utf-8")
    result = worker.process_paths(tmp_path, ["package-info.java"])
    assert result.problems == []


def test_process_paths_does_not_flag_an_empty_or_comment_only_java_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "Empty.java").write_text(
        "package p;\n// nothing else here\n", encoding="utf-8")
    result = worker.process_paths(tmp_path, ["Empty.java"])
    assert result.problems == []


def test_process_paths_is_deterministic_regardless_of_input_order(tmp_path: Path) -> None:
    """N4 (cold-read, PR-B fix round 3): comparing bare SETS of sizes
    cannot detect a cross-contamination bug (e.g. a.txt's claim
    accidentally getting b.txt's size) - as long as both sizes appear
    SOMEWHERE across the results, a set comparison passes vacuously
    regardless of which file each is actually attributed to. Comparing
    the relative_path -> byte_count MAPPING is strictly stronger: it
    fails if any single file's size differs from its OWN expected value
    depending on what order it happened to be processed in.

    N3 (fourth cold read, fix round 6): this originally keyed on
    content_digest, since dropped (dead - see WorkerFileClaim); a.txt
    and b.txt are deliberately different SIZES so byte_count alone still
    proves per-file attribution, not just per-file existence."""
    (tmp_path / "a.txt").write_bytes(b"hello")
    (tmp_path / "b.txt").write_bytes(b"world!")
    forward = worker.process_paths(tmp_path, ["a.txt", "b.txt"])
    backward = worker.process_paths(tmp_path, ["b.txt", "a.txt"])
    forward_by_path = {c.relative_path: c.byte_count for c in forward.file_claims}
    backward_by_path = {c.relative_path: c.byte_count for c in backward.file_claims}
    assert forward_by_path == backward_by_path
    assert forward_by_path == {"a.txt": 5, "b.txt": 6}


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


# ----------------------------------------------------------- JSON round-trip (B-1 regression)

def test_worker_result_json_round_trip_preserves_every_field(tmp_path: Path) -> None:
    """reviewer-3's B-1 repro, made permanent: adapter claims computed by
    process_paths must survive _result_to_json -> _result_from_json intact.
    Before this fix, java_results was silently dropped by both functions -
    process_paths computed it correctly, but a real scan run through the
    REAL subprocess (which must serialize/deserialize across stdout) always
    reconstructed an empty dict regardless of what was actually parsed."""
    (tmp_path / "A.java").write_text(
        "package p;\nclass A {\n  public static void main(String[] a) {}\n}\n",
        encoding="utf-8",
    )
    computed = worker.process_paths(tmp_path, ["A.java"])
    assert computed.java_results, "process_paths itself must have produced a java_results entry"

    round_tripped = worker._result_from_json(worker._result_to_json(computed))

    assert round_tripped.java_results == computed.java_results
    assert round_tripped.file_claims == computed.file_claims
    assert round_tripped.problems == computed.problems


def test_worker_result_json_round_trip_of_an_empty_java_results_stays_empty(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.txt").write_bytes(b"hello")
    computed = worker.process_paths(tmp_path, ["a.txt"])
    round_tripped = worker._result_from_json(worker._result_to_json(computed))
    assert round_tripped.java_results == {}


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

    assert captured["argv"] == [
        sys.executable, "-s", "-S", "-m", "agenttalk.comprehension.worker",
    ]
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
    real sanitized (allowlisted) environment.

    B-2 (reviewer-3, PR-B delta review): this used to skip whenever this
    dev environment's installed ``agenttalk`` predated this module, since
    the sanitized environment deliberately excludes PYTHONPATH and the
    child had no other way to resolve the package from a source checkout.
    That reasoning was right about *why* it skipped locally, but wrong to
    conclude the real-install case was unaffected: B-1 meant that wherever
    the child COULD start, its adapter results were silently dropped
    anyway. Now that run_sanitized_worker derives and validates the
    child's import root itself (:func:`worker._derive_child_import_root`)
    rather than relying on inherited PYTHONPATH, this must EXECUTE, not
    skip, regardless of how ``agenttalk`` happens to be installed on the
    machine running this test."""
    (tmp_path / "a.txt").write_bytes(b"hello")
    result = worker.run_sanitized_worker(tmp_path, ["a.txt"])
    assert result.file_claims[0].relative_path == "a.txt"
    assert result.file_claims[0].byte_count == 5


def test_run_sanitized_worker_end_to_end_real_subprocess_carries_java_results(
    tmp_path: Path,
) -> None:
    """B-1 + B-2 together, through the REAL subprocess (not process_paths
    called in-process): adapter claims computed by the real child must
    survive the actual stdin/stdout JSON channel, and the child must
    actually be able to start from this source checkout to prove it."""
    (tmp_path / "A.java").write_text(
        "package p;\nclass A {\n  public static void main(String[] a) {}\n}\n",
        encoding="utf-8",
    )
    result = worker.run_sanitized_worker(tmp_path, ["A.java"])
    assert result.java_results, "adapter claims must survive the real subprocess round-trip"
    assert result.java_results["A.java"]["units"][0]["qualified_name"] == "p.A"


def test_run_sanitized_worker_derives_the_child_import_root_from_this_process(
    tmp_path: Path,
) -> None:
    """The child's PYTHONPATH must be THIS function's own derived,
    validated value - never inherited from the caller's ambient
    environment (B-2: an inherited PYTHONPATH is itself an injection
    vector)."""
    import agenttalk

    expected_root = str(Path(agenttalk.__file__).resolve().parent.parent)
    assert worker._derive_child_import_root() == expected_root

    (tmp_path / "a.txt").write_bytes(b"hello")
    result = worker.run_sanitized_worker(tmp_path, ["a.txt"], timeout_seconds=30.0)
    assert result.file_claims[0].relative_path == "a.txt"


def test_run_sanitized_worker_starts_from_a_source_tree_layout_with_no_ambient_pythonpath(
    tmp_path: Path, monkeypatch,
) -> None:
    """B-2 (reviewer-3, PR-B delta review), regression test in the same
    shape as PR-A's
    test_host_identity_succeeds_under_the_dev_gates_allowlisted_environment:
    spawn the real child under the real sanitized env, from exactly the
    layout that broke before this fix - agenttalk importable ONLY via
    PYTHONPATH in a source checkout, with the PARENT's own ambient
    PYTHONPATH removed first, so the child could only start if this
    process derives the import root itself rather than happening to
    inherit a pre-set value."""
    monkeypatch.delenv("PYTHONPATH", raising=False)
    (tmp_path / "a.txt").write_bytes(b"hello")
    result = worker.run_sanitized_worker(tmp_path, ["a.txt"])
    assert result.file_claims[0].relative_path == "a.txt"
    assert result.file_claims[0].byte_count == 5
