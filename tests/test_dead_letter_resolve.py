"""dead-letter resolve / resolved-aware list + requeue (0.56.0).

`resolve` is an operator decision DISTINCT from dismiss and from requeue: it records that
a dead-letter was handled out-of-band, preserving the payload. The central disposition log
(.agenttalk/attention/dispositions.jsonl) is authoritative; a .resolved.json sidecar is
best-effort for copied-sink readability. A resolved dead-letter drops out of the default
`list`, out of the doctor dead-letter WARN, and out of the attention queue; requeueing it
requires an explicit --force-resolved --reason (which appends a requeued_after_resolve event
that reopens it). Resolution state survives reset (dispositions live under attention/).
"""

from __future__ import annotations

import json
import inspect
from pathlib import Path

import pytest

from agenttalk import cli, doctor
from agenttalk.store import Store
from agenttalk.wrapper import recv_api


def _store(tmp_path: Path) -> Store:
    s = Store(tmp_path)
    s.init(["claude", "beta"])
    s.set_operator_facing("claude")  # claude = operator-facing liaison
    return s


def _dead_letter(s: Store, body: str = "poison", agent: str = "beta") -> str:
    """Send a real message, take it off the head, and dead-letter it. Returns its id."""
    m = s.send(sender="claude", recipient=agent, body=body, kind="message", meta={})
    rec = recv_api.next_record(s, agent)
    assert rec["id"] == m.id
    s.dead_letter(agent, rec, reason="turn failed deterministically",
                  failure_class="poison_eligible", at="2026-07-02T00:00:00Z")
    return m.id


def _run(root: Path, *argv: str) -> int:
    return cli.main(["--root", str(root), *argv])


@pytest.mark.parametrize("disposed", [False, True])
def test_resolve_closes_notice_and_repeat_changes_nothing(tmp_path, disposed):
    from agenttalk import threads
    s = _store(tmp_path)
    mid = _dead_letter(s)
    emit = cli._dead_letter_notifier(s, "beta")
    assert emit({"msg_id": mid, "agent": "beta", "from": "claude", "kind": "message",
                 "attempts": 3, "failure_class": "poison_eligible"}, disposed=disposed)
    notice = next(m for m in s.valid_messages() if m.kind == "question")
    rid = notice.meta["request_id"]
    command = ("dead-letter", "resolve", "--from", "claude", "--agent", "beta", "--id", mid,
               "--reason", "handled out of band")
    assert _run(tmp_path, *command) == 0
    rows = threads.derive_threads(s.valid_messages(), agent="claude", cursor="")
    assert next(t for t in rows if t.request_id == rid).operator_state == "answered"
    # Include sidecars, the audit log, bus, and cursor state in the no-op check.
    before = {p.relative_to(s.dir): p.read_bytes() for p in s.dir.rglob("*") if p.is_file()}
    assert _run(tmp_path, *command) == 0
    after = {p.relative_to(s.dir): p.read_bytes() for p in s.dir.rglob("*") if p.is_file()}
    assert before == after


def test_resolve_requires_liaison_authority(tmp_path: Path) -> None:
    s = _store(tmp_path)
    mid = _dead_letter(s)
    # beta is not the operator-facing liaison -> exit 2, no disposition written
    rc = _run(tmp_path, "dead-letter", "resolve", "--from", "beta",
              "--agent", "beta", "--id", mid, "--reason", "not authorized")
    assert rc == 2
    assert cli._dead_letter_resolution_state(s).get(("beta", mid)) is None


