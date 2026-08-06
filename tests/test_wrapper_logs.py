from __future__ import annotations

import argparse
import io
import json
import os
import signal
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agenttalk import cli, wrapper_logs
from agenttalk import wrapper_runtime as runtime
from agenttalk.store import Store


NOW = 1_800_000_000.0


def _log_glob(root: Path, pattern: str) -> list[Path]:
    """Same as sorted(root.glob(pattern)), excluding the round-29 tail-ring
    cursor sibling (base_path.name + '.cursor') - a bare 'stdout.log*' or
    'stderr.log.*' glob sweeps it in too since it lives right alongside
    the numbered tail segments, but it holds a bare index, not log
    content, and is not a segment any of these tests mean to count."""
    return sorted(p for p in root.glob(pattern) if not p.name.endswith(".cursor"))


def _direct_wrap_environment(tmp_path: Path, blocked_state: Path) -> dict[str, str]:
    home = tmp_path / "home"
    temp = tmp_path / "temp"
    home.mkdir()
    temp.mkdir()
    env = os.environ.copy()
    for name in (
        wrapper_logs.ENV_STDOUT_PATH,
        wrapper_logs.ENV_STDERR_PATH,
        wrapper_logs.ENV_MAX_BYTES,
        wrapper_logs.ENV_SEGMENT_COUNT,
        wrapper_logs.ENV_LAUNCH_NONCE,
    ):
        env.pop(name, None)
    env.update(
        {
            "LOCALAPPDATA": str(blocked_state),
            "XDG_STATE_HOME": str(blocked_state),
            "USERPROFILE": str(home),
            "HOME": str(home),
            "TEMP": str(temp),
            "TMP": str(temp),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        }
    )
    return env


def _run_direct_wrapper(project: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {**env, "AGENTTALK_STUB_SCENARIO": "compute_no_reply"}
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "agenttalk",
            "--root",
            str(project),
            "wrap",
            "--for",
            "worker",
            "--cli",
            "claude",
            "--",
            sys.executable,
            str(Path(__file__).resolve().parent / "support" / "stub_cli.py"),
        ],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )


def _captured_stderr_logs(tmp_path: Path) -> list[Path]:
    return sorted(tmp_path.rglob("stderr.log"))


def _committed_generation_pool(
    root: Path,
    *,
    newest_sequence: str | None,
) -> tuple[str, list[Path], Path]:
    agent_leaf = wrapper_logs._wrapper_log_agent_leaf("worker")
    generations: list[Path] = []
    for sequence in range(1, wrapper_logs.WRAPPER_LOG_GENERATIONS + 2):
        generation = (
            root
            / agent_leaf
            / f"20260804T12000{sequence}000Z-{sequence:032x}"
        )
        generation.mkdir(parents=True)
        if sequence <= wrapper_logs.WRAPPER_LOG_GENERATIONS:
            (generation / ".sequence").write_text(str(sequence), encoding="utf-8")
        elif newest_sequence is not None:
            (generation / ".sequence").write_text(
                newest_sequence,
                encoding="utf-8",
            )
        (generation / ".committed").write_bytes(b"")
        (generation / "stdout.log").write_bytes(b"")
        (generation / "stderr.log").write_bytes(b"")
        generations.append(generation)
    return agent_leaf, generations, generations[-1]


def test_direct_wrap_launch_owns_bounded_log_capture(tmp_path: Path) -> None:
    project = tmp_path / "project"
    store = Store(project)
    store.init(["worker"])
    blocked_state = tmp_path / "blocked-state"
    blocked_state.write_text("not a directory", encoding="utf-8")
    env = _direct_wrap_environment(tmp_path, blocked_state)

    result = _run_direct_wrapper(project, env)

    assert result.returncode == 0, result.stderr
    assert "Plan: I would compute" in result.stdout
    # Read the wrapper's own recorded fact rather than guessing where the
    # fallback landed: a rejected preferred root can be superseded by a
    # fallback outside tmp_path (e.g. the OS temp directory), which a glob
    # confined to tmp_path would never find even though capture worked
    # correctly. See #113 - four rounds were spent tracing exactly this.
    location = wrapper_logs.read_wrapper_log_location(store.state_dir, "worker")
    assert location["status"] == "observed", (
        f"expected an observed wrapper log location, got {location!r}; "
        f"wrapper stderr was: {result.stderr!r}"
    )
    generation = Path(str(location["generation_dir"]))
    assert (generation / ".committed").exists()
    captured = "".join(
        path.read_text(encoding="utf-8")
        for path in _log_glob(generation, "stderr.log*")
    )
    assert '"event":"wrapper_exited"' in captured.replace(" ", "")
    captured_stdout = "".join(
        path.read_text(encoding="utf-8")
        for path in _log_glob(generation, "stdout.log*")
    )
    assert "Plan: I would compute" in captured_stdout


@pytest.mark.parametrize(
    ("failure_type", "mapped_exit"),
    [(OSError, 2), (RuntimeError, None)],
    ids=("mapped-os-error", "unexpected-runtime-error"),
)
def test_direct_wrap_captures_implicit_root_discovery_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[Exception],
    mapped_exit: int | None,
) -> None:
    project = tmp_path / "project"
    store = Store(project)
    store.init(["worker"])
    state = tmp_path / "state"
    env = _direct_wrap_environment(tmp_path, state)
    env.pop("AGENTTALK_ROOT", None)
    monkeypatch.setattr(os, "environ", env)
    monkeypatch.chdir(project)
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())

    def fail_root_discovery() -> Path:
        raise failure_type("ROOT-DISCOVERY-SENTINEL")

    monkeypatch.setattr(cli, "find_root", fail_root_discovery)

    argv = [
        "wrap",
        "--for",
        "worker",
        "--cli",
        "claude",
        "--",
        sys.executable,
        "-c",
        "pass",
    ]
    if mapped_exit is None:
        with pytest.raises(failure_type, match="ROOT-DISCOVERY-SENTINEL"):
            cli.main(argv)
    else:
        assert cli.main(argv) == mapped_exit

    [stderr_log] = _captured_stderr_logs(tmp_path)
    generation = stderr_log.parent
    assert (generation / ".committed").is_file()
    captured = "".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in _log_glob(generation, "stderr.log*")
    )
    assert "ROOT-DISCOVERY-SENTINEL" in captured
    location = wrapper_logs.read_wrapper_log_location(store.state_dir, "worker")
    assert location["status"] == "observed"
    assert Path(str(location["generation_dir"])) == generation.resolve()
    assert Path(str(location["stderr"])) == stderr_log.resolve()


def test_report_names_the_root_that_accepted_the_wrapper_generation(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    store = Store(project)
    store.init(["worker"])
    blocked_state = tmp_path / "blocked-state"
    blocked_state.write_text("not a directory", encoding="utf-8")
    env = _direct_wrap_environment(tmp_path, blocked_state)
    preferred = wrapper_logs.default_wrapper_log_root(
        project,
        platform=os.name,
        environ=env,
    )

    result = _run_direct_wrapper(project, env)
    assert result.returncode == 0, result.stderr
    # Read the wrapper's own recorded fact rather than guessing where the
    # fallback landed via a glob confined to tmp_path: the preferred root is
    # deliberately blocked above, and the fallback that supersedes it is not
    # guaranteed to resolve under tmp_path (e.g. the OS temp directory can
    # sit alongside it, not inside it). The wrapper already names its own
    # accepted location as a fact - asserting against that fact instead of
    # an inferred directory is the property this test is actually named for.
    location = wrapper_logs.read_wrapper_log_location(store.state_dir, "worker")
    assert location["status"] == "observed", (
        f"expected an observed wrapper log location, got {location!r}; "
        f"wrapper stderr was: {result.stderr!r}"
    )
    actual_root = Path(str(location["root"]))
    actual_generation = Path(str(location["generation_dir"]))
    actual_stderr = Path(str(location["stderr"]))
    assert actual_generation.is_dir()
    assert (actual_generation / ".committed").exists()
    assert actual_root != preferred.resolve()

    # Make the preferred location viable before asking. A report that recomputes
    # candidates instead of reading the wrapper's recorded fact now lies.
    blocked_state.unlink()
    blocked_state.mkdir()
    report_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agenttalk",
            "--root",
            str(project),
            "supervise",
            "--report",
            "--for",
            "worker",
        ],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )

    assert report_result.returncode == 0, report_result.stderr
    report = json.loads(report_result.stdout)
    assert report["selected_agent"] == "worker"
    location = report["agents"]["worker"]["wrapper_log"]
    assert location["status"] == "observed"
    assert Path(location["root"]) == actual_root
    assert Path(location["generation_dir"]) == actual_generation
    assert Path(location["stderr"]) == actual_stderr


