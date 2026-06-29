"""WP4 - the mechanical liaison RELAY (`agenttalk relay operator-answer|operator-command`).

A thin typed wrapper over the existing reply/send plumbing: it carries the operator's
words across the human<->bus boundary with an audit stamp (meta.operator_answer /
operator_command + operator_origin) and NO new message kind. Covers the validation, the
fail-closed authz, the infer-vs-require --to rule, and durable queueing.
"""
from __future__ import annotations

from pathlib import Path

from agenttalk import cli
from agenttalk import threads as th
from agenttalk.store import Store


def _store(tmp_path: Path, agents=("lead", "beta", "alpha")) -> Store:
    s = Store(tmp_path)
    s.init(list(agents))
    return s


def _main(tmp_path: Path, *argv: str) -> int:
    return cli.main(["--root", str(tmp_path), *argv])


def _escalation(s: Store, *, opener: str, liaison: str, rid: str):
    """opener escalates to the liaison (a needs_operator question)."""
    return s.send(sender=opener, recipient=liaison, kind="question",
                  subject="operator input needed", body="should I proceed?",
                  meta={"needs_operator": "true", "request_id": rid})


def _msgs_with(s: Store, *, sender=None, recipient=None, meta_key=None):
    out = []
    for m in s.valid_messages():
        if sender is not None and m.sender != sender:
            continue
        if recipient is not None and m.recipient != recipient:
            continue
        if meta_key is not None and not (m.meta or {}).get(meta_key):
            continue
        out.append(m)
    return out


# ----------------------------------------------------------- operator-answer

