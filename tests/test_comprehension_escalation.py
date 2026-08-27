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
    """The sender IS the liaison/lead — escalating to yourself is not a
    real escalation."""
    store = _make_store(tmp_path)
    store.set_operator_facing("alpha")
    with pytest.raises(esc.EscalationRoutingFailed):
        esc.escalate_attended_action_required(
            store, sender="alpha", action=esc.ACTION_RECOVER_STALE_LOCK, reason="x",
        )


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
