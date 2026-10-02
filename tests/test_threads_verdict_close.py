"""Answered is terminal, not necessarily successful (#277)."""
import json
from dataclasses import replace
from pathlib import Path

import pytest

from agenttalk import checkpoint, cli, gates, threads
from agenttalk.store import Message, Store
from agenttalk.wrapper import obligations, recv_api


# Exact meta copied from two real task-responses; no bodies or store paths.
# 20261001-040210-992391-Wytu and 20260930-193208-290578-L0ak.
REAL_REPLY_META = [
    {"verdict": "GO", "work_item": "fix-246-decode-regression", "stage": "read",
     "work_head": "752f201cde5263fcad87904d9c659a4445e8485c",
     "in_reply_to": "20261001-035409-363805-2vps", "request_id": "tk-3531c4e9b46b"},
    {"verdict": "FIX", "work_item": "ci-xdist-posix", "stage": "read",
     "work_head": "7a9b1bc6397dfe0074af5ac7cefd7c0f9fee3feb",
     "in_reply_to": "20260930-192523-684276-LqwY", "request_id": "tk-54c83cd45f8f"},
]


def _exchange(meta):
    opener = Message(id="001", ts="2026-10-01T00:00:00Z", sender="lead", recipient="worker",
                     kind="task", subject="work", body="do it", meta={"request_id": meta["request_id"]})
    reply = Message(id="002", ts=opener.ts, sender="worker", recipient="lead",
                    kind="task-response", subject="", body="result", meta=dict(meta))
    return opener, reply


@pytest.mark.parametrize("meta", REAL_REPLY_META)
def test_real_verdict_only_task_response_closes_after_read(meta):
    messages = _exchange(meta)
    assert threads.derive_threads(messages, agent="lead", cursor="001")[0].state == "reply-waiting"
    for agent in ("lead", "worker"):
        row = threads.derive_threads(messages, agent=agent, cursor="002")[0]
        assert row.state == "closed"
        assert row.next_owner is None


@pytest.mark.parametrize("status", ["accepted", "in-progress", "", None])
def test_explicit_status_overrides_verdict(status):
    messages = _exchange({"request_id": "tk", "status": status, "verdict": "GO"})
    assert threads.derive_threads(messages, agent="worker", cursor="002")[0].state == "owed-inbound"


@pytest.mark.parametrize("verdict", [None, "", "   "])
def test_empty_verdict_does_not_close(verdict):
    messages = _exchange({"request_id": "tk", "verdict": verdict})
    assert threads.derive_threads(messages, agent="worker", cursor="002")[0].state == "owed-inbound"


def test_verdict_requires_correct_response_direction_and_kind():
    opener, reply = _exchange({"request_id": "tk", "verdict": "done"})
    for field, value in (("sender", "outsider"), ("recipient", "outsider"), ("kind", "message")):
        bad = Message.from_dict({**reply.to_dict(), {"sender": "from", "recipient": "to"}.get(field, field): value})
        assert threads.derive_threads([opener, bad], agent="worker", cursor="002")[0].state == "owed-inbound"


def test_reask_after_verdict_only_close_reopens():
    opener, reply = _exchange(REAL_REPLY_META[0])
    assert threads.derive_threads([opener, reply], agent="lead", cursor="002")[0].state == "closed"
    reask = Message.from_dict({**opener.to_dict(), "id": "003"})
    assert threads.derive_threads([opener, reply, reask], agent="lead", cursor="003")[0].state == "open-outbound"
    assert threads.derive_threads([opener, reply, reask], agent="worker", cursor="003")[0].state == "owed-inbound"


def _store(tmp_path):
    store = Store(tmp_path)
    store.init(["lead", "worker"])
    store.set_role("lead", "lead")
    store.send(sender="lead", recipient="worker", kind="task", body="do it", meta={"request_id": "tk"})
    return store


@pytest.mark.parametrize("meta_args,warns", [([], True), (["--meta", "verdict=GO"], False),
                                               (["--meta", "status=accepted"], False)])
def test_reply_warning_stderr_only_stdout_unchanged(tmp_path, capsys, meta_args, warns):
    store = _store(tmp_path)
    capsys.readouterr()
    assert cli.main(["--root", str(tmp_path), "reply", "--from", "worker", "--to-request", "tk",
                     "--kind", "task-response", "-m", "result", *meta_args]) == 0
    output = capsys.readouterr()
    reply = store.valid_messages()[-1]
    assert output.out == cli.render(reply, header="AGENTTALK :: REPLY  worker -> lead") + "\n"
    if warns:
        assert len(output.err.splitlines()) == 1
        assert "stay open" in output.err and "--meta status=done" in output.err
        row = threads.derive_threads(store.valid_messages(), agent="worker", cursor=reply.id)[0]
        assert row.state == "owed-inbound"
    else:
        assert output.err == ""


@pytest.mark.parametrize("verdict", ["GO", "FIX"])
def test_checkpoint_status_and_barrier_do_not_confuse_answer_with_success(tmp_path: Path, capsys, verdict):
    store = _store(tmp_path)
    record = recv_api.next_record(store, "worker")
    reply = store.send(sender="worker", recipient="lead", kind="task-response", body="result",
                       meta={"request_id": "tk", "verdict": verdict, "in_reply_to": record["id"]})
    delivery = obligations.DetectionCommitGate.from_environment(store, "worker", fence="wrapper-1")
    assert delivery.resolve_landed_response(record).proof.evidence_id == reply.id
    store.set_cursor("lead", reply.id)
    store.set_cursor("worker", reply.id)
    for agent in ("lead", "worker"):
        state = checkpoint.collect_bus_state(store, agent)
        assert state["owed_in"] == state["owed_out"] == state["reply_waiting"] == []
    capsys.readouterr()
    assert cli.main(["--root", str(tmp_path), "status", "--json"]) == 0
    assert "open-outbound" not in capsys.readouterr().out
    history = [replace(m, ts="2000-01-01T00:00:00Z") for m in store.valid_messages()]
    assert cli._thread_warnings(store, store.load_config(), valid_msgs=history[:1])
    assert cli._thread_warnings(store, store.load_config(), valid_msgs=history) == []
    # A completed FIX answer does not override an independent quality gate.
    gates.set_gate(tmp_path, name="quality", status="red", severity="blocker", scope="global",
                   actor="lead", evidence_source="manual_review", required=True)
    assert cli.main(["--root", str(tmp_path), "check", "--for", "worker", "--to-request", "tk",
                     "--gates", "--json"]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "current" and result["gates"]["verdict"] == "HOLD"