def test_resolve_retries_only_pending_notices_without_rewriting_decision(tmp_path, monkeypatch):
    from agenttalk import attention, threads
    s = _store(tmp_path)
    mid = _dead_letter(s)
    emit = cli._dead_letter_notifier(s, "beta")
    info = {"msg_id": mid, "agent": "beta", "from": "claude", "kind": "message",
            "attempts": 3, "failure_class": "poison_eligible"}
    assert emit(info, disposed=False)
    assert emit(info, disposed=True)
    assert emit(dict(info, msg_id="unrelated"), disposed=False)
    pending = [m for m in s.valid_messages() if m.kind == "question" and m.meta["dl_msg_id"] == mid]
    assert len(pending) == 2
    command = ("dead-letter", "resolve", "--from", "claude", "--agent", "beta", "--id", mid,
               "--reason", "handled")
    real_send = Store.send

    def interrupted_send(store, **kwargs):
        if kwargs.get("meta", {}).get("request_id") == pending[1].meta["request_id"]:
            raise OSError("notice publication interrupted")
        return real_send(store, **kwargs)

    monkeypatch.setattr(Store, "send", interrupted_send)
    assert _run(tmp_path, *command) == 0
    decisions = attention.read_dispositions(s)
    sidecar = (s.dead_letter_dir / "beta" / f"{mid}.resolved.json").read_bytes()
    monkeypatch.setattr(Store, "send", real_send)
    assert _run(tmp_path, *command) == 0
    assert attention.read_dispositions(s) == decisions
    assert (s.dead_letter_dir / "beta" / f"{mid}.resolved.json").read_bytes() == sidecar
    answers = [m for m in s.valid_messages() if m.meta.get("dead_letter_resolved") == "true"]
    assert len(answers) == 2
    assert {m.meta["request_id"] for m in answers} == {m.meta["request_id"] for m in pending}
    rows = threads.derive_threads(s.valid_messages(), agent="claude", cursor="")
    assert sum(t.operator_state == "answered" for t in rows) == 2
    assert sum(t.operator_state == "pending" for t in rows) == 1


def test_resolve_requires_reason(tmp_path: Path) -> None:
    s = _store(tmp_path)
    mid = _dead_letter(s)
    # --reason is argparse-required; a whitespace-only reason passes argparse but the
    # runtime check rejects it (clean exit 2, no disposition written).
    rc = _run(tmp_path, "dead-letter", "resolve", "--from", "claude",
              "--agent", "beta", "--id", mid, "--reason", "   ")
    assert rc == 2
    assert cli._dead_letter_resolution_state(s).get(("beta", mid)) is None


@pytest.mark.parametrize("operation", ["resolve", "purge"])
@pytest.mark.parametrize("notice_target", ["claude", "liaison-b"])
def test_notice_closure_preserves_recorded_actor_across_callers(tmp_path, monkeypatch, operation, notice_target):
    from agenttalk import attention as A, threads
    s = Store(tmp_path)
    s.init(["claude", "beta", "liaison-b"])
    s.set_operator_facing(notice_target)
    mid = _dead_letter(s)
    assert cli._dead_letter_notifier(s, "beta")({
        "msg_id": mid, "agent": "beta", "from": "claude", "kind": "message",
        "attempts": 3, "failure_class": "poison_eligible",
    }, disposed=False)
    s.set_operator_facing("claude")
    real_send = Store.send

    def interrupted_send(store, **kwargs):
        if kwargs.get("meta", {}).get("dead_letter_resolved") == "true":
            raise OSError("notice send failed")
        return real_send(store, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Store, "send", interrupted_send)
        assert _run(tmp_path, "dead-letter", "resolve", "--from", "claude", "--agent", "beta", "--id", mid,
                    "--reason", "original decision", "--evidence", "original evidence") == 0
    before = A.dispositions_path(s).read_bytes()
    s.set_operator_facing("liaison-b")
    if operation == "resolve":
        assert _run(tmp_path, "dead-letter", "resolve", "--from", "liaison-b", "--agent", "beta", "--id", mid,
                    "--reason", "original decision", "--evidence", "original evidence") == 0
    else:
        assert _run(tmp_path, "dead-letter", "purge", "--resolved", "--from", "liaison-b") == 0
    answers = [m for m in s.valid_messages() if m.meta.get("dead_letter_resolved") == "true"]
    assert len(answers) == 1
    answer = answers[0]
    assert answer.sender == notice_target  # answer the original question's thread
    assert answer.meta["operator_origin"] == "claude"
    assert answer.meta["dead_letter_evidence"] == "original evidence"
    assert answer.body == f"Dead-letter beta/{mid} was resolved by claude: original decision"
    assert A.dispositions_path(s).read_bytes() == before
    rows = threads.derive_threads(s.valid_messages(), agent=notice_target, cursor="")
    assert next(t for t in rows if t.opener_kind == "question").operator_state == "answered"


def test_notice_closure_without_recorded_resolution_does_nothing(tmp_path, capsys):
    s = _store(tmp_path)
    mid = _dead_letter(s)
    assert cli._dead_letter_notifier(s, "beta")({
        "msg_id": mid, "agent": "beta", "from": "claude", "kind": "message",
        "attempts": 3, "failure_class": "poison_eligible",
    }, disposed=False)
    before = s.valid_messages()
    # A sidecar alone cannot supply attribution; only the central decision counts.
    (s.dead_letter_dir / "beta" / f"{mid}.resolved.json").write_text(
        json.dumps({"actor": "claude", "reason": "sidecar only"}), encoding="utf-8",
    )
    recorded = cli._recorded_dead_letter_resolution(s, agent="beta", msg_id=mid)
    assert recorded is None
    assert cli._close_dead_letter_notice_threads(s, agent="beta", msg_id=mid, resolution=recorded) == 0
    assert s.valid_messages() == before
    assert "no recorded resolution" in capsys.readouterr().err


