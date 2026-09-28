"""B6a gate isolation and bounded worker-owned board projection."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from agenttalk import cli, gates, web
from agenttalk.store import Store
from test_work_board_reducer import Bus, BUILDER, HEAD, ITEM, LEAD, REVIEWER, reviewed

NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)


def gate(root, name="wb.a.c1.unit", **extra):
    return gates.set_gate(root, name=name, status="red", severity="blocker", scope="global",
                          actor="alpha", evidence_source="automation_ci", **extra)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    return s


def test_unscoped_and_nonboard_checks_ignore_both_board_gate_sources(store, capsys):
    gate(store.root)
    state = gates.load_gate_state(store.root)
    state["required_gates"] = ["wb.missing.c1.unit"]
    gates.gates_path(store.root).write_text(json.dumps(state), encoding="utf-8")
    for scope in (None, "release"):
        result = gates.check_gates(store.root, scope=scope)
        assert result["verdict"] == "GO" and result["required_gates"] == [] and result["gates"] == []
        assert result["warnings"]
    store.send(sender="beta", recipient="alpha", kind="question", body="test", meta={"request_id": "q-1"})
    assert cli.main(["--root", str(store.root), "check", "--for", "alpha", "--to-request", "q-1", "--gates"]) == 0
    assert "GO" in capsys.readouterr().out


@pytest.mark.parametrize("path", ["web", "cli"])
def test_both_attention_paths_ignore_board_red_but_keep_global_blocker(store, path):
    gate(store.root)
    def holds():
        rows = (web.build_attention(web.RootDescriptor(store=store, label="test"))["items"] if path == "web"
                else cli._collect_attention_items(store, for_agent=None, roster=["alpha", "beta"]))
        return [r for r in rows if r["source"] == ("gate" if path == "web" else "gate_hold")]
    assert holds() == []
    gate(store.root, name="release")
    assert len(holds()) == 1
    assert gates.check_gates(store.root)["verdict"] == "HOLD"


def test_root_requirement_refused_without_writing(store):
    with pytest.raises(ValueError, match="root.*required"):
        gate(store.root, required=True)
    assert not gates.gates_path(store.root).exists()


@pytest.mark.parametrize("change", [{}, {"revision": None}, {"revision": "b" * 40},
                                 {"scope": "p/other/c1"}, {"severity": "warn"}, {"status": "red"}])
def test_board_checks_exact_name_scope_revision_and_blocker_evidence(store, change):
    record = {"name": f"wb.{ITEM}.c1.unit", "status": "green", "severity": "blocker", "scope": f"p/{ITEM}/c1",
              "actor": "alpha", "evidence_source": "automation_ci", "evidence": ["test run"], "revision": HEAD}
    gates.set_gate(store.root, **dict(record, **change))
    gate(store.root)  # another item's red must never contaminate this result
    result = gates.check_board(store.root, project="p", item=ITEM, cycle=1, revision=HEAD, keys=["unit"])
    expected = None if "revision" in change or "scope" in change else not bool(change)
    assert result is expected
    assert gates.check_board(store.root, project="p", item=ITEM, cycle=2, revision=HEAD, keys=["unit"]) is None


def snapshot(bus):
    from agenttalk.envelope_snapshot import Envelope, Snapshot, _digest
    envelopes = tuple(Envelope(m.id, {k: v for k, v in m.to_dict().items() if k != "body"},
                               _digest(m.to_dict()), 100, "active") for m in bus.messages)
    return Snapshot(1, 0, "cfg", (), 0, envelopes, len(envelopes), len(envelopes) * 100, 0,
                    archives_complete=True)


def project(bus, **kwargs):
    from agenttalk.work_board_feed import build
    return build(snapshot(bus), project="p", lead=LEAD, now=NOW,
                 gate_state={"gates": {}, "required_gates": []}, **kwargs)


def test_external_ci_unknown_merge_and_legacy_are_honest():
    bus = Bus()
    reviewed(bus)
    bus.task("tk-legacy", BUILDER, "build", item=None)
    bus.add(REVIEWER, LEAD, "task-response", {"request_id": "missing", "status": "done"}, raw=True)
    feed = project(bus)
    assert feed["items"][0]["workflow_column"] == "ready"
    assert "not tracked" in feed["items"][0]["checks"]
    assert feed["items"][0]["integration"] == {}
    assert feed["legacy"]["open_request_count"] == 1 and feed["unassigned"]["count"] == 1


def test_feed_uses_gate_adapter_for_reducer(store):
    from agenttalk.work_board_feed import build
    bus = Bus()
    b = bus.task("tk-build", BUILDER, "build", required_gates=["unit"])
    bus.reply(b, verdict="done")
    r = bus.task("tk-read", REVIEWER, "read", work_head=HEAD)
    bus.reply(r, verdict="GO")
    def feed():
        return build(snapshot(bus), project="p", lead=LEAD, now=NOW,
                     gate_state=gates.load_gate_state(store.root))["items"][0]
    assert feed()["workflow_column"] == "unknown"
    gates.set_gate(store.root, name=f"wb.{ITEM}.c1.unit", status="green", severity="blocker",
                   scope=f"p/{ITEM}/c1", actor="alpha", evidence_source="automation_ci",
                   evidence=["tests"], revision=HEAD)
    assert feed()["workflow_column"] == "ready"
    gate(store.root, name="release")
    assert feed()["global_gates"]["verdict"] == "HOLD"


@pytest.mark.parametrize("days,included", [(7, True), (8, False)])
def test_done_window_and_old_active_items(days, included):
    bus = Bus()
    reviewed(bus)
    bus.messages = [replace(m, ts=(NOW - timedelta(days=days)).isoformat()) for m in bus.messages]
    assert len(project(bus)["items"]) == 1  # active, irrespective of age
    assert bool(project(bus, integrated={(ITEM, HEAD): True})["items"]) is included


def test_card_and_byte_overflow_keep_counts_and_legacy():
    bus = Bus()
    for n in range(5):
        bus.task(f"tk-{n}", BUILDER, "build", item=f"item-{n}")
    bus.task("tk-legacy", BUILDER, "build", item=None)
    feed = project(bus, card_limit=2)
    assert (len(feed["items"]), feed["total_count"], feed["omitted_count"], feed["truncated"]) == (2, 5, 3, True)
    assert feed["legacy"]["open_request_count"] == 1
    from agenttalk.work_board_feed import bounded
    polled = bounded(feed)
    assert (polled["total_count"], polled["omitted_count"], polled["truncated"]) == (5, 3, True)
    feed = project(bus, byte_limit=2000)
    assert len(json.dumps(feed, indent=2, ensure_ascii=False).encode()) <= 2000
    assert feed["truncated"] and feed["omitted_count"] > 0


def test_capacity_counts_closure_before_card_truncation_and_preserves_unknown():
    bus = Bus()
    reviewed(bus)
    feed = project(bus, envelope_limit=3, card_limit=1)
    assert feed["coverage"]["status"] == "capacity_exceeded"
    assert feed["total_count"] is None and not any(i["column"] == "ready" for i in feed["items"])
    feed = project(bus, envelope_limit=6)
    assert feed["coverage"]["capacity_warning"] and feed["coverage"]["status"] == "complete"


def test_http_feed_is_cached_in_worker_with_root_validation(store, monkeypatch):
    import threading
    from urllib.error import HTTPError
    from urllib.request import urlopen
    store.set_role("alpha", "lead")
    store.send(sender="alpha", recipient="beta", kind="task", body="private task body",
               meta={"request_id": "tk-http", "work_item": "http-item", "stage": "build"})
    server = web.make_server(store, "127.0.0.1", 0)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        def forbidden(*args, **kwargs):
            pytest.fail("HTTP must not read envelopes or rebuild the board")
        from agenttalk import work_board_feed
        monkeypatch.setattr(work_board_feed, "build", forbidden)
        monkeypatch.setattr(store, "_scan_messages_with_paths", forbidden)
        url = f"http://127.0.0.1:{server.server_address[1]}/api/work-board"
        with urlopen(url, timeout=3) as response:  # noqa: S310 - fixed loopback test server
            feed = json.load(response)
        assert feed["coverage"]["status"] == "complete"
        assert feed["items"][0]["work_item"] == "http-item"
        assert feed["items"][0]["workflow_column"] == "queued"
        assert "private task body" not in json.dumps(feed)
        with pytest.raises(HTTPError) as error:
            urlopen(url + "?root=missing", timeout=3)  # noqa: S310 - fixed loopback test server
        assert error.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)


def test_malformed_history_cannot_crash_projection():
    bus = Bus()
    reviewed(bus)
    bus.add(LEAD, BUILDER, "task", {"request_id": [], "work_item": []}, raw=True)
    feed = project(bus)
    assert feed["items"] and feed["unassigned"]["count"] > 0


def test_board_fault_preserves_active_state_and_last_known_feed(store, monkeypatch):
    from agenttalk import work_board_feed
    from agenttalk.envelope_snapshot import SnapshotService
    now = [0]
    service = SnapshotService(store, clock=lambda: now[0])
    assert service.refresh()
    def fail(*args, **kwargs):
        raise ValueError("bad board projection")
    monkeypatch.setattr(work_board_feed, "build", fail)
    store.send(sender="alpha", recipient="beta", body="new message")
    now[0] = 5
    assert service.refresh()
    assert len(service.active(store.load_config())[0]) == 1
    assert service.board()["coverage"]["status"] != "complete"
    assert service.board()["total_count"] is None


def test_cancelled_item_is_excluded_but_missing_evidence_stays():
    bus = Bus()
    b = bus.task("tk-build", BUILDER, "build")
    bus.reply(b, status="declined")
    assert project(bus)["items"] == []


def test_root_direct_requirement_and_corrupt_gate_state(store):
    with pytest.raises(ValueError, match="root.*required"):
        gates.write_gate_state(store.root, {"required_gates": ["wb.a.c1.unit"], "gates": {}})
    gates.gates_path(store.root).write_text("not JSON", encoding="utf-8")
    assert gates.check_gates(store.root)["verdict"] == "HOLD"
    assert gates.check_board(store.root, project="p", item=ITEM, cycle=1, revision=HEAD, keys=["unit"]) is None


def test_production_card_limit_and_incomplete_archive_totals():
    from agenttalk.work_board_feed import build
    bus = Bus()
    for n in range(101):
        bus.task(f"tk-{n}", BUILDER, "build", item=f"item-{n}")
    bus.messages = [replace(m, ts=NOW.isoformat()) for m in bus.messages]
    feed = project(bus)
    assert (len(feed["items"]), feed["total_count"], feed["omitted_count"]) == (100, 101, 1)
    assert len(json.dumps(feed, indent=2, ensure_ascii=False).encode()) <= 256 * 1024
    feed = build(replace(snapshot(bus), archives_complete=False), project="p", lead=LEAD,
                 gate_state={"gates": {}, "required_gates": []}, now=NOW)
    assert feed["coverage"]["status"] == "building" and feed["total_count"] is None
    assert feed["legacy"]["open_request_count"] is None


def test_cap_includes_linked_untagged_archives_and_missing_anchors():
    from agenttalk.work_board_feed import build
    bus = Bus()
    reviewed(bus)
    m = bus.add(BUILDER, LEAD, "message", {"request_id": "tk-build"})
    snap = snapshot(bus)
    snap = replace(snap, envelopes=snap.envelopes[:-1], archives=(replace(snap.envelopes[-1], partition="compacted"),))
    feed = build(snap, project="p", lead=LEAD, now=NOW, envelope_limit=4,
                 gate_state={"gates": {}, "required_gates": []})
    assert feed["coverage"]["selected_envelopes"] == 5
    assert feed["coverage"]["status"] == "capacity_exceeded"
    bus.messages[-1] = replace(m, meta={"request_id": "tk-build", "in_reply_to": "missing"})
    assert project(bus)["coverage"]["status"] == "incomplete"


def test_stale_or_invalidated_worker_never_reports_complete(store):
    from agenttalk.envelope_snapshot import SnapshotService
    clock = [0]
    service = SnapshotService(store, clock=lambda: clock[0])
    assert service.refresh()
    clock[0] = 16
    feed = service.board()
    assert feed["coverage"]["status"] == "stale" and feed["total_count"] is None and feed["last_known"]
    clock[0] = 1
    service.invalidate()
    assert service.board()["coverage"]["status"] == "stale"


def test_anchor_only_reply_counts_as_recent_work():
    bus = Bus()
    reviewed(bus)
    recent = bus.messages[-1]
    bus.messages = [replace(m, ts=(NOW - timedelta(days=8)).isoformat()) for m in bus.messages]
    bus.messages[-1] = replace(recent, ts=NOW.isoformat(),
                              meta={k: v for k, v in recent.meta.items() if k not in ("request_id", "work_item")})
    assert project(bus, integrated={(ITEM, HEAD): True})["items"]
