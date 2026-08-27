"""Typed escalation route for the two attended-only actions PR-A ships.

R-4 (carried review item) + the plan's disposition #4: "the route lands in
PR-A [...] using the existing escalation kind" [...] "operator surface
documented in PR-A." This REUSES the existing escalation wire shape — an
ordinary tracked ``question`` message carrying ``meta.needs_operator =
"true"`` and an ``esc-`` request ID — exactly what ``cli.cmd_escalate`` and
``wrapper.obligations``'s compliance-breaker alert already send. It is NOT
a new bus kind, and it does not touch ``cli.py`` (that module's
``cmd_escalate`` is deeply argparse-Namespace-coupled; this module builds
the same wire shape directly against a ``Store`` instead, the same way
``wrapper/obligations.py`` already does for its own internal escalation).

No CLI exists in PR-A yet — ``--recover-stale-lock`` and
``--acknowledge-unignored-private-store`` are PR-B's flags, and proving an
interactive terminal + explicit confirmation is entirely their job. What a
HEADLESS caller (a wrapped agent, a scheduled scan, anything without a
human at a terminal) needs from PR-A is: when ``lock.ScanLockUnrecoverable``
or ``privacy.VcsPrivacyRefused`` surfaces, escalate to the operator instead
of either blocking forever or silently proceeding. That is this module's
one job — it never attempts the attended action itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from .errors import ComprehensionError
from .lock import ScanLockUnrecoverable
from .privacy import VcsPrivacyRefused

ACTION_RECOVER_STALE_LOCK = "recover-stale-lock"
ACTION_ACKNOWLEDGE_UNIGNORED_PRIVATE_STORE = "acknowledge-unignored-private-store"
_ACTIONS = (ACTION_RECOVER_STALE_LOCK, ACTION_ACKNOWLEDGE_UNIGNORED_PRIVATE_STORE)


class EscalationRoutingFailed(ComprehensionError):
    """No operator-facing liaison or lead could be resolved. An escalation
    that lands nowhere is exactly the invisible failure this module exists
    to prevent — mirrors ``cli.cmd_escalate``'s own refuse-loudly
    contract, never a silently-dropped send."""

    reason_code = "comprehension_escalation_routing_failed"


@dataclass(frozen=True)
class EscalationResult:
    request_id: str
    recipient: str
    message_id: str
    action: str


def _resolve_liaison(store: Any, *, sender: str) -> str:
    target: str | None = None
    try:
        target = store.operator_facing()
    except Exception:  # noqa: BLE001 - a corrupt roster must not crash the escalation path
        target = None
    if not target or target == sender:
        lead: str | None = None
        try:
            lead = store.sole_lead()
        except Exception:  # noqa: BLE001
            lead = None
        target = lead if lead and lead != sender else None
    if not target:
        raise EscalationRoutingFailed(
            "no operator-facing liaison or lead is configured/resolvable — the "
            "escalation cannot be routed anywhere; run `agenttalk roster "
            "set-operator-facing <agent>` or designate a lead"
        )
    return target


def escalate_attended_action_required(
    store: Any, *, sender: str, action: str, reason: str, work_id: str | None = None,
) -> EscalationResult:
    """Send the escalation. ``action`` is one of the two module-level
    constants above; ``reason`` is the bounded, human-readable detail from
    the refusal that triggered this call (never raw exception internals —
    callers pass a typed error's own ``.detail``, itself already bounded).
    """
    if action not in _ACTIONS:
        raise ComprehensionError(f"unknown attended comprehension action {action!r}")
    recipient = _resolve_liaison(store, sender=sender)
    request_id = "esc-" + uuid.uuid4().hex[:12]
    body = (
        f"Comprehension scan needs an attended `{action}` decision: {reason}\n\n"
        "This requires an interactive terminal and explicit confirmation — a "
        "headless agent or scheduled scan cannot supply it (design: "
        "\"Scripts and wrappers cannot supply the acknowledgement\")."
    )
    meta: dict[str, Any] = {
        "needs_operator": "true",
        "request_id": request_id,
        "comprehension_attended_action": action,
    }
    if work_id:
        meta["work_id"] = work_id
    msg = store.send(
        sender=sender, recipient=recipient, kind="question",
        subject=f"comprehension: attended {action} required", body=body, meta=meta,
    )
    return EscalationResult(
        request_id=request_id, recipient=recipient, message_id=msg.id, action=action,
    )


def escalate_scan_lock_unrecoverable(
    store: Any, *, sender: str, error: ScanLockUnrecoverable,
) -> EscalationResult:
    """One-call integration point for a caught :class:`ScanLockUnrecoverable`."""
    return escalate_attended_action_required(
        store, sender=sender, action=ACTION_RECOVER_STALE_LOCK, reason=error.detail,
    )


def escalate_vcs_privacy_refused(
    store: Any, *, sender: str, error: VcsPrivacyRefused, work_id: str | None = None,
) -> EscalationResult:
    """One-call integration point for a caught :class:`VcsPrivacyRefused`."""
    return escalate_attended_action_required(
        store, sender=sender, action=ACTION_ACKNOWLEDGE_UNIGNORED_PRIVATE_STORE,
        reason=error.detail, work_id=work_id,
    )
