"""Shared state/board snapshot contracts, using real Store envelopes."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from agenttalk import gates, web, work_board_feed
from agenttalk.store import Store


@pytest.fixture
def bus(tmp_path):
    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    store.send(sender="alpha", recipient="beta", body="private body")
    return store


def service(bus, **kwargs):
    from agenttalk.envelope_snapshot import SnapshotService
    return SnapshotService(bus, **kwargs)


def test_active_state_parity_and_cached_poll_has_no_envelope_reads(bus, monkeypatch):
    bus.send(sender="beta", recipient="alpha", body="reply", kind="message")
    (bus.messages_dir / "bad.json").write_text("{}", encoding="utf-8")
    roots = [web.RootDescriptor(store=bus, label="test")]
    with monkeypatch.context() as oracle:
        oracle.setattr(web, "_validated_for_state", lambda s, cfg: (s.valid_messages(), len(s.list_invalid_messages())))
        expected = web.build_state(roots)
    snapshot = service(bus)
    assert snapshot.refresh()
    def forbidden(*args, **kwargs):
        pytest.fail("HTTP projection must not scan envelopes")
    monkeypatch.setattr(bus, "_scan_messages_with_paths", forbidden)
    actual = web.build_state(roots, snapshots={str(bus.root.resolve()): snapshot})
    actual.pop("generated_at")
    expected.pop("generated_at")
    assert actual == expected
    assert snapshot.current.invalid_count == 1


def test_refresh_coalesces_and_invalidation_discards_inflight_generation(bus, monkeypatch):
    snapshot = service(bus, clock=lambda: 10)
    entered, release = threading.Event(), threading.Event()
    original = bus._scan_messages_with_paths
    def blocked(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)
    monkeypatch.setattr(bus, "_scan_messages_with_paths", blocked)
    with ThreadPoolExecutor() as pool:
        first = pool.submit(snapshot.refresh)
        assert entered.wait(5)
        assert snapshot.refresh() is False
        snapshot.invalidate()
        release.set()
        assert first.result() is False
    assert snapshot.current is None
    assert snapshot.refresh() is False  # no more than one start per five seconds


def test_refresh_generation_freshness_failure_and_recovery(bus, monkeypatch):
    now = [0.0]
    snapshot = service(bus, clock=lambda: now[0])
    snapshot.refresh()
    before = snapshot.current
    assert snapshot.coverage()["status"] == "complete"
    now[0] = 5
    snapshot.refresh()
    assert snapshot.current.generation > before.generation
    original = bus._scan_messages_with_paths
    def failed(**kwargs):
        raise OSError("scan failed")
    monkeypatch.setattr(bus, "_scan_messages_with_paths", failed)
    path = next(bus.messages_dir.glob("*.json"))
    path.write_text(path.read_text() + " ", encoding="utf-8")  # force a cache miss
    now[0] = 10
    assert snapshot.refresh() is False
    assert snapshot.current.active == before.active
    assert snapshot.coverage()["status"] == "stale"
    assert tuple(snapshot.active(bus.load_config())[0]) == before.active
    changed_config = dict(bus.load_config(), agents=["alpha"])
    with pytest.raises(ValueError, match="config generation"):
        snapshot.active(changed_config)
    now[0] = 21
    with pytest.raises(ValueError, match="stale"):
        snapshot.active(bus.load_config())
    monkeypatch.setattr(bus, "_scan_messages_with_paths", original)
    now[0] = 25
    assert snapshot.refresh()
    now[0] = 41
    assert snapshot.coverage()["status"] == "stale"
    with pytest.raises(ValueError, match="stale"):
        snapshot.active(bus.load_config())


def test_config_change_and_mid_scan_membership_change_fail_closed(bus, monkeypatch):
    snapshot = service(bus)
    snapshot.refresh()
    cfg = bus.load_config()
    cfg["agents"] = ["alpha"]
    with pytest.raises(ValueError, match="generation"):
        snapshot.active(cfg)
    other = service(bus)
    original = bus._scan_messages_with_paths
    def moved(**kwargs):
        rows = original(**kwargs)
        rows[0][0][1].unlink()
        return rows
    monkeypatch.setattr(bus, "_scan_messages_with_paths", moved)
    assert other.refresh() is False
    assert other.current is None


@pytest.mark.parametrize("limit,byte_limit,status", [(2, 10000, "complete"), (1, 10000, "capacity_exceeded"),
                                                    (2, 1, "capacity_exceeded")])
def test_selected_closure_caps_both_partitions_not_unrelated_active(bus, limit, byte_limit, status):
    from agenttalk.envelope_snapshot import Envelope, selected_closure
    for _ in range(3):
        bus.send(sender="alpha", recipient="beta", body="unrelated")
    snapshot = service(bus)
    snapshot.refresh()
    current = snapshot.current
    first = current.envelopes[0]
    archived = Envelope("archived-id", {"id": "archived-id", "meta": {}}, "digest", 7, "compacted")
    current = replace(current, archives=(archived,), archives_complete=True)
    result = selected_closure(current, {first.id, archived.id}, envelope_limit=limit, byte_limit=byte_limit)
    assert result["status"] == status
    assert result["selected_envelopes"] == 2
    assert result["selected_source_bytes"] == first.source_bytes + 7
    if status != "complete":
        assert result["items"] == []
    assert "private body" not in json.dumps(result)
    assert len(snapshot.active(bus.load_config())[0]) == 4  # no whole-active cap


def test_closure_deduplicates_prefers_active_and_never_truncates_conflicts(bus):
    from agenttalk.envelope_snapshot import selected_closure
    snapshot = service(bus)
    snapshot.refresh()
    current = snapshot.current
    first = current.envelopes[0]
    duplicate = replace(first, partition="compacted")
    complete = replace(current, archives=(duplicate,), archives_complete=True)
    result = selected_closure(complete, {first.id}, envelope_limit=1)
    assert result["selected_envelopes"] == 1
    assert result["items"][0]["partition"] == "active"
    assert result["capacity_warning"] is True
    conflict = replace(duplicate, digest="different")
    assert selected_closure(replace(complete, archives=(conflict,)), {first.id})["status"] == "conflict"
    assert selected_closure(complete, {"missing"})["status"] == "incomplete"
    assert selected_closure(replace(current, archives_complete=False), {first.id})["status"] == "building"


def test_signing_generation_revalidated_and_compaction_never_replays(bus, tmp_path, monkeypatch):
    from agenttalk import signing
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "test.key"))
    signing.init_key(bus.project_id())
    good = bus.send(sender="alpha", recipient="beta", body="signed")
    now = [0]
    snapshot = service(bus, clock=lambda: now[0])
    assert snapshot.refresh()
    assert [m.id for m in snapshot.active(bus.load_config())[0]] == [good.id]
    assert snapshot.current.invalid_count == 1  # earlier unsigned envelope
    key = tmp_path / "test.key"
    original = key.read_bytes()
    key.write_bytes(b"z" * 32)
    now[0] = 5
    assert snapshot.refresh()
    assert snapshot.current.invalid_count == 2 and not snapshot.current.active
    key.write_bytes(original)
    bus.archive_messages_below("9999")
    now[0] = 10
    assert snapshot.refresh()
    assert snapshot.active(bus.load_config())[0] == []
    assert snapshot.current.invalid_count == 1
    assert list(bus.compacted_dir.iterdir())


def test_http_poll_uses_snapshot_and_server_close_stops_worker(bus, monkeypatch):
    from test_web import _get
    server, thread, url = web.serve_in_thread(bus)
    try:
        def forbidden(*args, **kwargs):
            pytest.fail("HTTP reread envelopes")
        monkeypatch.setattr(bus, "_scan_messages_with_paths", forbidden)
        with _get(url + "/api/state") as response:
            root = json.load(response)["roots"][0]
        assert root["errors"] == [] and root["counts"]["messages"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    assert not thread.is_alive()
    assert all(not s._thread.is_alive() for s in server.envelope_snapshots.values())


@pytest.mark.parametrize("write_before_read", [False, True])
def test_http_mid_scan_write_keeps_team_view_and_retries(bus, monkeypatch, write_before_read):
    from test_web import _get
    # Drive refreshes with the injected clock, independently of OS scheduling.
    monkeypatch.setattr("agenttalk.envelope_snapshot.SnapshotService.start", lambda self: None)
    server, thread, url = web.serve_in_thread(bus)
    snapshot = next(iter(server.envelope_snapshots.values()))
    now = [snapshot.current.started + 5]
    snapshot.clock = lambda: now[0]
    original = bus._scan_messages_with_paths
    changed = False
    def racing(**kwargs):
        nonlocal changed
        if changed:
            return original(**kwargs)
        changed = True
        rows = None if write_before_read else original(**kwargs)
        bus.send(sender="alpha", recipient="beta", body="arrived during scan")
        return original(**kwargs) if write_before_read else rows
    try:
        with _get(url + "/api/state") as response:
            before = json.load(response)["roots"][0]
        monkeypatch.setattr(bus, "_scan_messages_with_paths", racing)
        path = next(bus.messages_dir.glob("*.json"))
        path.write_text(path.read_text() + " ", encoding="utf-8")
        assert snapshot.refresh() is False
        assert snapshot.coverage()["status"] == "stale"
        with _get(url + "/api/state") as response:
            during = json.load(response)["roots"][0]
        assert during.keys() == before.keys()
        assert during["errors"] == []
        assert during["counts"] == before["counts"]
        assert snapshot.refresh() is False  # bounded retry, no busy loop
        now[0] += .25
        assert snapshot.refresh()  # promptly eligible, not another five seconds
        with _get(url + "/api/state") as response:
            after = json.load(response)["roots"][0]
        assert after["errors"] == [] and after["counts"]["messages"] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_worker_runs_scheduled_membership_retry_without_normal_poll_delay(bus, monkeypatch):
    from agenttalk.envelope_snapshot import MembershipChanged
    now = [0]
    snapshot = service(bus, clock=lambda: now[0])
    assert snapshot.refresh()
    original = bus._scan_messages_with_paths
    def race(**kwargs):
        raise MembershipChanged("concurrent write")
    monkeypatch.setattr(bus, "_scan_messages_with_paths", race)
    path = next(bus.messages_dir.glob("*.json"))
    path.write_text(path.read_text() + " ", encoding="utf-8")
    now[0] = 5
    assert snapshot.refresh() is False
    monkeypatch.setattr(bus, "_scan_messages_with_paths", original)
    completed = threading.Event()
    refresh = snapshot.refresh
    def observed():
        if refresh():
            completed.set()
    monkeypatch.setattr(snapshot, "refresh", observed)
    now[0] = 5.25
    snapshot.start()
    try:
        assert completed.wait(2), "due race retry must not wait for the five-second poll"
        assert snapshot.error is None
    finally:
        snapshot.close()


# ----------------------------------------------- #239 fix round 1 (dev-5 cold read, F1-F5)
#
# Round 1's own fix ("reuse the whole board when message/archive membership + trust are
# unchanged") broke five real invariants by trying to enumerate every input that could make a
# cached board stale (gate state, waivers, time, transient failures, discarded generations,
# archive errors) - exactly what the lead's direction warned against. This round instead skips
# ONLY the expensive reduce()/closure computation, keyed on this cycle's own freshly-computed,
# never-throttled active-state facts (never an upstream guess) - and ALWAYS re-evaluates
# gate/waiver state fresh, cache hit or not, since it is cheap and can change with no file
# change at all (a waiver expiring). The five probes below are the reviewer's own repro,
# adapted to this file's fixtures; all five FAIL against round 1 and PASS here.

def _board_setup(tmp_path, *, clock):
    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    store.set_role("alpha", "lead")
    store.send(sender="alpha", recipient="beta", kind="task", body="work",
              meta={"request_id": "tk-probe", "work_item": "probe", "stage": "build"})
    snapshot = service(store, clock=clock)
    assert snapshot.refresh()
    return store, snapshot


def test_gate_only_change_is_observed_with_no_message(tmp_path):
    """F1: a global gate flip with an otherwise-unchanged bus must be reflected immediately,
    never frozen at the cached board's original GO verdict."""
    now = [0.0]
    store, snapshot = _board_setup(tmp_path, clock=lambda: now[0])
    assert snapshot.board()["items"][0]["global_gates"]["verdict"] == "GO"
    gates.set_gate(store.root, name="release", status="red", severity="blocker",
                   scope="global", actor="alpha", evidence_source="automation_ci")
    assert gates.check_gates(store.root)["verdict"] == "HOLD"
    now[0] += 6
    assert snapshot.refresh()
    assert snapshot.board()["items"][0]["global_gates"]["verdict"] == "HOLD"


