"""Integration tests for the attention CLI surface: escalate typed flags -> nested
meta.attention (strict CLI validation, exit 2 on malformed, no Store.send rejection)."""
from __future__ import annotations

from pathlib import Path

import pytest

from agenttalk import cli
from agenttalk.store import Store


_DISPOSITION_RACE_LOCK_TIMEOUT = 60.0


def _team(tmp_path: Path) -> Store:
    s = Store(tmp_path)
    s.init(["beta", "claude"])
    s.set_operator_facing("claude")     # so escalate from beta resolves target=claude
    return s


def _root(tmp_path: Path):
    return ["--root", str(tmp_path)]


def _esc_meta(tmp_path: Path) -> dict | None:
    for m in Store(tmp_path).valid_messages():
        if (m.meta or {}).get("needs_operator") == "true":
            return m.meta
    return None


def test_escalate_typed_flags_write_nested_attention(tmp_path: Path) -> None:
    _team(tmp_path)
    rc = cli.main([*_root(tmp_path), "escalate", "--from", "beta", "--quiet",
                   "-m", "need a call", "--decision", "ship or hold?",
                   "--why", "release gate", "--option", "ship", "--option", "hold",
                   "--recommendation", "hold", "--risk-severity", "high",
                   "--priority", "urgent", "--needed-by", "2026-07-03"])
    assert rc == 0
    meta = _esc_meta(tmp_path)
    assert meta is not None and "attention" in meta
    att = meta["attention"]
    assert att["schema_version"] == 1 and att["decision"] == "ship or hold?"
    assert att["options"] == ["ship", "hold"] and att["priority"] == "urgent"
    assert att["risk_severity"] == "high" and att["needed_by"] == "2026-07-03"


def test_escalate_malformed_typed_field_exits_2_no_message(tmp_path: Path) -> None:
    _team(tmp_path)
    # a semantically-invalid needed_by is caught by MY validator -> exit 2, no write
    rc = cli.main([*_root(tmp_path), "escalate", "--from", "beta", "--quiet",
                   "-m", "x", "--needed-by", "not-a-real-date"])
    assert rc == 2
    assert _esc_meta(tmp_path) is None            # nothing written on the malformed escalate
    # an argparse-level invalid choice exits via SystemExit(2) before our code runs
    with pytest.raises(SystemExit):
        cli.main([*_root(tmp_path), "escalate", "--from", "beta", "-m", "x",
                  "--priority", "whenever-ish"])


def test_escalate_untyped_still_valid(tmp_path: Path) -> None:
    _team(tmp_path)
    rc = cli.main([*_root(tmp_path), "escalate", "--from", "beta", "--quiet", "-m", "just asking"])
    assert rc == 0
    meta = _esc_meta(tmp_path)
    assert meta is not None and "attention" not in meta      # untyped escalation, no block


def _escalate(tmp_path: Path, **kw) -> str:
    args = [*_root(tmp_path), "escalate", "--from", "beta", "--quiet", "-m", "call"]
    for k, v in kw.items():
        args += [f"--{k.replace('_', '-')}", v]
    cli.main(args)
    return _esc_meta(tmp_path)["request_id"]


def test_attention_view_and_liaison_disposition_flow(tmp_path: Path) -> None:
    _team(tmp_path)
    rid = _escalate(tmp_path, decision="ship or hold?", priority="urgent", risk_severity="high")
    item = f"needs_operator:{rid}"
    # liaison defers it -> hidden from the default view, visible with --all
    assert cli.main([*_root(tmp_path), "attention", "defer", "--from", "claude",
                     "--item", item, "--until", "2099-01-01T00:00:00Z", "--reason", "later"]) == 0
    from agenttalk import attention as A
    s = Store(tmp_path)
    items = cli._collect_attention_items(s, for_agent="claude", roster=["beta", "claude"])
    disps, _ = A.read_dispositions(s)
    q = A.build_queue(items, disps, now_iso="2026-06-01T00:00:00Z")
    assert not [i for i in q["items"] if i["item_id"] == item]        # deferred -> hidden
    q_all = A.build_queue(items, disps, now_iso="2026-06-01T00:00:00Z", include_deferred=True)
    assert [i for i in q_all["items"] if i["item_id"] == item]        # --include-deferred shows it