def test_wrapper_log_location_uses_hashed_windows_safe_agent_files(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    store = Store(project)
    store.init(["worker"])
    observed: dict[str, Path] = {}

    for agent in ("worker", "worker.", "NUL"):
        root = tmp_path / f"logs-{len(observed)}"
        generation = (
            root
            / wrapper_logs._wrapper_log_agent_leaf(agent)
            / f"20260804T120000000Z-{len(observed):032x}"
        )
        generation.mkdir(parents=True)
        stdout = generation / "stdout.log"
        stderr = generation / "stderr.log"
        stdout.write_text("out", encoding="utf-8")
        stderr.write_text("err", encoding="utf-8")
        wrapper_logs._record_wrapper_log_location(
            project,
            agent,
            wrapper_logs.WrapperLogInstallation(
                True,
                confirmed=True,
                root=root,
                generation_dir=generation,
                stdout_path=stdout,
                stderr_path=stderr,
            ),
        )
        path = wrapper_logs._wrapper_log_location_path(store.state_dir, agent)
        observed[agent] = path
        assert path.name == f"{wrapper_logs._wrapper_log_agent_leaf(agent)}.json"
        assert wrapper_logs.read_wrapper_log_location(store.state_dir, agent)[
            "generation_dir"
        ] == str(generation.resolve())

    assert len(set(observed.values())) == 3
    assert not any(path.name in {"worker.json", "worker..json", "NUL.json"} for path in observed.values())


def test_supervise_plan_never_probes_wrapper_log_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    Store(tmp_path).init(["worker"])
    assert cli.main(["--root", str(tmp_path), "supervise", "--init"]) == 0
    capsys.readouterr()

    def _fail_probe(*_args, **_kwargs):
        raise AssertionError("supervisor plan probed wrapper log diagnostics")

    monkeypatch.setattr(wrapper_logs, "read_wrapper_log_location", _fail_probe)
    rc = cli.main(
        [
            "--root",
            str(tmp_path),
            "supervise",
            "--plan",
            "--now",
            str(NOW),
        ]
    )

    assert rc == 0
    assert "agents" in json.loads(capsys.readouterr().out)


def test_default_wrapper_log_root_is_project_scoped_and_outside_checkout(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    local = tmp_path / "local-state"

    resolved = wrapper_logs.default_wrapper_log_root(
        checkout,
        platform="posix",
        environ={"XDG_STATE_HOME": str(local)},
    )

    assert resolved.parent == local / "agenttalk" / "wrapper-logs"
    assert len(resolved.name) == 64
    assert checkout.resolve() not in resolved.resolve().parents


def test_default_wrapper_log_root_rejects_relative_ambient_state_path(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    home = tmp_path / "home"

    resolved = wrapper_logs.default_wrapper_log_root(
        checkout,
        platform="posix",
        environ={"XDG_STATE_HOME": ".agenttalk-logs", "HOME": str(home)},
    )

    assert resolved.parent == home / ".local" / "state" / "agenttalk" / "wrapper-logs"
    assert checkout.resolve() not in resolved.resolve().parents


def test_default_wrapper_log_root_rejects_absolute_state_path_inside_checkout(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    home = tmp_path / "home"

    resolved = wrapper_logs.default_wrapper_log_root(
        checkout,
        platform="posix",
        environ={
            "XDG_STATE_HOME": str(checkout / "logs"),
            "HOME": str(home),
        },
    )

    assert resolved.parent == home / ".local" / "state" / "agenttalk" / "wrapper-logs"
    assert checkout.resolve() not in resolved.resolve().parents


def test_default_wrapper_log_root_uses_independent_fallback_when_home_is_checkout(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    resolved = wrapper_logs.default_wrapper_log_root(
        checkout,
        platform="posix",
        environ={
            "XDG_STATE_HOME": str(checkout / "state"),
            "HOME": str(checkout / "home"),
        },
    )

    assert checkout.resolve() not in resolved.resolve().parents


def test_default_wrapper_log_root_tolerates_unresolvable_home_when_configured_path_is_valid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    local = tmp_path / "local-state"

    def _raise_no_home() -> Path:
        raise RuntimeError("could not determine home directory")

    monkeypatch.setattr(wrapper_logs.Path, "home", staticmethod(_raise_no_home))

    resolved = wrapper_logs.default_wrapper_log_root(
        checkout,
        platform="posix",
        environ={"XDG_STATE_HOME": str(local)},
    )

    assert resolved.parent == local / "agenttalk" / "wrapper-logs"
    assert checkout.resolve() not in resolved.resolve().parents


def test_default_wrapper_log_root_falls_back_to_tempdir_when_home_is_unresolvable_and_no_state_path_is_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    def _raise_no_home() -> Path:
        raise RuntimeError("could not determine home directory")

    monkeypatch.setattr(wrapper_logs.Path, "home", staticmethod(_raise_no_home))

    resolved = wrapper_logs.default_wrapper_log_root(
        checkout,
        platform="posix",
        environ={},
    )

    assert checkout.resolve() not in resolved.resolve().parents


def test_wrapper_log_candidates_do_not_consult_later_home_after_valid_configured_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    local = tmp_path / "local-state"

    def _unexpected_home(*_args, **_kwargs):
        raise AssertionError("later HOME candidate was consulted")

    monkeypatch.setattr(wrapper_logs, "_home_state_root", _unexpected_home)
    roots = wrapper_logs.wrapper_log_root_candidates(
        checkout,
        platform="posix",
        environ={"XDG_STATE_HOME": str(local)},
    )

    assert roots[0].parent == local / "agenttalk" / "wrapper-logs"


def test_wrapper_log_candidates_keep_preferred_when_later_fallback_resolution_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    local = tmp_path / "local-state"

    def _broken_temp(_project):
        raise OSError("injected later fallback failure")

    monkeypatch.setattr(wrapper_logs, "temporary_wrapper_log_root", _broken_temp)
    roots = wrapper_logs.wrapper_log_root_candidates(
        checkout,
        platform="posix",
        environ={"XDG_STATE_HOME": str(local)},
    )

    assert roots[0].parent == local / "agenttalk" / "wrapper-logs"


def test_reparse_attribute_check_is_exercised_without_symlink_privilege(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "state" / "agenttalk" / "wrapper-logs" / "project"
    blocked_ancestor = tmp_path / "state"
    real_check = wrapper_logs._is_reparse_or_symlink

    monkeypatch.setattr(
        wrapper_logs,
        "_is_reparse_or_symlink",
        lambda path: path == blocked_ancestor or real_check(path),
    )

    with pytest.raises(OSError, match="unsafe wrapper log root ancestry"):
        wrapper_logs._prepare_agent_log_dir(root, "agent-0123456789abcdef")


def test_wrapper_log_candidate_rechecks_original_ancestry_after_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    configured_state = tmp_path / "configured-state"
    home = tmp_path / "home"
    home.mkdir()
    real_check = wrapper_logs._is_reparse_or_symlink
    configured_visits = 0

    def swap_after_first_check(path: Path) -> bool:
        nonlocal configured_visits
        if path == configured_state:
            configured_visits += 1
            return configured_visits > 1
        return real_check(path)

    monkeypatch.setattr(
        wrapper_logs,
        "_is_reparse_or_symlink",
        swap_after_first_check,
    )

    roots = wrapper_logs.wrapper_log_root_candidates(
        checkout,
        platform="posix",
        environ={
            "XDG_STATE_HOME": str(configured_state),
            "HOME": str(home),
        },
    )

    assert configured_visits >= 2
    assert roots[0].is_relative_to(home.resolve())


def test_corrupt_committed_sequence_marks_retention_uncertain(
    tmp_path: Path,
) -> None:
    root = tmp_path / "logs"
    agent_leaf, _generations, _newest = _committed_generation_pool(
        root,
        newest_sequence="not-a-sequence",
    )

    _owned, _max_sequence, uncertain = wrapper_logs._owned_committed_generations(
        (root,),
        agent_leaf,
    )

    assert uncertain is True


def test_prune_preserves_newest_generation_when_its_sequence_is_corrupt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "logs"
    agent_leaf, generations, newest = _committed_generation_pool(
        root,
        newest_sequence="not-a-sequence",
    )

    wrapper_logs._prune_wrapper_log_generations((root,), agent_leaf)

    assert newest.is_dir()
    assert len([generation for generation in generations if generation.is_dir()]) == 5


def test_bare_legacy_generation_without_sequence_remains_prunable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "logs"
    agent_leaf, generations, newest = _committed_generation_pool(
        root,
        newest_sequence=None,
    )
    _owned, _max_sequence, uncertain = wrapper_logs._owned_committed_generations(
        (root,),
        agent_leaf,
    )

    wrapper_logs._prune_wrapper_log_generations((root,), agent_leaf)

    assert uncertain is False
    assert not newest.exists()
    assert len([generation for generation in generations if generation.is_dir()]) == 4


def test_prune_preserves_live_generation_and_reclaims_stale_active_lock(
    tmp_path: Path,
) -> None:
    root = tmp_path / "logs"
    agent_leaf = wrapper_logs._wrapper_log_agent_leaf("worker")
    generations: list[Path] = []
    for sequence in range(1, wrapper_logs.WRAPPER_LOG_GENERATIONS + 2):
        generation = (
            root
            / agent_leaf
            / f"20260804T12000{sequence}000Z-{sequence:032x}"
        )
        generation.mkdir(parents=True)
        # Simultaneous direct launches can legitimately observe the same prior
        # maximum. The nonce-bearing generation name must total-order the tie.
        (generation / ".sequence").write_text("1", encoding="utf-8")
        (generation / ".committed").write_bytes(b"")
        (generation / "stdout.log").write_bytes(b"")
        (generation / "stderr.log").write_bytes(b"")
        generations.append(generation)

    owned, _max_sequence, _uncertain = wrapper_logs._owned_committed_generations(
        (root,),
        agent_leaf,
    )
    assert len({sort_key for sort_key, _generation in owned}) == len(generations)

    active = generations[0]
    script = (
        "import os,sys\n"
        "from pathlib import Path\n"
        "from agenttalk import wrapper_logs\n"
        "lock=wrapper_logs._acquire_active_generation_lock(Path(sys.argv[1]))\n"
        "assert lock is not None\n"
        "print('ready', flush=True)\n"
        "sys.stdin.readline()\n"
        "os._exit(0)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(active)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"

        wrapper_logs._prune_wrapper_log_generations((root,), agent_leaf)
        assert active.is_dir()
        assert len([path for path in generations if path.is_dir()]) == 5

        assert process.stdin is not None
        process.stdin.write("exit\n")
        process.stdin.flush()
        assert process.wait(timeout=5) == 0

        wrapper_logs._prune_wrapper_log_generations((root,), agent_leaf)
        assert not active.exists()
        assert not wrapper_logs._active_generation_lock_path(active).exists()
        assert len([path for path in generations if path.is_dir()]) == 4
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_second_active_lock_attempt_cannot_unlink_live_holders_marker(
    tmp_path: Path,
) -> None:
    generation = (
        tmp_path
        / wrapper_logs._wrapper_log_agent_leaf("worker")
        / "20260804T120000000Z-0123456789abcdef0123456789abcdef"
    )
    generation.mkdir(parents=True)
    first = wrapper_logs._acquire_active_generation_lock(generation)
    assert first is not None
    try:
        assert wrapper_logs._acquire_active_generation_lock(generation) is None
        assert first.path.is_file()
        with wrapper_logs._guard_wrapper_log_prune(generation) as prunable:
            # On POSIX record locks are process-scoped, so the same-process
            # probe can acquire. The pathname invariant is the direction
            # control here; cross-process exclusion is proved above.
            if os.name == "nt":
                assert prunable is False
    finally:
        wrapper_logs._release_active_generation_lock(first)


@pytest.mark.skipif(os.name != "nt", reason="generated supervisor is Windows-only")
def test_python_and_powershell_share_active_generation_byte_lock(
    tmp_path: Path,
) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell Core is unavailable")
    generation = (
        tmp_path
        / wrapper_logs._wrapper_log_agent_leaf("worker")
        / "20260804T120000000Z-0123456789abcdef0123456789abcdef"
    )
    generation.mkdir(parents=True)
    marker = wrapper_logs._active_generation_lock_path(generation)
    script = tmp_path / "active-lock.ps1"
    script.write_text(
        """param([string]$Path, [string]$Mode)
$share = [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete
$fileMode = if ($Mode -eq 'hold') { [IO.FileMode]::CreateNew } else { [IO.FileMode]::Open }
$stream = New-Object IO.FileStream($Path, $fileMode, [IO.FileAccess]::ReadWrite, $share)
try {
  if ($stream.Length -lt 1) { $stream.SetLength(1) }
  try {
    $stream.Lock(0, 1)
    [Console]::Out.WriteLine('acquired')
    [Console]::Out.Flush()
    if ($Mode -eq 'hold') { $null = [Console]::In.ReadLine() }
    $stream.Unlock(0, 1)
  } catch {
    [Console]::Out.WriteLine('busy')
    [Console]::Out.Flush()
  }
} finally {
  $stream.Dispose()
}
""",
        encoding="utf-8",
    )

    python_lock = wrapper_logs._acquire_active_generation_lock(generation)
    assert python_lock is not None
    try:
        probe = subprocess.run(
            [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script), str(marker), "probe"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert probe.returncode == 0, probe.stderr
        assert probe.stdout.strip() == "busy"
    finally:
        wrapper_logs._release_active_generation_lock(python_lock)

    holder = subprocess.Popen(
        [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script), str(marker), "hold"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "acquired"
        with wrapper_logs._guard_wrapper_log_prune(generation) as prunable:
            assert prunable is False
        assert holder.stdin is not None
        holder.stdin.write("exit\n")
        holder.stdin.flush()
        assert holder.wait(timeout=10) == 0
        with wrapper_logs._guard_wrapper_log_prune(generation) as prunable:
            assert prunable is True
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)
        marker.unlink(missing_ok=True)


def test_direct_allocator_refuses_a_symlinked_root_ancestor(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    linked_state = tmp_path / "linked-state"
    try:
        linked_state.symlink_to(redirected, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "LOCALAPPDATA": str(linked_state),
        "XDG_STATE_HOME": str(linked_state),
        "USERPROFILE": str(home),
        "HOME": str(home),
    }
    preferred = wrapper_logs.default_wrapper_log_root(
        checkout,
        platform=os.name,
        environ=env,
    )

    targets = wrapper_logs._allocate_wrapper_log_targets(
        checkout,
        "worker",
        env,
    )

    assert targets is not None
    assert targets.root != preferred.resolve()
    assert not any(redirected.rglob("stdout.log"))


@pytest.mark.skipif(os.name != "nt", reason="Windows junction guard")
def test_direct_allocator_refuses_a_configured_state_junction(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    linked_state = tmp_path / "linked-state"
    home = tmp_path / "home"
    home.mkdir()
    temp = tmp_path / "temp"
    temp.mkdir()
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    assert powershell is not None, "Windows PowerShell is required for junction coverage"
    junction_script = tmp_path / "create-junction.ps1"
    junction_script.write_text(
        "param([string]$Link, [string]$Target)\n"
        "$ErrorActionPreference = 'Stop'\n"
        "New-Item -ItemType Junction -Path $Link -Target $Target | Out-Null\n",
        encoding="utf-8-sig",
    )
    creation = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(junction_script),
            str(linked_state),
            str(redirected),
        ],
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert creation.returncode == 0, creation.stderr
    assert linked_state.is_dir()
    assert (
        linked_state.lstat().st_file_attributes
        & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )

    env = {
        "LOCALAPPDATA": str(linked_state),
        "USERPROFILE": str(home),
        "TEMP": str(temp),
        "TMP": str(temp),
    }
    try:
        targets = wrapper_logs._allocate_wrapper_log_targets(
            checkout,
            "worker",
            env,
        )

        assert targets is not None
        assert targets.root.is_relative_to(home.resolve())
        assert targets.stdout_path.is_file()
        assert targets.stderr_path.is_file()
        assert not any(redirected.rglob("stdout.log"))
        assert not any(redirected.rglob("stderr.log"))
    finally:
        if linked_state.exists():
            linked_state.rmdir()


def test_restrictive_file_opener_bakes_0o600_into_os_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 22: verifies the ARGUMENT _open_tail's opener passes to
    os.open, independent of platform - the actual POSIX permission
    enforcement (mode bits meaning something real on disk) is checked
    separately, skipped on Windows, in
    test_bounded_stream_tee_creates_tail_path_restrictively_not_via_later_chmod."""
    recorded = {}
    real_open = os.open

    def spy(path: str, flags: int, mode: int) -> int:
        # No permissive default: _restrictive_file_opener always passes an
        # explicit mode, and a stray default here (e.g. 0o777) is itself
        # the shape CodeQL flags as an overly permissive mask on open(),
        # whether or not this test's own call path ever reaches it.
        recorded["mode"] = mode
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", spy)
    path = tmp_path / "probe-file"
    fd = wrapper_logs._restrictive_file_opener(str(path), os.O_WRONLY | os.O_CREAT)
    os.close(fd)

    assert recorded["mode"] == 0o600


def test_bounded_stream_tee_keeps_each_file_and_generation_within_cap(
    tmp_path: Path,
) -> None:
    original = io.StringIO()
    base = tmp_path / "stdout.log"
    tee = wrapper_logs.BoundedStreamTee(
        original,
        base,
        max_bytes=4096,
        segment_count=4,
    )

    tee.write("old-" * 3000)
    tee.write("TERMINAL-SENTINEL\n")
    tee.flush()
    tee.close()

    files = _log_glob(tmp_path, "stdout.log*")
    assert files
    assert all(path.stat().st_size <= 1024 for path in files)
    assert sum(path.stat().st_size for path in files) <= 3072
    assert len(original.getvalue().encode("utf-8")) <= 1024
    assert "TERMINAL-SENTINEL" in "".join(
        path.read_text(encoding="utf-8") for path in files
    )


def test_bounded_stream_tee_direct_mode_keeps_the_console_unbounded(
    tmp_path: Path,
) -> None:
    base = tmp_path / "stdout.log"
    file_stream = base.open("a", encoding="utf-8", buffering=1)
    console = io.StringIO()
    tee = wrapper_logs.BoundedStreamTee(
        file_stream,
        base,
        max_bytes=4096,
        segment_count=4,
        mirror=console,
    )
    output = "console-output-" * 1000

    tee.write(output)
    tee.close()
    file_stream.close()

    assert console.getvalue() == output
    files = _log_glob(tmp_path, "stdout.log*")
    assert all(path.stat().st_size <= 1024 for path in files)
    assert sum(path.stat().st_size for path in files) <= 4096


def test_bounded_stream_tee_newline_heavy_stream_stays_within_cap_on_disk(
    tmp_path: Path,
) -> None:
    """Finding B (PR 98 connector re-review, head 4323e20): the budget must be
    measured against what actually lands on disk, not the pre-translation
    UTF-8 length. A REAL text-mode file (unlike io.StringIO, used elsewhere in
    this file) applies the platform's newline translation on write - on
    Windows each accounted "\\n" becomes two bytes ("\\r\\n") on disk, so a
    newline-heavy stream could blow the per-file cap by nearly 2x while the
    accounting still believed it was exactly at the cap."""
    original_path = tmp_path / "original-stdout.txt"
    original = original_path.open("w", encoding="utf-8", newline=None)
    base = tmp_path / "stdout.log"
    tee = wrapper_logs.BoundedStreamTee(
        original,
        base,
        max_bytes=4096,
        segment_count=4,
    )

    tee.write("x\n" * 3000)
    tee.flush()
    tee.close()
    original.close()

    assert original_path.stat().st_size <= tee.segment_bytes


def test_bounded_stream_tee_line_buffered_original_flushes_on_newline(
    tmp_path: Path,
) -> None:
    """I4 (PR 98 cold review): writing straight to self._original.buffer
    (added to fix the CRLF cap overrun above) bypasses TextIOWrapper's own
    line-buffering entirely - stderr is line-buffered by default, so a
    diagnostic line written just before an uncatchable SIGKILL would sit
    unflushed in the underlying BufferedWriter's own (larger, not
    newline-triggered) buffer and never reach disk, defeating the entire
    reason this module exists. Read back through a SEPARATE file handle,
    with no explicit flush() call anywhere in this test, to prove the bytes
    actually reached the OS level rather than merely Python's own buffer."""
    original_path = tmp_path / "original-stderr.txt"
    original = original_path.open(
        "w", encoding="utf-8", newline=None, buffering=1
    )
    assert original.line_buffering
    base = tmp_path / "stderr.log"
    tee = wrapper_logs.BoundedStreamTee(
        original,
        base,
        max_bytes=4096,
        segment_count=4,
    )

    tee.write("final diagnostic before SIGKILL\n")
    # No tee.flush() / original.flush() here - simulating the kill landing
    # immediately after this write, before anything explicit could flush.
    on_disk = original_path.read_text(encoding="utf-8")

    tee.close()
    original.close()

    assert "final diagnostic before SIGKILL" in on_disk


def test_bounded_stream_tee_tail_flushes_after_each_write(tmp_path: Path) -> None:
    """I4 remnant (PR 98 cold review, round 3): the sibling test above fixed
    the ORIGINAL stream's flush - the tail ring's own files are raw binary
    BufferedWriters with no per-write flush of their own. Once the base
    segment's forwarding budget is spent, every further diagnostic write
    lands ONLY in the tail ring, so a small write can sit in that
    BufferedWriter's own internal buffer and never reach disk until an
    explicit flush()/close() or the buffer filling completely - the exact
    same durability gap, just one layer over. Read back through a SEPARATE
    file handle with zero explicit flush() calls anywhere in this test."""
    original = io.StringIO()
    base = tmp_path / "stderr.log"
    tee = wrapper_logs.BoundedStreamTee(
        original,
        base,
        max_bytes=4096,
        segment_count=4,
    )

    tee.write("tail diagnostic\n")
    tail_path = tmp_path / "stderr.log.1"
    on_disk = tail_path.read_text(encoding="utf-8")

    tee.close()

    assert "tail diagnostic" in on_disk


def test_bounded_stream_tee_failure_discards_excess_without_breaking_wrapper(
    tmp_path: Path,
) -> None:
    original = io.StringIO()
    blocked_parent = tmp_path / "blocked"
    blocked_parent.write_text("not a directory", encoding="utf-8")
    tee = wrapper_logs.BoundedStreamTee(
        original,
        blocked_parent / "stdout.log",
        max_bytes=4096,
        segment_count=4,
    )

    assert tee.write("x" * 10_000) == 10_000
    tee.flush()
    tee.close()

    assert len(original.getvalue().encode("utf-8")) == 1024


def test_bounded_stream_tee_tail_accounts_before_writing_not_after(
    tmp_path: Path,
) -> None:
    """Finding C (PR 98 connector re-review, head 6495534): a terminating
    signal's handler runs between bytecode instructions, so it can only land
    in the gap between the tail write and the size accounting that follows
    it - never inside the write call itself. Accounting AFTER the write
    leaves self._tail_size understated once the real bytes are already on
    disk if a signal lands in that gap, so the next write believes it has
    more room than it does and can push a segment past segment_bytes by up
    to another chunk. This interacts with the prior round's SIGTERM fix -
    making a terminating signal actually unwind (and log) promptly, instead
    of hanging, means a diagnostic write reaching this exact gap is now
    something normal termination can hit, not an exotic timing accident.

    Simulated directly rather than via a real OS signal: a tail whose
    write() raises must still have updated the accounting BEFORE that
    raise, proving accounting is ordered ahead of the write - the same
    ordering a signal's handler firing right after write() returns would
    otherwise be able to slip between."""
    original = io.StringIO()
    base = tmp_path / "stdout.log"
    tee = wrapper_logs.BoundedStreamTee(
        original,
        base,
        max_bytes=4096,
        segment_count=4,
    )
    tee._open_tail()
    real_tail = tee._tail

    class RaisingTail:
        def write(self, data: object) -> None:
            raise OSError("simulated interruption during the tail write")

    tee._tail = RaisingTail()
    size_before = tee._tail_size
    with pytest.raises(OSError):
        tee._write_tail(b"x" * 100)
    assert tee._tail_size == size_before + 100, (
        "the tail's size accounting was not updated before the write that "
        "raised - a signal landing in that gap would understate it instead"
    )

    tee._tail = real_tail
    tee.close()


def test_bounded_stream_tee_resume_appends_to_newest_segment_instead_of_truncating(
    tmp_path: Path,
) -> None:
    """Round 21 (connector finding; withdraws the trade-off accepted in
    round 20): print_bounded_uncaught_exception's fresh, second tee used to
    always reset to segment .1 and open it with "wb" - truncating whatever
    was there regardless of whether it was the OLDEST content (as normal
    rotation would discard anyway) or the NEWEST (a live wrapper's own
    lifecycle output, written moments before the crash). resume=True must
    find the segment the FIRST instance was actually writing to and APPEND
    to it instead.

    Round 29 correction: this only ever exercised the single-candidate
    case (one short write never rotates past .1), so it never actually
    proved which SELECTION mechanism resume used - see
    test_bounded_stream_tee_resume_follows_cursor_not_misleading_mtime for
    the adversarial case that does."""
    base = tmp_path / "stderr.log"
    original = io.StringIO()
    first = wrapper_logs.BoundedStreamTee(
        original, base, max_bytes=4096, segment_count=4,
    )
    first.write("FIRST-INSTANCE-NEWEST-LIFECYCLE-OUTPUT\n")
    first.close()

    tail_files = _log_glob(tmp_path, "stderr.log.*")
    assert len(tail_files) == 1
    newest = tail_files[0]
    newest_content_before = newest.read_bytes()
    assert b"FIRST-INSTANCE-NEWEST-LIFECYCLE-OUTPUT" in newest_content_before

    second = wrapper_logs.BoundedStreamTee(
        io.StringIO(), base, max_bytes=4096, segment_count=4, resume=True,
    )
    second.write("SECOND-INSTANCE-CRASH-SENTINEL\n")
    second.close()

    combined = newest.read_bytes()
    assert combined.startswith(newest_content_before), (
        "resume must APPEND to the newest segment, not truncate it"
    )
    assert b"SECOND-INSTANCE-CRASH-SENTINEL" in combined


def test_bounded_stream_tee_resume_follows_cursor_not_misleading_mtime(
    tmp_path: Path,
) -> None:
    """Round 29 connector finding (P2, against the round-21 fix above):
    choosing the current segment by st_mtime is ambiguous on filesystems
    with coarse timestamp resolution and wrong after a backward clock
    adjustment - on a tie the highest index wins even when a DIFFERENT
    suffix holds the newest output, and if that wrongly-chosen suffix is
    already near full, the next write truncates the segment that actually
    held the newest crash evidence. Proves the fix by constructing exactly
    that adversarial mtime shape and showing resume follows the persisted
    cursor instead: segment .1 is made to look newer BY MTIME than the
    segment the first instance actually finished writing to (.2); resume
    must still append to .2, the one recorded in the cursor file, not .1."""
    base = tmp_path / "stderr.log"
    first = wrapper_logs.BoundedStreamTee(
        io.StringIO(), base, max_bytes=4096, segment_count=4,
    )
    # segment_bytes = 4096 // 4 = 1024. Fill .1 then rotate into .2 - the
    # cursor now records index 1 (".2"), while .1 sits untouched since.
    first.write("A" * 1024)
    first.write("FIRST-INSTANCE-NEWEST-LIFECYCLE-OUTPUT\n")
    first.close()

    tail_files = {p.name: p for p in _log_glob(tmp_path, "stderr.log.*")}
    assert set(tail_files) == {"stderr.log.1", "stderr.log.2"}
    cursor_before = (tmp_path / "stderr.log.cursor").read_text(encoding="utf-8")
    assert cursor_before == "1"          # 0-based index for .2, the current one
    newest_content_before = tail_files["stderr.log.2"].read_bytes()
    assert b"FIRST-INSTANCE-NEWEST-LIFECYCLE-OUTPUT" in newest_content_before

    # Make the OLDER, already-superseded .1 look newer than .2 by mtime -
    # exactly the ambiguous shape a coarse filesystem clock or a backward
    # wall-clock adjustment can produce for real, without needing either.
    newer_than_now = os.path.getmtime(tail_files["stderr.log.2"]) + 5
    os.utime(tail_files["stderr.log.1"], (newer_than_now, newer_than_now))
    assert (
        tail_files["stderr.log.1"].stat().st_mtime
        > tail_files["stderr.log.2"].stat().st_mtime
    )

    second = wrapper_logs.BoundedStreamTee(
        io.StringIO(), base, max_bytes=4096, segment_count=4, resume=True,
    )
    second.write("SECOND-INSTANCE-CRASH-SENTINEL\n")
    second.close()

    # .2 (the cursor-recorded segment) got the append - the mtime-newer .1
    # was never touched, still holds only its original "A"*1024.
    combined = tail_files["stderr.log.2"].read_bytes()
    assert combined.startswith(newest_content_before), (
        "resume must follow the recorded cursor, not the misleading mtime"
    )
    assert b"SECOND-INSTANCE-CRASH-SENTINEL" in combined
    assert tail_files["stderr.log.1"].read_bytes() == b"A" * 1024


def test_bounded_stream_tee_resume_appends_not_truncates_when_no_cursor_exists(
    tmp_path: Path,
) -> None:
    """Round 29 rereview: the missing-cursor fallback - true of every
    generation already on disk the moment cursor-recording ships - must
    never truncate. My first draft treated "no cursor" as "resume was
    never requested at all", defaulting straight back to the ORIGINAL
    truncate-on-first-write behavior - which is strictly WORSE than the
    mtime scan it replaced on precisely the generations that predate this
    fix (every one on disk at upgrade time), and worse specifically in the
    crash path this module exists for: the mtime scan was USUALLY right,
    "no cursor -> wb" is wrong every time. Simulates a pre-fix generation
    directly - write .1's content by hand, with NO cursor file, exactly
    what an old-code generation looks like - then resumes into it and
    confirms the existing content survives, appended to rather than
    overwritten."""
    base = tmp_path / "stderr.log"
    pre_fix_tail = base.with_name("stderr.log.1")
    pre_fix_tail.write_bytes(b"PRE-FIX-GENERATION-CONTENT-NO-CURSOR-EXISTS\n")
    assert not (tmp_path / "stderr.log.cursor").exists()

    second = wrapper_logs.BoundedStreamTee(
        io.StringIO(), base, max_bytes=4096, segment_count=4, resume=True,
    )
    second.write("SECOND-INSTANCE-CRASH-SENTINEL\n")
    second.close()

    combined = pre_fix_tail.read_bytes()
    assert combined.startswith(b"PRE-FIX-GENERATION-CONTENT-NO-CURSOR-EXISTS\n"), (
        "a missing cursor must never truncate pre-existing content"
    )
    assert b"SECOND-INSTANCE-CRASH-SENTINEL" in combined


def test_bounded_stream_tee_resume_survives_a_stale_cursor_after_rewrite_failure(
    tmp_path: Path,
) -> None:
    """PR 98 round 29 connector finding, against 7a4ee43: if the
    best-effort cursor rewrite in _write_tail_cursor fails right after
    _advance_tail has already opened a new suffix, the STALE cursor
    (still pointing at the segment just rotated OUT of, which is already
    full) survives on disk. A later resume=True instance trusts it,
    immediately advances past the reported-full segment on its first
    write, and - before this fix - opened the NEXT suffix with "wb",
    destroying whatever the crashed instance had already written there
    before its own cursor-rewrite failure ever happened.

    Simulates exactly that: .1 hand-written full (segment_bytes), .2
    hand-written with real, never-recorded content (the segment the
    crashed instance HAD rotated into but never got to confirm), and a
    cursor file left stale at "0" (still naming .1). A resumed instance
    must find its OWN first write immediately cascades past the
    reported-full .1 into .2 - and append there, not truncate, because
    THIS instance has never itself visited .2 before, regardless of what
    the stale cursor said about .1."""
    base = tmp_path / "stderr.log"
    segment_bytes = 4096 // 4
    (tmp_path / "stderr.log.1").write_bytes(b"A" * segment_bytes)
    (tmp_path / "stderr.log.2").write_bytes(b"INSTANCE-A-REAL-UNRECORDED-CONTENT\n")
    (tmp_path / "stderr.log.cursor").write_text("0", encoding="utf-8")  # stale

    second = wrapper_logs.BoundedStreamTee(
        io.StringIO(), base, max_bytes=4096, segment_count=4, resume=True,
    )
    second.write("SECOND-INSTANCE-CRASH-SENTINEL\n")
    second.close()

    seg2 = (tmp_path / "stderr.log.2").read_bytes()
    assert seg2.startswith(b"INSTANCE-A-REAL-UNRECORDED-CONTENT\n"), (
        "a stale cursor's cascading rotation must never truncate a segment "
        "this instance has not itself visited before"
    )
    assert b"SECOND-INSTANCE-CRASH-SENTINEL" in seg2
    # .1 (where the stale cursor pointed) is untouched-then-appended-to on
    # this instance's own first visit, same guarantee as the no-cursor case.
    assert (tmp_path / "stderr.log.1").read_bytes().startswith(b"A" * segment_bytes)


def test_bounded_stream_tee_resume_still_rotates_and_truncates_the_next_segment(
    tmp_path: Path,
) -> None:
    """A resumed instance must still behave like a normal one once the
    resumed segment fills up - truncating the NEXT segment in ring order is
    the correct, expected ring-buffer behavior, only the FIRST open (the
    resumed one) is special."""
    base = tmp_path / "stderr.log"
    first = wrapper_logs.BoundedStreamTee(
        io.StringIO(), base, max_bytes=4096, segment_count=4,
    )
    # Long enough to cycle through all 3 tail segments more than once, so
    # every one of them ends up holding some of this text - avoids having
    # to manufacture a stale file with a manually-set mtime.
    first.write("old-content-that-must-be-evicted-by-rotation " * 200)
    first.close()

    tail_files = _log_glob(tmp_path, "stderr.log.*")
    assert len(tail_files) == 3
    before = {p.name: p.read_bytes() for p in tail_files}
    assert all(b"old-content" in content for content in before.values())

    second = wrapper_logs.BoundedStreamTee(
        io.StringIO(), base, max_bytes=4096, segment_count=4, resume=True,
    )
    # Enough to force a full extra rotation cycle past the resumed segment.
    second.write("x" * 3500)
    second.close()

    after = {p.name: p.read_bytes() for p in tail_files}
    evicted = [
        name for name, content in after.items() if b"old-content" not in content
    ]
    assert evicted, "at least one segment must have been freshly rotated into"
    # Every freshly-rotated segment must be a clean truncate - no stale
    # old-content leaking in alongside the new "x"s.
    for name in evicted:
        assert after[name].strip(b"x") == b""


def test_bounded_stream_tee_tail_rotates_before_splitting_a_utf8_code_point(
    tmp_path: Path,
) -> None:
    """Round 11 connector finding, the serious one: _write_tail sliced the
    encoded byte buffer at `available` with no regard for UTF-8 code point
    boundaries. When a multi-byte character's leading byte is the LAST byte
    that fits in the current segment, the old code split its encoded bytes
    across two files - the first segment ends with a dangling lead byte,
    the next begins with an orphaned continuation byte, and a strict UTF-8
    reader then fails to open EITHER file - not merely mis-render one
    character, the whole diagnostic becomes unopenable.

    Constructed at the byte level, not hoped into: 127 ASCII bytes exactly
    fill the segment to available=1, so e-acute (2-byte UTF-8) lands with
    its lead byte as the very last byte that would fit. Asserted on the
    raw bytes read back from disk, each segment decoded standalone - a
    string round-trip through Python would paper over exactly this."""
    original = io.StringIO()
    base = tmp_path / "stdout.log"
    tee = wrapper_logs.BoundedStreamTee(
        original,
        base,
        max_bytes=4096,
        segment_count=32,  # segment_bytes = 4096 // 32 = 128
    )
    assert tee.segment_bytes == 128

    payload = ("A" * 127) + "é" + "BB"  # 127 + 2 + 2 = 131 encoded bytes
    tee.write(payload)
    tee.close()

    seg1 = (tmp_path / "stdout.log.1").read_bytes()
    seg2 = (tmp_path / "stdout.log.2").read_bytes()

    seg1.decode("utf-8")
    seg2.decode("utf-8")

    assert seg1 == b"A" * 127
    assert seg2 == "éBB".encode("utf-8")


def test_signal_diagnostic_is_deferred_until_stream_write_unwinds(
    tmp_path: Path,
) -> None:
    original = io.StringIO()
    tee = wrapper_logs.BoundedStreamTee(
        original,
        tmp_path / "stderr.log",
        max_bytes=4096,
        segment_count=4,
    )
    lifecycle = wrapper_logs.WrapperLifecycleLog(
        "worker",
        stream=tee,
        clock=lambda: NOW,
    )

    lifecycle.defer_signal(signal.SIGTERM, terminating=True)

    tee._lock.acquire()
    try:
        assert original.getvalue() == ""
    finally:
        tee._lock.release()
    lifecycle.flush_deferred_signal()
    try:
        assert '"event":"wrapper_signal_received"' in original.getvalue()
    finally:
        tee.close()


def test_runtime_transitions_emit_only_factual_lifecycle_lines(
    tmp_path: Path,
) -> None:
    stream = io.StringIO()
    lifecycle = wrapper_logs.WrapperLifecycleLog(
        "worker",
        stream=stream,
        clock=lambda: NOW,
    )
    writer = runtime.WrapperRuntimeWriter(
        tmp_path,
        "worker",
        "generation-1",
        wrapper_pid=123,
        wrapper_start="start-123",
        clock=lambda: NOW,
        on_transition=lifecycle.runtime_transition,
    )

    writer.idle()
    writer.starting(message_id="msg-1", turn_id="turn-1")
    writer.active(456, "start-456")
    writer.progress()
    lifecycle.child_exited(456, "start-456", 7)
    writer.terminal(runtime.OUTCOME_FAILED)
    writer.idle()
    lifecycle.wrapper_exited(0, reason="loop_returned")

    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [row["event"] for row in rows] == [
        "waiting_for_mail",
        "turn_started",
        "child_spawned",
        "child_exited",
        "turn_ended",
        "waiting_for_mail",
        "wrapper_exited",
    ]
    child_exit = rows[3]
    assert child_exit["agent"] == "worker"
    assert child_exit["wrapper_pid"] == 123
    assert child_exit["turn_generation"] == 1
    assert child_exit["turn_id"] == "turn-1"
    assert child_exit["cli_launcher_pid"] == 456
    assert child_exit["progress_sequence"] == 1
    assert child_exit["last_progress_at"] is not None
    assert child_exit["child_pid"] == 456
    assert child_exit["return_code"] == 7
    assert rows[4]["last_outcome"] == runtime.OUTCOME_FAILED
    assert all(
        forbidden not in stream.getvalue().casefold()
        for forbidden in (
            '"healthy"',
            '"ok"',
            '"alive"',
            '"progressing"',
            "working normally",
        )
    )


def test_lifecycle_sink_failure_never_breaks_runtime_transition(
    tmp_path: Path,
) -> None:
    class BrokenStream:
        def write(self, _value: str) -> int:
            raise OSError("simulated full disk")

        def flush(self) -> None:
            raise OSError("simulated full disk")

    lifecycle = wrapper_logs.WrapperLifecycleLog(
        "worker",
        stream=BrokenStream(),
        clock=lambda: NOW,
    )
    writer = runtime.WrapperRuntimeWriter(
        tmp_path,
        "worker",
        "generation-1",
        wrapper_pid=123,
        wrapper_start="start-123",
        clock=lambda: NOW,
        on_transition=lifecycle.runtime_transition,
    )

    record = writer.idle()

    assert record["phase"] == runtime.PHASE_IDLE
    assert runtime.read_runtime(
        tmp_path,
        "worker",
        now_epoch=NOW,
    )["status"] == runtime.STATUS_VALID


def test_mid_turn_exception_trail_keeps_turn_and_child_without_fabricated_end(
    tmp_path: Path,
) -> None:
    stream = io.StringIO()
    lifecycle = wrapper_logs.WrapperLifecycleLog(
        "worker",
        stream=stream,
        clock=lambda: NOW,
    )
    writer = runtime.WrapperRuntimeWriter(
        tmp_path,
        "worker",
        "generation-1",
        wrapper_pid=123,
        wrapper_start="start-123",
        clock=lambda: NOW,
        on_transition=lifecycle.runtime_transition,
    )
    writer.starting(message_id="msg-1", turn_id="turn-41")
    writer.active(456, "start-456")

    lifecycle.wrapper_exception(RuntimeError("simulated abrupt wrapper failure"))

    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [row["event"] for row in rows] == [
        "turn_started",
        "child_spawned",
        "wrapper_exception",
    ]
    assert rows[-1]["turn_id"] == "turn-41"
    assert rows[-1]["cli_launcher_pid"] == 456
    assert rows[-1]["exception_type"] == "RuntimeError"
    assert "turn_ended" not in {row["event"] for row in rows}


def test_dead_letter_disposition_is_not_reported_as_a_driven_turn(
    tmp_path: Path,
) -> None:
    stream = io.StringIO()
    lifecycle = wrapper_logs.WrapperLifecycleLog(
        "worker",
        stream=stream,
        clock=lambda: NOW,
    )
    writer = runtime.WrapperRuntimeWriter(
        tmp_path,
        "worker",
        "generation-1",
        wrapper_pid=123,
        wrapper_start="start-123",
        clock=lambda: NOW,
        on_transition=lifecycle.runtime_transition,
    )

    writer.idle()
    writer.dead_letter(message_id="msg-dead")

    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [row["event"] for row in rows] == [
        "waiting_for_mail",
        "message_dead_lettered",
    ]
    assert rows[-1]["message_id"] == "msg-dead"
    assert rows[-1]["last_outcome"] == runtime.OUTCOME_DEAD_LETTER


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="SIGTERM unavailable")
def test_signal_logging_is_deferred_and_existing_handler_is_restored() -> None:
    stream = io.StringIO()
    lifecycle = wrapper_logs.WrapperLifecycleLog(
        "worker",
        stream=stream,
        clock=lambda: NOW,
        wrapper_pid=123,
    )
    calls: list[tuple[int, int]] = []

    def prior(signum: int, frame: object) -> None:
        calls.append((signum, len(stream.getvalue().splitlines())))

    old = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, prior)
    try:
        with wrapper_logs.capture_termination_signals(lifecycle):
            installed = signal.getsignal(signal.SIGTERM)
            assert callable(installed)
            installed(signal.SIGTERM, None)
        assert signal.getsignal(signal.SIGTERM) is prior
    finally:
        signal.signal(signal.SIGTERM, old)

    assert calls == [(signal.SIGTERM, 0)]
    row = json.loads(stream.getvalue())
    assert row["event"] == "wrapper_signal_received"
    assert row["signal"] == int(signal.SIGTERM)
    assert row["terminating"] is False
    assert lifecycle.terminal_emitted is False


@pytest.mark.skipif(not hasattr(signal, "SIGTERM"), reason="SIGTERM unavailable")
def test_ignored_signal_is_not_logged_as_wrapper_termination() -> None:
    stream = io.StringIO()
    lifecycle = wrapper_logs.WrapperLifecycleLog(
        "worker",
        stream=stream,
        clock=lambda: NOW,
        wrapper_pid=123,
    )
    old = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        with wrapper_logs.capture_termination_signals(lifecycle):
            installed = signal.getsignal(signal.SIGTERM)
            assert callable(installed)
            installed(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, old)

    assert stream.getvalue() == ""
    assert lifecycle.terminal_emitted is False


def test_stream_environment_context_restores_process_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, "a" * 32)
    before_out = io.StringIO()
    before_err = io.StringIO()
    monkeypatch.setattr("sys.stdout", before_out)
    monkeypatch.setattr("sys.stderr", before_err)

    with wrapper_logs.installed_standard_streams_from_environment(
        expected_nonce="a" * 32,
    ):
        import sys

        assert sys.stdout is not before_out
        assert sys.stderr is not before_err
        sys.stdout.write("child-output\n")
        sys.stderr.write("child-error\n")

    import sys

    assert sys.stdout is before_out
    assert sys.stderr is before_err
    assert "child-output" in "".join(
        path.read_text(encoding="utf-8") for path in _log_glob(tmp_path, "stdout.log*")
    )
    assert "child-error" in "".join(
        path.read_text(encoding="utf-8") for path in _log_glob(tmp_path, "stderr.log*")
    )
    assert os.environ[wrapper_logs.ENV_STDOUT_PATH] == str(out_path)


def test_stream_environment_confirms_the_generation_from_inside_the_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 23: replaces the round-19/20/21 predictive probe entirely - the
    supervisor no longer forecasts whether a launch will reach cmd_wrap, it
    waits for THIS wrapper process to say so by evidence. Simulates the
    supervisor's own .pending marker (New-WrapperLogPendingMarker) to prove
    the wrapper transitions it to .committed itself, at the point
    authentication has already succeeded and both tees are live - not
    merely that the marker exists at some point after the fact."""
    generation = tmp_path / "generation"
    generation.mkdir()
    (generation / ".pending").write_bytes(b"")
    out_path = generation / "stdout.log"
    err_path = generation / "stderr.log"
    nonce = "a" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    with wrapper_logs.installed_standard_streams_from_environment(
        expected_nonce=nonce,
    ):
        # Confirmed WHILE still inside the context - proving this happens
        # at stream-install time, not merely "eventually, somehow".
        assert (generation / ".committed").exists()
        assert not (generation / ".pending").exists()

    assert (generation / ".committed").exists()


def test_authenticated_supervisor_targets_record_their_actual_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    store = Store(project)
    store.init(["worker"])
    root = tmp_path / "preallocated-root"
    generation = (
        root
        / wrapper_logs._wrapper_log_agent_leaf("worker")
        / "20260804T120000000Z-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )
    generation.mkdir(parents=True)
    (generation / ".pending").write_bytes(b"")
    stdout_path = generation / "stdout.log"
    stderr_path = generation / "stderr.log"
    stdout_path.write_bytes(b"")
    stderr_path.write_bytes(b"")
    nonce = "a" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(stdout_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(stderr_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    with wrapper_logs.installed_standard_streams_from_environment(
        expected_nonce=nonce,
        project_root=project,
        agent="worker",
    ):
        sys.stderr.write("supervised target accepted\n")

    location = wrapper_logs.read_wrapper_log_location(store.state_dir, "worker")
    assert location["status"] == "observed"
    assert Path(str(location["root"])) == root.resolve()
    assert Path(str(location["generation_dir"])) == generation.resolve()


def test_invalid_location_path_resolution_cannot_crash_report_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store(tmp_path / "project")
    store.init(["worker"])
    location_path = wrapper_logs._wrapper_log_location_path(
        store.state_dir,
        "worker",
    )
    location_path.parent.mkdir(parents=True)
    location_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "agent": "worker",
                "root": str(tmp_path / "resolution-loop"),
                "generation_dir": str(tmp_path / "generation"),
                "stdout": str(tmp_path / "stdout.log"),
                "stderr": str(tmp_path / "stderr.log"),
            }
        ),
        encoding="utf-8",
    )
    real_resolve = Path.resolve

    def fail_one_path(path: Path, *args, **kwargs) -> Path:
        if path.name == "resolution-loop":
            raise RuntimeError("symlink loop")
        return real_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_one_path)

    location = wrapper_logs.read_wrapper_log_location(store.state_dir, "worker")

    assert location["status"] == "invalid"
    assert location["root"] is None


def test_stream_environment_without_a_matching_nonce_never_touches_markers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A launch that never authenticates never installs streams at all -
    it must not confirm a generation it was never actually authorized to
    write into either."""
    generation = tmp_path / "generation"
    generation.mkdir()
    (generation / ".pending").write_bytes(b"")
    out_path = generation / "stdout.log"
    err_path = generation / "stderr.log"
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, "a" * 32)

    with wrapper_logs.installed_standard_streams_from_environment(
        expected_nonce="b" * 32,
    ):
        pass

    assert not (generation / ".committed").exists()
    assert (generation / ".pending").exists()


def test_cmd_wrap_records_setup_exception_before_loop_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    nonce = "a" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    def fail_setup(_args: argparse.Namespace) -> int:
        raise RuntimeError("setup failed before run_loop")

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", fail_setup)
    args = argparse.Namespace(agent="worker", supervisor_launch_nonce=nonce)

    # New area (cold review): a bare re-raise here lets the original
    # RuntimeError reach main()'s uncaught-exception path, which prints its
    # OWN traceback AFTER streams are restored - to the unbounded raw file.
    # That is not a regression: the bounded diagnostic capture below (the
    # traceback that ends up IN the wrapper log tail) already happened,
    # via traceback.print_exc(file=sys.stderr), while sys.stderr was still
    # the bounded tee - BEFORE this raise. A second, redundant traceback on
    # the raw console afterward is exactly what an uncaught exception
    # should show an operator; #117's bounded capture does not depend on
    # suppressing it. Round 18: cmd_wrap propagates the ORIGINAL exception
    # here (not a converted SystemExit(1)) so an embedder or test runner
    # calling cli.main([...]) can catch RuntimeError specifically, inspect
    # it, and retry - the same contract main() had before this PR ever
    # touched cmd_wrap.
    with pytest.raises(RuntimeError, match="setup failed before run_loop"):
        cli.cmd_wrap(args)

    tail = "".join(
        path.read_text(encoding="utf-8") for path in _log_glob(tmp_path, "stderr.log*")
    )
    rows = [json.loads(line) for line in tail.splitlines() if line.startswith("{")]
    assert [row["event"] for row in rows] == ["wrapper_exception"]
    assert rows[0]["exception_type"] == "RuntimeError"
    # The traceback itself must also survive - it is captured through the
    # bounded stream before cmd_wrap's context manager restores raw streams.
    assert "setup failed before run_loop" in tail
    assert "Traceback (most recent call last)" in tail


def test_cmd_wrap_routine_system_exit_emits_exited_fact_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # cli._get_store's "not initialized" path writes its own actionable
    # diagnostic and calls sys.exit(2) directly - a routine, already-
    # explained exit, not a crash. Two regressions, in sequence:
    # (1) cmd_wrap's except block used to record wrapper_exception and
    # print a full Python traceback on top of it regardless of exception
    # type, turning a one-line diagnostic into crash-report noise; fixing
    # that by bare-raising on any SystemExit (2) overcorrected into
    # emitting NO lifecycle fact at all for this shape - no deferred signal
    # exists to have recorded one, unlike the signal-driven SystemExit
    # case - so the trail ended with no termination fact whatsoever,
    # indistinguishable from an OOM or a hard kill when reading the JSON
    # lines. Every termination path must emit exactly one termination
    # fact: here, a normalized wrapper_exited - not wrapper_exception,
    # since this was never an unexplained exception - and still no
    # traceback.
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    nonce = "e" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    def not_initialized(_args: argparse.Namespace) -> int:

        sys.stderr.write(
            "agenttalk: not initialized at X\n"
            "Run `agenttalk init --here` from the project root.\n"
        )
        raise SystemExit(2)

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", not_initialized)
    args = argparse.Namespace(agent="worker", supervisor_launch_nonce=nonce)

    with pytest.raises(SystemExit) as exc_info:
        cli.cmd_wrap(args)
    assert exc_info.value.code == 2

    tail = "".join(
        path.read_text(encoding="utf-8") for path in _log_glob(tmp_path, "stderr.log*")
    )
    rows = [json.loads(line) for line in tail.splitlines() if line.startswith("{")]
    assert [row["event"] for row in rows] == ["wrapper_exited"]
    assert rows[0]["exit_code"] == 2
    assert rows[0]["reason"] == "system_exit"
    assert "not initialized" in tail
    assert "Traceback (most recent call last)" not in tail


@pytest.mark.parametrize(
    "raise_exc,expected_code,expected_reason,expected_text",
    [
        (lambda: KeyboardInterrupt(), 130, "keyboard_interrupt", "interrupted"),
        (lambda: ValueError("bad value"), 2, "mapped_cli_exception", "bad value"),
        (lambda: FileNotFoundError("missing.toml"), 2, "mapped_cli_exception",
         "missing.toml"),
        (lambda: OSError("disk full"), 2, "mapped_cli_exception", "disk full"),
    ],
    ids=["KeyboardInterrupt", "ValueError", "FileNotFoundError", "OSError"],
)
def test_cmd_wrap_routine_exception_types_skip_crash_reporting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raise_exc,
    expected_code: int,
    expected_reason: str,
    expected_text: str,
) -> None:
    # I2's shape, one layer up: main() already has a concise, actionable
    # diagnostic for KeyboardInterrupt and (ValueError, FileNotFoundError,
    # OSError) - cmd_wrap's except block converts them to the SAME exit
    # codes at the bottom, but the crash-reporting block above that
    # conversion ran unconditionally for anything that wasn't SystemExit,
    # so an OSError got wrapper_exception + a full Python traceback BEFORE
    # being converted to the CLI's normal one-line error. The property:
    # the crash path runs ONLY for exceptions not in this known,
    # concise-diagnostic set - enumerated as a set (this parametrize), not
    # patched type by type. A future exception type that gains a concise
    # diagnostic elsewhere and is not added here must fail this test, not
    # silently fall through to the crash path.
    #
    # Round 17 connector finding: this used to assert cmd_wrap RAISES
    # SystemExit(expected_code) for all four - which was itself the bug.
    # main() previously RETURNED an int for exactly these two classes
    # (KeyboardInterrupt, and ValueError/FileNotFoundError/OSError); a
    # raised SystemExit is not an Exception subclass, so it bypasses
    # main()'s own except clauses entirely and can escape a caller that
    # invokes cli.main([...]) programmatically expecting an int back
    # (an embedder, or a test runner). cmd_wrap now RETURNS the code
    # instead, restoring that contract regardless of whether cmd_wrap's
    # own exception handling sits in front of main()'s.
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    nonce = "f" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    def raiser(_args: argparse.Namespace) -> int:
        raise raise_exc()

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", raiser)
    args = argparse.Namespace(agent="worker", supervisor_launch_nonce=nonce)

    assert cli.cmd_wrap(args) == expected_code

    tail = "".join(
        path.read_text(encoding="utf-8") for path in _log_glob(tmp_path, "stderr.log*")
    )
    rows = [json.loads(line) for line in tail.splitlines() if line.startswith("{")]
    assert [row["event"] for row in rows] == ["wrapper_exited"]
    assert rows[0]["exit_code"] == expected_code
    assert rows[0]["reason"] == expected_reason
    assert expected_text in tail
    assert "Traceback (most recent call last)" not in tail


@pytest.mark.parametrize(
    "raise_exc,expected_return,expected_raise",
    [
        (lambda: SystemExit(2), None, SystemExit),
        (lambda: KeyboardInterrupt(), 130, None),
        (lambda: ValueError("bad value"), 2, None),
        (lambda: FileNotFoundError("missing.toml"), 2, None),
        (lambda: OSError("disk full"), 2, None),
        (lambda: RuntimeError("truly unexpected"), None, RuntimeError),
    ],
    ids=["SystemExit", "KeyboardInterrupt", "ValueError", "FileNotFoundError",
         "OSError", "RuntimeError"],
)
def test_cmd_wrap_and_main_exception_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raise_exc,
    expected_return: int | None,
    expected_raise: type[BaseException] | None,
) -> None:
    """Round 17/18 connector findings: the stated contract, tested end to
    end through cli.main() - not just cmd_wrap in isolation - because the
    property that actually matters is what an embedder or a test runner
    calling cli.main([...]) programmatically observes, and that can only
    be proven by calling main() itself.

    THE CONTRACT (exception classes that can reach cmd_wrap's handler):

    | class                        | cmd_wrap/main()   | lifecycle fact       | tb  |
    |-------------------------------|-------------------|-----------------------|-----|
    | SystemExit (deliberate)       | raises (same)     | wrapper_exited(code,"system_exit") | no |
    | KeyboardInterrupt             | returns 130       | wrapper_exited(130,"keyboard_interrupt") | no |
    | ValueError/FileNotFoundError/ | returns 2         | wrapper_exited(2,"mapped_cli_exception") | no |
    | OSError                       |                   |                       |     |
    | anything else (unexpected)    | raises (original) | wrapper_exception(exc) | yes |

    Every row now preserves main()'s PRE-#117 contract exactly - no
    behavior change anywhere except that rows 2 and 3 are now actually
    correct (they used to raise SystemExit, breaking main()'s contract
    for those two classes specifically):

    - SystemExit: main() has never caught bare SystemExit (no except
      clause for it, ever) - letting it propagate matches what happens
      with no exception handling here at all.
    - KeyboardInterrupt, ValueError/FileNotFoundError/OSError: main()
      RETURNED an int for these before this PR touched cmd_wrap.
      Raising SystemExit(code) for them bypassed main()'s own except
      clauses (SystemExit is not an Exception subclass) and let it
      escape a caller expecting an int back - round 17's fix.
    - Anything else (unexpected): main() had no RETURN contract for this
      class either - it let the ORIGINAL exception type propagate
      uncaught. Converting it to SystemExit(1) (round 17's own crash-
      design choice) destroyed the type information an embedder needs
      and substituted a class an ordinary `except Exception` would miss
      - round 18's fix: propagate the original after recording the
      crash fact, restoring the SAME pre-#117 status quo, not a new
      contract. The "one known exit code for any crash" goal was a
      CONSOLE concern the console gets for free (Python exits 1 on any
      uncaught exception regardless), so nothing is lost there either."""
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    nonce = "3" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    def raiser(_args: argparse.Namespace) -> int:
        raise raise_exc()

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", raiser)

    argv = ["--supervisor-launch-nonce", nonce, "wrap", "--for", "worker"]
    if expected_raise is SystemExit:
        with pytest.raises(SystemExit) as exc_info:
            cli.main(argv)
        assert exc_info.value.code == 2
    elif expected_raise is not None:
        with pytest.raises(expected_raise):
            cli.main(argv)
    else:
        assert cli.main(argv) == expected_return


def test_cmd_wrap_unclassified_exception_still_gets_crash_reporting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The other half of the property: an exception type NOT in the known,
    # concise-diagnostic set must still fall through to the crash path -
    # this is the visible-omission guard the enumeration exists for.
    #
    # Round 18: this used to assert cmd_wrap converts the exception to
    # SystemExit(1) - the same contract violation as rows 2/3 (KeyboardInterrupt,
    # ValueError/FileNotFoundError/OSError), one row down. main() never had
    # a RETURN contract for an unexpected type - it would have let the
    # ORIGINAL type propagate uncaught - so cmd_wrap must propagate the
    # original RuntimeError here too, after recording the crash fact, not
    # substitute a SystemExit an embedder's `except Exception` would miss.
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    nonce = "9" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    def raiser(_args: argparse.Namespace) -> int:
        raise RuntimeError("truly unexpected")

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", raiser)
    args = argparse.Namespace(agent="worker", supervisor_launch_nonce=nonce)

    with pytest.raises(RuntimeError, match="truly unexpected"):
        cli.cmd_wrap(args)

    tail = "".join(
        path.read_text(encoding="utf-8") for path in _log_glob(tmp_path, "stderr.log*")
    )
    rows = [json.loads(line) for line in tail.splitlines() if line.startswith("{")]
    assert [row["event"] for row in rows] == ["wrapper_exception"]
    assert rows[0]["exception_type"] == "RuntimeError"
    assert "Traceback (most recent call last)" in tail


def test_cmd_wrap_entry_bounds_python_level_wrapper_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    out_path.write_text("B" * 1024, encoding="utf-8")
    err_path.write_text("", encoding="utf-8")
    nonce = "c" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setenv(wrapper_logs.ENV_MAX_BYTES, "4096")
    monkeypatch.setenv(wrapper_logs.ENV_SEGMENT_COUNT, "4")
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    def emit_output(_args: argparse.Namespace) -> int:

        sys.stdout.write("x" * 20_000)
        sys.stdout.write("FINAL-SENTINEL\n")
        return 0

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", emit_output)
    args = argparse.Namespace(agent="worker", supervisor_launch_nonce=nonce)

    assert cli.cmd_wrap(args) == 0

    files = _log_glob(tmp_path, "stdout.log*")
    assert all(path.stat().st_size <= 1024 for path in files)
    assert sum(path.stat().st_size for path in files) <= 4096
    assert "FINAL-SENTINEL" in "".join(
        path.read_text(encoding="utf-8") for path in files
    )


def test_cmd_wrap_uncaught_exception_traceback_is_bounded_and_in_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    out_path.write_text("", encoding="utf-8")
    err_path.write_text("E" * 1024, encoding="utf-8")
    nonce = "d" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setenv(wrapper_logs.ENV_MAX_BYTES, "4096")
    monkeypatch.setenv(wrapper_logs.ENV_SEGMENT_COUNT, "4")
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    def blow_up(_args: argparse.Namespace) -> int:

        # The initial segment is already full; more chatter before the crash
        # forces several rotations, so the eventual traceback lands in a
        # tail segment rather than the (already-evicted) first one.
        sys.stderr.write("x" * 20_000)
        raise RuntimeError("TRACEBACK-SENTINEL-BOOM")

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", blow_up)
    args = argparse.Namespace(agent="worker", supervisor_launch_nonce=nonce)

    # Round 18: cmd_wrap now propagates the ORIGINAL RuntimeError rather
    # than converting it to SystemExit(1) - the bounded capture below is
    # unaffected either way, since it happens before this raise.
    with pytest.raises(RuntimeError, match="TRACEBACK-SENTINEL-BOOM"):
        cli.cmd_wrap(args)

    files = _log_glob(tmp_path, "stderr.log*")
    assert all(path.stat().st_size <= 1024 for path in files)
    assert sum(path.stat().st_size for path in files) <= 4096
    tail = "".join(
        path.read_text(encoding="utf-8", errors="replace") for path in files
    )
    assert "TRACEBACK-SENTINEL-BOOM" in tail
    assert "Traceback (most recent call last)" in tail


def test_print_bounded_uncaught_exception_writes_into_tail_ring_when_config_is_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    err_path.write_text("E" * 1024, encoding="utf-8")
    nonce = "f" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setenv(wrapper_logs.ENV_MAX_BYTES, "4096")
    monkeypatch.setenv(wrapper_logs.ENV_SEGMENT_COUNT, "4")
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())
    monkeypatch.setattr(wrapper_logs, "_LAST_STDERR_LOG_CONFIG", None)

    # Simulate cmd_wrap's own tee lifecycle: installed, writes some real
    # lifecycle output (round 21: this is the content a naive fresh tee
    # would have truncated), then torn down normally by the unconditional
    # finally in installed_standard_streams_from_environment - exactly the
    # state the tee is in by the time agenttalk/__main__.py's top-level
    # fallback would ever run.
    with wrapper_logs.installed_standard_streams_from_environment(
        expected_nonce=nonce,
    ):
        sys.stderr.write("LIVE-WRAPPER-LIFECYCLE-OUTPUT-BEFORE-THE-CRASH\n")

    try:
        raise RuntimeError("TOP-LEVEL-SENTINEL-BOOM")
    except RuntimeError:
        wrapper_logs.print_bounded_uncaught_exception()

    files = _log_glob(tmp_path, "stderr.log*")
    assert all(path.stat().st_size <= 1024 for path in files)
    assert sum(path.stat().st_size for path in files) <= 4096
    tail = "".join(
        path.read_text(encoding="utf-8", errors="replace") for path in files
    )
    assert "TOP-LEVEL-SENTINEL-BOOM" in tail
    assert "Traceback (most recent call last)" in tail
    # Round 21: the crash traceback's own bounded write must not have
    # truncated the lifecycle output the FIRST tee had already written -
    # only a genuinely full ring may evict it via normal rotation.
    assert "LIVE-WRAPPER-LIFECYCLE-OUTPUT-BEFORE-THE-CRASH" in tail
    # The raw file the supervisor redirects to must not receive this second
    # copy directly - only the bounded tail ring does.
    assert "TOP-LEVEL-SENTINEL-BOOM" not in err_path.read_text(encoding="utf-8")


def test_print_bounded_uncaught_exception_prints_normally_when_no_wrapper_log_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wrapper_logs, "_LAST_STDERR_LOG_CONFIG", None)
    captured = io.StringIO()
    monkeypatch.setattr("sys.stderr", captured)

    try:
        raise RuntimeError("MANUAL-RUN-SENTINEL")
    except RuntimeError:
        wrapper_logs.print_bounded_uncaught_exception()

    assert "MANUAL-RUN-SENTINEL" in captured.getvalue()
    assert "Traceback (most recent call last)" in captured.getvalue()