def test_board_failure_is_retried_without_another_message(tmp_path, monkeypatch):
    """F2: a transient board failure must not become permanent - the very next refresh, with
    no new message at all, must retry and recover once the failure clears."""
    now = [0.0]
    store, snapshot = _board_setup(tmp_path, clock=lambda: now[0])
    def failed(*a, **k):
        raise OSError("transient board failure")
    monkeypatch.setattr(work_board_feed, "build", failed)
    store.send(sender="alpha", recipient="beta", body="new message")
    now[0] += 6
    assert snapshot.refresh()
    assert snapshot.board()["coverage"]["status"] == "stale"
    monkeypatch.undo()
    now[0] += 6
    assert snapshot.refresh()
    assert snapshot.board()["coverage"]["status"] == "complete"


def test_invalidated_build_does_not_poison_the_reuse_key(tmp_path, monkeypatch):
    """F3: invalidate() firing mid-build must discard that generation entirely, including the
    #239 reuse key - the NEXT refresh must not republish stale facts as if they were current."""
    now = [0.0]
    store, snapshot = _board_setup(tmp_path, clock=lambda: now[0])
    original = work_board_feed.build
    def invalidated(*a, **k):
        result = original(*a, **k)
        snapshot.invalidate()
        return result
    monkeypatch.setattr(work_board_feed, "build", invalidated)
    store.send(sender="alpha", recipient="beta", body="new message")
    now[0] += 6
    assert snapshot.refresh() is False
    monkeypatch.undo()
    now[0] += 6
    assert snapshot.refresh()
    assert len(snapshot.active(store.load_config())[0]) == 2