def test_attention_defer_rejects_malformed_until(tmp_path: Path) -> None:
    # codex F2 write side: a malformed --until is refused at the CLI (exit 2, nothing
    # written), so it can never be persisted and later hide a blocking item.
    _team(tmp_path)
    rid = _escalate(tmp_path, decision="d")
    rc = cli.main([*_root(tmp_path), "attention", "defer", "--from", "claude",
                   "--item", f"needs_operator:{rid}", "--until", "zzzz", "--reason", "later"])
    assert rc == 2
    from agenttalk import attention as A
    valid, _ = A.read_dispositions(Store(tmp_path))
    assert valid == []                     # nothing persisted


def test_attention_show_deferred_item_by_id(tmp_path: Path) -> None:
    # fable-max #1: a DISPOSITIONED item must be auditable via `show --item` + an include flag.
    import io
    from contextlib import redirect_stdout
    _team(tmp_path)
    rid = _escalate(tmp_path, decision="ship or hold?")
    item = f"needs_operator:{rid}"
    assert cli.main([*_root(tmp_path), "attention", "defer", "--from", "claude",
                     "--item", item, "--until", "2099-01-01T00:00:00Z", "--reason", "later"]) == 0
    # default show cannot see the deferred item
    assert cli.main([*_root(tmp_path), "attention", "show", "--item", item]) == 1
    # --include-deferred (and --all) make it auditable by id
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main([*_root(tmp_path), "attention", "show", "--item", item,
                       "--include-deferred", "--json"])
    assert rc == 0
    import json as _json
    q = _json.loads(buf.getvalue())
    assert len(q["items"]) == 1 and q["items"][0]["state"] == "deferred"
    assert cli.main([*_root(tmp_path), "attention", "show", "--item", item, "--all"]) == 0


def test_attention_stats_view(tmp_path: Path) -> None:
    # north-star CLI: --stats reports derived counts (surfaced + dispositioned) as JSON.
    import io
    from contextlib import redirect_stdout
    _team(tmp_path)
    r1 = _escalate(tmp_path, decision="a")
    _escalate(tmp_path, decision="b")           # a second active escalation
    # defer one -> dispositioned; the other stays active
    assert cli.main([*_root(tmp_path), "attention", "defer", "--from", "claude",
                     "--item", f"needs_operator:{r1}", "--until", "2099-01-01T00:00:00Z",
                     "--reason", "later"]) == 0
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main([*_root(tmp_path), "attention", "--stats", "--json"])
    assert rc == 0
    import json as _json
    s = _json.loads(buf.getvalue())["stats"]
    assert s["surfaced_active"] == 1                       # r2 active (r1 deferred)
    assert s["active_by_source"].get("needs_operator") == 1
    assert s["dispositioned"]["deferred"] == 1


def test_attention_stats_surfaces_torn_disposition_warning(tmp_path: Path) -> None:
    # F8 (reviewer-1): --stats must carry the SAME degraded-input warnings as the queue view,
    # so a torn disposition log can never make a stats read look complete.
    import io
    import json as _json
    from contextlib import redirect_stdout
    from agenttalk import attention as A
    s = _team(tmp_path)
    dp = A.dispositions_path(s)                             # append a torn/invalid line
    dp.parent.mkdir(parents=True, exist_ok=True)
    dp.write_text("{not valid json\n", encoding="utf-8")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main([*_root(tmp_path), "attention", "--stats", "--json"])
    assert rc == 0
    out = _json.loads(buf.getvalue())
    assert any("disposition_log" in w for w in out.get("warnings", []))


def test_attention_stats_surfaces_no_liaison_warning(tmp_path: Path) -> None:
    # F8 (reviewer-1): --stats carries the no_liaison warning too (parity with the queue view),
    # so counts never look complete while the needs_operator source is being skipped.
    import io
    import json as _json
    from contextlib import redirect_stdout
    s = Store(tmp_path)
    s.init(["beta", "claude"])                              # no roles -> no liaison / no sole-lead
    assert s.operator_facing() is None and s.sole_lead() is None
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main([*_root(tmp_path), "attention", "--stats", "--json"])
    assert rc == 0
    out = _json.loads(buf.getvalue())
    assert out["for"] is None
    assert any("no_liaison" in w for w in out.get("warnings", []))