def test_notice_closure_signature_cannot_accept_call_site_attribution():
    parameters = inspect.signature(cli._close_dead_letter_notice_threads).parameters
    assert set(parameters) == {"store", "agent", "msg_id", "resolution"}
    assert not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())


def test_resolve_then_purge_notice_to_retired_liaison(tmp_path, monkeypatch, capsys):
    from agenttalk import threads
    s = Store(tmp_path)
    s.init(["claude", "beta", "liaison-b"])
    s.set_operator_facing("liaison-b")
    mid = _dead_letter(s)
    assert cli._dead_letter_notifier(s, "beta")({
        "msg_id": mid, "agent": "beta", "from": "claude", "kind": "message",
        "attempts": 3, "failure_class": "poison_eligible",
    }, disposed=False)
    notice = next(m for m in s.valid_messages() if m.kind == "question")
    s.set_operator_facing("claude")
    s.retire_agent("liaison-b", reason="handoff")
    real_send = Store.send

    def no_retired_send(store, **kwargs):
        assert kwargs.get("sender") != "liaison-b", "must not try a retired sender"
        return real_send(store, **kwargs)

    monkeypatch.setattr(Store, "send", no_retired_send)
    capsys.readouterr()
    assert _run(tmp_path, "dead-letter", "resolve", "--from", "claude", "--agent", "beta", "--id", mid,
                "--reason", "handled") == 0
    diagnostic = f"notice {notice.id} was addressed to retired liaison-b; nothing to close"
    output = capsys.readouterr()
    assert len(output.err.splitlines()) == 1 and diagnostic in output.err
    assert _run(tmp_path, "dead-letter", "purge", "--resolved", "--from", "claude") == 0
    output = capsys.readouterr()
    assert len(output.err.splitlines()) == 1 and diagnostic in output.err
    assert s.list_dead_letters("beta") == []
    assert not any(m.meta.get("dead_letter_resolved") for m in s.valid_messages())
    # Report-only observation requested by the lead: general thread derivation
    # still leaves this point-to-point operator question open for its sender.
    rows = threads.derive_threads(s.valid_messages(), agent="beta", cursor=notice.id,
                                  retired=set(s.retired_agents()))
    row = next(t for t in rows if t.request_id == notice.meta["request_id"])
    assert row.state == "open-outbound"


@pytest.mark.parametrize("failure", [OSError("disk unavailable\ntry later"), ValueError("send rejected")])
def test_notice_send_failure_reports_notice_and_error(tmp_path, monkeypatch, capsys, failure):
    s = _store(tmp_path)
    mid = _dead_letter(s)
    assert cli._dead_letter_notifier(s, "beta")({
        "msg_id": mid, "agent": "beta", "from": "claude", "kind": "message",
        "attempts": 3, "failure_class": "poison_eligible",
    }, disposed=False)
    notice = next(m for m in s.valid_messages() if m.kind == "question")

    def fail_send(*args, **kwargs):
        raise failure

    monkeypatch.setattr(Store, "send", fail_send)
    capsys.readouterr()
    assert _run(tmp_path, "dead-letter", "resolve", "--from", "claude", "--agent", "beta", "--id", mid,
                "--reason", "handled") == 0
    output = capsys.readouterr()
    assert len(output.err.splitlines()) == 1
    assert notice.id in output.err and " ".join(str(failure).splitlines()) in output.err
    assert not any(m.meta.get("dead_letter_resolved") for m in s.valid_messages())


def test_purge_refuses_when_recorded_resolution_is_unavailable(tmp_path, monkeypatch, capsys):
    s = _store(tmp_path)
    mid = _dead_letter(s)
    assert _run(tmp_path, "dead-letter", "resolve", "--from", "claude", "--agent", "beta", "--id", mid,
                "--reason", "handled") == 0
    before = s.valid_messages()
    monkeypatch.setattr(cli, "_recorded_dead_letter_resolution", lambda *args, **kwargs: None)
    assert _run(tmp_path, "dead-letter", "purge", "--resolved", "--from", "claude") == 2
    assert "no recorded resolution" in capsys.readouterr().err
    assert s.read_dead_letter_payload("beta", mid) is not None
    assert s.valid_messages() == before