def test_archive_error_does_not_disable_active_refresh(tmp_path, monkeypatch):
    """F4: an unreadable archive partition must stay isolated to board/archive coverage - it
    must never age out or block the ACTIVE snapshot refresh."""
    now = [0.0]
    store, snapshot = _board_setup(tmp_path, clock=lambda: now[0])
    original = snapshot._membership
    def unreadable(compacted=False):
        if compacted:
            raise PermissionError("archive temporarily unavailable")
        return original(compacted)
    monkeypatch.setattr(snapshot, "_membership", unreadable)
    store.send(sender="alpha", recipient="beta", body="new message")
    now[0] += 6
    assert snapshot.refresh()
    assert len(snapshot.active(store.load_config())[0]) == 2
    assert snapshot.coverage()["status"] == "stale"


def test_waiver_expiry_is_observed_without_any_file_change(tmp_path, monkeypatch):
    """F1/F5: a waiver expiring is a pure function of WALL TIME - no message, no gate file
    write, nothing to key a reuse cache on. It must still flip the board's verdict."""
    now = [0.0]
    store, snapshot = _board_setup(tmp_path, clock=lambda: now[0])
    gates.set_gate(store.root, name="release", status="red", severity="blocker",
                   scope="global", actor="alpha", evidence_source="automation_ci")
    wall = [datetime.now(timezone.utc)]
    gates.waive_gate(store.root, name="release", operator="human", reason="temporary",
                     scope="global", expires=(wall[0] + timedelta(seconds=10)).isoformat())
    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return wall[0]
    monkeypatch.setattr(gates, "datetime", FakeDateTime)
    store.send(sender="alpha", recipient="beta", body="waiver now active")
    now[0] += 6
    assert snapshot.refresh()
    assert snapshot.board()["items"][0]["global_gates"]["verdict"] == "GO"
    wall[0] += timedelta(seconds=20)
    now[0] += 20
    assert gates.check_gates(store.root)["verdict"] == "HOLD"
    assert snapshot.refresh()
    assert snapshot.board()["items"][0]["global_gates"]["verdict"] == "HOLD"