def test_attention_dismiss_forbidden_for_needs_operator(tmp_path: Path) -> None:
    _team(tmp_path)
    rid = _escalate(tmp_path, decision="d")
    rc = cli.main([*_root(tmp_path), "attention", "dismiss", "--from", "claude",
                   "--item", f"needs_operator:{rid}", "--reason", "nah"])
    assert rc == 2            # needs_operator is blocking - dismiss refused (gate 7)


def test_attention_disposition_requires_authorized_actor(tmp_path: Path) -> None:
    _team(tmp_path)
    rid = _escalate(tmp_path, decision="d")
    # beta is not the liaison -> exit 2, no disposition written
    rc = cli.main([*_root(tmp_path), "attention", "answered-elsewhere", "--from", "beta",
                   "--item", f"needs_operator:{rid}", "--reason", "answered in standup"])
    assert rc == 2
    from agenttalk import attention as A
    valid, _ = A.read_dispositions(Store(tmp_path))
    assert valid == []


def test_attention_unknown_item_exits_2(tmp_path: Path) -> None:
    _team(tmp_path)
    rc = cli.main([*_root(tmp_path), "attention", "defer", "--from", "claude",
                   "--item", "needs_operator:does-not-exist", "--until", "2099-01-01", "--reason", "x"])
    assert rc == 2


def test_attention_readonly_surfaces_without_liaison_with_warning(tmp_path: Path) -> None:
    # codex F4: with NO operator_facing and NO sole-lead, the READ-ONLY view must still
    # SURFACE the global sources + a no_liaison warning (exit 0), not hard-exit 2. Only the
    # per-recipient needs_operator branch is skipped; the disposition WRITES still gate on an
    # authorized actor (covered by test_attention_disposition_requires_authorized_actor).
    import io
    from contextlib import redirect_stdout
    s = Store(tmp_path)
    s.init(["beta", "claude"])   # no roles -> sole_lead() is None; no liaison set
    assert s.operator_facing() is None and s.sole_lead() is None
    s.write_config_blocked_hold("beta", summary="a global source needing no for-agent")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main([*_root(tmp_path), "attention", "--json"])
    assert rc == 0
    import json as _json
    q = _json.loads(buf.getvalue())
    assert q["for"] is None
    assert any("no_liaison" in w for w in q.get("warnings", []))            # warning present
    assert [i for i in q["items"] if i["source"] == "config_blocked"]       # global source shown


def test_attention_disposition_still_exits_2_without_liaison(tmp_path: Path) -> None:
    # the read-only relaxation must NOT relax the WRITE path: a disposition with no authorized
    # actor still exits 2 (gate 5).
    s = Store(tmp_path)
    s.init(["beta", "claude"])
    s.write_config_blocked_hold("beta", summary="x")
    rc = cli.main([*_root(tmp_path), "attention", "defer", "--from", "beta",
                   "--item", "config_blocked:beta", "--until", "2099-01-01", "--reason", "later"])
    assert rc == 2


def test_config_blocked_defer_then_different_fault_resurfaces_via_cli(tmp_path: Path) -> None:
    # end-to-end: a config-blocked hold is deferred by the liaison, then the SAME agent hits
    # a DIFFERENT fault (new summary). The snapshot-bound disposition (gate 1) must not hide
    # the new fault - it resurfaces in the CLI queue.
    s = _team(tmp_path)
    s.write_config_blocked_hold("beta", summary="missing ANTHROPIC_API_KEY")
    item = "config_blocked:beta"
    assert cli.main([*_root(tmp_path), "attention", "defer", "--from", "claude",
                     "--item", item, "--until", "2099-01-01T00:00:00Z",
                     "--reason", "operator aware of the key"]) == 0

    from agenttalk import attention as A
    # deferred -> hidden while the fault is unchanged
    items = cli._collect_attention_items(s, for_agent="claude", roster=["beta", "claude"])
    disps, _ = A.read_dispositions(s)
    q = A.build_queue(items, disps, now_iso="2026-06-01T00:00:00Z")
    assert not [i for i in q["items"] if i["item_id"] == item]

    # a DIFFERENT fault for the same agent -> new content hash -> resurfaces despite the defer
    s.write_config_blocked_hold("beta", summary="unreadable settings.json")
    items2 = cli._collect_attention_items(s, for_agent="claude", roster=["beta", "claude"])
    q2 = A.build_queue(items2, disps, now_iso="2026-06-01T00:00:00Z")
    row = [i for i in q2["items"] if i["item_id"] == item]
    assert row and row[0]["state"] == "active"


