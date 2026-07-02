from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agenttalk import cli, deadman
from agenttalk.store import Store


NOW = 1_000_000.0


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _store(tmp_path: Path, agents: str = "lead,worker") -> Store:
    store = Store(tmp_path)
    store.init(agents.split(","))
    return store


def _set_msg_time(store: Store, msg_id: str, epoch: float) -> None:
    path = store.messages_dir / f"{msg_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["ts"] = _iso(epoch)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _question(store: Store, rid: str, *, epoch: float) -> str:
    msg = store.send(
        sender="lead",
        recipient="worker",
        kind="question",
        subject="SECRET_SUBJECT",
        body="SECRET_BODY",
        meta={"request_id": rid},
    )
    _set_msg_time(store, msg.id, epoch)
    return msg.id


def test_deadman_alarms_on_stale_owed_inbound_without_content(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _question(store, "q-1", epoch=NOW - 1000)

    rc, report = deadman.check(store, threshold_seconds=100, now=datetime.fromtimestamp(
        NOW, timezone.utc))

    assert rc == 3
    assert report["status"] == "alarm"
    assert report["counts"]["stale_obligation"] == 1
    item = report["buckets"]["stale_obligation"][0]
    assert item["agent"] == "worker"
    assert item["kind"] == "question"
    assert item["request_id"] == "q-1"
    rendered = json.dumps(report)
    assert "SECRET_SUBJECT" not in rendered
    assert "SECRET_BODY" not in rendered


def test_deadman_unread_response_is_separate_and_optional_alarm(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _question(store, "q-1", epoch=NOW - 1000)
    reply = store.send(
        sender="worker",
        recipient="lead",
        kind="message",
        subject="SECRET_REPLY_SUBJECT",
        body="SECRET_REPLY_BODY",
        meta={"request_id": "q-1"},
    )
    _set_msg_time(store, reply.id, NOW - 900)

    now = datetime.fromtimestamp(NOW, timezone.utc)
    rc, report = deadman.check(store, threshold_seconds=100, now=now)
    assert rc == 0
    assert report["counts"]["stale_obligation"] == 0
    assert report["counts"]["stale_unread_response"] == 1

    rc_alarm, alarm = deadman.check(
        store, threshold_seconds=100, alarm_unread_response=True, now=now)
    assert rc_alarm == 3
    assert alarm["counts"]["stale_unread_response"] == 1
    rendered = json.dumps(alarm)
    assert "SECRET_REPLY_SUBJECT" not in rendered
    assert "SECRET_REPLY_BODY" not in rendered


def test_deadman_stale_wake_is_control_alarm(tmp_path: Path) -> None:
    store = _store(tmp_path)
    wake = store.send(sender="lead", recipient="worker", kind="wake", body="wake up")
    _set_msg_time(store, wake.id, NOW - 1000)

    rc, report = deadman.check(store, threshold_seconds=100, now=datetime.fromtimestamp(
        NOW, timezone.utc))

    assert rc == 3
    assert report["counts"]["stale_control"] == 1
    assert report["buckets"]["stale_control"][0]["kind"] == "wake"


def test_deadman_closed_and_rescinded_threads_do_not_alarm(tmp_path: Path) -> None:
    store = _store(tmp_path)
    closed_id = _question(store, "q-closed", epoch=NOW - 1000)
    store.close_thread("worker", "q-closed", seen_msg_id=closed_id)
    _question(store, "q-rescinded", epoch=NOW - 1000)
    rescind = store.send(
        sender="lead",
        recipient="worker",
        kind="rescind",
        subject="rescind",
        body="new plan",
        meta={"request_id": "q-rescinded"},
    )
    _set_msg_time(store, rescind.id, NOW - 900)

    rc, report = deadman.check(store, threshold_seconds=100, now=datetime.fromtimestamp(
        NOW, timezone.utc))

    assert rc == 0
    assert report["counts"]["stale_obligation"] == 0
    assert report["counts"]["stale_unread_response"] == 0
    assert report["counts"]["stale_control"] == 0


def test_deadman_fails_closed_on_malformed_config(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.config_path.write_text("{", encoding="utf-8")

    rc, report = deadman.check(store, threshold_seconds=100, now=datetime.fromtimestamp(
        NOW, timezone.utc))

    assert rc == 3
    assert report["status"] == "error"
    assert report["counts"]["errors"] == 1
    assert report["errors"][0]["class"] in {"JSONDecodeError", "ValueError"}


def test_deadman_fails_closed_on_malformed_deadman_config(tmp_path: Path) -> None:
    store = _store(tmp_path)
    cfg = store.load_config()
    cfg["deadman"] = {"mail_age_slo_seconds": -1}
    store.config_path.write_text(json.dumps(cfg), encoding="utf-8")

    rc, report = deadman.check(store, now=datetime.fromtimestamp(NOW, timezone.utc))

    assert rc == 3
    assert report["status"] == "error"
    assert report["errors"][0]["class"] == "ValueError"


def test_deadman_does_not_read_supervisor_state(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    (store.dir / "supervisor-state.json").write_text("not json", encoding="utf-8")
    original_read_text = Path.read_text

    def guarded_read_text(self: Path, *args, **kwargs):
        if self.name == "supervisor-state.json":
            raise AssertionError("deadman must not read supervisor-state.json")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    rc, report = deadman.check(store, threshold_seconds=100, now=datetime.fromtimestamp(
        NOW, timezone.utc))

    assert rc == 0
    assert report["status"] == "ok"


def test_deadman_cli_json_uses_config_threshold(tmp_path: Path, capsys) -> None:
    store = _store(tmp_path)
    cfg = store.load_config()
    cfg["deadman"] = {"mail_age_slo_seconds": 100, "alarm_unread_response": False}
    store.config_path.write_text(json.dumps(cfg), encoding="utf-8")
    _question(store, "q-1", epoch=NOW - 1000)

    rc = cli.main(["--root", str(tmp_path), "deadman", "--json", "--now", str(NOW)])

    assert rc == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["threshold_seconds"] == 100.0
    assert payload["counts"]["stale_obligation"] == 1


def test_deadman_cli_rejects_nonpositive_threshold(tmp_path: Path, capsys) -> None:
    _store(tmp_path)

    rc = cli.main(["--root", str(tmp_path), "deadman", "--threshold-seconds", "0"])

    assert rc == 2
    assert "must be positive" in capsys.readouterr().err
