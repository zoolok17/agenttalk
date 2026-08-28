"""#55 slice-1 PR-A: scan.lock single-writer lock + provable stale recovery
(DESIGN-55-comprehension-plane.md, "Local storage model").

Every ``acquire_scan_lock`` call below threads a REAL
``PrivacyPreflightResult`` from the ``comprehension_privacy`` fixture,
which itself runs the real preflight against a real git repository (the
``comprehension_privacy_root`` fixture) — reviewer-3's B-1 finding on this
PR (rq-5bd5427ad64d) explicitly forbids a permissive test-only
constructor for this type. ``comprehension_dir`` is the
``root/.agenttalk/comprehension`` directory this all acts on.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agenttalk.comprehension import lock as lockmod
from agenttalk.comprehension import privacy as privacymod
from agenttalk.comprehension.errors import (
    InvalidComprehensionDir,
    PrivacyProofRootMismatch,
    ScanLockContended,
    ScanLockUnrecoverable,
)
from agenttalk.comprehension.privacy import PrivacyPreflightResult, VcsPrivacyRefused
from agenttalk.lifecycle_lock import ProcessIdentity


# ----------------------------------------------------------- acquire / release happy path

def test_acquire_then_release_round_trips(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    handle = lockmod.acquire_scan_lock(
        comprehension_dir, privacy=comprehension_privacy,
        predecessor_index_digest="deadbeef")
    assert handle.path == comprehension_dir / "scan.lock"
    assert handle.path.exists()
    record = json.loads(handle.path.read_text(encoding="utf-8"))
    assert record["state"] == "held"
    assert record["pid"] == os.getpid()
    assert record["predecessor_index_digest"] == "deadbeef"
    assert record["owner_token"] == handle.owner_token
    lockmod.release_scan_lock(handle)
    assert not handle.path.exists()


def test_acquire_records_the_privacy_disposition_into_the_lock(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """Per the design's requirement that the disposition be recorded
    (reviewer-3 B-1 on PR-A, rq-5bd5427ad64d): the vcs_privacy disposition
    must be durable from the first byte written, not only reachable later
    via scan.json."""
    handle = lockmod.acquire_scan_lock(
        comprehension_dir, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    assert handle.vcs_privacy == "ignored"
    assert handle.work_id is None
    record = json.loads(handle.path.read_text(encoding="utf-8"))
    assert record["vcs_privacy"] == "ignored"
    assert record["work_id"] is None
    lockmod.release_scan_lock(handle)


def test_acquire_records_an_acknowledged_disposition_and_its_work_id(
    comprehension_privacy_root: Path, comprehension_dir: Path,
) -> None:
    acknowledged = privacymod.acknowledge_unignored_private_store(
        comprehension_privacy_root, vcs_kind="git", work_id="migrate-checkout")
    handle = lockmod.acquire_scan_lock(
        comprehension_dir, privacy=acknowledged, predecessor_index_digest=None)
    assert handle.vcs_privacy == "acknowledged_unignored"
    assert handle.work_id == "migrate-checkout"
    record = json.loads(handle.path.read_text(encoding="utf-8"))
    assert record["vcs_privacy"] == "acknowledged_unignored"
    assert record["work_id"] == "migrate-checkout"
    lockmod.release_scan_lock(handle)


def test_acquire_with_no_predecessor_index_digest_persists_null(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    handle = lockmod.acquire_scan_lock(
        comprehension_dir, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    record = json.loads(handle.path.read_text(encoding="utf-8"))
    assert record["predecessor_index_digest"] is None
    lockmod.release_scan_lock(handle)


def test_release_lets_a_new_acquire_succeed(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    first = lockmod.acquire_scan_lock(
        comprehension_dir, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    lockmod.release_scan_lock(first)
    second = lockmod.acquire_scan_lock(
        comprehension_dir, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    assert second.owner_token != first.owner_token
    lockmod.release_scan_lock(second)


# ----------------------------------------------------------- live contention

def test_second_acquire_while_first_is_live_is_contended(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """The CURRENT test process is a real, observably-alive process with a
    real process-start identity — a second acquire attempt hits the exact
    live-contention path with no monkeypatching required."""
    first = lockmod.acquire_scan_lock(
        comprehension_dir, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    with pytest.raises(ScanLockContended) as exc_info:
        lockmod.acquire_scan_lock(
            comprehension_dir, privacy=comprehension_privacy,
            predecessor_index_digest=None)
    assert exc_info.value.holder_pid == os.getpid()
    lockmod.release_scan_lock(first)


# ----------------------------------------------------------- stale-lock reclaim (definitely dead)

def test_definitely_dead_holder_is_reclaimed_automatically(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult, monkeypatch,
) -> None:
    stale = lockmod.acquire_scan_lock(
        comprehension_dir, privacy=comprehension_privacy,
        predecessor_index_digest="stale-digest")
    monkeypatch.setattr(lockmod, "process_observation", lambda pid: ("dead", None))
    fresh = lockmod.acquire_scan_lock(
        comprehension_dir, privacy=comprehension_privacy,
        predecessor_index_digest="fresh-digest")
    assert fresh.owner_token != stale.owner_token
    record = json.loads(fresh.path.read_text(encoding="utf-8"))
    assert record["predecessor_index_digest"] == "fresh-digest"
    lockmod.release_scan_lock(fresh)


def test_reclaim_only_happens_once_per_acquire_call(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult, monkeypatch,
) -> None:
    """A dead lock that keeps reappearing (pathological) must not spin
    forever — bounded retries, then a typed refusal."""
    lockmod.acquire_scan_lock(
        comprehension_dir, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    calls = {"n": 0}

    def always_recreates_after_reclaim(path):
        calls["n"] += 1
        record = lockmod._read_lock_record(path)
        os.remove(path)
        # Simulate a competitor immediately recreating a "dead" lock so the
        # exclusive-create keeps losing the race.
        lockmod._write_exclusive(path, record)

    monkeypatch.setattr(lockmod, "process_observation", lambda pid: ("dead", None))
    monkeypatch.setattr(lockmod, "_classify_and_maybe_reclaim", always_recreates_after_reclaim)
    with pytest.raises(ScanLockUnrecoverable, match="repeated reclaim"):
        lockmod.acquire_scan_lock(
            comprehension_dir, privacy=comprehension_privacy,
            predecessor_index_digest=None)
    assert calls["n"] > 1


# ----------------------------------------------------------- stale-lock: unverifiable (PID reuse)

def test_alive_but_identity_mismatch_is_unrecoverable_not_reclaimed(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult, monkeypatch,
) -> None:
    """design: 'PID reuse cannot prove death because the process-start
    identity must also match.'"""
    stale = lockmod.acquire_scan_lock(
        comprehension_dir, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    different_identity = ProcessIdentity(
        scheme=stale.process_identity.scheme, value=stale.process_identity.value + "-reused")
    monkeypatch.setattr(
        lockmod, "process_observation", lambda pid: ("alive", different_identity))
    with pytest.raises(ScanLockUnrecoverable, match="PID reuse"):
        lockmod.acquire_scan_lock(
            comprehension_dir, privacy=comprehension_privacy,
            predecessor_index_digest=None)
    assert stale.path.exists()  # never deleted — reclaim did not happen


# ----------------------------------------------------------- stale-lock: unverifiable (unknown platform)

def test_unknown_liveness_is_unrecoverable(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult, monkeypatch,
) -> None:
    stale = lockmod.acquire_scan_lock(
        comprehension_dir, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    monkeypatch.setattr(lockmod, "process_observation", lambda pid: ("unknown", None))
    with pytest.raises(ScanLockUnrecoverable, match="could not be observed"):
        lockmod.acquire_scan_lock(
            comprehension_dir, privacy=comprehension_privacy,
            predecessor_index_digest=None)
    assert stale.path.exists()


# ----------------------------------------------------------- stale-lock: unverifiable (different host)

def test_different_host_identity_is_unrecoverable_even_if_pid_matches(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult, monkeypatch,
) -> None:
    monkeypatch.setattr(lockmod, "host_identity", lambda: "host-a")
    stale = lockmod.acquire_scan_lock(
        comprehension_dir, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    monkeypatch.setattr(lockmod, "host_identity", lambda: "host-b")
    with pytest.raises(ScanLockUnrecoverable, match="different host"):
        lockmod.acquire_scan_lock(
            comprehension_dir, privacy=comprehension_privacy,
            predecessor_index_digest=None)
    assert stale.path.exists()


# ----------------------------------------------------------- malformed lock record

@pytest.mark.parametrize("raw", [
    "not json at all",
    "{}",
    '{"schema_version": 1, "state": "held"}',
    '{"schema_version": 99, "state": "held", "owner_token": "x", "pid": 1, '
    '"process_identity": {"scheme": "a", "value": "b"}, "host_identity": "h", '
    '"acquired_at": "2026-01-01T00:00:00Z", "predecessor_index_digest": null, '
    '"vcs_privacy": "ignored", "work_id": null}',
])
def test_malformed_lock_record_is_unrecoverable(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult, raw: str,
) -> None:
    lock_path = comprehension_dir / "scan.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(raw, encoding="utf-8")
    with pytest.raises(ScanLockUnrecoverable, match="malformed"):
        lockmod.acquire_scan_lock(
            comprehension_dir, privacy=comprehension_privacy,
            predecessor_index_digest=None)


# ----------------------------------------------------------- release() ownership check

def test_release_refuses_if_owner_token_no_longer_matches(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    handle = lockmod.acquire_scan_lock(
        comprehension_dir, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    record = json.loads(handle.path.read_text(encoding="utf-8"))
    record["owner_token"] = "someone-else"
    handle.path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ScanLockUnrecoverable, match="owner_token"):
        lockmod.release_scan_lock(handle)


def test_release_refuses_if_lock_file_is_gone(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    handle = lockmod.acquire_scan_lock(
        comprehension_dir, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    os.remove(handle.path)
    with pytest.raises(ScanLockUnrecoverable):
        lockmod.release_scan_lock(handle)


# ----------------------------------------------------------- recover_stale_lock (attended-only)

def test_recover_stale_lock_clears_an_existing_lock_unconditionally(
    comprehension_dir: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """recover_stale_lock performs NO liveness check — it is the attended
    override, called only after a human has already confirmed the prior
    scan is gone (design: the CLI flag requires attendance; this function
    is what it calls once attendance is proven)."""
    stale = lockmod.acquire_scan_lock(
        comprehension_dir, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    assert stale.path.exists()
    lockmod.recover_stale_lock(comprehension_dir)
    assert not stale.path.exists()
    fresh = lockmod.acquire_scan_lock(
        comprehension_dir, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    lockmod.release_scan_lock(fresh)


def test_recover_stale_lock_on_an_absent_lock_is_a_silent_no_op(tmp_path: Path) -> None:
    lockmod.recover_stale_lock(tmp_path)  # must not raise
    assert not (tmp_path / "scan.lock").exists()


# ----------------------------------------------------------- host_identity()

def test_host_identity_returns_a_non_empty_string() -> None:
    assert isinstance(lockmod.host_identity(), str)
    assert lockmod.host_identity()


# ----------------------------------------------------------- B-1 regression: privacy precondition

def test_acquire_scan_lock_requires_a_privacy_result(tmp_path: Path) -> None:
    """reviewer-3 B-1 (rq-5bd5427ad64d), reproduced as a permanent
    regression test: omitting ``privacy`` entirely must be a TypeError,
    raised before any filesystem write."""
    with pytest.raises(TypeError):
        lockmod.acquire_scan_lock(tmp_path, predecessor_index_digest=None)  # type: ignore[call-arg]
    assert not tmp_path.exists() or not any(tmp_path.iterdir())


def test_acquire_scan_lock_rejects_a_non_privacy_result_object(tmp_path: Path) -> None:
    """A wrong-typed ``privacy`` argument must also be a TypeError, not
    merely a missing one — closes the loophole of passing e.g. a bare
    string or dict that happens to be truthy."""
    with pytest.raises(TypeError):
        lockmod.acquire_scan_lock(
            tmp_path, privacy="ignored", predecessor_index_digest=None)  # type: ignore[arg-type]
    assert not tmp_path.exists() or not any(tmp_path.iterdir())


def test_refused_preflight_leaves_zero_bytes_under_comprehension_dir(tmp_path: Path) -> None:
    """Reproduces reviewer-3's exact bypass probe: in a git repo where the
    preflight REFUSES, there is no way to obtain a ``PrivacyPreflightResult``
    to pass to ``acquire_scan_lock`` at all, so the write path is
    structurally unreachable and nothing is ever written."""
    import subprocess
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)  # noqa: S603,S607  # nosec B603 B607
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=False)  # noqa: S603,S607  # nosec B603 B607
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "base", "--allow-empty"], check=True)
    with pytest.raises(VcsPrivacyRefused):
        result = privacymod.run_privacy_preflight(tmp_path)
        lockmod.acquire_scan_lock(  # unreachable: `result` never gets assigned
            tmp_path, privacy=result, predecessor_index_digest=None)
    assert not (tmp_path / ".agenttalk").exists()


# ----------------------------------------------------------- finding 1 regression: cross-root proof reuse

def test_a_proof_from_one_root_cannot_unlock_a_different_root(
    comprehension_privacy_root: Path, comprehension_privacy: PrivacyPreflightResult,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """reviewer-1 cold-read finding 1 on PR-A (rq-6cc5560b62f6), reproduced
    as a permanent regression test: a REAL, proven ``PrivacyPreflightResult``
    from protected root A must never unlock ``acquire_scan_lock`` at an
    unrelated root B, even though both are real git repos where the
    preflight genuinely proves ``ignored``."""
    other_root = tmp_path_factory.mktemp("other-root")
    import subprocess
    subprocess.run(["git", "-C", str(other_root), "init", "-q"], check=True)  # noqa: S603,S607  # nosec B603 B607
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(other_root), "config", "user.email", "t@t"], check=True)
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["git", "-C", str(other_root), "config", "user.name", "t"], check=True)
    (other_root / ".gitignore").write_text(".agenttalk/\n", encoding="utf-8")
    other_comprehension_dir = other_root / ".agenttalk" / "comprehension"

    with pytest.raises(PrivacyProofRootMismatch):
        lockmod.acquire_scan_lock(
            other_comprehension_dir, privacy=comprehension_privacy,
            predecessor_index_digest=None)
    assert not other_comprehension_dir.exists()


def test_acknowledge_proof_is_also_bound_to_its_own_root(
    comprehension_dir: Path, tmp_path_factory: pytest.TempPathFactory,
) -> None:
    other_root = tmp_path_factory.mktemp("other-root")
    acknowledged = privacymod.acknowledge_unignored_private_store(
        other_root, vcs_kind="none", work_id="w1")
    with pytest.raises(PrivacyProofRootMismatch):
        lockmod.acquire_scan_lock(
            comprehension_dir, privacy=acknowledged, predecessor_index_digest=None)


# ------------------------------------- finding 1 round 2: comprehension_dir shape must be exact

def test_a_wrongly_shaped_comprehension_dir_is_rejected_even_at_a_proven_root(
    comprehension_privacy_root: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """reviewer-1 cold-read finding 1 on PR-A, round 2 (rq-6cc5560b62f6),
    reproduced: naively deriving the project root as "two parents up" let
    ``acquire_scan_lock(root / "unignored" / "store", ...)`` recover the
    SAME real, proven root and pass the root-binding check, while writing
    scan.lock OUTSIDE ``.agenttalk`` entirely. A ``comprehension_dir`` that
    is not exactly ``<root>/.agenttalk/comprehension`` must be refused
    before any filesystem access, even when the privacy proof it derives a
    root from is completely genuine."""
    wrong_shape_dir = comprehension_privacy_root / "unignored" / "store"
    with pytest.raises(InvalidComprehensionDir):
        lockmod.acquire_scan_lock(
            wrong_shape_dir, privacy=comprehension_privacy, predecessor_index_digest=None)
    assert not wrong_shape_dir.exists()
    assert not (comprehension_privacy_root / "unignored").exists()


# ----------------------------------------------------------- finding 3 regression: no socket import

def test_comprehension_package_imports_no_socket_or_network_module() -> None:
    """reviewer-1 cold-read finding 3 on PR-A (rq-6cc5560b62f6): the
    design's offline contract (lines ~800-813) prohibits network-capable
    imports anywhere in ``agenttalk.comprehension`` — a static per-file
    import scan, closing the CHANNEL (the whole package), not merely the
    one ``lock.py`` instance the reviewer reproduced against.

    ``platform`` is banned here too (PR-B item 2, lead's follow-on after
    the item-2 checkpoint): empirically confirmed TWICE now — once fixing
    ``lock.host_identity()``, once independently in
    ``discovery.detect_platform_identity()`` — that
    ``platform.node()``/``platform.machine()``/``platform.system()`` ALL
    transitively import and use ``socket`` on Windows (CPython's
    ``platform.uname()`` builds the whole tuple, node/hostname included,
    as one cached unit even when only one other field is read). Banning
    the whole module here closes the CLASS so a third call site can never
    reopen it silently; this package's socket-free platform/architecture
    detection lives in ``ctypes``/``os.uname()`` instead.
    """
    import ast
    import importlib
    import pathlib

    banned = {
        "socket", "ssl", "http", "http.client", "urllib", "urllib.request",
        "urllib3", "requests", "ftplib", "smtplib", "telnetlib", "asyncio",
        "socketserver", "xmlrpc", "platform",
    }
    package = importlib.import_module("agenttalk.comprehension")
    package_dir = pathlib.Path(package.__file__).parent
    offenders = []
    for source_path in package_dir.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module] if node.module else []
            elif isinstance(node, ast.Call):
                # N9 (fourth cold read, fix round 6): the static Import/
                # ImportFrom scan above is blind to the DYNAMIC import
                # spellings - importlib.import_module("socket") and
                # __import__("socket") are ordinary function calls, not
                # import statements at all, and would sail straight
                # through the check above even though they achieve
                # exactly what it exists to ban. Only a literal string
                # argument can be checked statically; anything else
                # (a computed module name) is a residual this static
                # scan cannot see either way, same as any static analysis.
                is_import_module_call = (
                    isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"
                )
                is_dunder_import_call = (
                    isinstance(node.func, ast.Name) and node.func.id == "__import__"
                )
                if not (is_import_module_call or is_dunder_import_call) or not node.args:
                    continue
                first_arg = node.args[0]
                names = (
                    [first_arg.value]
                    if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str)
                    else []
                )
            else:
                continue
            for name in names:
                if name is not None and (name in banned or name.split(".")[0] in banned):
                    offenders.append(f"{source_path.relative_to(package_dir)}: {name}")
    assert offenders == []


def test_host_identity_never_transitively_imports_socket_at_runtime() -> None:
    """reviewer-1 cold-read finding 3 on PR-A, round 2 (rq-6cc5560b62f6),
    reproduced: the STATIC import scan above only sees THIS package's own
    ``import`` statements — it cannot see a stdlib helper (the old
    ``platform.node()`` call) transitively importing ``socket`` at
    runtime. Observed ``socket_before=False`` -> ``socket_after=True`` in a
    fresh process at the prior SHA. Runs in a genuinely fresh subprocess
    (never the test runner's own interpreter, which may have already
    imported ``socket`` via unrelated pytest/OS plumbing) and asserts
    ``socket`` is absent from ``sys.modules`` both before AND after calling
    ``host_identity()``.
    """
    import subprocess
    import sys

    probe = (
        "import sys, json\n"
        "before = 'socket' in sys.modules\n"
        "from agenttalk.comprehension.lock import host_identity\n"
        "host_identity()\n"
        "after = 'socket' in sys.modules\n"
        "print(json.dumps({'before': before, 'after': after}))\n"
    )
    result = subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    import json
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed == {"before": False, "after": False}, (
        f"host_identity() transitively imported socket: {observed}\nstderr: {result.stderr}")


def test_host_identity_succeeds_under_the_dev_gates_allowlisted_environment(
    tmp_path: Path,
) -> None:
    """reviewer-1 cold-read finding 3 on PR-A, round 3 (rq-6cc5560b62f6):
    ``host_identity()`` reading ``os.environ.get("COMPUTERNAME")`` as its
    ONLY Windows source broke unconditionally under ``dev_gate.py``'s own
    allowlisted subprocess environment — the gate's ``_base_env`` never
    forwards ``COMPUTERNAME``/``HOSTNAME`` to the pytest child process it
    spawns for either the source or wheel leg, so every comprehension test
    calling ``acquire_scan_lock``/``create_staging_dir`` failed on Windows
    CI. This is the #76 "close the channel, not the instance" gap: the
    comprehension suite had never once run under the gate's own stripped
    environment on a dev host, so nothing caught it locally before CI did.

    Reuses ``dev_gate._base_env`` DIRECTLY (never a hand-rolled
    approximation of it) so this test can never drift out of sync with the
    real gate's environment contract — if the allowlist ever changes,
    this test inherits that change automatically. Explicitly pops
    ``COMPUTERNAME``/``HOSTNAME`` in addition (defense in depth: they are
    not currently in the allowlist, but if a future change added one back,
    this test would still exercise the true worst case).
    """
    import subprocess
    import sys

    from agenttalk.dev_gate import _base_env

    env = _base_env(tmp_path)
    env.pop("COMPUTERNAME", None)
    env.pop("HOSTNAME", None)
    # The gate's real "source" leg sets PYTHONPATH itself (source_environment);
    # here we point it at whatever import root THIS test process's own
    # agenttalk.comprehension.lock actually resolved from, so the probe
    # exercises the exact code under test rather than a stale installed copy.
    env["PYTHONPATH"] = str(Path(lockmod.__file__).resolve().parents[2])
    probe = "from agenttalk.comprehension.lock import host_identity\nprint(host_identity())\n"
    result = subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, "-c", probe], capture_output=True, text=True, env=env)
    assert result.returncode == 0, (
        "host_identity() failed under the dev-gate's allowlisted environment "
        f"(COMPUTERNAME/HOSTNAME both absent): {result.stderr}")
    assert result.stdout.strip()