@pytest.mark.parametrize("legacy", [False, True], ids=["interrupted-send", "old-resolution"])
@pytest.mark.parametrize("retry_reason,retry_evidence,warns", [
    ("transient network blip", "original-log", False),
    ("confirmed fixed upstream", "new-log", True),
    ("transient network blip", "new-log", True),
])
def test_resolve_retry_uses_recorded_resolution(
    tmp_path, monkeypatch, capsys, legacy, retry_reason, retry_evidence, warns,
):
    from agenttalk import attention as A, threads
    s = _store(tmp_path)
    mid = _dead_letter(s)
    assert cli._dead_letter_notifier(s, "beta")({
        "msg_id": mid, "agent": "beta", "from": "claude", "kind": "message",
        "attempts": 3, "failure_class": "poison_eligible",
    }, disposed=False)
    side = s.dead_letter_dir / "beta" / f"{mid}.resolved.json"
    command = ("dead-letter", "resolve", "--from", "claude", "--agent", "beta", "--id", mid)
    if legacy:
        # On-disk shape written by the resolver before #277: a durable decision
        # and sidecar, with the early (dl_disposed=false) notice still unanswered.
        entry = next(m for m in s.list_dead_letters("beta") if m["message_id"] == mid)
        source_hash = A.dead_letter_entry_source_hash(entry)
        resolution = {
            "event_id": "att-old-resolution", "actor": "claude",
            "reason": "transient network blip", "evidence": "original-log",
            "at": "2026-10-01T00:00:00Z",
        }
        A.append_disposition(s, {
            **resolution, "schema_version": A.SCHEMA_VERSION,
            "item_id": A.item_id(A.SOURCE_DEAD_LETTER, "beta", mid),
            "source": A.SOURCE_DEAD_LETTER, "action": A.ACTION_RESOLVE_DEAD_LETTER,
            "source_snapshot": {"source_hash": source_hash,
                                "refs": [{"kind": "dead_letter", "agent": "beta", "message_id": mid}]},
        })
        side.write_text(json.dumps(dict(resolution, source_hash=source_hash)), encoding="utf-8")
    else:
        real_send = Store.send

        def interrupted_send(store, **kwargs):
            if kwargs.get("meta", {}).get("dead_letter_resolved") == "true":
                raise OSError("notice publication interrupted")
            return real_send(store, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Store, "send", interrupted_send)
            assert _run(tmp_path, *command, "--reason", "transient network blip",
                        "--evidence", "original-log") == 0
    assert not any(m.meta.get("dead_letter_resolved") for m in s.valid_messages())
    before = A.dispositions_path(s).read_bytes(), side.read_bytes()
    capsys.readouterr()
    assert _run(tmp_path, *command, "--reason", retry_reason, "--evidence", retry_evidence) == 0
    output = capsys.readouterr()
    answers = [m for m in s.valid_messages() if m.meta.get("dead_letter_resolved") == "true"]
    assert len(answers) == 1
    assert answers[0].body.endswith(": transient network blip")
    assert answers[0].meta["dead_letter_evidence"] == "original-log"
    if warns:
        assert len(output.err.splitlines()) == 1
        assert "recorded reason/evidence" in output.err
    else:
        assert output.err == ""
    assert (A.dispositions_path(s).read_bytes(), side.read_bytes()) == before
    rows = threads.derive_threads(s.valid_messages(), agent="claude", cursor="")
    assert next(t for t in rows if t.opener_kind == "question").operator_state == "answered"


def test_resolve_hides_from_default_list_and_doctor(tmp_path: Path) -> None:
    s = _store(tmp_path)
    mid = _dead_letter(s)
    # before: doctor warns, default list shows it
    assert doctor._check_dead_letter(s) is not None
    assert any(m["message_id"] == mid for m in s.list_dead_letters())

    rc = _run(tmp_path, "dead-letter", "resolve", "--from", "claude",
              "--agent", "beta", "--id", mid, "--reason", "handled out of band")
    assert rc == 0
    assert cli._dead_letter_resolution_state(s).get(("beta", mid)) == "resolved"

    # after: hidden from the default list and from the doctor WARN (all resolved -> None)
    listed = [m["message_id"] for m in s.list_dead_letters()]
    assert mid in listed  # store still HAS the payload (preserved)
    assert doctor._check_dead_letter(s) is None


