"""Phase A of the wrapper integration (design C): the machine-readable recv API.

The wrapper consumes its inbox via recv_api IN-PROCESS (never by shell-parsing
human output). These tests pin the structured schema (incl correlation_id) and the
EXACT cursor semantics the API must MIRROR, not change:
  * GLOBAL = consuming: floor at store.cursor; commit advances the GLOBAL cursor.
  * SCOPED = --to-request: floor at max(thread_seen, cursor); commit advances ONLY
    the per-thread seen pointer, NEVER the global cursor, NEVER closes the thread.
"""

from __future__ import annotations

import json

from agenttalk import cli
from agenttalk.store import Store
from agenttalk.wrapper import recv_api


def _store(tmp_path) -> Store:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    return s


# --------------------------------------------------------------- schema

def test_record_schema_lifts_ids_and_correlation(tmp_path) -> None:
    s = _store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="hi", subject="s",
           meta={"request_id": "rq-1"})
    rec = recv_api.next_record(s, "beta")
    assert rec is not None
    assert rec["from"] == "alpha" and rec["to"] == "beta"
    assert rec["kind"] == "message" and rec["subject"] == "s" and rec["body"] == "hi"
    assert rec["request_id"] == "rq-1" and rec["broadcast_id"] is None
    assert rec["correlation_id"] == "rq-1"           # request_id wins
    assert rec["meta"] == {"request_id": "rq-1"}      # meta returned unchanged
    assert rec["mode"] == "global"
    assert rec["cursor"]["before"] == "" and rec["cursor"]["after"] == rec["id"]


def test_correlation_id_from_broadcast(tmp_path) -> None:
    s = _store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="b",
           meta={"broadcast_id": "bc-9", "audience": "all"})
    rec = recv_api.next_record(s, "beta")
    assert rec["request_id"] is None and rec["broadcast_id"] == "bc-9"
    assert rec["correlation_id"] == "bc-9"            # falls back to broadcast_id


def test_control_kinds_excluded(tmp_path) -> None:
    s = _store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="real")
    s.send(sender="alpha", recipient="beta", body="", kind="composing",
           meta={"request_id": "rq-1"})
    recs = recv_api.records(s, "beta")
    assert all(r["kind"] != "composing" for r in recs)
    assert [r["body"] for r in recs] == ["real"]


# --------------------------------------------------- GLOBAL cursor semantics

def test_global_floor_and_commit_advances_global_cursor(tmp_path) -> None:
    s = _store(tmp_path)
    m1 = s.send(sender="alpha", recipient="beta", body="one")
    m2 = s.send(sender="alpha", recipient="beta", body="two")
    # floor at the (empty) global cursor -> both unread, oldest first.
    recs = recv_api.records(s, "beta")
    assert [r["id"] for r in recs] == [m1.id, m2.id]
    # consume the first: GLOBAL commit advances the global cursor.
    recv_api.commit(s, "beta", recs[0])
    assert s.cursor("beta") == m1.id
    # now only the second is unread.
    assert [r["id"] for r in recv_api.records(s, "beta")] == [m2.id]


# --------------------------------------------------- SCOPED cursor semantics

def test_scoped_commit_marks_thread_seen_only_not_global_cursor(tmp_path) -> None:
    s = _store(tmp_path)
    # an unrelated global message + two on the rq-7 thread.
    s.send(sender="alpha", recipient="beta", body="unrelated")
    t1 = s.send(sender="alpha", recipient="beta", body="t1", meta={"request_id": "rq-7"})
    t2 = s.send(sender="alpha", recipient="beta", body="t2", meta={"request_id": "rq-7"})
    rec = recv_api.next_record(s, "beta", scoped_request_id="rq-7")
    assert rec["mode"] == "scoped" and rec["id"] == t1.id
    assert rec["scoped"]["request_id"] == "rq-7" and rec["scoped"]["seen_before"] == ""
    assert rec["scoped"]["closed"] is False and rec["scoped"]["superseded"] is False
    # scoped commit advances ONLY the thread pointer - NEVER the global cursor.
    recv_api.commit(s, "beta", rec)
    assert s.thread_seen("beta", "rq-7") == t1.id
    assert s.cursor("beta") == "", "scoped recv must NOT eat the global inbox cursor"
    # next scoped record is the second thread message; the unrelated global msg is
    # still unread on the GLOBAL cursor (untouched).
    assert recv_api.next_record(s, "beta", scoped_request_id="rq-7")["id"] == t2.id
    assert any(r["body"] == "unrelated" for r in recv_api.records(s, "beta"))


def test_scoped_floor_respects_global_cursor(tmp_path) -> None:
    # floor = max(thread_seen, global cursor): a message already below the GLOBAL
    # cursor is not re-handed-out by a scoped read.
    s = _store(tmp_path)
    t1 = s.send(sender="alpha", recipient="beta", body="t1", meta={"request_id": "rq-7"})
    t2 = s.send(sender="alpha", recipient="beta", body="t2", meta={"request_id": "rq-7"})
    s.advance_cursor("beta", t1.id)                  # global cursor now past t1
    rec = recv_api.next_record(s, "beta", scoped_request_id="rq-7")
    assert rec["id"] == t2.id                         # t1 is below the floor


# --------------------------------------------------- recv --json CLI mirror

def test_recv_json_cli_mirror(tmp_path, capsys) -> None:
    s = _store(tmp_path)
    s.send(sender="alpha", recipient="beta", body="one", meta={"request_id": "rq-1"})
    m2 = s.send(sender="alpha", recipient="beta", body="two")
    rc = cli.main(["--root", str(tmp_path), "recv", "--for", "beta", "--json"])
    assert rc == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    recs = [json.loads(ln) for ln in lines]
    assert [r["body"] for r in recs] == ["one", "two"]
    assert recs[0]["correlation_id"] == "rq-1" and recs[0]["mode"] == "global"
    # default recv --json only peeks (no --ack) -> cursor unmoved.
    assert s.cursor("beta") == ""
    # --ack advances the global cursor past the last message (newest id).
    rc = cli.main(["--root", str(tmp_path), "recv", "--for", "beta", "--json", "--ack"])
    assert rc == 0
    assert s.cursor("beta") == m2.id
