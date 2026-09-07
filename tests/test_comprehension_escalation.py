"""#55 slice-1 PR-A: typed escalation route for the two attended-only
actions (R-4 / plan disposition #4). Reuses the existing escalation wire
shape (a tracked `question` with meta.needs_operator="true") against a
REAL Store, the same way wrapper/obligations.py's compliance-breaker alert
already does — never through cli.cmd_escalate's argparse-coupled path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agenttalk.comprehension import escalation as esc
from agenttalk.comprehension.errors import ComprehensionError
from agenttalk.comprehension.lock import ScanLockUnrecoverable
from agenttalk.comprehension.privacy import VcsPrivacyRefused
from agenttalk.store import Store


def _make_store(tmp_path: Path) -> Store:
    s = Store(tmp_path)
    s.init(["alpha", "lead"])
    return s


# ----------------------------------------------------------- happy path: liaison

def test_escalate_routes_to_operator_facing_liaison(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.set_operator_facing("lead")
    result = esc.escalate_attended_action_required(
        store, sender="alpha", action=esc.ACTION_RECOVER_STALE_LOCK,
        reason="pid 4242's identity could not be observed exactly",
    )
    assert result.recipient == "lead"
    assert result.request_id.startswith("esc-")
    assert result.action == esc.ACTION_RECOVER_STALE_LOCK


def test_escalated_message_matches_the_existing_escalation_wire_shape(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.set_operator_facing("lead")
    result = esc.escalate_attended_action_required(
        store, sender="alpha", action=esc.ACTION_RECOVER_STALE_LOCK, reason="test reason",
    )
    import json
    files = list((store.dir / "messages").glob("*.json"))
    (msg_path,) = [p for p in files if json.loads(p.read_text(encoding="utf-8"))["id"]
                   == result.message_id]
    raw = json.loads(msg_path.read_text(encoding="utf-8"))
    assert raw["kind"] == "question"
    assert raw["meta"]["needs_operator"] == "true"
    assert raw["meta"]["request_id"] == result.request_id
    assert raw["to"] == "lead"
    assert raw["from"] == "alpha"


# ----------------------------------------------------------- fallback to lead

def test_escalate_falls_back_to_lead_when_no_liaison_configured(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.set_role("lead", "lead")
    result = esc.escalate_attended_action_required(
        store, sender="alpha", action=esc.ACTION_RECOVER_STALE_LOCK, reason="x",
    )
    assert result.recipient == "lead"


def test_escalate_prefers_liaison_over_lead(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.init(["alpha", "lead", "gamma"])
    store.set_role("lead", "lead")
    store.set_operator_facing("gamma")
    result = esc.escalate_attended_action_required(
        store, sender="alpha", action=esc.ACTION_RECOVER_STALE_LOCK, reason="x",
    )
    assert result.recipient == "gamma"


# ----------------------------------------------------------- routing failure

def test_escalate_raises_when_nothing_is_resolvable(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    with pytest.raises(esc.EscalationRoutingFailed):
        esc.escalate_attended_action_required(
            store, sender="alpha", action=esc.ACTION_RECOVER_STALE_LOCK, reason="x",
        )


def test_escalate_raises_when_sender_is_the_only_candidate(tmp_path: Path) -> None:
    """The sender IS the liaison, with no distinct lead-chat operator
    identity resolvable either — escalating to yourself is not a real
    escalation. (Whenever lead_chat_identities() DOES resolve, sender ==
    operator_facing() routes to the distinct reserved "operator" principal
    instead — see the F-2 lead-chat routing tests above; this test breaks
    that resolution deliberately, via an unset operator_identity, to reach
    the genuine no-candidate case.)"""
    store = _make_store(tmp_path)
    store.set_operator_facing("alpha")
    cfg = store.load_config()
    # Deliberately invalid (must equal avatars.OPERATOR_PRINCIPAL) so
    # lead_chat_identities() raises instead of resolving a distinct
    # "operator" target — config.json backfills a MISSING operator_identity
    # automatically, so an explicit bad value (not a pop) is what's needed
    # to break the resolution here.
    cfg["operator_identity"] = "alpha"
    store._write_config(cfg)
    with pytest.raises(esc.EscalationRoutingFailed):
        esc.escalate_attended_action_required(
            store, sender="alpha", action=esc.ACTION_RECOVER_STALE_LOCK, reason="x",
        )


# ----------------------------------------------------------- F-2: lead-chat routing parity

def test_escalate_from_the_lead_chat_lead_routes_to_the_operator_identity(
    tmp_path: Path,
) -> None:
    """reviewer-3 F-2 on PR-A (rq-5bd5427ad64d): cmd_escalate routes an
    escalation FROM the lead-chat lead to the reserved operator identity,
    not the ordinary operator-facing liaison. Mirror that branch exactly
    — without it, this would fall through to sole_lead() (== sender) and
    raise EscalationRoutingFailed instead of succeeding, exactly the gap
    F-2 describes."""
    store = _make_store(tmp_path)
    store.set_role("lead", "lead")
    result = esc.escalate_attended_action_required(
        store, sender="lead", action=esc.ACTION_RECOVER_STALE_LOCK, reason="x",
    )
    assert result.recipient == "operator"


def test_escalate_from_a_non_lead_sender_ignores_the_lead_chat_branch(tmp_path: Path) -> None:
    """The lead-chat branch only fires when sender IS the lead-chat lead —
    a different sender still gets ordinary operator_facing/lead routing."""
    store = _make_store(tmp_path)
    store.set_role("lead", "lead")
    store.set_operator_facing("lead")
    result = esc.escalate_attended_action_required(
        store, sender="alpha", action=esc.ACTION_RECOVER_STALE_LOCK, reason="x",
    )
    assert result.recipient == "lead"


# ----------------------------------------------------------- unknown action

def test_escalate_rejects_an_unknown_action(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.set_operator_facing("lead")
    with pytest.raises(ComprehensionError, match="unknown"):
        esc.escalate_attended_action_required(
            store, sender="alpha", action="something-else", reason="x",
        )


# ----------------------------------------------------------- typed-error integration points

def test_escalate_scan_lock_unrecoverable_wires_the_errors_detail(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.set_operator_facing("lead")
    error = ScanLockUnrecoverable("pid 4242 is alive but its identity does not match")
    result = esc.escalate_scan_lock_unrecoverable(store, sender="alpha", error=error)
    assert result.action == esc.ACTION_RECOVER_STALE_LOCK
    import json
    files = list((store.dir / "messages").glob("*.json"))
    (msg_path,) = [p for p in files if json.loads(p.read_text(encoding="utf-8"))["id"]
                   == result.message_id]
    body = json.loads(msg_path.read_text(encoding="utf-8"))["body"]
    assert "pid 4242 is alive but its identity does not match" in body


def test_escalate_scan_lock_unrecoverable_carries_the_real_cross_host_remedy(
    tmp_path: Path,
) -> None:
    """MICRO-ROUND 50 (Cluster 4, m4 BLOCKER, wrong-data): FIX ROUND 26
    corrected the misleading generic "run --recover-stale-lock" remedy
    on ScanLockUnrecoverable's own str() representation for a cross-host
    lock (that flag alone cannot verify a foreign host's process state) -
    but this escalation route used to wire only `error.detail` (the bare
    fact, with NO remedy text at all) into the durable, operator-facing
    message, silently losing the very correction round 26 made. The
    call-site-specific remedy (naming the OTHER host, and explicitly
    warning that re-running the flag alone will not help) must now
    reach the durable record, not just a transient stderr print."""
    store = _make_store(tmp_path)
    store.set_operator_facing("lead")
    error = ScanLockUnrecoverable(
        "scan.lock was recorded on a different host ('other-host')",
        remedy=(
            "--recover-stale-lock can force-clear this record, but it cannot verify "
            "a foreign host's process state at all - confirm independently that the "
            "scan on host 'other-host' is genuinely gone before relying on it; "
            "re-running the flag alone will not help if that host keeps re-acquiring "
            "the lock"
        ),
    )
    result = esc.escalate_scan_lock_unrecoverable(store, sender="alpha", error=error)
    import json
    files = list((store.dir / "messages").glob("*.json"))
    (msg_path,) = [p for p in files if json.loads(p.read_text(encoding="utf-8"))["id"]
                   == result.message_id]
    body = json.loads(msg_path.read_text(encoding="utf-8"))["body"]
    assert "scan.lock was recorded on a different host" in body
    assert "cannot verify a foreign host's process state" in body
    assert "re-running the flag alone will not help" in body


def test_escalate_vcs_privacy_refused_wires_the_errors_detail_and_work_id(
    tmp_path: Path,
) -> None:
    store = _make_store(tmp_path)
    store.set_operator_facing("lead")
    error = VcsPrivacyRefused(
        "2 path(s) under .agenttalk/comprehension/ are already tracked by Git",
        vcs_kind="git",
    )
    result = esc.escalate_vcs_privacy_refused(
        store, sender="alpha", error=error, work_id="migrate-checkout")
    assert result.action == esc.ACTION_ACKNOWLEDGE_UNIGNORED_PRIVATE_STORE
    import json
    files = list((store.dir / "messages").glob("*.json"))
    (msg_path,) = [p for p in files if json.loads(p.read_text(encoding="utf-8"))["id"]
                   == result.message_id]
    raw = json.loads(msg_path.read_text(encoding="utf-8"))
    assert "already tracked by Git" in raw["body"]
    assert raw["meta"]["work_id"] == "migrate-checkout"
