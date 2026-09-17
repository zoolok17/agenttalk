"""Tests for #156 increment 1: `agenttalk backup` / `agenttalk.recovery`.

Covers exactly what reviewer-3's GO-WITH-CHANGES named as required: a
concurrent-send-during-backup load test proving the fence stays brief (the
#154 regression guard), a manifest/identity test, and a hardlink-fallback
test. Also covers the self-check (manifest vs. actual bytes) and the
sequence corroboration, since both code paths already exist and are real
guarantees worth pinning.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from agenttalk import recovery
from agenttalk.store import Store


def _store(root: Path, *, n_seed_messages: int = 0) -> Store:
    s = Store(root)
    s.init(["alpha", "beta"])
    for i in range(n_seed_messages):
        s.send(sender="alpha", recipient="beta", body=f"seed-{i}")
    return s


# --------------------------------------------------------- manifest/identity

def test_backup_manifest_matches_actual_snapshot_bytes(tmp_path: Path) -> None:
    store_root = tmp_path / "project"
    dest_root = tmp_path / "recovery"
    s = _store(store_root, n_seed_messages=3)

    result = recovery.create_backup(s, dest_root=dest_root)

    manifest = json.loads((result.destination / "manifest.json").read_text())
    assert manifest["project_id"] == result.project_id
    assert manifest["manifest_hash"] == result.manifest_hash
    assert manifest["file_count"] == result.file_count == len(result.files)
    assert set(manifest["files"]) == set(result.files)
    # Every hash in the manifest matches the actual byte content that landed
    # in the snapshot directory - not just an internally-consistent lie.
    for rel, expected_hash in manifest["files"].items():
        actual = recovery._hash_file(result.destination / rel)
        assert actual == expected_hash, rel
    # Lock/guard artifacts must never appear in the snapshot at all (#156:
    # hardlinking or copying them risks aliasing a live lock's inode).
    assert not any(recovery._is_lock_artifact(Path(f).name) for f in manifest["files"])


def test_backup_project_id_is_stable_across_delete_and_recreate(tmp_path: Path) -> None:
    # The whole point of path-derived identity (#156's design comment):
    # deleting and recreating the store at the SAME path must recompute the
    # identical project_id, so a later backup still lands in the same
    # recovery/<project_id>/ directory as an earlier one.
    store_root = tmp_path / "project"
    dest_root = tmp_path / "recovery"
    s1 = _store(store_root)
    first = recovery.create_backup(s1, dest_root=dest_root)

    import shutil
    shutil.rmtree(store_root)
    s2 = _store(store_root)
    second = recovery.create_backup(s2, dest_root=dest_root)

    assert first.project_id == second.project_id
    assert first.destination.parent == second.destination.parent
    assert first.destination != second.destination  # distinct timestamped backups


def test_backup_sequence_at_snapshot_matches_publication_order(tmp_path: Path) -> None:
    store_root = tmp_path / "project"
    dest_root = tmp_path / "recovery"
    s = _store(store_root, n_seed_messages=5)

    result = recovery.create_backup(s, dest_root=dest_root)

    order = s._read_message_publication_order()
    assert result.sequence_at_snapshot == order["append_sequence"] == 5


def test_backup_refuses_uninitialized_store(tmp_path: Path) -> None:
    s = Store(tmp_path / "never-initialized")
    with pytest.raises(FileNotFoundError):
        recovery.create_backup(s, dest_root=tmp_path / "recovery")


def test_backup_excludes_every_lock_artifact_the_store_can_create(tmp_path: Path) -> None:
    """reviewer-3's PR #177 finding: a hand-maintained exclusion list missed
    several `_exclusive_lock`-class marker files (operation-publication.lock,
    the per-agent waiting/awaiting/lead-loop-lease locks - one of which,
    `lead-loop-lease.lock`, wasn't even in the reviewer's own list). Covers
    both shapes a lock leaves on disk: (a) every hidden `.generation` guard,
    produced by a normal clean acquire-then-release of every lock the real
    store API exposes (`_exclusive_lock`'s own `finally` unlinks the BARE
    marker on a clean release - the guard is the only thing that persists);
    (b) a BARE marker left behind the way a crashed holder leaves one -
    `_exclusive_lock`'s own docstring documents this as "ownerless legacy
    crash remnants" - reproduced the same way reviewer-3's own executed
    proof did (writing the file directly, since forcing a real mid-acquire
    crash isn't practical in a unit test). Then backs up and asserts, for
    every one of these files: it still shows hardlink count 1 on the LIVE
    store afterward (the actual failure mode - a hardlinked marker breaks
    every future acquisition of that lock), and its name never appears in
    the manifest. Exercises `store.is_lock_or_guard_artifact` transitively
    through `recovery._is_lock_artifact` - must go red if recovery.py ever
    reverts to its own separate hand list instead of that shared
    predicate (verified manually: reverting to the pre-fix six-name list
    makes this test fail on the missed bare names below)."""
    store_root = tmp_path / "project"
    dest_root = tmp_path / "recovery"
    s = _store(store_root)

    # (a) Clean acquire/release of every lock the real store API exposes -
    # leaves every .generation guard behind.
    with s.config_lock():
        pass
    with s.coverage_transaction_lock():
        pass
    with s.coverage_handoff_lock():
        pass
    with s._supervisor_lifecycle_lock():
        pass
    with s._powershell_selection_lock():
        pass
    with s._waiting_lock("alpha"):
        pass
    with s._awaiting_lock("alpha"):
        pass
    with s._lead_loop_lease_lock("alpha"):
        pass
    with s._exclusive_lock(s.state_dir / "operation-publication.lock",
                           what="wrapper operation publication"):
        pass
    with s._retirement_lock():
        pass
    with s._message_publication_lock():
        pass

    # (b) Simulate a crashed holder for every BARE marker name that a
    # clean release removes - this is the actual shape reviewer-3's
    # executed proof used, and the actual gap the old hand list had.
    bare_marker_paths = [
        s.dir / "config.lock",
        s.dir / "supervisor-lifecycle.lock",
        s.dir / "powershell-host.lock",
        s.dir / "assurance" / "coverage.lock",
        s.dir / "assurance" / "coverage-handoff.lock",
        s.state_dir / "operation-publication.lock",
        s.state_dir / "alpha.waiting.lock",
        s.awaiting_dir / "alpha.lock",
        s.state_dir / "alpha.lead-loop-lease.lock",
    ]
    for p in bare_marker_paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"pid": 999999, "protocol": "o_excl_v2", '
                     '"generation": "dead", "at": "2020-01-01T00:00:00Z"}',
                     encoding="utf-8")

    # Enumerate the files we EXPECT to be lock artifacts independently of
    # `recovery._is_lock_artifact` (the function under test) - every
    # ".generation" guard on disk (a name shape store.py itself reserves
    # for exactly this, not something recovery.py invented) plus the bare
    # markers this test wrote directly. A circular enumeration (asking the
    # function under test what IT thinks counts) is exactly how the
    # original version of this test stayed green despite the gap reviewer-3
    # found - it never catches a name the predicate itself is missing.
    generation_guards = [p for p in store_root.rglob(".*.generation") if p.is_file()]
    expected_lock_paths = generation_guards + bare_marker_paths
    assert len(generation_guards) >= 11, sorted(p.name for p in generation_guards)
    for p in expected_lock_paths:
        assert p.is_file(), p  # every path this test itself created

    # The function under test must classify every one of them as a lock
    # artifact - this is the assertion that actually fails under the old
    # hand list, for the bare marker names it never enumerated.
    misclassified = [p.name for p in expected_lock_paths if not recovery._is_lock_artifact(p.name)]
    assert misclassified == [], (
        f"recovery._is_lock_artifact fails to recognize: {misclassified} - "
        f"these would be hardlinked/copied into a backup and alias a live lock"
    )

    result = recovery.create_backup(s, dest_root=dest_root)

    for p in expected_lock_paths:
        assert p.stat().st_nlink == 1, (
            f"{p.name}: hardlink count changed - a live acquisition of this "
            f"lock would now fail with 'unsafe lock path'"
        )
    manifest_names = {Path(rel).name for rel in result.files}
    aliased = manifest_names & {p.name for p in expected_lock_paths}
    assert aliased == set(), f"lock artifact(s) leaked into the backup: {aliased}"


# ------------------------------------------------------------------ self-check

def test_backup_self_check_catches_a_tampered_staged_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_root = tmp_path / "project"
    dest_root = tmp_path / "recovery"
    s = _store(store_root, n_seed_messages=2)

    real_hash_file = recovery._hash_file
    seen: set[Path] = set()

    def flaky_hash_file(path: Path) -> str:
        # First time each path is hashed = the manifest's recorded value;
        # the SECOND time the SAME path is hashed = the self-check re-read.
        # Corrupt only that second read so the guard has something real to
        # catch, regardless of how many files a tiny store happens to have.
        if path in seen:
            return "0" * 64
        seen.add(path)
        return real_hash_file(path)

    monkeypatch.setattr(recovery, "_hash_file", flaky_hash_file)

    with pytest.raises(ValueError, match="self-check failed"):
        recovery.create_backup(s, dest_root=dest_root)

    # No half-published backup: only .tmp-* staging dirs (if anything) may
    # remain, no directory that looks like a completed, timestamped backup.
    published = [
        p for p in dest_root.rglob("*")
        if p.is_dir() and not p.name.startswith(".tmp-") and p.parent != dest_root
    ]
    assert published == []


# --------------------------------------------------------------- hardlink fallback

def test_backup_falls_back_to_copy_when_hardlinks_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_root = tmp_path / "project"
    dest_root = tmp_path / "recovery"
    s = _store(store_root, n_seed_messages=3)

    monkeypatch.setattr(recovery, "_probe_hardlink_support", lambda *_a, **_kw: False)

    result = recovery.create_backup(s, dest_root=dest_root)

    assert result.hardlink_used is False
    assert result.file_count > 0
    for rel, expected_hash in result.files.items():
        assert recovery._hash_file(result.destination / rel) == expected_hash
    # Copied files are independent inodes - mutating the live store file
    # must never change the snapshot's already-hashed content.
    live_file = next(iter(s.messages_dir.glob("*.json")))
    original = live_file.read_text(encoding="utf-8")
    live_file.write_text(original + "\n// tampered\n", encoding="utf-8")
    for rel, expected_hash in result.files.items():
        assert recovery._hash_file(result.destination / rel) == expected_hash


# ------------------------------------------------ concurrent-send load test (#154)

def test_backup_fence_does_not_cause_send_timeouts_under_concurrent_load(
    tmp_path: Path,
) -> None:
    """The actual regression guard for the #154 disagreement: a brief fence
    around a hardlink clone (bounded by file count) must never make a
    concurrent `send()` time out waiting for the SAME generation guard -
    unlike an O(store size) fence, which #154 already proved does exactly
    that in production (12 documented wrapper deaths). Seeds a few hundred
    messages so the clone has real work to do, then hammers concurrent
    sends from multiple threads while a backup runs, and asserts zero
    TimeoutErrors anywhere."""
    store_root = tmp_path / "project"
    dest_root = tmp_path / "recovery"
    s = _store(store_root, n_seed_messages=300)

    send_errors: list[Exception] = []
    backup_errors: list[Exception] = []
    stop = threading.Event()

    def sender(w: int) -> None:
        i = 0
        while not stop.is_set():
            try:
                s.send(sender="alpha", recipient="beta", body=f"w{w}-m{i}")
            except Exception as e:  # noqa: BLE001 - collected, asserted below
                send_errors.append(e)
                return
            i += 1

    def backer_upper() -> None:
        try:
            recovery.create_backup(s, dest_root=dest_root)
        except Exception as e:  # noqa: BLE001 - collected, asserted below
            backup_errors.append(e)

    senders = [threading.Thread(target=sender, args=(w,)) for w in range(6)]
    for t in senders:
        t.start()
    time.sleep(0.05)  # let senders get into their tight loop first
    backup_thread = threading.Thread(target=backer_upper)
    backup_thread.start()
    backup_thread.join(timeout=30)
    assert not backup_thread.is_alive(), "backup did not finish within 30s"
    stop.set()
    for t in senders:
        t.join(timeout=10)

    assert backup_errors == []
    timeout_errors = [e for e in send_errors if isinstance(e, TimeoutError)]
    assert timeout_errors == [], (
        f"{len(timeout_errors)} send() TimeoutError(s) attributable to the "
        f"backup fence - this is the exact #154 regression shape: {timeout_errors[:3]}"
    )
    assert send_errors == [], send_errors