def test_resolved_dead_letter_absent_from_attention_queue(tmp_path: Path) -> None:
    # a resolved dead-letter must not resurface in the operator attention queue (CLI).
    from agenttalk.wrapper import recv_api
    s = _team(tmp_path)
    m = s.send(sender="claude", recipient="beta", body="poison", kind="message", meta={})
    rec = recv_api.next_record(s, "beta")
    s.dead_letter("beta", rec, reason="deterministic failure",
                  failure_class="poison_eligible", at="2026-07-02T00:00:00Z")
    dl_item = f"dead_letter:beta:{m.id}"
    from agenttalk import attention as A
    # before resolve: present in the queue
    items = cli._collect_attention_items(s, for_agent="claude", roster=["beta", "claude"])
    q = A.build_queue(items, A.read_dispositions(s)[0], now_iso="2026-06-01T00:00:00Z")
    assert [i for i in q["items"] if i["item_id"] == dl_item]
    # resolve it, then rebuild: gone
    assert cli.main([*_root(tmp_path), "dead-letter", "resolve", "--from", "claude",
                     "--agent", "beta", "--id", m.id, "--reason", "handled offline"]) == 0
    items2 = cli._collect_attention_items(s, for_agent="claude", roster=["beta", "claude"])
    q2 = A.build_queue(items2, A.read_dispositions(s)[0], now_iso="2026-06-01T00:00:00Z")
    assert not [i for i in q2["items"] if i["item_id"] == dl_item]


def test_disposition_race_under_lock_preserves_all_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # concurrent disposition appends serialize under store._config_lock() - no torn lines,
    # no lost writes, and the fail-safe reader finds every event.
    import threading

    from agenttalk import attention as A
    s = _team(tmp_path)
    n = 24
    start = threading.Barrier(n)
    errors: list[Exception] = []
    real_config_lock = s._config_lock

    def _config_lock_with_test_budget(
        *, timeout: float = _DISPOSITION_RACE_LOCK_TIMEOUT, poll: float = 0.05,
    ):
        return real_config_lock(timeout=timeout, poll=poll)

    monkeypatch.setattr(s, "_config_lock", _config_lock_with_test_budget)

    def _append(i: int) -> None:
        try:
            start.wait(timeout=_DISPOSITION_RACE_LOCK_TIMEOUT)
            A.append_disposition(s, {
                "schema_version": A.SCHEMA_VERSION, "event_id": f"att-race{i:04d}",
                "item_id": f"needs_operator:rid-{i}", "source": A.SOURCE_NEEDS_OPERATOR,
                "action": A.ACTION_DEFER, "actor": "claude", "reason": "race",
                "at": "2026-07-02T00:00:00Z", "until": "2099-01-01T00:00:00Z",
                "source_snapshot": {"source_hash": f"h{i}", "refs": []}})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_append, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    valid, problems = A.read_dispositions(s)
    assert problems == []                                  # no torn lines
    assert {e["event_id"] for e in valid} == {f"att-race{i:04d}" for i in range(n)}


# ----------------------------------------------------------- cluster C: projection completeness

