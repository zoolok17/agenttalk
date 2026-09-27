"""All compacted evidence is discovered without replaying it into active state."""
import json
from pathlib import Path

import pytest

from agenttalk.envelope_snapshot import SnapshotService, selected_closure
from agenttalk.store import Store


def test_empty_roster_and_partitions_keep_existing_state_refusal(tmp_path):
    store = Store(tmp_path)
    store.init([])
    snap = SnapshotService(store)
    assert not snap.refresh()
    with pytest.raises(ValueError, match="roster is empty"):
        snap.active(store.load_config())


@pytest.fixture
def history(tmp_path):
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    store.set_role("lead", "lead")
    opener = store.send(sender="lead", recipient="worker", kind="task", body="private opener",
                        meta={"request_id": "task-old", "work_item": "archived-item", "stage": "read"})
    reply = store.send(sender="worker", recipient="lead", kind="task-response", body="private FIX",
                       meta={"request_id": opener.meta["request_id"], "verdict": "FIX"})
    legacy = store.send(sender="lead", recipient="worker", body="legacy private")
    for path in store.messages_dir.iterdir():
        data = json.loads(path.read_text(encoding="utf-8"))
        data["ts"] = "2020-01-01T00:00:00Z"  # discovery must never apply the UI's seven-day window
        path.write_text(json.dumps(data), encoding="utf-8")
    store.archive_messages_below("9999")
    return store, opener, reply, legacy


def test_all_archived_item_unlinked_fix_and_legacy_discovered(history):
    store, opener, reply, legacy = history
    snap = SnapshotService(store)
    assert snap.coverage()["status"] == "building"
    assert snap.refresh()
    assert snap.coverage()["status"] == "complete"
    assert snap.active(store.load_config()) == ([], 0)
    facts = {e.id: e.fields for e in snap.current.archives}
    assert set(facts) == {opener.id, reply.id, legacy.id}
    assert facts[reply.id]["meta"]["verdict"] == "FIX"
    assert facts[opener.id]["meta"]["work_item"] == "archived-item"
    assert "work_item" not in facts[legacy.id]["meta"]
    assert "private" not in json.dumps(facts)
    assert selected_closure(snap.current, {"missing-opener", reply.id})["status"] == "incomplete"


def test_compaction_collision_dedup_and_conflicting_contents(history):
    store, opener, _, _ = history
    original = store.compacted_dir / (opener.id + ".json")
    active = store.messages_dir / original.name
    active.write_bytes(original.read_bytes())
    moved = store.archive_messages_below("9999")
    assert ".json." in moved[0]["to"]
    now = [0]
    snap = SnapshotService(store, clock=lambda: now[0])
    assert snap.refresh()
    result = selected_closure(snap.current, {opener.id})
    assert result["status"] == "complete" and result["selected_envelopes"] == 1
    active.write_bytes(original.read_bytes())
    now[0] = 5
    assert snap.refresh()
    assert selected_closure(snap.current, {opener.id})["items"][0]["partition"] == "active"
    data = json.loads(original.read_text())
    data["body"] = "different"
    original.write_text(json.dumps(data), encoding="utf-8")
    now[0] = 10
    assert snap.refresh()
    assert selected_closure(snap.current, {opener.id})["status"] == "conflict"


def test_cache_reads_only_changed_files_and_invalidates_on_config(history, monkeypatch):
    store, opener, _, _ = history
    now, reads = [0], []
    original = Path.read_text
    def counted(path, *args, **kwargs):
        if path.parent in (store.messages_dir, store.compacted_dir):
            reads.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", counted)
    store.send(sender="lead", recipient="worker", body="active")
    snap = SnapshotService(store, clock=lambda: now[0])
    assert snap.refresh() and len(reads) == 4
    reads.clear()
    now[0] = 5
    assert snap.refresh() and reads == []
    path = store.compacted_dir / (opener.id + ".json")
    path.write_text(original(path) + " ", encoding="utf-8")
    now[0] = 10
    assert snap.refresh() and reads == [path]
    reads.clear()
    store.add_agent("new")
    now[0] = 15
    assert snap.refresh() and len(reads) == 4


def test_bounded_discovery_keeps_active_available_and_never_claims_partial_complete(history):
    store, _, _, _ = history
    live = store.send(sender="lead", recipient="worker", body="live")
    now = [0]
    snap = SnapshotService(store, clock=lambda: now[0], archive_slice_limit=1)
    for step in range(3):
        now[0] = step * 5
        assert snap.refresh()
        assert snap.active(store.load_config())[0][0].id == live.id
        assert snap.coverage()["status"] == ("complete" if step == 2 else "building")