def test_refresh_skips_the_expensive_reduce_when_facts_are_unchanged(bus, monkeypatch):
    """#239 root cause, round 2: refresh() re-read, re-reduced and re-built the WHOLE board
    every ~5s cycle even when nothing had changed - the confirmed dominant driver of the
    reported runaway memory growth. This is the cheap, deterministic regression proxy: 200
    refreshes on an unchanged store must call work_board_feed.build exactly once (the first),
    never again - while gate state (checked separately above) stays live throughout."""
    now = [0.0]
    snapshot = service(bus, clock=lambda: now[0])
    assert snapshot.refresh()
    first_items = snapshot._board["items"]
    calls = []
    original_build = work_board_feed.build
    def counting_build(*args, **kwargs):
        calls.append(1)
        return original_build(*args, **kwargs)
    monkeypatch.setattr(work_board_feed, "build", counting_build)
    for _ in range(200):
        now[0] += 6.0
        assert snapshot.refresh()
    assert calls == [], "the expensive reduce/build must never rerun once facts are unchanged"
    assert snapshot._board["items"] is not first_items  # a new dict every cycle (gate re-stamp)
    assert snapshot._board["items"] == first_items       # but equal content - nothing rebuilt
    # A real change (a new message) must still trigger a genuine rebuild.
    bus.send(sender="alpha", recipient="beta", body="a new message arrives")
    now[0] += 6.0
    assert snapshot.refresh()
    assert len(calls) == 1