def test_resolve_writes_best_effort_sidecar_but_central_is_authoritative(tmp_path: Path) -> None:
    s = _store(tmp_path)
    mid = _dead_letter(s)
    _run(tmp_path, "dead-letter", "resolve", "--from", "claude",
         "--agent", "beta", "--id", mid, "--reason", "central + sidecar")
    sidecar = s.dead_letter_dir / "beta" / f"{mid}.resolved.json"
    assert sidecar.exists()  # best-effort sidecar written
    # central log is the authority (state derives from dispositions.jsonl, not the sidecar)
    assert cli._dead_letter_resolution_state(s).get(("beta", mid)) == "resolved"


def test_resolved_visible_under_resolved_and_all_flags(tmp_path: Path, capsys) -> None:
    s = _store(tmp_path)
    mid = _dead_letter(s)
    _run(tmp_path, "dead-letter", "resolve", "--from", "claude",
         "--agent", "beta", "--id", mid, "--reason", "audit view")
    capsys.readouterr()
    assert _run(tmp_path, "dead-letter", "list") == 0
    assert "none" in capsys.readouterr().out  # default hides it
    assert _run(tmp_path, "dead-letter", "list", "--resolved") == 0
    assert mid in capsys.readouterr().out     # --resolved surfaces it
    assert _run(tmp_path, "dead-letter", "list", "--all") == 0
    assert mid in capsys.readouterr().out     # --all surfaces it


def test_requeue_resolved_refused_without_force(tmp_path: Path) -> None:
    s = _store(tmp_path)
    mid = _dead_letter(s)
    _run(tmp_path, "dead-letter", "resolve", "--from", "claude",
         "--agent", "beta", "--id", mid, "--reason", "resolved first")
    # requeue a RESOLVED item without --force-resolved -> refused (exit 2), still resolved
    assert _run(tmp_path, "dead-letter", "requeue", "--agent", "beta", "--id", mid) == 2
    assert cli._dead_letter_resolution_state(s).get(("beta", mid)) == "resolved"


def test_requeue_resolved_with_force_reopens(tmp_path: Path) -> None:
    s = _store(tmp_path)
    mid = _dead_letter(s)
    _run(tmp_path, "dead-letter", "resolve", "--from", "claude",
         "--agent", "beta", "--id", mid, "--reason", "resolved first")
    rc = _run(tmp_path, "dead-letter", "requeue", "--agent", "beta", "--id", mid,
              "--force-resolved", "--reason", "reopen: was not actually handled",
              "--from", "claude")
    assert rc == 0
    # requeued_after_resolve reopens it -> state is 'requeued', no longer 'resolved'
    assert cli._dead_letter_resolution_state(s).get(("beta", mid)) == "requeued"


def test_resolution_survives_reset(tmp_path: Path) -> None:
    s = _store(tmp_path)
    mid = _dead_letter(s)
    _run(tmp_path, "dead-letter", "resolve", "--from", "claude",
         "--agent", "beta", "--id", mid, "--reason", "persist across reset")
    s.reset()
    s2 = Store(tmp_path)
    # dispositions live under attention/, preserved by reset -> resolution state survives
    assert cli._dead_letter_resolution_state(s2).get(("beta", mid)) == "resolved"


def test_resolve_unknown_dead_letter_refused(tmp_path: Path) -> None:
    _store(tmp_path)  # roster + liaison so authority resolves; the item itself is missing
    rc = _run(tmp_path, "dead-letter", "resolve", "--from", "claude",
              "--agent", "beta", "--id", "20260101-000000-000000-zzzz",
              "--reason", "no such item")
    assert rc == 2


def test_force_requeue_resolved_requires_liaison_authority(tmp_path: Path) -> None:
    # codex F1: reopening a RESOLVED dead-letter is an authority disposition write - a
    # non-liaison must NOT be able to force-requeue it (was routed through _resolve_self).
    s = _store(tmp_path)
    mid = _dead_letter(s)
    _run(tmp_path, "dead-letter", "resolve", "--from", "claude",
         "--agent", "beta", "--id", mid, "--reason", "resolved by liaison")
    rc = _run(tmp_path, "dead-letter", "requeue", "--agent", "beta", "--id", mid,
              "--force-resolved", "--reason", "beta tries to reopen", "--from", "beta")
    assert rc == 2
    # still resolved; no requeued_after_resolve appended by the unauthorized caller
    assert cli._dead_letter_resolution_state(s).get(("beta", mid)) == "resolved"