@pytest.mark.parametrize("fault", ["json", "filename", "unicode", "read", "list"])
def test_archive_errors_degrade_board_only(history, monkeypatch, fault):
    store, opener, _, _ = history
    path = store.compacted_dir / (opener.id + ".json")
    if fault == "json":
        path.write_text("{}", encoding="utf-8")
    elif fault == "filename":
        path.rename(path.with_name(path.name + ".not-a-timestamp"))
    elif fault == "unicode":
        path.write_bytes(b"\xff")
    else:
        operation = "read_text" if fault == "read" else "iterdir"
        original = getattr(Path, operation)
        def failed(target, *args, **kwargs):
            if target == (path if fault == "read" else store.compacted_dir):
                raise OSError("unavailable")
            return original(target, *args, **kwargs)
        monkeypatch.setattr(Path, operation, failed)
    snap = SnapshotService(store)
    assert snap.refresh()
    assert snap.active(store.load_config()) == ([], 0)
    assert snap.coverage()["status"] in {"incomplete", "stale"}
    assert selected_closure(snap.current, {opener.id})["status"] != "complete"


@pytest.mark.parametrize("limit,status,warning", [(6, "complete", False), (5, "complete", True),
                                                 (2, "capacity_exceeded", True)])
def test_real_partitions_selected_budget_excludes_unrelated_legacy(history, limit, status, warning):
    store, opener, reply, legacy = history
    live = store.send(sender="lead", recipient="worker", body="live")
    snap = SnapshotService(store)
    assert snap.refresh()
    result = selected_closure(snap.current, {opener.id, reply.id, live.id}, envelope_limit=limit)
    assert (result["status"], result["capacity_warning"]) == (status, warning)
    assert result["selected_envelopes"] == 3
    assert snap.coverage()["discovered_files"] == 4
    assert legacy.id not in {r["id"] for r in result["items"]}
    assert selected_closure(snap.current, {opener.id, live.id}, byte_limit=1)["status"] == "capacity_exceeded"


def test_archive_read_recovery_and_cache_cancellation(history, monkeypatch):
    store, opener, _, _ = history
    now = [0]
    snap = SnapshotService(store, clock=lambda: now[0])
    original = Path.read_text
    def unavailable(path, *args, **kwargs):
        if path.parent == store.compacted_dir:
            raise OSError("temporary read failure")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", unavailable)
    assert snap.refresh() and snap.coverage()["status"] == "incomplete"
    monkeypatch.setattr(Path, "read_text", original)
    now[0] = 5
    assert snap.refresh() and snap.coverage()["status"] == "complete"
    snap.close()
    now[0] = 10
    assert not snap.refresh()
    assert selected_closure(snap.current, {opener.id})["status"] == "complete"


def test_invalid_cached_file_cannot_starve_remaining_discovery(history, monkeypatch):
    store, _, _, _ = history
    first = sorted(store.compacted_dir.iterdir())[0]
    first.write_text("{}", encoding="utf-8")
    original = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda path: iter(sorted(original(path))))
    now = [0]
    snap = SnapshotService(store, clock=lambda: now[0], archive_slice_limit=1)
    for step in range(3):
        now[0] = step * 5
        assert snap.refresh()
    assert snap.coverage()["status"] == "incomplete"
    assert len(snap.current.archives) == 2


def test_signing_change_invalidates_archives_without_stat_change(tmp_path, monkeypatch):
    from agenttalk import signing
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    key = tmp_path / "signing.key"
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(key))
    signing.init_key(store.project_id())
    store.send(sender="lead", recipient="worker", body="signed private")
    store.archive_messages_below("9999")
    now = [0]
    snap = SnapshotService(store, clock=lambda: now[0])
    assert snap.refresh() and snap.coverage()["status"] == "complete"
    original = key.read_bytes()
    key.write_bytes(b"z" * 32)
    now[0] = 5
    assert snap.refresh() and snap.coverage()["status"] == "incomplete"
    assert not snap.current.archives
    key.write_bytes(original)
    now[0] = 10
    assert snap.refresh() and snap.coverage()["status"] == "complete"


def test_first_http_snapshot_discovers_archives_without_replaying_or_reading_on_poll(history, monkeypatch):
    from agenttalk import web
    from test_web import _get
    store, opener, _, _ = history
    monkeypatch.setattr(SnapshotService, "start", lambda self: None)
    server, thread, url = web.serve_in_thread(store)
    try:
        snap = next(iter(server.envelope_snapshots.values()))
        assert snap.coverage()["status"] == "complete"
        assert opener.id in {e.id for e in snap.current.archives}
        def forbidden(**kwargs):
            pytest.fail("HTTP must not scan either partition")
        monkeypatch.setattr(store, "_scan_messages_with_paths", forbidden)
        with _get(url + "/api/state") as response:
            root = json.load(response)["roots"][0]
        assert root["errors"] == [] and root["counts"]["messages"] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
