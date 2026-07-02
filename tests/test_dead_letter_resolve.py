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

from pathlib import Path

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


def test_resolve_requires_liaison_authority(tmp_path: Path) -> None:
    s = _store(tmp_path)
    mid = _dead_letter(s)
    # beta is not the operator-facing liaison -> exit 2, no disposition written
    rc = _run(tmp_path, "dead-letter", "resolve", "--from", "beta",
              "--agent", "beta", "--id", mid, "--reason", "not authorized")
    assert rc == 2
    assert cli._dead_letter_resolution_state(s).get(("beta", mid)) is None


def test_resolve_requires_reason(tmp_path: Path) -> None:
    s = _store(tmp_path)
    mid = _dead_letter(s)
    # --reason is argparse-required; a whitespace-only reason passes argparse but the
    # runtime check rejects it (clean exit 2, no disposition written).
    rc = _run(tmp_path, "dead-letter", "resolve", "--from", "claude",
              "--agent", "beta", "--id", mid, "--reason", "   ")
    assert rc == 2
    assert cli._dead_letter_resolution_state(s).get(("beta", mid)) is None


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