def _install_boundary_test_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nonce: str,
) -> tuple[Path, Path]:
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    err_path.write_text("E" * 1024, encoding="utf-8")
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setenv(wrapper_logs.ENV_MAX_BYTES, "4096")
    monkeypatch.setenv(wrapper_logs.ENV_SEGMENT_COUNT, "4")
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())
    monkeypatch.setattr(wrapper_logs, "_LAST_STDERR_LOG_CONFIG", None)

    def blow_up(_args: argparse.Namespace) -> int:

        sys.stderr.write("x" * 20_000)
        raise RuntimeError("BOUNDARY-SENTINEL-BOOM " + "z" * 20_000)

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", blow_up)
    return out_path, err_path


def test_console_main_bounds_the_top_level_traceback_and_returns_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 20: agenttalk/__main__.py, the installed console script, and
    cli.py's own __main__ guard are three real top-level entry points that
    all need this SAME property - drives console_main() itself (the one
    shared function all three now call), not a hand-replicated copy of
    what any one of them does."""
    nonce = "a" * 32
    _out_path, err_path = _install_boundary_test_env(tmp_path, monkeypatch, nonce)

    result = cli.console_main(
        ["--supervisor-launch-nonce", nonce, "wrap", "--for", "worker"]
    )

    assert result == 1
    files = _log_glob(tmp_path, "stderr.log*")
    assert all(path.stat().st_size <= 1024 for path in files)
    assert sum(path.stat().st_size for path in files) <= 4096


def test_main_still_propagates_original_exception_type_for_embedders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The embedder-safety property console_main was built to preserve:
    a program that imports cli and calls main([...]) directly never goes
    through console_main at all, so it must still see the ORIGINAL
    exception type, uncaught - exactly as if console_main did not exist."""
    nonce = "b" * 32
    _install_boundary_test_env(tmp_path, monkeypatch, nonce)

    try:
        cli.main(["--supervisor-launch-nonce", nonce, "wrap", "--for", "worker"])
    except SystemExit as exc:
        raise AssertionError(
            "an unexpected exception must not be converted to SystemExit"
        ) from exc
    except RuntimeError as exc:
        assert "BOUNDARY-SENTINEL-BOOM" in str(exc)
    else:
        raise AssertionError("cli.main should have raised")
    # Deliberately no sys.excepthook assertion here: console_main never
    # installs one on any path (the design that would have needed one was
    # rejected in round 18), so there is nothing of THIS code's making to
    # check for - and the ambient hook is not this test's to assert on.
    # merely `import pytest` on Python <3.11 replaces sys.excepthook with
    # the `exceptiongroup` backport's handler (confirmed by reproducing:
    # `python -c "import sys; import pytest; print(sys.excepthook)"` shows
    # exceptiongroup_excepthook, before any of this test's code ever runs),
    # which is exactly what made an earlier, now-removed version of this
    # assertion fail on the 3.10 CI legs - a fact about the test
    # environment, not about main()/console_main.


