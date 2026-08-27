"""#55 slice-1 PR-A: scan.lock single-writer lock + provable stale recovery
(DESIGN-55-comprehension-plane.md, "Local storage model").

Every ``acquire_scan_lock`` call below threads a REAL
``PrivacyPreflightResult`` from the ``comprehension_privacy`` fixture,
which itself runs the real preflight against a real git repository (the
``comprehension_privacy_root`` fixture) — reviewer-3's B-1 finding on this
PR (rq-5bd5427ad64d) explicitly forbids a permissive test-only
constructor for this type.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agenttalk.comprehension import lock as lockmod
from agenttalk.comprehension import privacy as privacymod
from agenttalk.comprehension.errors import ScanLockContended, ScanLockUnrecoverable
from agenttalk.comprehension.privacy import PrivacyPreflightResult, VcsPrivacyRefused
from agenttalk.lifecycle_lock import ProcessIdentity


# ----------------------------------------------------------- acquire / release happy path

def test_acquire_then_release_round_trips(
    comprehension_privacy_root: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    handle = lockmod.acquire_scan_lock(
        comprehension_privacy_root, privacy=comprehension_privacy,
        predecessor_index_digest="deadbeef")
    assert handle.path == comprehension_privacy_root / "scan.lock"
    assert handle.path.exists()
    record = json.loads(handle.path.read_text(encoding="utf-8"))
    assert record["state"] == "held"
    assert record["pid"] == os.getpid()
    assert record["predecessor_index_digest"] == "deadbeef"
    assert record["owner_token"] == handle.owner_token
    lockmod.release_scan_lock(handle)
    assert not handle.path.exists()


def test_acquire_records_the_privacy_disposition_into_the_lock(
    comprehension_privacy_root: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """Per the design's requirement that the disposition be recorded
    (reviewer-3 B-1 on PR-A, rq-5bd5427ad64d): the vcs_privacy disposition
    must be durable from the first byte written, not only reachable later
    via scan.json."""
    handle = lockmod.acquire_scan_lock(
        comprehension_privacy_root, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    assert handle.vcs_privacy == "ignored"
    assert handle.work_id is None
    record = json.loads(handle.path.read_text(encoding="utf-8"))
    assert record["vcs_privacy"] == "ignored"
    assert record["work_id"] is None
    lockmod.release_scan_lock(handle)


def test_acquire_records_an_acknowledged_disposition_and_its_work_id(
    comprehension_privacy_root: Path,
) -> None:
    acknowledged = privacymod.acknowledge_unignored_private_store(
        vcs_kind="git", work_id="migrate-checkout")
    handle = lockmod.acquire_scan_lock(
        comprehension_privacy_root, privacy=acknowledged, predecessor_index_digest=None)
    assert handle.vcs_privacy == "acknowledged_unignored"
    assert handle.work_id == "migrate-checkout"
    record = json.loads(handle.path.read_text(encoding="utf-8"))
    assert record["vcs_privacy"] == "acknowledged_unignored"
    assert record["work_id"] == "migrate-checkout"
    lockmod.release_scan_lock(handle)


def test_acquire_with_no_predecessor_index_digest_persists_null(
    comprehension_privacy_root: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    handle = lockmod.acquire_scan_lock(
        comprehension_privacy_root, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    record = json.loads(handle.path.read_text(encoding="utf-8"))
    assert record["predecessor_index_digest"] is None
    lockmod.release_scan_lock(handle)


def test_release_lets_a_new_acquire_succeed(
    comprehension_privacy_root: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    first = lockmod.acquire_scan_lock(
        comprehension_privacy_root, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    lockmod.release_scan_lock(first)
    second = lockmod.acquire_scan_lock(
        comprehension_privacy_root, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    assert second.owner_token != first.owner_token
    lockmod.release_scan_lock(second)


# ----------------------------------------------------------- live contention

def test_second_acquire_while_first_is_live_is_contended(
    comprehension_privacy_root: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """The CURRENT test process is a real, observably-alive process with a
    real process-start identity — a second acquire attempt hits the exact
    live-contention path with no monkeypatching required."""
    first = lockmod.acquire_scan_lock(
        comprehension_privacy_root, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    with pytest.raises(ScanLockContended) as exc_info:
        lockmod.acquire_scan_lock(
            comprehension_privacy_root, privacy=comprehension_privacy,
            predecessor_index_digest=None)
    assert exc_info.value.holder_pid == os.getpid()
    lockmod.release_scan_lock(first)


# ----------------------------------------------------------- stale-lock reclaim (definitely dead)

def test_definitely_dead_holder_is_reclaimed_automatically(
    comprehension_privacy_root: Path, comprehension_privacy: PrivacyPreflightResult, monkeypatch,
) -> None:
    stale = lockmod.acquire_scan_lock(
        comprehension_privacy_root, privacy=comprehension_privacy,
        predecessor_index_digest="stale-digest")
    monkeypatch.setattr(lockmod, "process_observation", lambda pid: ("dead", None))
    fresh = lockmod.acquire_scan_lock(
        comprehension_privacy_root, privacy=comprehension_privacy,
        predecessor_index_digest="fresh-digest")
    assert fresh.owner_token != stale.owner_token
    record = json.loads(fresh.path.read_text(encoding="utf-8"))
    assert record["predecessor_index_digest"] == "fresh-digest"
    lockmod.release_scan_lock(fresh)


def test_reclaim_only_happens_once_per_acquire_call(
    comprehension_privacy_root: Path, comprehension_privacy: PrivacyPreflightResult, monkeypatch,
) -> None:
    """A dead lock that keeps reappearing (pathological) must not spin
    forever — bounded retries, then a typed refusal."""
    lockmod.acquire_scan_lock(
        comprehension_privacy_root, privacy=comprehension_privacy,
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
            comprehension_privacy_root, privacy=comprehension_privacy,
            predecessor_index_digest=None)
    assert calls["n"] > 1


# ----------------------------------------------------------- stale-lock: unverifiable (PID reuse)

def test_alive_but_identity_mismatch_is_unrecoverable_not_reclaimed(
    comprehension_privacy_root: Path, comprehension_privacy: PrivacyPreflightResult, monkeypatch,
) -> None:
    """design: 'PID reuse cannot prove death because the process-start
    identity must also match.'"""
    stale = lockmod.acquire_scan_lock(
        comprehension_privacy_root, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    different_identity = ProcessIdentity(
        scheme=stale.process_identity.scheme, value=stale.process_identity.value + "-reused")
    monkeypatch.setattr(
        lockmod, "process_observation", lambda pid: ("alive", different_identity))
    with pytest.raises(ScanLockUnrecoverable, match="PID reuse"):
        lockmod.acquire_scan_lock(
            comprehension_privacy_root, privacy=comprehension_privacy,
            predecessor_index_digest=None)
    assert stale.path.exists()  # never deleted — reclaim did not happen


# ----------------------------------------------------------- stale-lock: unverifiable (unknown platform)

def test_unknown_liveness_is_unrecoverable(
    comprehension_privacy_root: Path, comprehension_privacy: PrivacyPreflightResult, monkeypatch,
) -> None:
    stale = lockmod.acquire_scan_lock(
        comprehension_privacy_root, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    monkeypatch.setattr(lockmod, "process_observation", lambda pid: ("unknown", None))
    with pytest.raises(ScanLockUnrecoverable, match="could not be observed"):
        lockmod.acquire_scan_lock(
            comprehension_privacy_root, privacy=comprehension_privacy,
            predecessor_index_digest=None)
    assert stale.path.exists()


# ----------------------------------------------------------- stale-lock: unverifiable (different host)

def test_different_host_identity_is_unrecoverable_even_if_pid_matches(
    comprehension_privacy_root: Path, comprehension_privacy: PrivacyPreflightResult, monkeypatch,
) -> None:
    monkeypatch.setattr(lockmod, "host_identity", lambda: "host-a")
    stale = lockmod.acquire_scan_lock(
        comprehension_privacy_root, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    monkeypatch.setattr(lockmod, "host_identity", lambda: "host-b")
    with pytest.raises(ScanLockUnrecoverable, match="different host"):
        lockmod.acquire_scan_lock(
            comprehension_privacy_root, privacy=comprehension_privacy,
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
    comprehension_privacy_root: Path, comprehension_privacy: PrivacyPreflightResult, raw: str,
) -> None:
    lock_path = comprehension_privacy_root / "scan.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(raw, encoding="utf-8")
    with pytest.raises(ScanLockUnrecoverable, match="malformed"):
        lockmod.acquire_scan_lock(
            comprehension_privacy_root, privacy=comprehension_privacy,
            predecessor_index_digest=None)


# ----------------------------------------------------------- release() ownership check

def test_release_refuses_if_owner_token_no_longer_matches(
    comprehension_privacy_root: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    handle = lockmod.acquire_scan_lock(
        comprehension_privacy_root, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    record = json.loads(handle.path.read_text(encoding="utf-8"))
    record["owner_token"] = "someone-else"
    handle.path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ScanLockUnrecoverable, match="owner_token"):
        lockmod.release_scan_lock(handle)


def test_release_refuses_if_lock_file_is_gone(
    comprehension_privacy_root: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    handle = lockmod.acquire_scan_lock(
        comprehension_privacy_root, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    os.remove(handle.path)
    with pytest.raises(ScanLockUnrecoverable):
        lockmod.release_scan_lock(handle)


# ----------------------------------------------------------- recover_stale_lock (attended-only)

def test_recover_stale_lock_clears_an_existing_lock_unconditionally(
    comprehension_privacy_root: Path, comprehension_privacy: PrivacyPreflightResult,
) -> None:
    """recover_stale_lock performs NO liveness check — it is the attended
    override, called only after a human has already confirmed the prior
    scan is gone (design: the CLI flag requires attendance; this function
    is what it calls once attendance is proven)."""
    stale = lockmod.acquire_scan_lock(
        comprehension_privacy_root, privacy=comprehension_privacy,
        predecessor_index_digest=None)
    assert stale.path.exists()
    lockmod.recover_stale_lock(comprehension_privacy_root)
    assert not stale.path.exists()
    fresh = lockmod.acquire_scan_lock(
        comprehension_privacy_root, privacy=comprehension_privacy,
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