def test_capacity_tripped_surfaces_and_below_threshold_is_silent(tmp_path: Path) -> None:
    # codex F3: threshold-tripped capacity surfaces; routine headroom does not flood the queue.
    from agenttalk import attention as A
    s = _team(tmp_path)
    s.write_capacity("beta", {"source_agent": "beta", "observed_at": "2026-07-02T00:00:00Z",
                              "source": "claude_statusline", "context_used_percent": 95.0})
    items = cli._collect_attention_items(s, for_agent="claude", roster=["beta", "claude"])
    assert [i for i in items if i["source"] == A.SOURCE_CAPACITY]
    s.write_capacity("beta", {"source_agent": "beta", "observed_at": "2026-07-02T00:00:00Z",
                              "source": "claude_statusline", "context_used_percent": 10.0})
    items2 = cli._collect_attention_items(s, for_agent="claude", roster=["beta", "claude"])
    assert not [i for i in items2 if i["source"] == A.SOURCE_CAPACITY]


def _publish_hold_close(s, close_id: str) -> None:
    from agenttalk import close as close_mod
    rec = close_mod.empty_close(close_id, scope="release", revision="a" * 40,
                                revision_kind="sha", gate_scope="release", opened_by="claude",
                                opened_at="2026-07-02T00:00:00Z", epoch_at_open=None,
                                required_lenses=[], revision_clean=True, dirty_artifact=None)
    rec = close_mod.record_publish(rec, verdict=close_mod.VERDICT_HOLD, by="claude",
                                   at="2026-07-02T00:00:00Z", reason="gate not green",
                                   gate_check={"verdict": "HOLD", "blockers": [{"name": "tests"}]},
                                   residual_risk=None, barrier_epoch=None)
    close_mod.create_close(s, rec)


def test_published_hold_close_surfaces_and_malformed_degrades(tmp_path: Path) -> None:
    # codex F3: a PUBLISHED-HOLD close surfaces (cheap read of the snapshot, no gate recompute);
    # a malformed close record degrades to a bounded source_error, never crashing the queue.
    from agenttalk import attention as A, close as close_mod
    s = _team(tmp_path)
    _publish_hold_close(s, "cl-hold-1")
    items = cli._collect_attention_items(s, for_agent="claude", roster=["beta", "claude"])
    assert [i for i in items if i["item_id"] == "close_hold:cl-hold-1"]
    # a torn/malformed close record -> degraded warning, queue still built
    (close_mod.closes_dir(s) / "cl-bad.json").write_text("{not json", encoding="utf-8")
    items2 = cli._collect_attention_items(s, for_agent="claude", roster=["beta", "claude"])
    assert [i for i in items2 if i["source"] == A.SOURCE_ERROR
            and "close_hold" in i["item_id"]]


# ----------------------------------------------------------- cluster C: wrapper-notice coalescing (F6)

def _dl_message(s, body: str = "poison"):
    from agenttalk.wrapper import recv_api
    m = s.send(sender="claude", recipient="beta", body=body, kind="message", meta={})
    rec = recv_api.next_record(s, "beta")
    s.dead_letter("beta", rec, reason="deterministic", failure_class="poison_eligible",
                  at="2026-07-02T00:00:00Z")
    return m.id


def test_wrapper_dead_letter_notice_coalesced_into_sink_row(tmp_path: Path) -> None:
    # reviewer-2 F6: the REAL wrapper notifier emits a needs_operator TWIN of a dead-lettered
    # message. It must coalesce into the canonical sink row, and resolving the sink row must
    # remove BOTH (not orphan the twin). Uses the real _dead_letter_notifier metadata.
    from agenttalk import attention as A
    s = _team(tmp_path)
    mid = _dl_message(s)
    emit = cli._dead_letter_notifier(s, "beta")
    assert emit({"msg_id": mid, "agent": "beta", "from": "claude", "kind": "message",
                 "attempts": 3, "failure_class": "poison_eligible"}, disposed=True) is True
    items = cli._collect_attention_items(s, for_agent="claude", roster=["beta", "claude"])
    assert not [i for i in items if i["source"] == A.SOURCE_NEEDS_OPERATOR]   # twin coalesced
    assert [i for i in items if i["item_id"] == f"dead_letter:beta:{mid}"]    # canonical remains
    # resolve the sink row -> BOTH gone from the active queue
    assert cli.main([*_root(tmp_path), "dead-letter", "resolve", "--from", "claude",
                     "--agent", "beta", "--id", mid, "--reason", "handled"]) == 0
    items2 = cli._collect_attention_items(s, for_agent="claude", roster=["beta", "claude"])
    q2 = A.build_queue(items2, A.read_dispositions(s)[0], now_iso="2026-06-01T00:00:00Z")
    assert not [i for i in q2["items"]
                if i["source"] in (A.SOURCE_NEEDS_OPERATOR, A.SOURCE_DEAD_LETTER)]