def test_console_main_passes_systemexit_through_unchanged() -> None:
    """SystemExit (argparse's own error exit here) is never main()'s to
    return-ify, and console_main must not touch it either - row 1 of the
    contract table, now checked at the shared function too."""
    with pytest.raises(SystemExit) as excinfo:
        cli.console_main(["not-a-real-subcommand"])
    assert excinfo.value.code == 2


def test_console_main_returns_main_result_unchanged_for_routine_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KeyboardInterrupt/ValueError/OSError already return ints from inside
    main() itself (round 17) - console_main must pass those straight
    through, not just the exception-propagation cases."""

    def raise_keyboard_interrupt(_args: argparse.Namespace) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", raise_keyboard_interrupt)
    assert cli.console_main(["wrap", "--for", "worker"]) == 130


@pytest.mark.parametrize("patch_target", ["build_parser", "parse_args"])
def test_keyboard_interrupt_before_dispatch_still_returns_130(
    monkeypatch: pytest.MonkeyPatch, patch_target: str,
) -> None:
    """Round 24 connector finding: a KeyboardInterrupt raised while
    build_parser() or parser.parse_args() is running happens BEFORE
    main()'s own try ever started, so it used to propagate straight past
    main() (as if main() had no exception handling at all) instead of
    returning 130 like the contract table's KeyboardInterrupt row already
    promises for every OTHER point in main(). Once console_main's broad
    `except BaseException` was added (round 20), that same propagating
    KeyboardInterrupt fell into ITS crash-reporting branch instead,
    misreporting a Ctrl-C as an unexpected crash (return 1) rather than the
    conventional cancellation status. The fix widens main()'s own try to
    cover the whole function body - the SAME contract-table row now covers
    this window too, rather than adding a second, special-cased catch in
    console_main beside it."""

    def raise_it(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    if patch_target == "build_parser":
        monkeypatch.setattr(cli, "build_parser", raise_it)
    else:
        monkeypatch.setattr(argparse.ArgumentParser, "parse_args", raise_it)

    assert cli.main([]) == 130
    assert cli.console_main([]) == 130


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["--root", "R", "wrap", "--for", "worker", "--loop",
             "--", "codex", "exec", "--json"],
            True,
        ),
        (["--root", "R", "wrap", "--help"], False),
        (["--root", "R", "wrap", "-h"], False),
        # --min-interval takes a value; a dangling flag with none is a
        # parse error (argparse exits 2) before cmd_wrap is ever reached.
        (["--root", "R", "wrap", "--min-interval"], False),
        (["--root", "R", "wrap", "--nonexistent-flag"], False),
        (["--root", "R", "status"], False),
        # A --help intended for the WRAPPED cli (after --) is captured by
        # wrap's own REMAINDER positional and never reaches agenttalk's
        # own -h/--help recognition.
        (
            ["--root", "R", "wrap", "--for", "worker", "--", "codex", "--help"],
            True,
        ),
    ],
)
def test_resolves_to_cmd_wrap_matches_whether_argparse_actually_dispatches(
    argv: list[str],
    expected: bool,
) -> None:
    assert cli._resolves_to_cmd_wrap(argv) is expected


def test_internal_check_wrap_dispatch_exit_code_matches_resolution_and_is_silent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main([
        "_internal-check-wrap-dispatch", "--",
        "--root", "R", "wrap", "--for", "worker", "--loop", "--", "codex",
    ]) == 0
    assert cli.main([
        "_internal-check-wrap-dispatch", "--", "--root", "R", "wrap", "--help",
    ]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_cmd_wrap_records_terminating_signal_without_exception_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_path = tmp_path / "stdout.log"
    err_path = tmp_path / "stderr.log"
    nonce = "b" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    def signal_during_setup(_args: argparse.Namespace) -> int:
        installed = signal.getsignal(signal.SIGTERM)
        assert callable(installed)
        installed(signal.SIGTERM, None)
        raise AssertionError("terminating signal handler returned")

    monkeypatch.setattr(cli, "_cmd_wrap_with_logging", signal_during_setup)
    args = argparse.Namespace(agent="worker", supervisor_launch_nonce=nonce)

    with pytest.raises(SystemExit):
        cli.cmd_wrap(args)

    rows = [
        json.loads(line)
        for path in _log_glob(tmp_path, "stderr.log*")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["event"] for row in rows] == ["wrapper_signal_received"]
    assert rows[0]["signal"] == int(signal.SIGTERM)
    assert rows[0]["terminating"] is True


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_stream_environment_hardens_sensitive_log_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generation = tmp_path / "project" / "worker" / "generation"
    generation.mkdir(parents=True, mode=0o755)
    out_path = generation / "stdout.log"
    err_path = generation / "stderr.log"
    out_path.write_text("", encoding="utf-8")
    err_path.write_text("", encoding="utf-8")
    out_path.chmod(0o644)
    err_path.chmod(0o644)
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, "a" * 32)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())

    with wrapper_logs.installed_standard_streams_from_environment(
        expected_nonce="a" * 32,
    ):
        pass

    assert stat.S_IMODE(out_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(err_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(generation.stat().st_mode) == 0o700
    assert stat.S_IMODE(generation.parent.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_bounded_stream_tee_creates_tail_path_restrictively_not_via_later_chmod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 22 (connector finding, security): chmod-AFTER cannot close the
    window between creating a path and tightening it - another local user
    on a shared POSIX account could open a handle inside that window and
    keep it past the chmod. Checking the FINAL mode after everything runs
    would pass even with the old create-then-chmod code (the chmod still
    ran and still corrected it eventually) - that is exactly the test
    shape the connector warned against. This neuters os.chmod entirely
    (monkeypatched to raise, caught by the existing contextlib.suppress)
    AND sets a fully permissive ambient umask, so the ONLY way the mode
    ends up correct is if the creation call itself (the opener passed to
    Path.open, and mkdir's own mode= argument) already requested it -
    proving the fix is atomic-at-creation, not merely fast-to-correct."""
    old_umask = os.umask(0o000)
    monkeypatch.setattr(
        os, "chmod",
        lambda *a, **k: (_ for _ in ()).throw(OSError("chmod disabled for this test")),
    )
    try:
        base = tmp_path / "project" / "worker" / "generation" / "stderr.log"
        original = io.StringIO()
        tee = wrapper_logs.BoundedStreamTee(
            original, base, max_bytes=4096, segment_count=4,
        )
        tee.write("SENTINEL\n")
        tee.close()
    finally:
        os.umask(old_umask)

    tail_files = _log_glob(base.parent, "stderr.log.*")
    assert tail_files
    for path in tail_files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert stat.S_IMODE(base.parent.stat().st_mode) == 0o700


def test_ambient_log_paths_without_matching_launch_nonce_are_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_path = tmp_path / "must-not-open.stdout"
    err_path = tmp_path / "must-not-open.stderr"
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(out_path))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(err_path))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, "a" * 32)
    before_out = io.StringIO()
    before_err = io.StringIO()
    monkeypatch.setattr("sys.stdout", before_out)
    monkeypatch.setattr("sys.stderr", before_err)

    with wrapper_logs.installed_standard_streams_from_environment(
        expected_nonce="b" * 32,
    ):
        import sys

        assert sys.stdout is before_out
        assert sys.stderr is before_err

    assert not out_path.exists()
    assert not err_path.exists()
    lifecycle = wrapper_logs.WrapperLifecycleLog.from_environment(
        "worker",
        expected_nonce="b" * 32,
    )
    assert lifecycle.enabled is False