def test_resolve_rejects_path_traversal_id(tmp_path: Path) -> None:
    # reviewer-2 F5 SECURITY: a traversal --id must be refused and write NOTHING outside the
    # sink (no <target>.resolved.json, no disposition). config.json exists after init.
    s = _store(tmp_path)
    assert (tmp_path / ".agenttalk" / "config.json").is_file()  # the traversal target
    rc = _run(tmp_path, "dead-letter", "resolve", "--from", "claude",
              "--agent", "beta", "--id", "../../config", "--reason", "exploit")
    assert rc == 2
    assert not (tmp_path / ".agenttalk" / "config.resolved.json").exists()  # no sidecar escape
    from agenttalk import attention as A
    valid, _ = A.read_dispositions(s)
    assert valid == []                                            # no forged disposition


def test_read_dead_letter_payload_path_bind(tmp_path: Path) -> None:
    # store-level defense-in-depth: an escaping id degrades to None (never reads an arbitrary
    # .agenttalk file), regardless of caller.
    s = _store(tmp_path)
    assert s.read_dead_letter_payload("beta", "../../config") is None
    assert s.read_dead_letter_payload("beta", "..\\..\\config") is None


def test_list_shows_resolve_flow_tip_for_unresolved(tmp_path: Path, capsys) -> None:
    # fable-max #2: don't auto-quiet a requeued-not-resolved dead-letter; the list points at
    # the resolve flow instead. Tip shown for the unresolved view, NOT the --resolved view.
    s = _store(tmp_path)
    mid = _dead_letter(s)
    capsys.readouterr()
    assert _run(tmp_path, "dead-letter", "list") == 0
    assert "dead-letter resolve" in capsys.readouterr().out          # flow tip present
    # once resolved, the --resolved audit view does not nag with the tip
    _run(tmp_path, "dead-letter", "resolve", "--from", "claude",
         "--agent", "beta", "--id", mid, "--reason", "handled")
    capsys.readouterr()
    assert _run(tmp_path, "dead-letter", "list", "--resolved") == 0
    assert "tip:" not in capsys.readouterr().out


def test_force_requeue_whitespace_reason_exits_2_sends_nothing(tmp_path: Path) -> None:
    # codex F7: a whitespace-only --reason on force-requeue folds to an INVALID disposition
    # line, so it must exit 2 and SEND NOTHING (not requeue with a blank audit).
    s = _store(tmp_path)
    mid = _dead_letter(s)
    _run(tmp_path, "dead-letter", "resolve", "--from", "claude",
         "--agent", "beta", "--id", mid, "--reason", "resolved")
    before = len(s.valid_messages())
    rc = _run(tmp_path, "dead-letter", "requeue", "--agent", "beta", "--id", mid,
              "--force-resolved", "--reason", "   ", "--from", "claude")
    assert rc == 2
    assert len(s.valid_messages()) == before                      # nothing sent
    assert cli._dead_letter_resolution_state(s).get(("beta", mid)) == "resolved"  # not reopened
    from agenttalk import attention as A
    valid, _ = A.read_dispositions(s)
    assert not [e for e in valid if e["action"] == A.ACTION_REQUEUED_AFTER_RESOLVE]


def test_force_requeue_corrupt_payload_leaves_no_orphan_reopen_audit(tmp_path: Path) -> None:
    # fable-max #6: the reopen audit is appended only AFTER the send succeeds. A corrupt
    # payload fails the parse BEFORE the send, so NO requeued_after_resolve is left behind.
    s = _store(tmp_path)
    mid = _dead_letter(s)
    _run(tmp_path, "dead-letter", "resolve", "--from", "claude",
         "--agent", "beta", "--id", mid, "--reason", "resolved")
    (s.dead_letter_dir / "beta" / f"{mid}.json").write_text("not json", encoding="utf-8")
    rc = _run(tmp_path, "dead-letter", "requeue", "--agent", "beta", "--id", mid,
              "--force-resolved", "--reason", "reopen please", "--from", "claude")
    assert rc == 2                                                # corrupt payload
    # still resolved, and NO orphan reopen audit was appended before the failed send
    assert cli._dead_letter_resolution_state(s).get(("beta", mid)) == "resolved"
    from agenttalk import attention as A
    valid, _ = A.read_dispositions(s)
    assert not [e for e in valid if e["action"] == A.ACTION_REQUEUED_AFTER_RESOLVE]