def test_dead_letter_resolve_closes_related_wrapper_notice_thread(tmp_path: Path) -> None:
    from agenttalk import threads
    s = _team(tmp_path)
    mid = _dl_message(s)
    emit = cli._dead_letter_notifier(s, "beta")
    assert emit({"msg_id": mid, "agent": "beta", "from": "claude", "kind": "message",
                 "attempts": 3, "failure_class": "poison_eligible"}, disposed=True) is True
    notice = next(
        m for m in s.valid_messages()
        if (m.meta or {}).get("dead_letter") == "true"
        and (m.meta or {}).get("dl_msg_id") == mid
    )
    rid = notice.meta["request_id"]
    before = next(t for t in threads.derive_threads(
        s.valid_messages(), agent="claude", cursor="", closed_rids=set())
        if t.request_id == rid)
    assert before.operator_state == "pending"

    assert cli.main([*_root(tmp_path), "dead-letter", "resolve", "--from", "claude",
                     "--agent", "beta", "--id", mid, "--reason", "handled"]) == 0

    after = next(t for t in threads.derive_threads(
        s.valid_messages(), agent="claude", cursor="", closed_rids=set())
        if t.request_id == rid)
    assert after.operator_state == "answered"
    answer = next(
        m for m in s.valid_messages()
        if m.sender == "claude" and m.recipient == "beta"
        and (m.meta or {}).get("request_id") == rid
    )
    assert answer.meta["operator_answer"] == "true"
    assert answer.meta["dead_letter_resolved"] == "true"


def test_status_dead_letter_count_is_unresolved_only(tmp_path: Path) -> None:
    import io
    import json as _json
    from contextlib import redirect_stdout

    s = _team(tmp_path)
    mid = _dl_message(s)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cli.main([*_root(tmp_path), "status", "--json"]) == 0
    assert _json.loads(buf.getvalue())["dead_lettered_count"] == 1

    assert cli.main([*_root(tmp_path), "dead-letter", "resolve", "--from", "claude",
                     "--agent", "beta", "--id", mid, "--reason", "handled"]) == 0
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cli.main([*_root(tmp_path), "status", "--json"]) == 0
    assert "dead_lettered_count" not in _json.loads(buf.getvalue())


def test_dead_letter_purge_resolved_archives_payload_and_sidecars(tmp_path: Path) -> None:
    import io
    import json as _json
    from contextlib import redirect_stdout

    s = _team(tmp_path)
    mid = _dl_message(s)
    assert cli.main([*_root(tmp_path), "dead-letter", "resolve", "--from", "claude",
                     "--agent", "beta", "--id", mid, "--reason", "handled"]) == 0

    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cli.main([*_root(tmp_path), "dead-letter", "purge", "--resolved",
                         "--from", "claude", "--json"]) == 0
    out = _json.loads(buf.getvalue())
    assert out["count"] == 1
    assert not (s.dead_letter_dir / "beta" / f"{mid}.json").exists()
    archived = list((s.dir / "dead-letter-archive").glob(f"*/beta/{mid}.json"))
    assert len(archived) == 1

    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cli.main([*_root(tmp_path), "dead-letter", "list", "--all", "--json"]) == 0
    assert _json.loads(buf.getvalue()) == []