def test_relay_operator_answer_validates_and_routes_back(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    _escalation(s, opener="beta", liaison="lead", rid="esc-1")
    rc = _main(tmp_path, "relay", "operator-answer", "--from", "lead",
               "--to-request", "esc-1", "-m", "operator says: proceed", "--quiet")
    assert rc == 0
    # a reply from the liaison routed BACK to the asking lead-loop, stamped + correlated
    replies = _msgs_with(s, sender="lead", recipient="beta", meta_key="operator_answer")
    assert len(replies) == 1
    r = replies[0]
    assert (r.meta or {}).get("request_id") == "esc-1"
    assert (r.meta or {}).get("operator_origin") == "lead"
    assert "proceed" in r.body
    # the escalation thread is now ANSWERED (no longer pending)
    row = th.derive_threads(s.valid_messages(), agent="lead", cursor="")[0]
    assert row.operator_state == "answered"


def test_relay_operator_answer_rejects_non_escalation(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    # an ORDINARY question (no needs_operator) addressed to the liaison
    s.send(sender="beta", recipient="lead", kind="question", subject="q",
           body="ordinary", meta={"request_id": "q-1"})
    rc = _main(tmp_path, "relay", "operator-answer", "--from", "lead",
               "--to-request", "q-1", "-m", "answer", "--quiet")
    assert rc == 2
    assert _msgs_with(s, meta_key="operator_answer") == []   # nothing sent


def test_relay_operator_answer_rejects_unknown_or_foreign_thread(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    # unknown rid
    assert _main(tmp_path, "relay", "operator-answer", "--from", "lead",
                 "--to-request", "nope", "-m", "x", "--quiet") == 2
    # an escalation addressed to a DIFFERENT liaison: lead is the opener_sender here, so
    # the thread exists from lead's view but is NOT addressed to lead -> refuse.
    _escalation(s, opener="lead", liaison="alpha", rid="esc-9")
    rc = _main(tmp_path, "relay", "operator-answer", "--from", "lead",
               "--to-request", "esc-9", "-m", "x", "--quiet")
    assert rc == 2
    assert _msgs_with(s, meta_key="operator_answer") == []


def test_relay_operator_answer_rejects_already_answered(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    _escalation(s, opener="beta", liaison="lead", rid="esc-1")
    assert _main(tmp_path, "relay", "operator-answer", "--from", "lead",
                 "--to-request", "esc-1", "-m", "first", "--quiet") == 0
    # a SECOND answer to the now-answered escalation is refused
    rc = _main(tmp_path, "relay", "operator-answer", "--from", "lead",
               "--to-request", "esc-1", "-m", "second", "--quiet")
    assert rc == 2
    assert len(_msgs_with(s, meta_key="operator_answer")) == 1   # still only the first


def test_relay_operator_answer_empty_body_refused(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    _escalation(s, opener="beta", liaison="lead", rid="esc-1")
    assert _main(tmp_path, "relay", "operator-answer", "--from", "lead",
                 "--to-request", "esc-1", "-m", "", "--quiet") == 2


# ----------------------------------------------------------- operator-command

def test_relay_operator_command_stamps_and_mints_request_id(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    s.set_managed_lead_loop("beta")
    rc = _main(tmp_path, "relay", "operator-command", "--from", "lead", "--to", "beta",
               "-m", "operator: re-run the suite", "--quiet")
    assert rc == 0
    cmds = _msgs_with(s, sender="lead", recipient="beta", meta_key="operator_command")
    assert len(cmds) == 1
    m = cmds[0]
    assert m.kind == "question"                              # default kind
    assert (m.meta or {}).get("operator_origin") == "lead"
    rid = (m.meta or {}).get("request_id")
    assert isinstance(rid, str) and rid.startswith("opc-")   # a fresh tracked thread


def test_relay_operator_command_infers_single_managed_target(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    s.set_managed_lead_loop("beta")                          # exactly ONE managed lead-loop
    rc = _main(tmp_path, "relay", "operator-command", "--from", "lead",
               "-m", "do it", "--quiet")                     # no --to -> inferred
    assert rc == 0
    assert len(_msgs_with(s, recipient="beta", meta_key="operator_command")) == 1


def test_relay_operator_command_requires_to_when_ambiguous(tmp_path: Path) -> None:
    s = _store(tmp_path, agents=("lead", "beta", "gamma"))
    s.set_operator_facing("lead")
    s.set_managed_lead_loop("beta")
    s.set_managed_lead_loop("gamma")                         # TWO managed -> --to required
    rc = _main(tmp_path, "relay", "operator-command", "--from", "lead",
               "-m", "do it", "--quiet")
    assert rc == 2
    assert _msgs_with(s, meta_key="operator_command") == []


def test_relay_operator_command_requires_to_when_none_managed(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_operator_facing("lead")                            # NO managed lead-loop configured
    rc = _main(tmp_path, "relay", "operator-command", "--from", "lead",
               "-m", "do it", "--quiet")
    assert rc == 2


def test_relay_operator_command_fail_closed_non_liaison(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    s.set_managed_lead_loop("beta")
    # alpha is NOT the operator-facing liaison -> fail closed, nothing sent
    rc = _main(tmp_path, "relay", "operator-command", "--from", "alpha", "--to", "beta",
               "-m", "sneaky command", "--quiet")
    assert rc == 2
    assert _msgs_with(s, meta_key="operator_command") == []


def test_relay_operator_command_audited_override(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    s.set_managed_lead_loop("beta")
    # a non-liaison MAY relay with an audited override + reason
    rc = _main(tmp_path, "relay", "operator-command", "--from", "alpha", "--to", "beta",
               "--override", "--reason", "lead is down, operator at alpha console",
               "-m", "operator: pause", "--quiet")
    assert rc == 0
    cmds = _msgs_with(s, recipient="beta", meta_key="operator_command")
    assert len(cmds) == 1
    assert (cmds[0].meta or {}).get("operator_command_override") == "true"
    assert "lead is down" in (cmds[0].meta or {}).get("override_reason", "")
    # override WITHOUT a reason is refused
    rc2 = _main(tmp_path, "relay", "operator-command", "--from", "alpha", "--to", "beta",
                "--override", "-m", "x", "--quiet")
    assert rc2 == 2


def test_relay_operator_command_rejects_caller_request_id(tmp_path: Path) -> None:
    # codex WP4 MAJOR regression: operator-command OWNS its correlation id. A caller-
    # supplied --meta request_id is REFUSED for BOTH kinds, so a spontaneous command can
    # never graft onto an existing thread (question) nor give a message a tracked id.
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    s.set_managed_lead_loop("beta")
    # an existing escalation thread the command must NOT be able to graft onto
    _escalation(s, opener="beta", liaison="lead", rid="esc-existing")
    rc_q = _main(tmp_path, "relay", "operator-command", "--from", "lead", "--to", "beta",
                 "--kind", "question", "--meta", "request_id=esc-existing",
                 "-m", "sneaky graft", "--quiet")
    assert rc_q == 2
    rc_m = _main(tmp_path, "relay", "operator-command", "--from", "lead", "--to", "beta",
                 "--kind", "message", "--meta", "request_id=should-not-stick",
                 "-m", "x", "--quiet")
    assert rc_m == 2
    # nothing was sent under either id (no operator_command message exists at all)
    assert _msgs_with(s, meta_key="operator_command") == []
    # the existing escalation is untouched (still just its opener)
    assert len([m for m in s.valid_messages()
                if (m.meta or {}).get("request_id") == "esc-existing"]) == 1


def test_relay_operator_command_question_always_mints_fresh_opc(tmp_path: Path) -> None:
    # even with no caller id, a question gets a FRESH opc- id (never reuses/echoes).
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    s.set_managed_lead_loop("beta")
    assert _main(tmp_path, "relay", "operator-command", "--from", "lead", "--to", "beta",
                 "--kind", "question", "-m", "do it", "--quiet") == 0
    cmds = _msgs_with(s, recipient="beta", meta_key="operator_command")
    rid = (cmds[0].meta or {}).get("request_id")
    assert isinstance(rid, str) and rid.startswith("opc-")


def test_relay_operator_command_scrubs_forged_audit_meta(tmp_path: Path) -> None:
    # lead WP4 P3: the handler is AUTHORITATIVE for the reserved audit/control/routing meta.
    # A non-override command must NOT carry the audited-exception markers even when --meta
    # injects them; a forged operator_origin is overwritten with the real sender; a sibling
    # operator_answer and a grafted routing key are scrubbed.
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    s.set_managed_lead_loop("beta")
    rc = _main(tmp_path, "relay", "operator-command", "--from", "lead", "--to", "beta",
               "--meta", "operator_command_override=true",
               "--meta", "override_reason=forged",
               "--meta", "operator_origin=alpha",
               "--meta", "operator_answer=true",
               "--meta", "broadcast_id=b-forged",
               "-m", "normal command", "--quiet")
    assert rc == 0
    m = _msgs_with(s, recipient="beta", meta_key="operator_command")[0]
    meta = m.meta or {}
    assert "operator_command_override" not in meta      # no forged audited-exception
    assert "override_reason" not in meta
    assert meta.get("operator_origin") == "lead"        # real sender, not the forged alpha
    assert "operator_answer" not in meta                # sibling discriminator scrubbed
    assert "broadcast_id" not in meta                   # routing graft scrubbed


def test_relay_operator_command_override_markers_only_on_real_override(tmp_path: Path) -> None:
    # the audited-exception markers appear ONLY on the genuine --override path.
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    s.set_managed_lead_loop("beta")
    rc = _main(tmp_path, "relay", "operator-command", "--from", "alpha", "--to", "beta",
               "--override", "--reason", "lead down",
               "--meta", "operator_command_override=false",   # caller tries to suppress it
               "-m", "pause", "--quiet")
    assert rc == 0
    m = _msgs_with(s, recipient="beta", meta_key="operator_command")[0]
    # the handler owns the markers: real override -> true + the real reason (not the caller's)
    assert (m.meta or {}).get("operator_command_override") == "true"
    assert (m.meta or {}).get("override_reason") == "lead down"


def test_relay_operator_answer_scrubs_sibling_and_forged_meta(tmp_path: Path) -> None:
    # an operator-answer must NOT carry a grafted operator_command, and a forged
    # operator_origin is overwritten with the real relaying liaison.
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    _escalation(s, opener="beta", liaison="lead", rid="esc-1")
    rc = _main(tmp_path, "relay", "operator-answer", "--from", "lead", "--to-request", "esc-1",
               "--meta", "operator_command=true",
               "--meta", "operator_origin=alpha",
               "-m", "proceed", "--quiet")
    assert rc == 0
    r = _msgs_with(s, sender="lead", recipient="beta", meta_key="operator_answer")[0]
    assert "operator_command" not in (r.meta or {})     # sibling discriminator scrubbed
    assert (r.meta or {}).get("operator_origin") == "lead"   # real sender, not forged alpha


def test_relay_operator_command_message_kind_no_request_id(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    s.set_managed_lead_loop("beta")
    rc = _main(tmp_path, "relay", "operator-command", "--from", "lead", "--to", "beta",
               "--kind", "message", "-m", "fyi from operator", "--quiet")
    assert rc == 0
    cmds = _msgs_with(s, recipient="beta", meta_key="operator_command")
    assert len(cmds) == 1 and cmds[0].kind == "message"
    assert "request_id" not in (cmds[0].meta or {})          # message is fire-and-forget


# ----------------------------------------------------------- liaison-down (durability)

def test_relay_queues_durably_for_a_down_target(tmp_path: Path) -> None:
    # condition 4: a relayed command QUEUES durably regardless of whether the target
    # lead-loop is currently up - it is a normal bus message sitting in the inbox until
    # consumed (the open thread, not a block, represents the pending work).
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    s.set_managed_lead_loop("beta")
    assert _main(tmp_path, "relay", "operator-command", "--from", "lead", "--to", "beta",
                 "-m", "operator: prioritize the hotfix", "--quiet") == 0
    # the message is durably present + addressed to beta even though beta never ran
    queued = _msgs_with(s, recipient="beta", meta_key="operator_command")
    assert len(queued) == 1
    # and it has not been consumed (beta's cursor never advanced)
    assert s.cursor("beta") == ""