def test_dead_letter_purge_refuses_if_wrapper_notice_still_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = _team(tmp_path)
    mid = _dl_message(s)
    emit = cli._dead_letter_notifier(s, "beta")
    assert emit({"msg_id": mid, "agent": "beta", "from": "claude", "kind": "message",
                 "attempts": 3, "failure_class": "poison_eligible"}, disposed=True) is True

    original_send = Store.send

    def fail_notice_close(self, *args, **kwargs):
        meta = kwargs.get("meta") or {}
        if meta.get("dead_letter_resolved") == "true":
            raise OSError("simulated notice close failure")
        return original_send(self, *args, **kwargs)

    monkeypatch.setattr(Store, "send", fail_notice_close)

    assert cli.main([*_root(tmp_path), "dead-letter", "resolve", "--from", "claude",
                     "--agent", "beta", "--id", mid, "--reason", "handled"]) == 0
    assert cli.main([*_root(tmp_path), "dead-letter", "purge", "--resolved",
                     "--from", "claude"]) == 2
    assert (s.dead_letter_dir / "beta" / f"{mid}.json").exists()


def test_dead_letter_purge_dry_run_does_not_move_resolved_payload(tmp_path: Path) -> None:
    import io
    import json as _json
    from contextlib import redirect_stdout

    s = _team(tmp_path)
    mid = _dl_message(s)
    assert cli.main([*_root(tmp_path), "dead-letter", "resolve", "--from", "claude",
                     "--agent", "beta", "--id", mid, "--reason", "handled"]) == 0
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cli.main([*_root(tmp_path), "dead-letter", "purge", "--resolved",
                         "--from", "claude", "--dry-run", "--json"]) == 0
    out = _json.loads(buf.getvalue())
    assert out["dry_run"] is True
    assert out["count"] == 1
    assert (s.dead_letter_dir / "beta" / f"{mid}.json").exists()


def test_dead_letter_purge_resolved_leaves_unresolved_items_live(tmp_path: Path) -> None:
    import io
    import json as _json
    from contextlib import redirect_stdout

    s = _team(tmp_path)
    resolved_mid = _dl_message(s, body="resolved poison")
    unresolved_mid = _dl_message(s, body="still live poison")
    assert cli.main([*_root(tmp_path), "dead-letter", "resolve", "--from", "claude",
                     "--agent", "beta", "--id", resolved_mid,
                     "--reason", "handled"]) == 0

    assert cli.main([*_root(tmp_path), "dead-letter", "purge", "--resolved",
                     "--from", "claude"]) == 0

    assert not (s.dead_letter_dir / "beta" / f"{resolved_mid}.json").exists()
    assert (s.dead_letter_dir / "beta" / f"{unresolved_mid}.json").exists()
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cli.main([*_root(tmp_path), "dead-letter", "list", "--json"]) == 0
    live = _json.loads(buf.getvalue())
    assert [m["message_id"] for m in live] == [unresolved_mid]


def test_wrapper_notice_kept_when_no_canonical_row(tmp_path: Path) -> None:
    # a not-yet-disposed dead-letter notice (dl_disposed=false, NO sink row) is the SOLE
    # signal and must be KEPT (fail-safe: coalesce only a proven-redundant twin).
    from agenttalk import attention as A
    s = _team(tmp_path)
    emit = cli._dead_letter_notifier(s, "beta")
    assert emit({"msg_id": "20260101-000000-000000-aaaa", "agent": "beta", "from": "claude",
                 "kind": "message", "attempts": 2, "failure_class": "poison_eligible"},
                disposed=False) is True
    items = cli._collect_attention_items(s, for_agent="claude", roster=["beta", "claude"])
    assert [i for i in items if i["source"] == A.SOURCE_NEEDS_OPERATOR]       # kept


def test_wrapper_config_blocked_notice_coalesced_when_hold_exists(tmp_path: Path) -> None:
    from agenttalk import attention as A
    s = _team(tmp_path)
    s.write_config_blocked_hold("beta", summary="exec denied")
    emit = cli._dead_letter_notifier(s, "beta")
    assert emit({"msg_id": "x", "agent": "beta", "from": "claude", "kind": "message",
                 "attempts": 1, "failure_class": "config_blocked", "summary": "exec denied"},
                disposed=False) is True
    items = cli._collect_attention_items(s, for_agent="claude", roster=["beta", "claude"])
    assert not [i for i in items if i["source"] == A.SOURCE_NEEDS_OPERATOR]   # twin coalesced
    assert [i for i in items if i["source"] == A.SOURCE_CONFIG_BLOCKED]       # canonical hold row
