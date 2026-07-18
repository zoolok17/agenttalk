"""Detection-grade wrapped-turn commit enforcement.

This module deliberately provides a detection boundary, not a same-user security
boundary.  The parent wrapper snapshots operator launch policy, records canonical
transitions in a durable store-owned journal, and refuses to advance an eligible
head unless replay proves its delivery assignment terminal.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
# Dispatch argv uses a pinned interpreter and never invokes a shell.
import subprocess  # nosec B404
import uuid
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable

from agenttalk import threads
from agenttalk._atomic import write_text as _atomic_write_text
from agenttalk.store import CONTROL_KINDS, PROC_DEAD, Message, _process_liveness

POLICY_ENV = "AGENTTALK_COMMIT_GATE_POLICY"
POLICY_SCHEMA_VERSION = 1
LEDGER_SCHEMA_VERSION = 1
REDUCER_VERSION = "question-replay-v1"
DETECTION_GRADE = "detection"
SECURITY_GRADE = "security"
PARTICIPANT_CAPABILITIES = (
    "answer",
    "broadcast_policy_satisfied",
    "composing",
    "delivery_failed",
    "human_escalation",
    "manual_close",
    "operator_resolution",
    "rescind",
    "transfer",
)
PARTICIPANT_CAPABILITIES_DIGEST = hashlib.sha256(
    json.dumps(PARTICIPANT_CAPABILITIES, separators=(",", ":")).encode("utf-8")
).hexdigest()
MAX_PAID_DISPATCHES_TOTAL = 2
COMPLIANCE_BREAKER_TRIP = 3
MAX_DISPATCH_TOKEN_CEILING = 262_144
MAX_DISPATCH_WALL_TIME_SECONDS = 3_600.0
MAX_DISPATCH_RESERVED_COST = 100.0
MAX_CONCURRENT_PAID_DISPATCHES = 1
MAX_OPERATION_INFRA_ATTEMPTS = 16
MAX_OPERATION_INFRA_SECONDS = 900.0
MAX_FINALIZATION_MISSES = 12
MAX_FINALIZATION_SECONDS = 900.0
MAX_DEFERRAL_SECONDS = 3600.0
PROOF_UNREADABLE_SECONDS = 900.0


class ResolverState(str, Enum):
    NOT_OWED = "not_owed"
    CLASSIFICATION_UNKNOWN = "classification_unknown"
    INACTIVE = "inactive"
    ACTIVE = "active"
    BLOCKED = "blocked"
    BLOCKED_POLICY = "blocked_policy"
    BLOCKED_COMPLIANCE = "blocked_compliance"
    OWED_UNSATISFIED = "owed_unsatisfied"
    SATISFIED = "satisfied"
    SUPERSEDED = "superseded"
    TRANSFERRED = "transferred"
    OPERATOR_RESOLVED = "operator_resolved"
    BROADCAST_POLICY_SATISFIED = "broadcast_policy_satisfied"
    IN_PROGRESS = "in_progress"
    DEFERRED = "deferred"
    ACTION_ATTEMPT_INFRA = "action_attempt_infra"
    ACTION_REJECTED = "action_rejected"
    INDETERMINATE = "indeterminate"
    DELIVERY_EXHAUSTED = "delivery_exhausted"


TERMINAL_STATES = frozenset({
    ResolverState.SATISFIED,
    ResolverState.SUPERSEDED,
    ResolverState.TRANSFERRED,
    ResolverState.OPERATOR_RESOLVED,
    ResolverState.BROADCAST_POLICY_SATISFIED,
    ResolverState.DELIVERY_EXHAUSTED,
})
CLOSED_ADMISSION_STATES = frozenset({
    "blocked",
    "broadcast_policy_satisfied",
    "delivery_failed",
    "finalized",
    "operator_resolved",
    "transferred",
})


class GateError(RuntimeError):
    """Base class for authoritative commit-gate failures."""


class LedgerUnreadable(GateError):
    """The canonical journal cannot be read safely."""


class StaleRevision(GateError):
    """A scoped compare-and-swap lost a race."""


class DispatchRefused(GateError):
    """A paid dispatch did not satisfy every reservation precondition."""


@dataclass(frozen=True)
class PolicySnapshot:
    status: ResolverState
    generation: str
    grade: str | None = None
    reason: str = ""
    agent: str | None = None

    @classmethod
    def inactive(
        cls,
        reason: str = "no policy configured",
        *,
        agent: str | None = None,
    ) -> "PolicySnapshot":
        return cls(ResolverState.NOT_OWED, "inactive", reason=reason, agent=agent)

    @classmethod
    def from_mapping(cls, raw: object, agent: str) -> "PolicySnapshot":
        if not isinstance(raw, dict) or raw.get("schema_version") != POLICY_SCHEMA_VERSION:
            return cls(
                ResolverState.BLOCKED_POLICY,
                "unreadable",
                reason="policy schema invalid",
                agent=agent,
            )
        agents = raw.get("agents")
        if not isinstance(agents, dict):
            return cls(
                ResolverState.BLOCKED_POLICY,
                "unreadable",
                reason="policy agents invalid",
                agent=agent,
            )
        canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        generation = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        entry = agents.get(agent)
        if entry is None:
            return cls(
                ResolverState.NOT_OWED,
                generation,
                reason="agent has no configured grade",
                agent=agent,
            )
        if not isinstance(entry, dict) or not isinstance(entry.get("grade"), str):
            return cls(
                ResolverState.BLOCKED_POLICY,
                generation,
                reason="configured grade is invalid",
                agent=agent,
            )
        grade = entry.get("grade")
        if grade not in {DETECTION_GRADE, SECURITY_GRADE}:
            return cls(
                ResolverState.BLOCKED_POLICY,
                generation,
                reason="configured grade is unsupported",
                agent=agent,
            )
        enabled = entry.get("enabled", True)
        if enabled is not True and enabled is not False:
            return cls(
                ResolverState.BLOCKED_POLICY,
                generation,
                reason="configured enabled flag is invalid",
                agent=agent,
            )
        if enabled is False:
            return cls(
                ResolverState.INACTIVE,
                generation,
                grade,
                "operator disabled gate",
                agent,
            )
        if grade == SECURITY_GRADE:
            return cls(
                ResolverState.BLOCKED,
                generation,
                SECURITY_GRADE,
                "security-grade prerequisites are not available in this build",
                agent,
            )
        return cls(
            ResolverState.ACTIVE,
            generation,
            DETECTION_GRADE,
            "policy ready",
            agent,
        )

    @classmethod
    def from_path(cls, path: Path, agent: str) -> "PolicySnapshot":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return cls(
                ResolverState.BLOCKED_POLICY,
                "unreadable",
                reason=f"operator policy unreadable: {type(exc).__name__}",
                agent=agent,
            )
        return cls.from_mapping(raw, agent)

    @classmethod
    def from_environment(cls, agent: str) -> "PolicySnapshot":
        configured = os.environ.get(POLICY_ENV)
        if not configured:
            return cls.inactive(agent=agent)
        return cls.from_path(Path(configured).expanduser().resolve(), agent)


@dataclass(frozen=True)
class ObligationKey:
    store_epoch: str
    inbound_id: str
    correlation_id: str
    requester: str
    responder: str
    question_generation: int
    delivery_generation: int
    obligation_class: str
    reducer_version: str = REDUCER_VERSION
    participant_capabilities_digest: str = PARTICIPANT_CAPABILITIES_DIGEST

    def to_dict(self) -> dict:
        return {
            "store_epoch": self.store_epoch,
            "inbound_id": self.inbound_id,
            "correlation_id": self.correlation_id,
            "requester": self.requester,
            "responder": self.responder,
            "question_generation": self.question_generation,
            "delivery_generation": self.delivery_generation,
            "obligation_class": self.obligation_class,
            "reducer_version": self.reducer_version,
            "participant_capabilities_digest": self.participant_capabilities_digest,
        }

    @property
    def digest(self) -> str:
        raw = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Resolution:
    state: ResolverState
    reason: str
    key: ObligationKey | None = None
    evidence_id: str | None = None
    scoped_revision: int = 0
    ledger_revision: int | None = None
    compliance_success: bool = False
    activation_generation: str | None = None
    readiness_generation: str | None = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def allows_legacy_commit(self) -> bool:
        return self.state in {
            ResolverState.NOT_OWED,
            ResolverState.CLASSIFICATION_UNKNOWN,
            ResolverState.INACTIVE,
        }


@dataclass(frozen=True)
class DispatchPermit:
    key_digest: str
    nonce: str
    composing_nonce: str
    purpose: str
    paid_dispatches_total: int
    draft_path: Path
    budgets_digest: str = ""


DEFAULT_DISPATCH_BUDGETS = {
    "token_ceiling": MAX_DISPATCH_TOKEN_CEILING,
    "wall_time_seconds": MAX_DISPATCH_WALL_TIME_SECONDS,
    "reserved_cost": MAX_DISPATCH_RESERVED_COST,
    "concurrency": MAX_CONCURRENT_PAID_DISPATCHES,
}


def _dispatch_budgets(raw: object) -> dict:
    value = DEFAULT_DISPATCH_BUDGETS if raw is None else raw
    if not isinstance(value, dict) or set(value) != set(DEFAULT_DISPATCH_BUDGETS):
        raise DispatchRefused("dispatch budgets are incomplete")
    token_ceiling = value.get("token_ceiling")
    wall_time_seconds = value.get("wall_time_seconds")
    reserved_cost = value.get("reserved_cost")
    concurrency = value.get("concurrency")
    if (
        not isinstance(token_ceiling, int)
        or isinstance(token_ceiling, bool)
        or token_ceiling < 1
        or token_ceiling > MAX_DISPATCH_TOKEN_CEILING
    ):
        raise DispatchRefused("dispatch token ceiling exceeds the v1 budget")
    if (
        not isinstance(wall_time_seconds, (int, float))
        or isinstance(wall_time_seconds, bool)
        or not math.isfinite(float(wall_time_seconds))
        or float(wall_time_seconds) <= 0
        or float(wall_time_seconds) > MAX_DISPATCH_WALL_TIME_SECONDS
    ):
        raise DispatchRefused("dispatch wall-time ceiling exceeds the v1 budget")
    if (
        not isinstance(reserved_cost, (int, float))
        or isinstance(reserved_cost, bool)
        or not math.isfinite(float(reserved_cost))
        or float(reserved_cost) < 0
        or float(reserved_cost) > MAX_DISPATCH_RESERVED_COST
    ):
        raise DispatchRefused("dispatch reserved-cost ceiling exceeds the v1 budget")
    if concurrency != MAX_CONCURRENT_PAID_DISPATCHES:
        raise DispatchRefused("dispatch concurrency budget exhausted")
    return {
        "token_ceiling": token_ceiling,
        "wall_time_seconds": float(wall_time_seconds),
        "reserved_cost": float(reserved_cost),
        "concurrency": concurrency,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _epoch(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _correlation(meta: object) -> str | None:
    if not isinstance(meta, dict):
        return None
    for name in ("request_id", "broadcast_id"):
        value = meta.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def _true(meta: dict, name: str) -> bool | None:
    if name not in meta:
        return False
    value = meta.get(name)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        value = value.strip().casefold()
        if value in {"true", "1", "yes"}:
            return True
        if value in {"false", "0", "no"}:
            return False
    return None


def _broadcast_descriptor(meta: object, *, requester: object = None) -> dict | None:
    if not isinstance(meta, dict) or not isinstance(meta.get("broadcast_id"), str):
        return None
    broadcast_id = meta["broadcast_id"]
    members = meta.get("membership_snapshot")
    policy = meta.get("response_policy")
    quorum = meta.get("response_quorum")
    if (
        not isinstance(members, list)
        or not members
        or any(not isinstance(member, str) or not member for member in members)
        or len(set(members)) != len(members)
        or not isinstance(requester, str)
        or not requester
        or meta.get("request_id") != broadcast_id
        or policy not in {"each", "any", "quorum"}
        or meta.get("broadcast_policy_version") != 1
        or (
            policy == "quorum"
            and (
                not isinstance(quorum, int)
                or isinstance(quorum, bool)
                or quorum < 1
                or quorum > len(members)
            )
        )
    ):
        return None
    return {
        "broadcast_id": broadcast_id,
        "requester": requester,
        "membership_snapshot": list(members),
        "response_policy": policy,
        "response_quorum": quorum if policy == "quorum" else None,
        "broadcast_policy_version": 1,
    }


def _broadcast_descriptor_digest(descriptor: dict) -> str:
    return hashlib.sha256(_canonical(descriptor).encode("utf-8")).hexdigest()


def _valid_hex_digest(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64 or value != value.lower():
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def operation_payload_digest(
    *,
    operation: str,
    body: str,
    kind: str,
    recipient: str,
    in_reply_to: str | None = None,
    request_id: str | None = None,
    broadcast_id: str | None = None,
    origin_request_id: str | None = None,
    origin_inbound_id: str | None = None,
    origin_obligation_key_digest: str | None = None,
    expected_roster_revision: str | None = None,
) -> str:
    payload = {
        "operation": operation,
        "body": body,
        "kind": kind,
        "recipient": recipient,
        "in_reply_to": in_reply_to,
        "request_id": request_id,
        "broadcast_id": broadcast_id,
        "origin_request_id": origin_request_id,
        "origin_inbound_id": origin_inbound_id,
    }
    if origin_obligation_key_digest is not None:
        payload["origin_obligation_key_digest"] = origin_obligation_key_digest
    if expected_roster_revision is not None:
        payload["expected_roster_revision"] = expected_roster_revision
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _message_operation_valid(message: Message, *, operation: str, nonce: str) -> bool:
    meta = message.meta or {}
    if meta.get("operation_nonce") != nonce:
        return False
    expected = operation_payload_digest(
        operation=operation,
        body=message.body,
        kind=message.kind,
        recipient=message.recipient,
        in_reply_to=meta.get("in_reply_to"),
        request_id=meta.get("request_id"),
        broadcast_id=meta.get("broadcast_id"),
        origin_request_id=meta.get("origin_request_id"),
        origin_inbound_id=meta.get("origin_inbound_id"),
        origin_obligation_key_digest=meta.get("origin_obligation_key_digest"),
        expected_roster_revision=meta.get("expected_roster_revision"),
    )
    return meta.get("operation_digest") == expected


def _roster_snapshot(cfg: dict) -> dict:
    roster = list(cfg.get("agents") or [])
    roles = cfg.get("roles") if isinstance(cfg.get("roles"), dict) else {}
    liaison = cfg.get("operator_facing")
    authorized = {
        name for name in roster
        if isinstance(roles.get(name), str) and roles[name].casefold() == "lead"
    }
    if isinstance(liaison, str) and liaison in roster:
        authorized.add(liaison)
    payload = {
        "agents": roster,
        "roles": {name: roles[name] for name in sorted(roles)},
        "operator_facing": liaison if isinstance(liaison, str) else None,
    }
    return {
        "revision": hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest(),
        "authorized_liaisons": sorted(authorized),
    }


def _new_ledger() -> dict:
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "store_epoch": uuid.uuid4().hex,
        "append_sequence": 0,
        "revision": 0,
        "messages": {},
        "transitions": [],
        "scoped_revisions": {},
        "obligations": {},
        "inbound_index": {},
        "no_admission_claims": {},
        "dispatch_nonces": {},
        "breakers": {},
        "delivery_index": {},
        "broadcasts": {},
        "cursor_dispositions": {},
        "telemetry": {},
    }


def _validate_ledger(raw: object) -> dict:
    if not isinstance(raw, dict) or raw.get("schema_version") != LEDGER_SCHEMA_VERSION:
        raise LedgerUnreadable("obligation ledger schema invalid")
    raw.setdefault("dispatch_nonces", {})
    raw.setdefault("broadcasts", {})
    required = (
        "store_epoch", "append_sequence", "revision", "messages", "transitions",
        "scoped_revisions", "obligations", "inbound_index", "breakers",
        "no_admission_claims", "dispatch_nonces", "delivery_index", "broadcasts",
        "telemetry",
        "cursor_dispositions",
    )
    if not isinstance(raw.get("store_epoch"), str):
        raise LedgerUnreadable("obligation ledger epoch invalid")
    if not all(name in raw for name in required):
        raise LedgerUnreadable("obligation ledger fields missing")
    if not all(isinstance(raw.get(name), dict) for name in (
        "messages", "scoped_revisions", "obligations", "inbound_index",
        "no_admission_claims", "dispatch_nonces", "breakers", "delivery_index",
        "broadcasts", "telemetry",
        "cursor_dispositions",
    )):
        raise LedgerUnreadable("obligation ledger maps invalid")
    if not isinstance(raw.get("transitions"), list):
        raise LedgerUnreadable("obligation transitions invalid")
    sequence = raw.get("append_sequence")
    revision = raw.get("revision")
    if (
        not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 0
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 0
    ):
        raise LedgerUnreadable("obligation ledger counters invalid")
    transition_sequences = [
        row.get("sequence") if isinstance(row, dict) else None
        for row in raw["transitions"]
    ]
    if transition_sequences != list(range(1, sequence + 1)):
        raise LedgerUnreadable("obligation append sequence is non-contiguous")
    if any(
        not isinstance(row, dict)
        or not isinstance(row.get("sequence"), int)
        or row["sequence"] < 1
        or row["sequence"] > sequence
        for row in raw["messages"].values()
    ):
        raise LedgerUnreadable("obligation message sequence invalid")
    return raw


class DetectionCommitGate:
    """One wrapper-process view of the detection-grade commit authority."""

    def __init__(
        self,
        store,
        agent: str,
        policy: PolicySnapshot,
        *,
        fence: str,
        now: Callable[[], str] = _now_iso,
        producer_alive: Callable[[str], bool] | None = None,
        policy_loader: Callable[[], PolicySnapshot] | None = None,
        dispatch_budgets: dict | None = None,
    ) -> None:
        self.store = store
        self.agent = agent
        self.policy = policy
        self.fence = fence
        self.now = now
        self.producer_alive = producer_alive or (lambda _token: False)
        self.policy_loader = policy_loader
        self.dispatch_budgets = _dispatch_budgets(dispatch_budgets)
        self.path = store.state_dir / "owed-action" / "ledger.json"
        self.checkpoint_path = store.state_dir / "owed-action" / "ledger.checkpoint.json"
        self.epoch_anchor_path = store.state_dir / "owed-action" / "epoch-anchor.json"
        self.proof_health_path = store.state_dir / "owed-action" / "proof-health.json"
        self.drafts = store.state_dir / "owed-action" / "drafts" / agent

    @classmethod
    def from_environment(cls, store, agent: str, *, fence: str) -> "DetectionCommitGate":
        return cls(
            store,
            agent,
            PolicySnapshot.from_environment(agent),
            fence=fence,
            policy_loader=lambda: PolicySnapshot.from_environment(agent),
        )

    def _current_policy(self) -> PolicySnapshot:
        return self.policy_loader() if self.policy_loader is not None else self.policy

    def _revalidate_admission_policy(
        self,
        observed: PolicySnapshot,
    ) -> Resolution | None:
        """Fail closed if policy changed after admission classification."""
        current = self._current_policy()
        if (
            current.status == observed.status
            and current.generation == observed.generation
            and current.grade == observed.grade
            and current.agent == observed.agent
        ):
            return None
        if current.status in {
            ResolverState.BLOCKED,
            ResolverState.BLOCKED_POLICY,
            ResolverState.BLOCKED_COMPLIANCE,
        }:
            return Resolution(current.status, current.reason)
        return Resolution(
            ResolverState.BLOCKED_POLICY,
            "operator policy changed during admission; retry classification",
        )

    @staticmethod
    def _claim_matches_policy(claim: dict, policy: PolicySnapshot) -> bool:
        return (
            claim.get("policy_status") == policy.status.value
            and claim.get("policy_generation") == policy.generation
        )

    @staticmethod
    def _claim_is_untouched(claim: dict) -> bool:
        return (
            claim.get("state") == "open"
            and not claim.get("drive_started_at")
            and not claim.get("drive_succeeded_at")
        )

    @staticmethod
    def _claim_has_no_legacy_work(claim: dict) -> bool:
        return not claim.get("drive_started_at") and not claim.get(
            "drive_succeeded_at"
        )

    @staticmethod
    def _policy_blocks_durable_terminal_projection(policy: PolicySnapshot) -> bool:
        return policy.status in {
            ResolverState.BLOCKED_POLICY,
            ResolverState.BLOCKED_COMPLIANCE,
        } or (
            policy.status == ResolverState.BLOCKED
            and policy.grade != SECURITY_GRADE
        )

    @staticmethod
    def _exact_no_admission_disposition_matches(
        claim: dict,
        disposition: object,
        record: dict,
        resolution: Resolution,
    ) -> bool:
        disposition_state = (
            "delivery_failed"
            if resolution.state == ResolverState.DELIVERY_EXHAUSTED
            else resolution.state.value
        )
        return (
            claim.get("state") == "finalized"
            and claim.get("resolution") == resolution.state.value
            and claim.get("terminal_evidence_id") == resolution.evidence_id
            and isinstance(disposition, dict)
            and disposition.get("inbound_id") == record.get("id")
            and disposition.get("mode") == record.get("mode", "global")
            and disposition.get("state") == disposition_state
        )

    @classmethod
    def _durable_no_admission_disposition_matches(
        cls,
        claim: dict,
        disposition: object,
        record: dict,
        resolution: Resolution,
    ) -> bool:
        return (
            cls._claim_has_no_legacy_work(claim)
            and cls._exact_no_admission_disposition_matches(
                claim,
                disposition,
                record,
                resolution,
            )
        )

    def _replay_pinned_terminal_transition(
        self,
        record: dict,
        messages: list[Message],
        ledger: dict,
        *,
        transition: str,
        state: ResolverState,
        evidence_id: object,
    ) -> Resolution:
        """Replay the immutable prefix that established an exact terminal winner."""
        inbound_id = record.get("id")
        candidates = [
            event
            for event in ledger["transitions"]
            if isinstance(event, dict)
            and event.get("transition") == transition
            and isinstance(event.get("data"), dict)
            and event["data"].get("inbound_id") == inbound_id
            and event["data"].get("state") == state.value
            and (evidence_id is None or event.get("source_id") == evidence_id)
        ]
        if len(candidates) != 1:
            return Resolution(
                ResolverState.INDETERMINATE,
                "pinned terminal authority is ambiguous",
            )
        authority = candidates[0]
        cutoff = int(authority.get("sequence", 0))
        prefix_rows = {
            message_id: row
            for message_id, row in ledger["messages"].items()
            if int(row.get("sequence", 0)) < cutoff
        }
        prefix_ledger = {
            **ledger,
            "messages": prefix_rows,
            "transitions": [
                event
                for event in ledger["transitions"]
                if int(event.get("sequence", 0)) < cutoff
            ],
        }
        prefix_messages = [
            message for message in messages if message.id in prefix_rows
        ]
        replayed = self._resolve_replay(
            record,
            prefix_messages,
            prefix_ledger,
            admission=None,
        )
        if (
            replayed.state != state
            or not isinstance(replayed.evidence_id, str)
            or authority.get("source_id") != replayed.evidence_id
        ):
            return Resolution(
                ResolverState.INDETERMINATE,
                "pinned terminal authority conflicts with replay",
            )
        return replayed

    def _recognized_zero_work_terminal_authority(
        self,
        record: dict,
        messages: list[Message],
        ledger: dict,
        claim: object,
    ) -> Resolution | None:
        """Return exact authority for a durably recognized zero-work terminal.

        Policy controls whether new work may start.  It cannot revoke a terminal
        that canonical replay and the no-admission journal have already agreed
        upon.  This predicate intentionally excludes ordinary semantic claims
        and every claim that has authorized or retained legacy work.
        """
        if not isinstance(claim, dict) or not self._claim_has_no_legacy_work(claim):
            return None
        try:
            state = ResolverState(str(claim.get("resolution")))
        except ValueError:
            return (
                Resolution(
                    ResolverState.INDETERMINATE,
                    "zero-work terminal authority resolution is invalid",
                )
                if claim.get("claim_kind") in {
                    "pre_admission_terminal",
                    "transfer_target_abort",
                }
                else None
            )
        inbound_id = record.get("id")
        claim_kind: str
        if state in TERMINAL_STATES:
            persisted_evidence = claim.get("terminal_evidence_id")
            replayed = self._replay_pinned_terminal_transition(
                record,
                messages,
                ledger,
                transition="PRE_ADMISSION_TERMINAL_NORMALIZED",
                state=state,
                evidence_id=persisted_evidence,
            )
            if replayed.state == ResolverState.INDETERMINATE:
                return replayed
            recognized = True
            claim_kind = "pre_admission_terminal"
        elif state == ResolverState.NOT_OWED:
            inbound_message = next(
                (message for message in messages if message.id == inbound_id),
                None,
            )
            inbound_meta = (
                inbound_message.meta
                if isinstance(inbound_message, Message)
                and isinstance(inbound_message.meta, dict)
                else {}
            )
            transfer_digest = claim.get("transfer_from_key_digest")
            transfer_events = [
                event
                for event in ledger["transitions"]
                if isinstance(event, dict)
                and event.get("transition") == "TRANSFER_TARGET_ABORTED"
                and event.get("source_id") == inbound_id
            ]
            if not (
                claim.get("claim_kind") == "transfer_target_abort"
                or _valid_hex_digest(transfer_digest)
                or transfer_events
            ):
                return None
            recognized = bool(
                _valid_hex_digest(transfer_digest)
                and inbound_meta.get("transfer_from_key_digest") == transfer_digest
                and any(
                    isinstance(event.get("data"), dict)
                    and event["data"].get("inbound_id") == inbound_id
                    and event["data"].get("transfer_from_key_digest")
                    == transfer_digest
                    for event in transfer_events
                )
            )
            replayed = Resolution(
                ResolverState.NOT_OWED,
                "transfer_target_abort_authority",
            )
            claim_kind = "transfer_target_abort"
        else:
            return None
        if claim.get("state") not in {"open", "finalized"}:
            return Resolution(
                ResolverState.INDETERMINATE,
                "zero-work terminal authority has an invalid state",
            )
        persisted_evidence = claim.get("terminal_evidence_id")
        if persisted_evidence is not None and persisted_evidence != replayed.evidence_id:
            return Resolution(
                ResolverState.INDETERMINATE,
                "zero-work terminal authority evidence is torn",
            )
        if claim.get("claim_kind") not in {None, claim_kind} or not recognized:
            return Resolution(
                ResolverState.INDETERMINATE,
                "zero-work terminal lacks durable recognition authority",
            )
        legacy_transition = any(
            isinstance(event, dict)
            and event.get("transition") in {
                "PRE_ADMISSION_DRIVE_AUTHORIZED",
                "PRE_ADMISSION_SUCCESS_RETAINED",
            }
            and event.get("source_id") == inbound_id
            for event in ledger["transitions"]
        )
        if legacy_transition:
            return Resolution(
                ResolverState.INDETERMINATE,
                "zero-work terminal conflicts with legacy authority",
            )
        if claim.get("state") == "finalized" and not (
            self._exact_no_admission_disposition_matches(
                claim,
                ledger["cursor_dispositions"].get(self.agent),
                record,
                replayed,
            )
        ):
            return Resolution(
                ResolverState.INDETERMINATE,
                "finalized no-admission disposition is torn",
            )
        return replayed

    def _rebind_zero_work_terminal_authority_locked(
        self,
        ledger: dict,
        claim: dict,
        record: dict,
        resolution: Resolution,
        policy: PolicySnapshot,
    ) -> bool:
        """Bind exact terminal authority to current readable policy and owner."""
        previous = {
            "policy_status": claim.get("policy_status"),
            "policy_generation": claim.get("policy_generation"),
            "fence": claim.get("fence"),
            "claim_kind": claim.get("claim_kind"),
            "terminal_evidence_id": claim.get("terminal_evidence_id"),
        }
        claim["policy_status"] = policy.status.value
        claim["policy_generation"] = policy.generation
        claim["claim_kind"] = (
            "pre_admission_terminal"
            if resolution.state in TERMINAL_STATES
            else "transfer_target_abort"
        )
        claim["terminal_evidence_id"] = resolution.evidence_id
        if claim.get("state") == "open":
            claim["fence"] = self.fence
            claim["owner_pid"] = os.getpid()
        current = {
            "policy_status": claim.get("policy_status"),
            "policy_generation": claim.get("policy_generation"),
            "fence": claim.get("fence"),
            "claim_kind": claim.get("claim_kind"),
            "terminal_evidence_id": claim.get("terminal_evidence_id"),
        }
        if current == previous:
            return False
        self._append(
            ledger,
            "ZERO_WORK_TERMINAL_AUTHORITY_REBOUND",
            scope=str(record.get("id")),
            source_id=resolution.evidence_id,
            data={
                "previous_policy_status": previous["policy_status"],
                "previous_policy_generation": previous["policy_generation"],
                "policy_status": policy.status.value,
                "policy_generation": policy.generation,
                "previous_fence": previous["fence"],
                "fence": claim.get("fence"),
            },
        )
        return True

    def _authorized_legacy_terminal_replay(
        self,
        record: dict,
        messages: list[Message],
        ledger: dict,
        claim: object,
        policy: PolicySnapshot,
    ) -> Resolution | None:
        """Recognize an exact terminal landed after durable legacy authorization."""
        if not isinstance(claim, dict) or not (
            claim.get("state") == "open"
            and claim.get("drive_started_at")
            and not claim.get("drive_succeeded_at")
            and claim.get("claim_kind") is None
        ):
            return None
        try:
            claim_state = ResolverState(str(claim.get("resolution")))
        except ValueError:
            return Resolution(
                ResolverState.INDETERMINATE,
                "authorized legacy claim resolution is invalid",
            )
        if not Resolution(claim_state, "").allows_legacy_commit:
            return Resolution(
                ResolverState.INDETERMINATE,
                "authorized legacy claim is not a no-admission claim",
            )
        try:
            attempts = int(claim.get("drive_attempts", 0))
        except (TypeError, ValueError):
            return Resolution(
                ResolverState.INDETERMINATE,
                "authorized legacy drive attempt count is invalid",
            )
        authorizations = [
            event
            for event in ledger["transitions"]
            if isinstance(event, dict)
            and event.get("transition") == "PRE_ADMISSION_DRIVE_AUTHORIZED"
            and event.get("source_id") == record.get("id")
            and isinstance(event.get("data"), dict)
        ]
        try:
            authorized_attempts = {
                int(event["data"].get("attempt", 0)) for event in authorizations
            }
        except (TypeError, ValueError):
            authorized_attempts = set()
        if (
            attempts < 1
            or len(authorizations) != attempts
            or authorized_attempts != set(range(1, attempts + 1))
        ):
            return Resolution(
                ResolverState.INDETERMINATE,
                "authorized legacy terminal lacks exact drive authority",
            )
        if any(
            event["data"].get("policy_generation") != claim.get("policy_generation")
            for event in authorizations
        ):
            return Resolution(
                ResolverState.INDETERMINATE,
                "authorized legacy drive policy authority is torn",
            )
        replayed = self._resolve_replay(record, messages, ledger, admission=None)
        if replayed.state not in TERMINAL_STATES:
            return None
        if not self._claim_matches_policy(claim, policy):
            return self._policy_authority_failure(
                policy,
                reason="legacy terminal policy changed after work started",
            )
        if not isinstance(replayed.evidence_id, str):
            return Resolution(
                ResolverState.INDETERMINATE,
                "authorized legacy terminal evidence is invalid",
            )
        return replayed

    def _retain_same_generation_legacy_terminal_locked(
        self,
        ledger: dict,
        claim: dict,
        record: dict,
        replayed: Resolution,
        policy: PolicySnapshot,
    ) -> None:
        """Linearize an exact same-policy terminal before cursor finalization."""
        claim["state"] = "finalization_pending"
        claim["drive_succeeded_at"] = self.now()
        claim["resolution"] = replayed.state.value
        claim["claim_kind"] = "same_generation_legacy_terminal"
        claim["terminal_evidence_id"] = replayed.evidence_id
        self._append(
            ledger,
            "LEGACY_DRIVE_TERMINAL_RETAINED",
            scope=str(record.get("id")),
            source_id=replayed.evidence_id,
            data={
                "inbound_id": record.get("id"),
                "state": replayed.state.value,
                "policy_generation": policy.generation,
                "authorization_attempt": int(claim.get("drive_attempts", 0)),
            },
        )

    def _recover_authorized_legacy_terminal(
        self,
        record: dict,
    ) -> Resolution | None:
        """Recover a terminal landed after authorization but before retention."""
        inbound_id = record.get("id")
        if not isinstance(inbound_id, str):
            return None
        with ExitStack() as locks:
            locks.enter_context(self.store._message_publication_lock())
            try:
                messages, _ = self._validated_messages()
            except LedgerUnreadable as exc:
                return Resolution(ResolverState.BLOCKED, str(exc))
            observed_policy = self._current_policy()
            if self._policy_blocks_durable_terminal_projection(observed_policy):
                return Resolution(observed_policy.status, observed_policy.reason)
            locks.enter_context(
                self.store._exclusive_lock(
                    self.path.with_suffix(".lock"), timeout=10.0,
                )
            )
            policy_block = self._revalidate_admission_policy(observed_policy)
            if policy_block is not None:
                return policy_block
            ledger = self._load()
            claim = ledger["no_admission_claims"].get(inbound_id)
            replayed = self._authorized_legacy_terminal_replay(
                record,
                messages,
                ledger,
                claim,
                observed_policy,
            )
            if replayed is None or replayed.state not in TERMINAL_STATES:
                return replayed
            if not isinstance(claim, dict):
                return Resolution(
                    ResolverState.INDETERMINATE,
                    "authorized legacy terminal claim disappeared",
                )
            if claim.get("fence") != self.fence:
                if not self._can_reassign_no_admission_claim(claim):
                    return Resolution(
                        ResolverState.INDETERMINATE,
                        "prior authorized legacy owner is still authoritative",
                    )
                previous_fence = claim.get("fence")
                claim["fence"] = self.fence
                claim["owner_pid"] = os.getpid()
                self._append(
                    ledger,
                    "NO_ADMISSION_CLAIM_REASSIGNED",
                    scope=inbound_id,
                    source_id=replayed.evidence_id,
                    data={
                        "previous_fence": previous_fence,
                        "fence": self.fence,
                        "reason": "authorized_legacy_terminal",
                    },
                )
            self._retain_same_generation_legacy_terminal_locked(
                ledger,
                claim,
                record,
                replayed,
                observed_policy,
            )
            self._write(ledger)
            policy_block = self._revalidate_admission_policy(observed_policy)
            if policy_block is not None:
                return policy_block
            revision = int(ledger["scoped_revisions"].get(inbound_id, 0))
        return Resolution(
            replayed.state,
            "no_admission_finalization_pending",
            evidence_id=replayed.evidence_id,
            scoped_revision=revision,
            ledger_revision=revision,
            activation_generation=observed_policy.generation,
            readiness_generation=observed_policy.generation,
        )

    def _recognized_same_generation_legacy_terminal(
        self,
        record: dict,
        messages: list[Message],
        ledger: dict,
        claim: object,
        policy: PolicySnapshot,
    ) -> Resolution | None:
        """Recover a terminal produced by already-authorized same-policy work."""
        if not isinstance(claim, dict) or claim.get("claim_kind") != (
            "same_generation_legacy_terminal"
        ):
            return None
        if not self._claim_matches_policy(claim, policy):
            return self._policy_authority_failure(
                policy,
                reason="legacy terminal policy changed after work started",
            )
        if claim.get("state") not in {"finalization_pending", "finalized"} or not (
            claim.get("drive_started_at") and claim.get("drive_succeeded_at")
        ):
            return Resolution(
                ResolverState.INDETERMINATE,
                "legacy terminal retention state is torn",
            )
        try:
            state = ResolverState(str(claim.get("resolution")))
        except ValueError:
            state = ResolverState.INDETERMINATE
        evidence_id = claim.get("terminal_evidence_id")
        if state not in TERMINAL_STATES or not isinstance(evidence_id, str):
            return Resolution(
                ResolverState.INDETERMINATE,
                "legacy terminal retention evidence is invalid",
            )
        retained = [
            event
            for event in ledger["transitions"]
            if isinstance(event, dict)
            and event.get("transition") == "LEGACY_DRIVE_TERMINAL_RETAINED"
            and event.get("source_id") == evidence_id
            and isinstance(event.get("data"), dict)
            and event["data"].get("inbound_id") == record.get("id")
            and event["data"].get("state") == state.value
            and event["data"].get("policy_generation") == policy.generation
        ]
        if len(retained) != 1:
            return Resolution(
                ResolverState.INDETERMINATE,
                "legacy terminal retention authority is ambiguous",
            )
        replayed = self._replay_pinned_terminal_transition(
            record,
            messages,
            ledger,
            transition="LEGACY_DRIVE_TERMINAL_RETAINED",
            state=state,
            evidence_id=evidence_id,
        )
        if replayed.state == ResolverState.INDETERMINATE:
            return replayed
        if claim.get("state") == "finalized" and not (
            self._exact_no_admission_disposition_matches(
                claim,
                ledger["cursor_dispositions"].get(self.agent),
                record,
                replayed,
            )
        ):
            return Resolution(
                ResolverState.INDETERMINATE,
                "finalized legacy terminal disposition is torn",
            )
        return replayed

    def _recover_durable_no_admission_disposition(
        self,
        record: dict,
        messages: list[Message],
        ledger: dict,
        policy: PolicySnapshot,
    ) -> Resolution | None:
        """Replay a zero-work terminal whose durable disposition already won."""
        if self._policy_blocks_durable_terminal_projection(policy):
            return Resolution(policy.status, policy.reason)
        inbound_id = record.get("id")
        claim = ledger["no_admission_claims"].get(inbound_id)
        recognized = self._recognized_zero_work_terminal_authority(
            record,
            messages,
            ledger,
            claim,
        )
        if recognized is not None:
            if recognized.state == ResolverState.INDETERMINATE:
                return recognized
            replay_revision = int(ledger["scoped_revisions"].get(inbound_id, 0))
            with self.store._exclusive_lock(
                self.path.with_suffix(".lock"), timeout=10.0,
            ):
                current_policy = self._current_policy()
                if self._policy_blocks_durable_terminal_projection(current_policy):
                    return Resolution(current_policy.status, current_policy.reason)
                current = self._load()
                if int(current["scoped_revisions"].get(inbound_id, 0)) != (
                    replay_revision
                ):
                    return Resolution(
                        ResolverState.INDETERMINATE,
                        "zero-work terminal authority CAS miss",
                    )
                current_claim = current["no_admission_claims"].get(inbound_id)
                current_authority = self._recognized_zero_work_terminal_authority(
                    record,
                    messages,
                    current,
                    current_claim,
                )
                if current_authority is None or (
                    current_authority.state == ResolverState.INDETERMINATE
                ):
                    return current_authority or Resolution(
                        ResolverState.INDETERMINATE,
                        "zero-work terminal authority disappeared",
                    )
                if (
                    isinstance(current_claim, dict)
                    and current_claim.get("state") == "open"
                    and current_claim.get("fence") != self.fence
                    and not self._can_reassign_no_admission_claim(current_claim)
                ):
                    return Resolution(
                        ResolverState.INDETERMINATE,
                        "prior no-admission claim owner is still authoritative",
                    )
                if not isinstance(current_claim, dict):
                    return Resolution(
                        ResolverState.INDETERMINATE,
                        "zero-work terminal authority claim disappeared",
                    )
                if self._rebind_zero_work_terminal_authority_locked(
                    current,
                    current_claim,
                    record,
                    current_authority,
                    current_policy,
                ):
                    self._write(current)
                revision = int(current["scoped_revisions"].get(inbound_id, 0))
            return Resolution(
                current_authority.state,
                (
                    "no_admission_disposition_pending"
                    if current_claim.get("state") == "finalized"
                    else "no_admission_finalization_pending"
                    if current_authority.state == ResolverState.NOT_OWED
                    else current_authority.reason
                ),
                evidence_id=current_authority.evidence_id,
                scoped_revision=revision,
                ledger_revision=revision,
                activation_generation=current_policy.generation,
                readiness_generation=current_policy.generation,
            )
        authorized_terminal = self._authorized_legacy_terminal_replay(
            record,
            messages,
            ledger,
            claim,
            policy,
        )
        if authorized_terminal is not None:
            if authorized_terminal.state not in TERMINAL_STATES:
                return authorized_terminal
            recovered_terminal = self._recover_authorized_legacy_terminal(record)
            if recovered_terminal is not None:
                return recovered_terminal
            return Resolution(
                ResolverState.INDETERMINATE,
                "authorized legacy terminal disappeared during recovery",
            )
        legacy_terminal = self._recognized_same_generation_legacy_terminal(
            record,
            messages,
            ledger,
            claim,
            policy,
        )
        if legacy_terminal is not None:
            if legacy_terminal.state not in TERMINAL_STATES:
                return legacy_terminal
            replay_revision = int(ledger["scoped_revisions"].get(inbound_id, 0))
            with self.store._exclusive_lock(
                self.path.with_suffix(".lock"), timeout=10.0,
            ):
                current_policy = self._current_policy()
                if self._policy_blocks_durable_terminal_projection(current_policy):
                    return Resolution(current_policy.status, current_policy.reason)
                current = self._load()
                if int(current["scoped_revisions"].get(inbound_id, 0)) != (
                    replay_revision
                ):
                    return Resolution(
                        ResolverState.INDETERMINATE,
                        "legacy terminal recovery CAS miss",
                    )
                current_claim = current["no_admission_claims"].get(inbound_id)
                current_terminal = self._recognized_same_generation_legacy_terminal(
                    record,
                    messages,
                    current,
                    current_claim,
                    current_policy,
                )
                if current_terminal is None or current_terminal.state not in (
                    TERMINAL_STATES
                ):
                    return current_terminal or Resolution(
                        ResolverState.INDETERMINATE,
                        "legacy terminal retention disappeared",
                    )
                if not isinstance(current_claim, dict):
                    return Resolution(
                        ResolverState.INDETERMINATE,
                        "legacy terminal claim disappeared",
                    )
                if (
                    current_claim.get("state") == "finalization_pending"
                    and current_claim.get("fence") != self.fence
                ):
                    if not self._can_reassign_no_admission_claim(current_claim):
                        return Resolution(
                            ResolverState.INDETERMINATE,
                            "prior legacy terminal owner is still authoritative",
                        )
                    previous_fence = current_claim.get("fence")
                    current_claim["fence"] = self.fence
                    current_claim["owner_pid"] = os.getpid()
                    self._append(
                        current,
                        "NO_ADMISSION_CLAIM_REASSIGNED",
                        scope=str(inbound_id),
                        source_id=current_terminal.evidence_id,
                        data={
                            "previous_fence": previous_fence,
                            "fence": self.fence,
                            "reason": "same_generation_legacy_terminal",
                        },
                    )
                    self._write(current)
                revision = int(current["scoped_revisions"].get(inbound_id, 0))
            return Resolution(
                current_terminal.state,
                (
                    "no_admission_disposition_pending"
                    if current_claim.get("state") == "finalized"
                    else "no_admission_finalization_pending"
                ),
                evidence_id=current_terminal.evidence_id,
                scoped_revision=revision,
                ledger_revision=revision,
                activation_generation=current_policy.generation,
                readiness_generation=current_policy.generation,
            )
        if not isinstance(claim, dict) or claim.get("state") != "finalized":
            return None
        if not self._claim_has_no_legacy_work(claim):
            return None
        try:
            state = ResolverState(str(claim.get("resolution")))
        except ValueError:
            return Resolution(
                ResolverState.INDETERMINATE,
                "finalized no-admission resolution is invalid",
            )
        evidence_id = claim.get("terminal_evidence_id")
        if evidence_id is not None and not isinstance(evidence_id, str):
            return Resolution(
                ResolverState.INDETERMINATE,
                "finalized no-admission evidence is invalid",
            )
        resolution: Resolution
        if state in TERMINAL_STATES:
            replayed = self._resolve_replay(record, messages, ledger, admission=None)
            if replayed.state != state or replayed.evidence_id != evidence_id:
                return Resolution(
                    ResolverState.INDETERMINATE,
                    "finalized no-admission terminal conflicts with replay",
                )
            resolution = replayed
        elif state in {
            ResolverState.NOT_OWED,
            ResolverState.CLASSIFICATION_UNKNOWN,
            ResolverState.INACTIVE,
        }:
            inbound_message = next(
                (message for message in messages if message.id == inbound_id),
                None,
            )
            inbound_meta = (
                inbound_message.meta
                if isinstance(inbound_message, Message)
                and isinstance(inbound_message.meta, dict)
                else {}
            )
            transfer_digest = claim.get("transfer_from_key_digest")
            if not (
                state == ResolverState.NOT_OWED
                and _valid_hex_digest(transfer_digest)
                and inbound_meta.get("transfer_from_key_digest") == transfer_digest
            ):
                return Resolution(
                    ResolverState.INDETERMINATE,
                    "finalized no-admission legacy claim lacks zero-work authority",
                )
            resolution = Resolution(
                state,
                "no_admission_disposition_pending",
                evidence_id=evidence_id,
            )
        else:
            return Resolution(
                ResolverState.INDETERMINATE,
                "finalized no-admission state is not terminal",
            )
        if not self._durable_no_admission_disposition_matches(
            claim,
            ledger["cursor_dispositions"].get(self.agent),
            record,
            resolution,
        ):
            return Resolution(
                ResolverState.INDETERMINATE,
                "finalized no-admission disposition is torn",
            )
        revision = int(ledger["scoped_revisions"].get(inbound_id, 0))
        return Resolution(
            resolution.state,
            resolution.reason,
            evidence_id=resolution.evidence_id,
            scoped_revision=revision,
            ledger_revision=revision,
            activation_generation=policy.generation,
            readiness_generation=policy.generation,
        )

    def _policy_authority_failure(
        self,
        policy: PolicySnapshot,
        *,
        reason: str,
    ) -> Resolution:
        if policy.status in {
            ResolverState.BLOCKED,
            ResolverState.BLOCKED_POLICY,
            ResolverState.BLOCKED_COMPLIANCE,
        }:
            return Resolution(policy.status, policy.reason)
        return Resolution(ResolverState.BLOCKED_POLICY, reason)

    def _load(self, *, create: bool = True) -> dict:
        if not self.path.exists():
            if self.epoch_anchor_path.exists():
                raise LedgerUnreadable("obligation ledger missing while epoch anchor exists")
            if not create:
                raise LedgerUnreadable("obligation ledger missing")
            return _new_ledger()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise LedgerUnreadable(f"obligation ledger unreadable: {type(exc).__name__}") from exc
        ledger = _validate_ledger(raw)
        if self.epoch_anchor_path.exists():
            try:
                anchor = json.loads(self.epoch_anchor_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise LedgerUnreadable("obligation epoch anchor unreadable") from exc
            if not isinstance(anchor, dict) or anchor.get("store_epoch") != ledger["store_epoch"]:
                raise LedgerUnreadable("obligation store epoch mismatch")
            if int(anchor.get("append_sequence", -1)) > int(ledger["append_sequence"]):
                raise LedgerUnreadable("obligation append sequence rolled back")
            if int(anchor.get("revision", -1)) > int(ledger["revision"]):
                raise LedgerUnreadable("obligation ledger revision rolled back")
        return ledger

    def _write(self, ledger: dict) -> None:
        ledger["revision"] = int(ledger.get("revision", 0)) + 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(self.path, json.dumps(ledger, indent=2, ensure_ascii=False))
        _atomic_write_text(
            self.epoch_anchor_path,
            json.dumps({
                "store_epoch": ledger["store_epoch"],
                "append_sequence": ledger["append_sequence"],
                "revision": ledger["revision"],
            }, indent=2),
        )
        try:
            _atomic_write_text(
                self.checkpoint_path,
                json.dumps(ledger, indent=2, ensure_ascii=False),
            )
        except OSError:
            # The primary journal and epoch anchor are authoritative. A failed
            # recovery checkpoint must not turn a committed transaction into an
            # apparent failure.
            pass

    def _record_projection_rebuild(
        self,
        *,
        success: bool,
        detail: str,
        observed_at: str,
    ) -> None:
        now_text = observed_at
        lock = self.proof_health_path.with_suffix(".lock")
        with self.store._exclusive_lock(lock, timeout=10.0):
            try:
                health = json.loads(self.proof_health_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                health = {}
            if not isinstance(health, dict):
                health = {}
            health.setdefault("state", "blocked")
            health.setdefault("first_failure_at", now_text)
            health["rebuild_attempts"] = int(health.get("rebuild_attempts", 0)) + 1
            health["last_rebuild_at"] = now_text
            health["last_rebuild_succeeded"] = success
            health["last_rebuild_detail"] = detail[:512]
            self.proof_health_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(
                self.proof_health_path,
                json.dumps(health, indent=2, ensure_ascii=False),
            )

    def _try_rebuild_projection(self, *, observed_at: str) -> bool:
        """Restore a byte-corrupt projection only from an epoch-matched checkpoint."""
        try:
            with self.store._exclusive_lock(
                self.path.with_suffix(".lock"), timeout=10.0,
            ):
                checkpoint = _validate_ledger(json.loads(
                    self.checkpoint_path.read_text(encoding="utf-8")
                ))
                anchor = json.loads(self.epoch_anchor_path.read_text(encoding="utf-8"))
                if not isinstance(anchor, dict) or any((
                    anchor.get("store_epoch") != checkpoint["store_epoch"],
                    int(anchor.get("append_sequence", -1))
                    != checkpoint["append_sequence"],
                    int(anchor.get("revision", -1)) != checkpoint["revision"],
                )):
                    raise LedgerUnreadable("checkpoint does not match epoch anchor")
                _atomic_write_text(
                    self.path,
                    json.dumps(checkpoint, indent=2, ensure_ascii=False),
                )
        except (OSError, ValueError, json.JSONDecodeError, LedgerUnreadable) as exc:
            self._record_projection_rebuild(
                success=False,
                detail=str(exc),
                observed_at=observed_at,
            )
            return False
        self._record_projection_rebuild(
            success=True,
            detail="checkpoint restored",
            observed_at=observed_at,
        )
        return True

    def _append(self, ledger: dict, transition: str, *, scope: str | None = None,
                source_id: str | None = None, key_digest: str | None = None,
                data: dict | None = None) -> dict:
        sequence = int(ledger["append_sequence"]) + 1
        ledger["append_sequence"] = sequence
        event = {
            "sequence": sequence,
            "transition": transition,
            "at": self.now(),
            "source_id": source_id,
            "key_digest": key_digest,
            "data": data or {},
        }
        ledger["transitions"].append(event)
        if scope:
            revisions = ledger["scoped_revisions"]
            revisions[scope] = int(revisions.get(scope, 0)) + 1
        return event

    def _index_messages(
        self,
        messages: list[Message],
        *,
        invalid_records: list[tuple[str, str]] | None = None,
    ) -> dict:
        """Normalize validated bus records into the monotonic canonical journal."""
        messages = self.store.publication_ordered_messages(messages)
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            ledger = self._load()
            changed = False
            for message in messages:
                if message.id in ledger["messages"]:
                    continue
                rid = _correlation(message.meta)
                meta = message.meta or {}
                related_inbounds = [
                    mid for mid, row in ledger["messages"].items()
                    if isinstance(row, dict)
                    and row.get("kind") == "question"
                    and row.get("correlation_id") == rid
                ]
                if message.kind == "question" and rid:
                    related_inbounds.append(message.id)
                anchor = meta.get("in_reply_to")
                if isinstance(anchor, str) and anchor:
                    related_inbounds.append(anchor)
                event = self._append(
                    ledger,
                    "BUS_RECORD_APPENDED",
                    scope=rid,
                    source_id=message.id,
                    data={
                        "kind": message.kind,
                        "sender": message.sender,
                        "recipient": message.recipient,
                        "correlation_id": rid,
                        "timestamp": message.ts,
                    },
                )
                operation_nonce = meta.get("operation_nonce")
                operation_digest = meta.get("operation_digest")
                operation_payload_valid = bool(
                    isinstance(operation_nonce, str)
                    and isinstance(operation_digest, str)
                    and operation_digest == operation_payload_digest(
                        operation=(
                            "composing" if message.kind == "composing" else "terminal"
                        ),
                        body=message.body,
                        kind=message.kind,
                        recipient=message.recipient,
                        in_reply_to=meta.get("in_reply_to"),
                        request_id=meta.get("request_id"),
                        broadcast_id=meta.get("broadcast_id"),
                        origin_request_id=meta.get("origin_request_id"),
                        origin_inbound_id=meta.get("origin_inbound_id"),
                        origin_obligation_key_digest=meta.get(
                            "origin_obligation_key_digest"
                        ),
                        expected_roster_revision=meta.get(
                            "expected_roster_revision"
                        ),
                    )
                )
                message_row = {
                    "sequence": event["sequence"],
                    "correlation_id": rid,
                    "kind": message.kind,
                    "sender": message.sender,
                    "recipient": message.recipient,
                    "in_reply_to": meta.get("in_reply_to"),
                    "request_id": meta.get("request_id"),
                    "broadcast_id": meta.get("broadcast_id"),
                    "membership_snapshot": meta.get("membership_snapshot"),
                    "response_policy": meta.get("response_policy"),
                    "response_quorum": meta.get("response_quorum"),
                    "broadcast_policy_version": meta.get("broadcast_policy_version"),
                    "roster_revision": meta.get("roster_revision"),
                    "authorized_liaisons": meta.get("authorized_liaisons"),
                    "operation_nonce": operation_nonce,
                    "operation_payload_valid": operation_payload_valid,
                }
                ledger["messages"][message.id] = message_row
                revisions = ledger["scoped_revisions"]
                for inbound_id in set(related_inbounds):
                    revisions[inbound_id] = int(revisions.get(inbound_id, 0)) + 1

                descriptor = (
                    _broadcast_descriptor(meta, requester=message.sender)
                    if message.kind == "question" and meta.get("broadcast_id")
                    else None
                )
                if descriptor is not None:
                    bid = descriptor["broadcast_id"]
                    digest = _broadcast_descriptor_digest(descriptor)
                    aggregate = ledger["broadcasts"].get(bid)
                    if not isinstance(aggregate, dict):
                        aggregate = {
                            "state": "open",
                            "generation": 1,
                            "descriptor": descriptor,
                            "descriptor_digest": digest,
                            "member_inbounds": {},
                            "winning_ids": [],
                            "affected_member_keys": [],
                            "history": [],
                        }
                        ledger["broadcasts"][bid] = aggregate
                    if aggregate.get("descriptor_digest") != digest:
                        if aggregate.get("state") != "blocked":
                            aggregate["state"] = "blocked"
                            aggregate["blocked_reason"] = (
                                "immutable broadcast policy copies conflict"
                            )
                            self._append(
                                ledger,
                                "BROADCAST_POLICY_CONFLICT",
                                scope=message.id,
                                source_id=message.id,
                                data={
                                    "broadcast_id": bid,
                                    "expected_descriptor_digest": aggregate.get(
                                        "descriptor_digest"
                                    ),
                                    "observed_descriptor_digest": digest,
                                },
                            )
                    else:
                        prior_for_member = aggregate.get("member_inbounds", {}).get(
                            message.recipient,
                        )
                        if (
                            aggregate.get("state") == "policy_satisfied"
                            and isinstance(prior_for_member, list)
                            and prior_for_member
                        ):
                            history = list(aggregate.get("history") or [])
                            history.append({
                                name: value
                                for name, value in aggregate.items()
                                if name != "history"
                            })
                            aggregate = {
                                "state": "open",
                                "generation": int(
                                    aggregate.get("generation", 1)
                                ) + 1,
                                "descriptor": descriptor,
                                "descriptor_digest": digest,
                                "member_inbounds": {},
                                "winning_ids": [],
                                "affected_member_keys": [],
                                "history": history,
                            }
                            ledger["broadcasts"][bid] = aggregate
                        member_inbounds = aggregate.setdefault("member_inbounds", {})
                        member_inbounds.setdefault(message.recipient, []).append(message.id)
                        message_row["broadcast_generation"] = int(
                            aggregate.get("generation", 1)
                        )
                        if aggregate.get("state") == "policy_satisfied":
                            self._append(
                                ledger,
                                "BROADCAST_POLICY_SATISFIED",
                                scope=message.id,
                                source_id=(aggregate.get("winning_ids") or [None])[-1],
                                data={
                                    "aggregate": False,
                                    "broadcast_id": bid,
                                    "inbound_id": message.id,
                                    "winning_ids": list(
                                        aggregate.get("winning_ids") or []
                                    ),
                                    "winning_classes": list(
                                        aggregate.get("winning_classes") or []
                                    ),
                                    "policy": descriptor["response_policy"],
                                    "broadcast_policy_version": 1,
                                    "broadcast_generation": aggregate.get(
                                        "generation", 1
                                    ),
                                    "transaction_id": aggregate.get("transaction_id"),
                                    "prospective": True,
                                },
                            )
                if isinstance(anchor, str) and anchor:
                    anchor_row = ledger["messages"].get(anchor)
                    key_id = ledger["inbound_index"].get(anchor)
                    admission = ledger["obligations"].get(key_id) if key_id else None
                    anchor_descriptor = (
                        _broadcast_descriptor(
                            anchor_row,
                            requester=anchor_row.get("sender"),
                        )
                        if isinstance(anchor_row, dict)
                        else None
                    )
                    current_aggregate = (
                        ledger["broadcasts"].get(anchor_descriptor["broadcast_id"])
                        if anchor_descriptor is not None
                        else None
                    )
                    anchor_generation = anchor_row.get("broadcast_generation") if isinstance(
                        anchor_row, dict
                    ) else None
                    aggregate_candidates = (
                        [current_aggregate]
                        + list(current_aggregate.get("history") or [])
                        if isinstance(current_aggregate, dict)
                        else []
                    )
                    aggregate = next((
                        candidate for candidate in aggregate_candidates
                        if isinstance(candidate, dict)
                        and int(candidate.get("generation", 1))
                        == int(anchor_generation or 1)
                    ), None)
                    prospective_policy_close = any(
                        candidate.get("transition") == "BROADCAST_POLICY_SATISFIED"
                        and isinstance(candidate.get("data"), dict)
                        and candidate["data"].get("aggregate") is False
                        and candidate["data"].get("inbound_id") == anchor
                        and int(candidate.get("sequence", 0)) < int(event["sequence"])
                        for candidate in ledger["transitions"]
                    )
                    closed_by_policy = bool(
                        isinstance(aggregate, dict)
                        and aggregate.get("state") == "policy_satisfied"
                        and int(event["sequence"])
                        > int(aggregate.get("aggregate_sequence", 0))
                        and (
                            isinstance(admission, dict)
                            and admission.get("state") == "broadcast_policy_satisfied"
                            or not isinstance(admission, dict)
                            and prospective_policy_close
                        )
                    )
                    exact_late_response = bool(
                        anchor_descriptor is not None
                        and _correlation(meta) == anchor_descriptor["broadcast_id"]
                        and message.kind not in CONTROL_KINDS
                        and message.sender == anchor_row.get("recipient")
                        and message.recipient == anchor_row.get("sender")
                        and closed_by_policy
                        and message.id not in (aggregate.get("winning_ids") or [])
                    )
                    if exact_late_response:
                        self._append(
                            ledger,
                            "LATE_RESPONSE",
                            scope=anchor,
                            source_id=message.id,
                            key_digest=key_id,
                            data={
                                "broadcast_id": anchor_descriptor["broadcast_id"],
                                "closed_state": (
                                    admission.get("state")
                                    if isinstance(admission, dict)
                                    else "prospective_policy_satisfied"
                                ),
                                "transaction_id": aggregate.get("transaction_id"),
                            },
                        )
                if (
                    message.kind == "question"
                    and meta.get("broadcast_id")
                    and descriptor is None
                ):
                    telemetry = ledger["telemetry"]
                    telemetry["legacy_broadcast_unenforced_total"] = int(
                        telemetry.get("legacy_broadcast_unenforced_total", 0)
                    ) + 1
                changed = True
            seen_invalid = ledger["telemetry"].setdefault("invalid_records", {})
            for ident, reason in invalid_records or []:
                fingerprint = hashlib.sha256(
                    f"{ident}\0{reason}".encode("utf-8", errors="replace")
                ).hexdigest()
                if fingerprint in seen_invalid:
                    continue
                seen_invalid[fingerprint] = {
                    "id": str(ident),
                    "reason": str(reason)[:512],
                    "first_seen_at": self.now(),
                }
                self._append(
                    ledger,
                    "INVALID_RECORD_IGNORED",
                    data={"fingerprint": fingerprint, "id": str(ident)},
                )
                changed = True
            if changed or not self.path.exists():
                self._write(ledger)
            return ledger

    def _validated_messages(self) -> tuple[list[Message], dict]:
        try:
            messages = self.store.publication_ordered_messages()
            invalid_records = self.store.list_invalid_messages()
        except (OSError, ValueError, TimeoutError, RuntimeError) as exc:
            observed_at = self.now()
            failure = LedgerUnreadable("validated bus replay unavailable")
            self.record_proof_failure(
                error_class=type(failure).__name__,
                path=str(self.path),
                observed_at=observed_at,
            )
            raise failure from exc
        try:
            ledger = self._index_messages(messages, invalid_records=invalid_records)
        except (OSError, ValueError, TimeoutError, RuntimeError) as exc:
            observed_at = self.now()
            if isinstance(exc, LedgerUnreadable):
                if self._try_rebuild_projection(observed_at=observed_at):
                    failure = LedgerUnreadable(
                        "canonical projection rebuilt; fail-closed replay required"
                    )
                else:
                    failure = exc
            else:
                failure = LedgerUnreadable("canonical append service unavailable")
            self.record_proof_failure(
                error_class=type(failure).__name__,
                path=str(self.path),
                observed_at=observed_at,
            )
            if failure is exc:
                raise
            raise failure from exc
        self.clear_proof_failure()
        return messages, ledger

    def record_proof_failure(
        self,
        *,
        error_class: str,
        path: str,
        observed_at: str | None = None,
    ) -> dict:
        """Persist continuous unreadability; elapsed time alone exhausts it."""
        now_text = observed_at or self.now()
        lock = self.proof_health_path.with_suffix(".lock")
        with self.store._exclusive_lock(lock, timeout=10.0):
            try:
                health = json.loads(self.proof_health_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                health = {}
            if not isinstance(health, dict) or health.get("state") != "blocked":
                health = {
                    "state": "blocked",
                    "first_failure_at": now_text,
                    "failures": 0,
                    "alerted": False,
                }
            health["failures"] = int(health.get("failures", 0)) + 1
            health["last_failure_at"] = now_text
            health["fingerprint"] = {
                "error_class": str(error_class),
                "path": str(path),
            }
            first = _epoch(health.get("first_failure_at"))
            now = _epoch(now_text)
            elapsed = 0.0 if first is None or now is None else max(0.0, now - first)
            health["elapsed_seconds"] = elapsed
            if elapsed >= PROOF_UNREADABLE_SECONDS:
                health["exhausted"] = True
                health["alerted"] = True
                health.setdefault("alerted_at", now_text)
                incident_id = health.setdefault("incident_id", uuid.uuid4().hex)
                health.setdefault("incident", {
                    "kind": "PROOF_REPLAY_INCIDENT",
                    "incident_id": incident_id,
                    "first_failure_at": health.get("first_failure_at"),
                    "exhausted_at": now_text,
                    "authority": "elapsed_time",
                })
            self.proof_health_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(
                self.proof_health_path,
                json.dumps(health, indent=2, ensure_ascii=False),
            )
            return health

    def clear_proof_failure(self) -> None:
        try:
            health = json.loads(self.proof_health_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            health = None
        if isinstance(health, dict) and health.get("disposition_block") is True:
            return
        try:
            self.proof_health_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _eligibility(
        self,
        record: dict,
        *,
        policy: PolicySnapshot | None = None,
    ) -> tuple[ResolverState, str, str | None]:
        policy = policy or self._current_policy()
        try:
            health = json.loads(self.proof_health_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            health = None
        if isinstance(health, dict) and health.get("disposition_block") is True:
            return (
                ResolverState.BLOCKED,
                str(health.get("reason") or "failed-delivery disposition unavailable"),
                _correlation(record.get("meta")),
            )
        if policy.status in {
            ResolverState.BLOCKED,
            ResolverState.BLOCKED_POLICY,
            ResolverState.BLOCKED_COMPLIANCE,
        }:
            return policy.status, policy.reason, None
        if policy.status == ResolverState.INACTIVE:
            return ResolverState.INACTIVE, policy.reason, None
        if policy.status != ResolverState.ACTIVE:
            return ResolverState.NOT_OWED, policy.reason, None
        if record.get("kind") != "question":
            return ResolverState.NOT_OWED, "kind is detection log-only", None
        meta = record.get("meta") if isinstance(record.get("meta"), dict) else {}
        consult = _true(meta, "consult")
        if consult is None:
            return ResolverState.CLASSIFICATION_UNKNOWN, "consult flag unsupported", None
        if consult:
            return ResolverState.NOT_OWED, "consult excluded", None
        rid = _correlation(meta)
        if not rid:
            return ResolverState.CLASSIFICATION_UNKNOWN, "tracked correlation missing", None
        if record.get("to") != self.agent:
            return ResolverState.NOT_OWED, "not addressed to this wrapper", rid
        bid = meta.get("broadcast_id")
        if bid:
            descriptor = _broadcast_descriptor(meta, requester=record.get("from"))
            if descriptor is None:
                return (
                    ResolverState.CLASSIFICATION_UNKNOWN,
                    "legacy broadcast is not enforcement-eligible",
                    rid,
                )
            if self.agent not in descriptor["membership_snapshot"]:
                return (
                    ResolverState.CLASSIFICATION_UNKNOWN,
                    "broadcast recipient is absent from frozen membership",
                    rid,
                )
        transfer_from = meta.get("transfer_from_key_digest")
        transfer_generation = meta.get("transfer_policy_generation")
        if transfer_from is not None or transfer_generation is not None:
            if not _valid_hex_digest(transfer_from) or not _valid_hex_digest(
                transfer_generation
            ):
                return (
                    ResolverState.CLASSIFICATION_UNKNOWN,
                    "transfer destination marker is invalid",
                    rid,
                )
        obligation_class = (
            "human_escalation" if _true(meta, "escalation_required") is True else "answer"
        )
        return ResolverState.ACTIVE, obligation_class, rid

    def _record_message(self, record: dict, messages: list[Message]) -> Message | None:
        mid = record.get("id")
        return next((message for message in messages if message.id == mid), None)

    def _key_from(self, raw: dict) -> ObligationKey:
        return ObligationKey(**raw)

    def _resolve_replay(
        self,
        record: dict,
        messages: list[Message],
        ledger: dict,
        *,
        admission: dict | None,
    ) -> Resolution:
        inbound = self._record_message(record, messages)
        rid = _correlation(record.get("meta"))
        if inbound is None or rid is None:
            return Resolution(ResolverState.INDETERMINATE, "exact inbound missing")
        if (
            inbound.kind != "question"
            or inbound.sender != record.get("from")
            or inbound.recipient != self.agent
            or _correlation(inbound.meta) != rid
        ):
            return Resolution(ResolverState.CLASSIFICATION_UNKNOWN, "validated inbound mismatch")
        key = self._key_from(admission["key"]) if admission is not None else None
        inbound_descriptor = _broadcast_descriptor(
            inbound.meta,
            requester=inbound.sender,
        )
        aggregate_blocked_reason: str | None = None
        if inbound_descriptor is not None:
            aggregate = ledger["broadcasts"].get(
                inbound_descriptor["broadcast_id"],
            )
            if isinstance(aggregate, dict) and aggregate.get("state") == "blocked":
                aggregate_blocked_reason = str(
                    aggregate.get("blocked_reason")
                    or "immutable broadcast policy is blocked"
                )
        obligation_class = (
            str(admission.get("obligation_class"))
            if admission is not None
            else (
                "human_escalation"
                if _true(inbound.meta or {}, "escalation_required") is True
                else "answer"
            )
        )
        if key is not None and (
            key.reducer_version != REDUCER_VERSION
            or key.participant_capabilities_digest != PARTICIPANT_CAPABILITIES_DIGEST
        ):
            return Resolution(
                ResolverState.INDETERMINATE,
                "admission-pinned replay rules are unavailable",
                key,
            )
        if admission is not None and admission.get("cursor_projection_blocked") is True:
            return Resolution(
                ResolverState.BLOCKED,
                str(
                    admission.get("cursor_projection_blocked_reason")
                    or "cursor projection retry bound exhausted"
                ),
                key,
            )
        transfer_blocked_state = (
            admission.get("transfer_blocked_state")
            if admission is not None
            else None
        )
        if (
            admission is not None
            and admission.get("state") == "open"
            and isinstance(admission.get("broadcast_policy_close_pending"), dict)
        ):
            return Resolution(
                ResolverState.OWED_UNSATISFIED,
                "reserved dispatch owns pending broadcast policy close",
                key,
                scoped_revision=int(
                    ledger["scoped_revisions"].get(inbound.id, 0)
                ),
            )
        inbound_sequence = int(ledger["messages"].get(inbound.id, {}).get("sequence", 0))
        watermark = int(admission.get("watermark_sequence", 0)) if admission else inbound_sequence
        scope_revision = int(ledger["scoped_revisions"].get(inbound.id, 0))
        transition_states = {
            "MANUAL_CLOSE": ResolverState.SATISFIED,
            "TRANSFERRED": ResolverState.TRANSFERRED,
            "OPERATOR_RESOLUTION": ResolverState.OPERATOR_RESOLVED,
            "BROADCAST_POLICY_SATISFIED": ResolverState.BROADCAST_POLICY_SATISFIED,
            "DELIVERY_FAILED": ResolverState.DELIVERY_EXHAUSTED,
        }
        blocked_event: dict | None = None
        for event in ledger["transitions"]:
            if int(event.get("sequence", 0)) <= inbound_sequence:
                continue
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            exact_key = key is not None and event.get("key_digest") == key.digest
            prospective = key is None and data.get("inbound_id") == inbound.id
            if not (exact_key or prospective):
                continue
            transition = event.get("transition")
            if key is None and transition == "DELIVERY_FAILED":
                continue
            if (
                transition == "DELIVERY_FAILED"
                and admission is not None
                and admission.get("state") == "blocked"
            ):
                continue
            if transition == "OBLIGATION_BLOCKED":
                blocked_event = event
                continue
            state = transition_states.get(transition)
            if state is not None:
                return Resolution(
                    state,
                    str(event.get("transition", "terminal")).casefold(),
                    key,
                    event.get("source_id"),
                    scope_revision,
                )
        candidates = [
            message for message in messages
            if (
                _correlation(message.meta) == rid
                or (message.meta or {}).get("origin_request_id") == rid
            )
            and message.id != inbound.id
        ]
        candidates.sort(key=lambda message: int(
            ledger["messages"].get(message.id, {}).get("sequence", 0)))
        composing: Message | None = None
        for message in candidates:
            sequence = int(ledger["messages"].get(message.id, {}).get("sequence", 0))
            if sequence <= inbound_sequence:
                continue
            meta = message.meta or {}
            # Existing requester rescinds are intentionally rid-scoped and close every
            # open generation existing when the rescind is appended.
            if (
                message.kind == "rescind"
                and message.sender == inbound.sender
                and _correlation(meta) == rid
            ):
                supersedes = meta.get("supersedes")
                if supersedes is not None:
                    try:
                        superseded_key = self._key_from(supersedes)
                    except (TypeError, ValueError):
                        return Resolution(
                            ResolverState.INDETERMINATE,
                            "authorized exact-key supersession is uninterpretable",
                            key,
                            message.id,
                            scope_revision,
                        )
                    if key is None or superseded_key != key:
                        continue
                return Resolution(
                    ResolverState.SUPERSEDED,
                    (
                        "requester superseded exact generation"
                        if supersedes is not None
                        else "requester rescinded all visible generations"
                    ),
                    key,
                    message.id,
                    scope_revision,
                )
            if admission is not None and sequence <= watermark:
                continue
            exact = meta.get("in_reply_to") == inbound.id
            exact_correlation = _correlation(meta) == rid
            terminal_nonce = meta.get("operation_nonce")
            reservations = admission.get("reservations", {}) if admission else {}
            terminal_token_valid = bool(
                admission is None
                or isinstance(terminal_nonce, str)
                and isinstance(reservations.get(terminal_nonce), dict)
                and _message_operation_valid(
                    message,
                    operation="terminal",
                    nonce=terminal_nonce,
                )
            )
            if (
                message.kind == "composing"
                and message.sender == self.agent
                and exact
                and exact_correlation
            ):
                composing_token = meta.get("operation_nonce")
                composing_token_valid = any(
                    isinstance(row, dict)
                    and row.get("composing_nonce") == composing_token
                    for row in reservations.values()
                ) and isinstance(composing_token, str) and _message_operation_valid(
                    message,
                    operation="composing",
                    nonce=composing_token,
                )
                if composing_token_valid:
                    composing = message
                continue
            if obligation_class == "human_escalation":
                if (
                    message.kind == "question"
                    and message.sender == self.agent
                    and meta.get("origin_request_id") == rid
                    and meta.get("origin_inbound_id") == inbound.id
                    and isinstance(meta.get("roster_revision"), str)
                    and message.recipient in (meta.get("authorized_liaisons") or [])
                    and terminal_token_valid
                ):
                    return Resolution(
                        ResolverState.SATISFIED,
                        "human escalation landed",
                        key,
                        message.id,
                        scope_revision,
                        compliance_success=True,
                    )
                continue
            if not exact or not exact_correlation:
                continue
            if not terminal_token_valid:
                continue
            event = threads._classify_event(  # noqa: SLF001 - normative reducer authority
                "question", message, inbound.sender, self.agent, self.agent,
            )
            if event and event[0] == "terminal":
                return Resolution(
                    ResolverState.SATISFIED,
                    "thread replay closed assignment",
                    key,
                    message.id,
                    scope_revision,
                    compliance_success=True,
                )
        # A canonical terminal published for the exact assignment must never be
        # masked by a locally synthesized BLOCKED transition.  Internal BLOCKED
        # remains authoritative only when replay found no qualifying terminal.
        if blocked_event is not None:
            return Resolution(
                ResolverState.BLOCKED,
                "obligation_blocked",
                key,
                blocked_event.get("source_id"),
                scope_revision,
            )
        if aggregate_blocked_reason is not None:
            return Resolution(
                ResolverState.BLOCKED,
                aggregate_blocked_reason,
                key,
                scoped_revision=scope_revision,
            )
        if isinstance(transfer_blocked_state, str):
            try:
                transfer_state = ResolverState(transfer_blocked_state)
            except ValueError:
                transfer_state = ResolverState.BLOCKED
            return Resolution(
                transfer_state,
                str(
                    admission.get("transfer_blocked_reason")
                    or "transfer destination validation is blocked"
                ),
                key,
                scoped_revision=scope_revision,
            )
        if composing is not None and admission is not None:
            first = _epoch(admission.get("first_deferred_at"))
            now = _epoch(self.now())
            continuation = admission.get("durable_continuation")
            owner = admission.get("producer_token")
            live = isinstance(owner, str) and self.producer_alive(owner)
            if first is not None and now is not None:
                if now - first <= MAX_DEFERRAL_SECONDS:
                    if live or isinstance(continuation, dict):
                        return Resolution(
                            ResolverState.IN_PROGRESS,
                            "authenticated producer active or continuation scheduled",
                            key,
                            composing.id,
                            scope_revision,
                        )
                else:
                    return Resolution(
                        ResolverState.OWED_UNSATISFIED,
                        "post_budget_composing",
                        key,
                        composing.id,
                        scope_revision,
                        activation_generation=(
                            str(admission.get("activation_generation"))
                            if admission.get("activation_generation") is not None
                            else None
                        ),
                        readiness_generation=(
                            str(admission.get("readiness_generation"))
                            if admission.get("readiness_generation") is not None
                            else None
                        ),
                    )
        return Resolution(
            ResolverState.OWED_UNSATISFIED,
            "replay leaves next move with responder",
            key,
            scoped_revision=scope_revision,
            activation_generation=(
                str(admission.get("activation_generation")) if admission else None
            ),
            readiness_generation=(
                str(admission.get("readiness_generation")) if admission else None
            ),
        )

    def _persist_post_budget_composing(self, resolution: Resolution) -> Resolution:
        """Durably classify an expired composing marker before another dispatch."""
        if (
            resolution.state != ResolverState.OWED_UNSATISFIED
            or resolution.reason != "post_budget_composing"
            or resolution.key is None
            or not isinstance(resolution.evidence_id, str)
        ):
            return resolution
        key = resolution.key
        with ExitStack() as locks:
            # Canonical publication and this projection/classification share an
            # ordering domain. If a bus record already landed but its eager
            # projection hook failed or is waiting, replay it before the CAS.
            locks.enter_context(self.store._message_publication_lock())
            try:
                messages, _ = self._validated_messages()
            except LedgerUnreadable as exc:
                return Resolution(ResolverState.BLOCKED, str(exc), key)
            locks.enter_context(
                self.store._exclusive_lock(
                    self.path.with_suffix(".lock"),
                    timeout=10.0,
                ),
            )
            ledger = self._load()
            admission = ledger["obligations"].get(key.digest)
            if (
                not isinstance(admission, dict)
                or admission.get("state") != "open"
                or admission.get("fence") != self.fence
                or not self._live_dispatch_fence_owned()
            ):
                return Resolution(
                    ResolverState.INDETERMINATE,
                    "post-budget composing owner changed before classification",
                    key,
                )
            breaker = ledger["breakers"].get(self.agent, {})
            if breaker.get("tripped") is True or breaker.get("config_blocked") is True:
                return Resolution(
                    ResolverState.BLOCKED_COMPLIANCE,
                    "compliance breaker tripped",
                    key,
                )
            if (
                int(ledger["scoped_revisions"].get(key.inbound_id, 0))
                != resolution.scoped_revision
            ):
                return Resolution(
                    ResolverState.INDETERMINATE,
                    "post-budget composing classification CAS miss",
                    key,
                )
            evidence_id = admission.get("post_budget_composing_evidence_id")
            if evidence_id is not None and not isinstance(evidence_id, str):
                return Resolution(
                    ResolverState.INDETERMINATE,
                    "post-budget composing evidence is invalid",
                    key,
                )
            changed = False
            if evidence_id is None:
                evidence_id = resolution.evidence_id
                admission["post_budget_composing_evidence_id"] = evidence_id
                self._append(
                    ledger,
                    "OWED_ACTION_MISSING",
                    source_id=evidence_id,
                    key_digest=key.digest,
                    data={"evidence": "post_budget_composing"},
                )
                changed = True
            else:
                evidence = [
                    event
                    for event in ledger["transitions"]
                    if event.get("transition") == "OWED_ACTION_MISSING"
                    and event.get("source_id") == evidence_id
                    and event.get("key_digest") == key.digest
                    and isinstance(event.get("data"), dict)
                    and event["data"].get("evidence") == "post_budget_composing"
                ]
                if len(evidence) != 1:
                    return Resolution(
                        ResolverState.INDETERMINATE,
                        "post-budget composing evidence transition is invalid",
                        key,
                    )
            captured_rows = [
                (nonce, row)
                for nonce, row in admission.get("reservations", {}).items()
                if isinstance(row, dict) and row.get("state") == "action_infra"
            ]
            if any(
                not self._historical_capture_valid(key, admission, nonce, row)
                for nonce, row in captured_rows
            ):
                return Resolution(
                    ResolverState.INDETERMINATE,
                    "captured infrastructure evidence is invalid",
                    key,
                )
            captured_infra = bool(captured_rows)
            required = {
                "owed_action_missing_seen": True,
                "first_dispatch_classified": True,
                "last_exhaustion_class": (
                    "infrastructure" if captured_infra else "compliance"
                ),
            }
            for field, value in required.items():
                if admission.get(field) != value:
                    admission[field] = value
                    changed = True
            if changed:
                self._write(ledger)
            return Resolution(
                ResolverState.OWED_UNSATISFIED,
                "post_budget_composing",
                key,
                evidence_id,
                int(ledger["scoped_revisions"].get(key.inbound_id, 0)),
                ledger_revision=int(ledger["revision"]),
                activation_generation=(
                    str(admission.get("activation_generation"))
                    if admission.get("activation_generation") is not None
                    else None
                ),
                readiness_generation=(
                    str(admission.get("readiness_generation"))
                    if admission.get("readiness_generation") is not None
                    else None
                ),
            )

    def _historical_capture_valid(
        self,
        key: ObligationKey,
        admission: dict,
        nonce: str,
        row: dict,
    ) -> bool:
        """Validate durable capture identity without requiring the mutable draft."""
        digest = row.get("operation_payload_digest")
        intent = row.get("operation_intent")
        if (
            not self._valid_dispatch_nonce(nonce)
            or not isinstance(digest, str)
            or len(digest) != 64
            or digest != digest.lower()
            or not isinstance(intent, dict)
        ):
            return False
        try:
            int(digest, 16)
        except ValueError:
            return False
        marker = self.store.read_operation_intent(self.agent, nonce)
        if (
            not isinstance(marker, dict)
            or marker.get("operation_digest") != digest
            or marker.get("intent_digest")
            != self.store._operation_intent_digest(intent)
        ):
            return False
        if intent.get("operation") != "terminal" or intent.get(
            "in_reply_to",
        ) != key.inbound_id:
            return False
        if admission.get("obligation_class") == "human_escalation":
            return bool(
                intent.get("kind") == "question"
                and isinstance(intent.get("recipient"), str)
                and intent.get("recipient") not in {"", self.agent}
                and intent.get("request_id") == f"esc-{nonce[:12]}"
                and intent.get("broadcast_id") is None
                and intent.get("origin_request_id") == key.correlation_id
                and intent.get("origin_inbound_id") == key.inbound_id
                and intent.get("origin_obligation_key_digest") == key.digest
                and _valid_hex_digest(intent.get("expected_roster_revision"))
            )
        broadcast_id = admission.get("broadcast_id")
        return bool(
            intent.get("kind") == "message"
            and intent.get("recipient") == key.requester
            and intent.get("request_id")
            == (None if isinstance(broadcast_id, str) else key.correlation_id)
            and intent.get("broadcast_id")
            == (broadcast_id if isinstance(broadcast_id, str) else None)
            and intent.get("origin_request_id") is None
            and intent.get("origin_inbound_id") is None
        )

    def _persist_unsatisfied_attempts(self, resolution: Resolution) -> Resolution:
        """Recover split result/classification writes before a corrected retry."""
        if resolution.state != ResolverState.OWED_UNSATISFIED or resolution.key is None:
            return resolution
        key = resolution.key
        with ExitStack() as locks:
            locks.enter_context(self.store._message_publication_lock())
            try:
                messages, _ = self._validated_messages()
            except LedgerUnreadable as exc:
                return Resolution(ResolverState.BLOCKED, str(exc), key)
            locks.enter_context(
                self.store._exclusive_lock(
                    self.path.with_suffix(".lock"),
                    timeout=10.0,
                ),
            )
            ledger = self._load()
            admission = ledger["obligations"].get(key.digest)
            if (
                not isinstance(admission, dict)
                or admission.get("state") != "open"
                or admission.get("fence") != self.fence
                or not self._live_dispatch_fence_owned()
            ):
                return Resolution(
                    ResolverState.INDETERMINATE,
                    "unsatisfied attempt owner changed before classification",
                    key,
                )
            breaker = ledger["breakers"].get(self.agent, {})
            if breaker.get("tripped") is True or breaker.get("config_blocked") is True:
                return Resolution(
                    ResolverState.BLOCKED_COMPLIANCE,
                    "compliance breaker tripped",
                    key,
                )
            if (
                int(ledger["scoped_revisions"].get(key.inbound_id, 0))
                != resolution.scoped_revision
            ):
                return Resolution(
                    ResolverState.INDETERMINATE,
                    "unsatisfied attempt classification CAS miss",
                    key,
                )
            pending = [
                (nonce, row)
                for nonce, row in admission.get("reservations", {}).items()
                if isinstance(row, dict)
                and row.get("state") == "completed"
                and row.get("action_attempted") is True
            ]
            if not pending:
                return resolution
            reason = "replay remained unsatisfied after attempted action"
            for nonce, row in pending:
                row["state"] = "action_rejected"
                row["rejection_reason"] = reason
                self._append(
                    ledger,
                    "ACTION_REJECTED",
                    key_digest=key.digest,
                    data={"nonce": nonce, "reason": reason},
                )
            admission["owed_action_missing_seen"] = True
            admission["first_dispatch_classified"] = True
            admission["last_exhaustion_class"] = "compliance"
            self._write(ledger)
            return Resolution(
                resolution.state,
                resolution.reason,
                key,
                resolution.evidence_id,
                int(ledger["scoped_revisions"].get(key.inbound_id, 0)),
                ledger_revision=int(ledger["revision"]),
                compliance_success=resolution.compliance_success,
                activation_generation=resolution.activation_generation,
                readiness_generation=resolution.readiness_generation,
            )

    def _can_reassign_fenced_owner(self, owner: dict) -> bool:
        """Require current wrapper ownership and a stale prior owner before takeover."""
        if self.store.wrapper_wait_generation(self.agent) != self.fence:
            return False
        if self.store.is_managed_lead_loop(self.agent):
            lease = self.store.read_lead_loop_lease(self.agent)
            return bool(
                isinstance(lease, dict)
                and lease.get("wrapper_generation") == self.fence
            )
        return _process_liveness(owner.get("owner_pid")) == PROC_DEAD

    def _can_reassign_no_admission_claim(self, claim: dict) -> bool:
        return self._can_reassign_fenced_owner(claim)

    def _reconcile_closed_dispatch_slots_locked(self, ledger: dict) -> bool:
        """Release only terminal slots whose dispatch can no longer be authoritative."""
        changed = False
        for key_digest, admission in ledger["obligations"].items():
            if (
                not isinstance(admission, dict)
                or admission.get("state") not in CLOSED_ADMISSION_STATES
                or self._key_from(admission.get("key")).responder != self.agent
            ):
                continue
            for nonce, row in admission.get("reservations", {}).items():
                if not isinstance(row, dict):
                    continue
                prior_state = row.get("state")
                if prior_state == "reserved":
                    # No external model side effect was armed. A concurrent
                    # dispatch_record will revalidate the now-closed admission
                    # and fail, so this intent cannot consume capacity forever.
                    pass
                elif prior_state == "dispatching":
                    dispatch_fence = row.get("dispatch_fence")
                    if (
                        not isinstance(dispatch_fence, str)
                        or dispatch_fence == self.fence
                        or _process_liveness(row.get("dispatch_owner_pid"))
                        != PROC_DEAD
                        or not self._can_reassign_fenced_owner(admission)
                    ):
                        # A live or unprovably-dead owner remains fail-closed and
                        # continues to consume the agent-wide concurrency slot.
                        continue
                else:
                    continue
                row["state"] = "cancelled_terminal"
                row["cancelled_at"] = self.now()
                self._append(
                    ledger,
                    "TERMINAL_DISPATCH_RECONCILED",
                    key_digest=str(key_digest),
                    data={
                        "nonce": nonce,
                        "prior_state": prior_state,
                        "terminal_state": admission.get("terminal_state"),
                    },
                )
                changed = True
        return changed

    def _captured_payload_digest(self, row: dict, nonce: str) -> str | None:
        intent = row.get("operation_intent")
        draft = Path(str(row.get("draft_path", "")))
        if not isinstance(intent, dict):
            return None
        try:
            resolved = draft.resolve(strict=True)
            root = self.drafts.resolve(strict=True)
            resolved.relative_to(root)
            if draft.is_symlink() or resolved.stat().st_size > 1024 * 1024:
                return None
            body = resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError):
            return None
        if not body:
            return None
        digest = operation_payload_digest(
            operation=str(intent.get("operation", "")),
            body=body,
            kind=str(intent.get("kind", "")),
            recipient=str(intent.get("recipient", "")),
            in_reply_to=intent.get("in_reply_to"),
            request_id=intent.get("request_id"),
            broadcast_id=intent.get("broadcast_id"),
            origin_request_id=intent.get("origin_request_id"),
            origin_inbound_id=intent.get("origin_inbound_id"),
            origin_obligation_key_digest=intent.get(
                "origin_obligation_key_digest"
            ),
            expected_roster_revision=intent.get("expected_roster_revision"),
        )
        marker = self.store.read_operation_intent(self.agent, nonce)
        payload = body.encode("utf-8")
        if (
            not isinstance(marker, dict)
            or marker.get("operation_digest") != digest
            or marker.get("intent_digest")
            != self.store._operation_intent_digest(intent)
            or marker.get("payload_sha256") != hashlib.sha256(payload).hexdigest()
            or marker.get("payload_size") != len(payload)
        ):
            return None
        return digest

    def _recover_captured_dispatches_locked(
        self,
        ledger: dict,
        admission: dict,
        key: ObligationKey,
        *,
        previous_fence: str,
    ) -> None:
        """Recover a durable draft only after the prior wrapper is proven dead."""
        for nonce, row in admission.get("reservations", {}).items():
            if (
                not isinstance(row, dict)
                or row.get("state") != "dispatching"
                or row.get("dispatch_fence") != previous_fence
            ):
                continue
            digest = self._captured_payload_digest(row, str(nonce))
            if digest is None:
                continue
            recovered_at = self.now()
            row["state"] = "action_infra"
            row["operation_payload_digest"] = digest
            row["captured_at"] = recovered_at
            admission["first_dispatch_classified"] = True
            admission["last_exhaustion_class"] = "infrastructure"
            admission["operation_infra_attempts"] = max(
                1,
                int(admission.get("operation_infra_attempts", 0)),
            )
            admission["operation_infra_first_at"] = (
                admission.get("operation_infra_first_at") or recovered_at
            )
            self._append(
                ledger,
                "OPERATION_INTENT_RECOVERED",
                key_digest=key.digest,
                data={"nonce": nonce, "payload_digest": digest},
            )

    def _claim_open_admission(
        self,
        key_digest: str,
        inbound_id: str,
    ) -> tuple[dict, dict] | Resolution:
        """Fence an open admission to this live wrapper or fail closed."""
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            ledger = self._load()
            admission = ledger["obligations"].get(key_digest)
            if not isinstance(admission, dict):
                return Resolution(ResolverState.INDETERMINATE, "admission disappeared")
            if admission.get("state") != "open":
                return ledger, admission
            key = self._key_from(admission["key"])
            if key.inbound_id != inbound_id or key.responder != self.agent:
                return Resolution(
                    ResolverState.INDETERMINATE,
                    "admission ownership does not match exact inbound",
                    key,
                )
            previous_fence = admission.get("fence")
            if previous_fence == self.fence:
                return ledger, admission
            if self.store.wrapper_wait_generation(self.agent) != self.fence:
                return Resolution(
                    ResolverState.INDETERMINATE,
                    "replacement wrapper does not own the waiting generation",
                    key,
                )
            if previous_fence == "unclaimed":
                policy = self._current_policy()
                if (
                    policy.status != ResolverState.ACTIVE
                    or admission.get("activation_generation") != policy.generation
                    or admission.get("readiness_generation") != policy.generation
                ):
                    return Resolution(
                        ResolverState.BLOCKED_POLICY,
                        "transfer policy generation changed before claim",
                        key,
                    )
                transition = "DELIVERY_CLAIMED"
            else:
                if not isinstance(previous_fence, str) or not self._can_reassign_fenced_owner(
                    admission,
                ):
                    return Resolution(
                        ResolverState.INDETERMINATE,
                        "prior admission owner is still authoritative",
                        key,
                    )
                transition = "DELIVERY_CLAIM_REASSIGNED"
            admission["fence"] = self.fence
            admission["owner_pid"] = os.getpid()
            self._recover_captured_dispatches_locked(
                ledger,
                admission,
                key,
                previous_fence=str(previous_fence),
            )
            self._append(
                ledger,
                transition,
                scope=inbound_id,
                key_digest=key.digest,
                data={"previous_fence": previous_fence, "fence": self.fence},
            )
            self._write(ledger)
            return ledger, admission

    def _normalize_pre_admission_terminal(
        self,
        record: dict,
        inbound: Message,
        replayed: Resolution,
        observed_policy: PolicySnapshot,
    ) -> Resolution:
        """Durably recognize exact zero-work terminal authority before projection."""
        if replayed.state not in TERMINAL_STATES or not isinstance(
            replayed.evidence_id,
            str,
        ):
            return Resolution(
                ResolverState.INDETERMINATE,
                "pre-admission terminal replay is invalid",
            )
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            policy_block = self._revalidate_admission_policy(observed_policy)
            if policy_block is not None:
                return policy_block
            current = self._load()
            if int(current["scoped_revisions"].get(inbound.id, 0)) != (
                replayed.scoped_revision
            ):
                return Resolution(ResolverState.INDETERMINATE, "normalization CAS miss")
            if inbound.id in current["inbound_index"]:
                return Resolution(
                    ResolverState.INDETERMINATE,
                    "concurrent admission won before terminal normalization",
                )
            claim = current["no_admission_claims"].get(inbound.id)
            if isinstance(claim, dict):
                if claim.get("state") == "blocked":
                    return Resolution(
                        ResolverState.BLOCKED,
                        str(claim.get("blocked_reason") or "finalization blocked"),
                    )
                semantic_claim = bool(
                    self._claim_is_untouched(claim)
                    and claim.get("claim_kind") is None
                )
                if semantic_claim:
                    if claim.get("fence") != self.fence:
                        if not self._can_reassign_no_admission_claim(claim):
                            return Resolution(
                                ResolverState.INDETERMINATE,
                                "prior no-admission claim owner is still authoritative",
                            )
                        previous_fence = claim.get("fence")
                        claim["fence"] = self.fence
                        claim["owner_pid"] = os.getpid()
                        self._append(
                            current,
                            "NO_ADMISSION_CLAIM_REASSIGNED",
                            scope=inbound.id,
                            source_id=replayed.evidence_id,
                            data={
                                "previous_fence": previous_fence,
                                "fence": self.fence,
                                "reason": "terminal_normalization",
                            },
                        )
                    claim["resolution"] = replayed.state.value
                    claim["claim_kind"] = "pre_admission_terminal"
                    claim["terminal_evidence_id"] = replayed.evidence_id
                    claim["policy_status"] = observed_policy.status.value
                    claim["policy_generation"] = observed_policy.generation
                    self._append(
                        current,
                        "PRE_ADMISSION_TERMINAL_NORMALIZED",
                        scope=inbound.id,
                        source_id=replayed.evidence_id,
                        data={"inbound_id": inbound.id, "state": replayed.state.value},
                    )
                    self._write(current)
                elif not self._claim_matches_policy(claim, observed_policy):
                    return self._policy_authority_failure(
                        observed_policy,
                        reason="terminal normalization policy changed",
                    )
                if claim.get("resolution") != replayed.state.value or claim.get(
                    "state"
                ) not in {"open", "finalized"}:
                    return Resolution(
                        ResolverState.INDETERMINATE,
                        "no-admission claim conflicts with terminal replay",
                    )
                if claim.get("state") == "open" and claim.get("fence") != self.fence:
                    if not self._can_reassign_no_admission_claim(claim):
                        return Resolution(
                            ResolverState.INDETERMINATE,
                            "prior no-admission claim owner is still authoritative",
                        )
                    previous_fence = claim.get("fence")
                    claim["fence"] = self.fence
                    claim["owner_pid"] = os.getpid()
                    self._append(
                        current,
                        "NO_ADMISSION_CLAIM_REASSIGNED",
                        scope=inbound.id,
                        source_id=replayed.evidence_id,
                        data={
                            "previous_fence": previous_fence,
                            "fence": self.fence,
                        },
                    )
                    self._write(current)
            else:
                current["no_admission_claims"][inbound.id] = {
                    "fence": self.fence,
                    "owner_pid": os.getpid(),
                    "state": "open",
                    "resolution": replayed.state.value,
                    "claim_kind": "pre_admission_terminal",
                    "terminal_evidence_id": replayed.evidence_id,
                    "policy_status": observed_policy.status.value,
                    "policy_generation": observed_policy.generation,
                    "finalization_misses": 0,
                    "finalization_first_at": None,
                    "cursor_projection_misses": 0,
                    "cursor_projection_first_at": None,
                    "cursor_projection_inflight": False,
                    "cursor_projection_reserved_at": None,
                }
                self._append(
                    current,
                    "PRE_ADMISSION_TERMINAL_NORMALIZED",
                    scope=inbound.id,
                    source_id=replayed.evidence_id,
                    data={"inbound_id": inbound.id, "state": replayed.state.value},
                )
                self._write(current)
            scoped_revision = int(current["scoped_revisions"].get(inbound.id, 0))
        return Resolution(
            replayed.state,
            replayed.reason,
            evidence_id=replayed.evidence_id,
            scoped_revision=scoped_revision,
            ledger_revision=scoped_revision,
            activation_generation=observed_policy.generation,
            readiness_generation=observed_policy.generation,
        )

    def admit_or_finalize(self, record: dict) -> Resolution:
        observed_policy = self._current_policy()
        eligibility, detail, rid = self._eligibility(
            record,
            policy=observed_policy,
        )
        if eligibility != ResolverState.ACTIVE:
            blocked_states = {
                ResolverState.BLOCKED,
                ResolverState.BLOCKED_POLICY,
                ResolverState.BLOCKED_COMPLIANCE,
            }
            readable_policy_block = (
                eligibility == ResolverState.BLOCKED
                and observed_policy.status == ResolverState.BLOCKED
                and detail == observed_policy.reason
                and rid is None
                and not self._policy_blocks_durable_terminal_projection(
                    observed_policy
                )
            )
            if (
                eligibility in blocked_states
                and not readable_policy_block
            ):
                return Resolution(
                    eligibility,
                    detail,
                    activation_generation=observed_policy.generation,
                    readiness_generation=observed_policy.generation,
                )
            try:
                messages, ledger = self._validated_messages()
            except LedgerUnreadable as exc:
                return Resolution(ResolverState.BLOCKED, str(exc))
            inbound = self._record_message(record, messages)
            if inbound is None:
                return Resolution(
                    ResolverState.INDETERMINATE,
                    "non-admission head absent from validated replay",
                )
            recovered = self._recover_durable_no_admission_disposition(
                record,
                messages,
                ledger,
                observed_policy,
            )
            if recovered is not None:
                return recovered
            replayed_terminal = self._resolve_replay(
                record,
                messages,
                ledger,
                admission=None,
            )
            if replayed_terminal.state in TERMINAL_STATES:
                return self._normalize_pre_admission_terminal(
                    record,
                    inbound,
                    replayed_terminal,
                    observed_policy,
                )
            if eligibility in blocked_states:
                return Resolution(
                    eligibility,
                    detail,
                    activation_generation=observed_policy.generation,
                    readiness_generation=observed_policy.generation,
                )
            if observed_policy.generation == "inactive":
                return Resolution(
                    eligibility,
                    detail,
                    activation_generation=observed_policy.generation,
                    readiness_generation=observed_policy.generation,
                )
            replay_revision = int(ledger["scoped_revisions"].get(inbound.id, 0))
            if eligibility == ResolverState.CLASSIFICATION_UNKNOWN:
                name = "classification_unknown_heads"
                transition = "CLASSIFICATION_UNKNOWN"
            elif eligibility == ResolverState.INACTIVE:
                name = "inactive_policy_heads"
                transition = "POLICY_INACTIVE"
            else:
                name = "semantic_noneligible_heads"
                transition = "SEMANTICALLY_NOT_OWED"
            with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
                policy_block = self._revalidate_admission_policy(observed_policy)
                if policy_block is not None:
                    return policy_block
                current = self._load()
                if int(current["scoped_revisions"].get(inbound.id, 0)) != replay_revision:
                    return Resolution(
                        ResolverState.INDETERMINATE,
                        "no-admission claim CAS miss",
                    )
                if inbound.id in current["inbound_index"]:
                    return Resolution(
                        ResolverState.INDETERMINATE,
                        "concurrent admission won before no-admission claim",
                    )
                claim = current["no_admission_claims"].get(inbound.id)
                if isinstance(claim, dict):
                    if not self._claim_matches_policy(claim, observed_policy):
                        if not self._claim_is_untouched(claim):
                            return self._policy_authority_failure(
                                observed_policy,
                                reason=(
                                    "no-admission claim policy changed after legacy "
                                    "work started"
                                ),
                            )
                        previous_generation = claim.get("policy_generation")
                        del current["no_admission_claims"][inbound.id]
                        self._append(
                            current,
                            "NO_ADMISSION_CLAIM_POLICY_SUPERSEDED",
                            scope=inbound.id,
                            source_id=inbound.id,
                            data={
                                "previous_generation": previous_generation,
                                "policy_generation": observed_policy.generation,
                            },
                        )
                        claim = None
                if isinstance(claim, dict):
                    if claim.get("state") == "blocked":
                        return Resolution(
                            ResolverState.BLOCKED,
                            str(claim.get("blocked_reason") or "finalization blocked"),
                        )
                    if claim.get("resolution") != eligibility.value or claim.get(
                        "state"
                    ) not in {"open", "finalization_pending", "finalized"}:
                        return Resolution(
                            ResolverState.INDETERMINATE,
                            "no-admission claim conflicts with replay",
                        )
                    if claim.get("state") in {"open", "finalization_pending"} and claim.get(
                        "fence"
                    ) != self.fence:
                        if not self._can_reassign_no_admission_claim(claim):
                            return Resolution(
                                ResolverState.INDETERMINATE,
                                "prior no-admission claim owner is still authoritative",
                            )
                        previous_fence = claim.get("fence")
                        claim["fence"] = self.fence
                        claim["owner_pid"] = os.getpid()
                        self._append(
                            current,
                            "NO_ADMISSION_CLAIM_REASSIGNED",
                            scope=inbound.id,
                            source_id=inbound.id,
                            data={
                                "previous_fence": previous_fence,
                                "fence": self.fence,
                            },
                        )
                        self._write(current)
                if not isinstance(claim, dict):
                    current_telemetry = current["telemetry"]
                    current_telemetry[name] = int(current_telemetry.get(name, 0)) + 1
                    self._append(
                        current,
                        transition,
                        scope=inbound.id,
                        source_id=inbound.id,
                        data={"reason": detail},
                    )
                    claim = {
                        "fence": self.fence,
                        "owner_pid": os.getpid(),
                        "state": "open",
                        "resolution": eligibility.value,
                        "policy_status": observed_policy.status.value,
                        "policy_generation": observed_policy.generation,
                        "finalization_misses": 0,
                        "finalization_first_at": None,
                        "cursor_projection_misses": 0,
                        "cursor_projection_first_at": None,
                        "cursor_projection_inflight": False,
                        "cursor_projection_reserved_at": None,
                    }
                    current["no_admission_claims"][inbound.id] = claim
                    self._write(current)
                policy_block = self._revalidate_admission_policy(observed_policy)
                if policy_block is not None:
                    if self._claim_is_untouched(claim) and self._claim_matches_policy(
                        claim,
                        observed_policy,
                    ):
                        del current["no_admission_claims"][inbound.id]
                        self._append(
                            current,
                            "NO_ADMISSION_CLAIM_POLICY_INVALIDATED",
                            scope=inbound.id,
                            source_id=inbound.id,
                            data={"policy_generation": observed_policy.generation},
                        )
                        self._write(current)
                    return policy_block
                scope_revision = int(current["scoped_revisions"].get(inbound.id, 0))
            return Resolution(
                eligibility,
                (
                    "no_admission_finalization_pending"
                    if isinstance(claim, dict)
                    and claim.get("state") == "finalization_pending"
                    else "no_admission_disposition_pending"
                    if isinstance(claim, dict)
                    and claim.get("state") == "finalized"
                    else detail
                ),
                scoped_revision=scope_revision,
                ledger_revision=scope_revision,
                activation_generation=observed_policy.generation,
                readiness_generation=observed_policy.generation,
            )
        try:
            messages, ledger = self._validated_messages()
        except LedgerUnreadable as exc:
            return Resolution(ResolverState.BLOCKED, str(exc))
        inbound = self._record_message(record, messages)
        if inbound is None:
            return Resolution(ResolverState.INDETERMINATE, "inbound absent from validated replay")
        recovered = self._recover_durable_no_admission_disposition(
            record,
            messages,
            ledger,
            observed_policy,
        )
        if recovered is not None:
            return recovered
        existing_id = ledger["inbound_index"].get(inbound.id)
        if existing_id:
            admission = ledger["obligations"].get(existing_id)
            if not isinstance(admission, dict):
                return Resolution(ResolverState.INDETERMINATE, "admission index torn")
            existing_key = self._key_from(admission["key"])
            if self._reconcile_landed_pending_broadcast_close(existing_key):
                ledger = self._load()
                admission = ledger["obligations"].get(existing_id)
                if not isinstance(admission, dict):
                    return Resolution(ResolverState.INDETERMINATE, "admission index torn")
            replayed = self._resolve_replay(record, messages, ledger, admission=admission)
            if replayed.terminal:
                breaker = ledger["breakers"].get(self.agent, {})
                if isinstance(breaker, dict) and breaker.get("tripped") is True:
                    self._project_compliance_breaker_hold()
                    self._reconcile_compliance_breaker_alert()
                return replayed
            if admission.get("state") == "open" and admission.get("fence") != self.fence:
                claimed = self._claim_open_admission(existing_id, inbound.id)
                if isinstance(claimed, Resolution):
                    return claimed
                ledger, admission = claimed
                replayed = self._resolve_replay(
                    record,
                    messages,
                    ledger,
                    admission=admission,
                )
            classified = self._persist_post_budget_composing(replayed)
            if any(
                isinstance(row, dict)
                and row.get("state") == "completed"
                and row.get("action_attempted") is True
                for row in admission.get("reservations", {}).values()
            ):
                return self._persist_unsatisfied_attempts(classified)
            return classified
        inbound_meta = inbound.meta if isinstance(inbound.meta, dict) else {}
        if _valid_hex_digest(inbound_meta.get("transfer_from_key_digest")):
            # Publishing the immutable target and committing the source transfer are
            # separate operations.  The first ledger CAS wins: either the source has
            # already admitted this target, or this wrapper aborts the orphan target
            # without invoking the model.  A crash-before-transfer therefore cannot
            # leave the destination queue parked on an INDETERMINATE head forever.
            replay_revision = int(
                ledger["scoped_revisions"].get(inbound.id, 0)
            )
            with self.store._exclusive_lock(
                self.path.with_suffix(".lock"), timeout=10.0,
            ):
                policy_block = self._revalidate_admission_policy(observed_policy)
                if policy_block is not None:
                    return policy_block
                current = self._load()
                if int(current["scoped_revisions"].get(inbound.id, 0)) != (
                    replay_revision
                ):
                    return Resolution(
                        ResolverState.INDETERMINATE,
                        "transfer-target abort CAS miss",
                    )
                if inbound.id in current["inbound_index"]:
                    return Resolution(
                        ResolverState.INDETERMINATE,
                        "atomic source transfer won before target abort",
                    )
                claim = current["no_admission_claims"].get(inbound.id)
                if isinstance(claim, dict):
                    if not self._claim_matches_policy(claim, observed_policy):
                        return self._policy_authority_failure(
                            observed_policy,
                            reason="transfer-target claim policy changed",
                        )
                    if claim.get("state") == "blocked":
                        return Resolution(
                            ResolverState.BLOCKED,
                            str(
                                claim.get("blocked_reason")
                                or "transfer-target finalization blocked"
                            ),
                        )
                    if (
                        claim.get("resolution") != ResolverState.NOT_OWED.value
                        or claim.get("state")
                        not in {"open", "finalization_pending", "finalized"}
                    ):
                        return Resolution(
                            ResolverState.INDETERMINATE,
                            "transfer-target abort conflicts with prior claim",
                        )
                    if (
                        claim.get("state") in {"open", "finalization_pending"}
                        and claim.get("fence") != self.fence
                    ):
                        if not self._can_reassign_no_admission_claim(claim):
                            return Resolution(
                                ResolverState.INDETERMINATE,
                                "prior transfer-target abort owner is still authoritative",
                            )
                        previous_fence = claim.get("fence")
                        claim["fence"] = self.fence
                        claim["owner_pid"] = os.getpid()
                        self._append(
                            current,
                            "NO_ADMISSION_CLAIM_REASSIGNED",
                            scope=inbound.id,
                            source_id=inbound.id,
                            data={
                                "previous_fence": previous_fence,
                                "fence": self.fence,
                                "reason": "transfer_target_abort",
                            },
                        )
                        self._write(current)
                else:
                    claim = {
                        "fence": self.fence,
                        "owner_pid": os.getpid(),
                        "state": "open",
                        "resolution": ResolverState.NOT_OWED.value,
                        "claim_kind": "transfer_target_abort",
                        "policy_status": observed_policy.status.value,
                        "policy_generation": observed_policy.generation,
                        "finalization_misses": 0,
                        "finalization_first_at": None,
                        "cursor_projection_misses": 0,
                        "cursor_projection_first_at": None,
                        "cursor_projection_inflight": False,
                        "cursor_projection_reserved_at": None,
                        "transfer_from_key_digest": inbound_meta.get(
                            "transfer_from_key_digest"
                        ),
                    }
                    current["no_admission_claims"][inbound.id] = claim
                    self._append(
                        current,
                        "TRANSFER_TARGET_ABORTED",
                        scope=inbound.id,
                        source_id=inbound.id,
                        data={
                            "inbound_id": inbound.id,
                            "transfer_from_key_digest": inbound_meta.get(
                                "transfer_from_key_digest"
                            ),
                            "transfer_policy_generation": inbound_meta.get(
                                "transfer_policy_generation"
                            ),
                        },
                    )
                    self._write(current)
                scoped_revision = int(
                    current["scoped_revisions"].get(inbound.id, 0)
                )
            return Resolution(
                ResolverState.NOT_OWED,
                (
                    "no_admission_disposition_pending"
                    if claim.get("state") == "finalized"
                    else "no_admission_finalization_pending"
                ),
                scoped_revision=scoped_revision,
                ledger_revision=scoped_revision,
                activation_generation=observed_policy.generation,
                readiness_generation=observed_policy.generation,
            )
        before = self._resolve_replay(record, messages, ledger, admission=None)
        if before.state in TERMINAL_STATES:
            return self._normalize_pre_admission_terminal(
                record,
                inbound,
                before,
                observed_policy,
            )
        if before.state not in {ResolverState.OWED_UNSATISFIED}:
            return before
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            policy_block = self._revalidate_admission_policy(observed_policy)
            if policy_block is not None:
                return policy_block
            current = self._load()
            if int(current["scoped_revisions"].get(inbound.id, 0)) != before.scoped_revision:
                return Resolution(ResolverState.INDETERMINATE, "admission CAS miss")
            if inbound.id in current["inbound_index"]:
                return Resolution(ResolverState.INDETERMINATE, "concurrent admission won")
            claim = current["no_admission_claims"].get(inbound.id)
            if isinstance(claim, dict):
                previous_generation = claim.get("policy_generation")
                if (
                    isinstance(previous_generation, str)
                    and not self._claim_matches_policy(claim, observed_policy)
                    and self._claim_is_untouched(claim)
                ):
                    del current["no_admission_claims"][inbound.id]
                    self._append(
                        current,
                        "NO_ADMISSION_CLAIM_POLICY_SUPERSEDED",
                        scope=inbound.id,
                        source_id=inbound.id,
                        data={
                            "previous_generation": previous_generation,
                            "policy_generation": observed_policy.generation,
                        },
                    )
                elif not self._claim_matches_policy(claim, observed_policy):
                    return self._policy_authority_failure(
                        observed_policy,
                        reason=(
                            "no-admission claim policy changed after legacy work started"
                        ),
                    )
                else:
                    return Resolution(
                        ResolverState.INDETERMINATE,
                        "concurrent no-admission finalizer won",
                    )
            breaker = current["breakers"].get(self.agent, {})
            if breaker.get("tripped") is True or breaker.get("config_blocked") is True:
                return Resolution(
                    ResolverState.BLOCKED_COMPLIANCE,
                    "compliance breaker tripped",
                )
            question_generation = 1 + sum(
                1 for value in current["obligations"].values()
                if isinstance(value, dict) and value.get("correlation_id") == rid
            )
            key = ObligationKey(
                store_epoch=current["store_epoch"],
                inbound_id=inbound.id,
                correlation_id=rid,
                requester=inbound.sender,
                responder=self.agent,
                question_generation=question_generation,
                delivery_generation=1,
                obligation_class=detail,
            )
            admission = {
                "key": key.to_dict(),
                "correlation_id": rid,
                "obligation_class": detail,
                "state": "open",
                "watermark_sequence": current["append_sequence"],
                "scoped_revision": int(current["scoped_revisions"].get(inbound.id, 0)),
                "activation_generation": observed_policy.generation,
                "readiness_generation": observed_policy.generation,
                "fence": self.fence,
                "owner_pid": os.getpid(),
                "delivery_mode": record.get("mode", "global"),
                "scoped_request_id": (
                    record.get("scoped", {}).get("request_id")
                    if isinstance(record.get("scoped"), dict)
                    else None
                ),
                "paid_dispatches_total": 0,
                "paid_initial_dispatches_total": 0,
                "paid_recoveries_total": 0,
                "paid_continuations_total": 0,
                "continuation_used": False,
                "recovery_used": False,
                "first_dispatch_classified": False,
                "last_exhaustion_class": None,
                "reservations": {},
                "operation_infra_attempts": 0,
                "operation_infra_first_at": None,
                "operation_infra_retry_inflight": False,
                "finalization_misses": 0,
                "finalization_first_at": None,
                "finalization_retry_inflight": False,
                "cursor_projection_misses": 0,
                "cursor_projection_first_at": None,
                "cursor_projection_inflight": False,
                "cursor_projection_reserved_at": None,
                "owed_action_missing_seen": False,
                "created_at": self.now(),
                "broadcast_id": (inbound.meta or {}).get("broadcast_id"),
                "membership_snapshot": (inbound.meta or {}).get("membership_snapshot"),
                "response_policy": (inbound.meta or {}).get("response_policy"),
                "response_quorum": (inbound.meta or {}).get("response_quorum"),
                "broadcast_policy_version": (inbound.meta or {}).get(
                    "broadcast_policy_version"
                ),
                "broadcast_generation": current["messages"].get(
                    inbound.id,
                    {},
                ).get("broadcast_generation"),
            }
            current["obligations"][key.digest] = admission
            current["inbound_index"][inbound.id] = key.digest
            self._append(
                current,
                "OBLIGATION_ADMITTED",
                scope=inbound.id,
                source_id=inbound.id,
                key_digest=key.digest,
                data={"key": key.to_dict()},
            )
            self._write(current)
        return Resolution(
            ResolverState.OWED_UNSATISFIED,
            "obligation admitted",
            key,
            scoped_revision=int(current["scoped_revisions"].get(inbound.id, 0)),
            activation_generation=observed_policy.generation,
            readiness_generation=observed_policy.generation,
        )

    def resolve(self, record: dict) -> Resolution:
        try:
            messages, ledger = self._validated_messages()
        except LedgerUnreadable as exc:
            return Resolution(ResolverState.BLOCKED, str(exc))
        key_id = ledger["inbound_index"].get(record.get("id"))
        if not key_id:
            return self.admit_or_finalize(record)
        admission = ledger["obligations"].get(key_id)
        if not isinstance(admission, dict):
            return Resolution(ResolverState.INDETERMINATE, "admission missing")
        key = self._key_from(admission["key"])
        if self._reconcile_landed_pending_broadcast_close(key):
            ledger = self._load()
            admission = ledger["obligations"].get(key_id)
            if not isinstance(admission, dict):
                return Resolution(ResolverState.INDETERMINATE, "admission missing")
        replayed = self._resolve_replay(record, messages, ledger, admission=admission)
        if replayed.terminal:
            breaker = ledger["breakers"].get(self.agent, {})
            if isinstance(breaker, dict) and breaker.get("tripped") is True:
                self._project_compliance_breaker_hold()
                self._reconcile_compliance_breaker_alert()
            return replayed
        if admission.get("state") == "open" and admission.get("fence") != self.fence:
            claimed = self._claim_open_admission(key_id, str(record.get("id", "")))
            if isinstance(claimed, Resolution):
                return claimed
            ledger, admission = claimed
            replayed = self._resolve_replay(record, messages, ledger, admission=admission)
            if replayed.terminal:
                return replayed
        if admission.get("state") == "blocked":
            return Resolution(
                ResolverState.BLOCKED,
                str(admission.get("blocked_reason") or "obligation retry path blocked"),
                self._key_from(admission["key"]),
            )
        breaker = ledger["breakers"].get(self.agent, {})
        if breaker.get("tripped") is True:
            self._project_compliance_breaker_hold()
            self._reconcile_compliance_breaker_alert()
            return Resolution(
                ResolverState.BLOCKED_COMPLIANCE,
                "compliance breaker tripped",
                self._key_from(admission["key"]),
            )
        classified = self._persist_post_budget_composing(replayed)
        if any(
            isinstance(row, dict)
            and row.get("state") == "completed"
            and row.get("action_attempted") is True
            for row in admission.get("reservations", {}).values()
        ):
            return self._persist_unsatisfied_attempts(classified)
        return classified

    def _live_dispatch_fence_owned(self) -> bool:
        generation = self.store.wrapper_wait_generation(self.agent)
        if generation != self.fence:
            return False
        if self.store.is_managed_lead_loop(self.agent):
            lease = self.store.read_lead_loop_lease(self.agent)
            return bool(
                isinstance(lease, dict)
                and lease.get("wrapper_generation") == self.fence
            )
        marker = self.store.read_waiting(self.agent)
        return bool(
            isinstance(marker, dict)
            and marker.get("mode") == "wrapper-loop"
            and marker.get("pid") == os.getpid()
        )

    def _dispatch_head_is_current(self, admission: dict, key: ObligationKey) -> bool:
        from agenttalk.wrapper import recv_api

        scoped_request_id = (
            admission.get("scoped_request_id")
            if admission.get("delivery_mode") == "scoped"
            else None
        )
        head = recv_api.next_record(
            self.store,
            self.agent,
            scoped_request_id=scoped_request_id,
        )
        return bool(isinstance(head, dict) and head.get("id") == key.inbound_id)

    def _validate_dispatch_authority(
        self,
        ledger: dict,
        admission: dict | None,
        key: ObligationKey,
        resolution: Resolution,
    ) -> dict:
        if not isinstance(admission, dict) or admission.get("state") != "open":
            raise DispatchRefused("obligation is no longer open")
        if admission.get("fence") != self.fence or not self._live_dispatch_fence_owned():
            raise DispatchRefused("wrapper fence changed")
        if isinstance(admission.get("transfer_blocked_state"), str):
            raise DispatchRefused("transfer destination validation is blocked")
        current_policy = self._current_policy()
        activation = admission.get("activation_generation")
        readiness = admission.get("readiness_generation")
        if (
            current_policy.status != ResolverState.ACTIVE
            or activation != current_policy.generation
            or readiness != current_policy.generation
            or resolution.activation_generation not in {None, activation}
            or resolution.readiness_generation not in {None, readiness}
        ):
            raise DispatchRefused("activation or readiness generation changed")
        if not self._dispatch_head_is_current(admission, key):
            raise DispatchRefused("wrapper no longer owns the exact cursor head")
        breaker = ledger["breakers"].get(self.agent, {})
        if breaker.get("tripped") is True or breaker.get("config_blocked") is True:
            raise DispatchRefused("compliance breaker is tripped")
        return admission

    @staticmethod
    def _dispatch_request_digest(
        key: ObligationKey,
        purpose: str,
        budgets: dict,
        activation_generation: object,
        readiness_generation: object,
    ) -> str:
        return hashlib.sha256(_canonical({
            "key_digest": key.digest,
            "purpose": purpose,
            "budgets": budgets,
            "activation_generation": activation_generation,
            "readiness_generation": readiness_generation,
        }).encode("utf-8")).hexdigest()

    @staticmethod
    def _valid_dispatch_nonce(value: object) -> bool:
        if not isinstance(value, str) or len(value) != 32:
            return False
        try:
            int(value, 16)
        except ValueError:
            return False
        return value == value.lower()

    @staticmethod
    def _permit_from_reservation(
        key_digest: str,
        nonce: str,
        row: dict,
    ) -> DispatchPermit:
        return DispatchPermit(
            key_digest,
            nonce,
            str(row.get("composing_nonce", "")),
            str(row.get("purpose", "")),
            int(row.get("paid_dispatches_total", 0)),
            Path(str(row.get("draft_path", ""))),
            str(row.get("budgets_digest", "")),
        )

    def reserve_dispatch(
        self,
        resolution: Resolution,
        *,
        purpose: str,
        nonce: str | None = None,
        budgets: dict | None = None,
    ) -> DispatchPermit:
        if resolution.key is None or resolution.state != ResolverState.OWED_UNSATISFIED:
            raise DispatchRefused("dispatch requires an open unsatisfied obligation")
        if purpose not in {"initial", "recovery", "continuation"}:
            raise ValueError("invalid dispatch purpose")
        requested_budgets = _dispatch_budgets(
            self.dispatch_budgets if budgets is None else budgets,
        )
        key = resolution.key
        permit: DispatchPermit
        with ExitStack() as locks:
            # Hold final message publication across replay synchronization and
            # reservation CAS. A canonically published terminal can never be
            # hidden merely because its eager projection hook failed or waited.
            locks.enter_context(self.store._message_publication_lock())
            self._validated_messages()
            locks.enter_context(
                self.store._exclusive_lock(
                    self.path.with_suffix(".lock"),
                    timeout=10.0,
                ),
            )
            ledger = self._load()
            admission = ledger["obligations"].get(key.digest)
            admission = self._validate_dispatch_authority(
                ledger,
                admission,
                key,
                resolution,
            )
            total = int(admission.get("paid_dispatches_total", 0))
            if nonce is None:
                pending = [
                    (stored_nonce, row)
                    for stored_nonce, row in admission.get("reservations", {}).items()
                    if isinstance(row, dict)
                    and row.get("purpose") == purpose
                    and row.get("state") == "reserved"
                ]
                if len(pending) == 1:
                    nonce = pending[0][0]
                else:
                    nonce = hashlib.sha256(
                        f"{key.digest}:{purpose}:{total + 1}".encode("utf-8"),
                    ).hexdigest()[:32]
            if not self._valid_dispatch_nonce(nonce):
                raise DispatchRefused("dispatch nonce is invalid")
            request_digest = self._dispatch_request_digest(
                key,
                purpose,
                requested_budgets,
                admission.get("activation_generation"),
                admission.get("readiness_generation"),
            )
            nonce_index = ledger["dispatch_nonces"].get(nonce)
            if isinstance(nonce_index, dict):
                if nonce_index.get("request_digest") != request_digest:
                    self._append(
                        ledger,
                        "ACTION_REJECTED",
                        key_digest=key.digest,
                        data={"reason": "dispatch_nonce_reused_for_different_request"},
                    )
                    self._write(ledger)
                    raise DispatchRefused("dispatch nonce was reused for another request")
                existing = admission.get("reservations", {}).get(nonce)
                if not isinstance(existing, dict):
                    raise DispatchRefused("dispatch nonce index is torn")
                permit = self._permit_from_reservation(key.digest, nonce, existing)
            else:
                if isinstance(admission.get("broadcast_policy_close_pending"), dict):
                    raise DispatchRefused(
                        "broadcast policy is satisfied; only the reserved call may finish"
                    )
                if (
                    int(ledger["scoped_revisions"].get(key.inbound_id, 0))
                    != resolution.scoped_revision
                ):
                    raise StaleRevision("scoped replay revision changed before dispatch")
                if self._reconcile_closed_dispatch_slots_locked(ledger):
                    # Persist capacity recovery before any subsequent refusal or
                    # new reservation. A crash cannot resurrect a leaked slot.
                    self._write(ledger)
                active = 0
                for candidate in ledger["obligations"].values():
                    if not isinstance(candidate, dict):
                        continue
                    candidate_key = self._key_from(candidate.get("key"))
                    if candidate_key.responder != self.agent:
                        continue
                    active += sum(
                        1
                        for row in candidate.get("reservations", {}).values()
                        if isinstance(row, dict)
                        and row.get("state") in {"reserved", "dispatching"}
                    )
                if active >= requested_budgets["concurrency"]:
                    raise DispatchRefused("dispatch concurrency budget exhausted")
                if total >= MAX_PAID_DISPATCHES_TOTAL:
                    raise DispatchRefused("paid dispatch budget exhausted")
                if purpose == "initial" and total != 0:
                    raise DispatchRefused("initial dispatch already reserved")
                if purpose != "initial":
                    if total != 1 or admission.get("first_dispatch_classified") is not True:
                        raise DispatchRefused(
                            "extra dispatch requires one durably classified first attempt",
                        )
                    if any(
                        isinstance(row, dict)
                        and row.get("state") == "completed"
                        and row.get("action_attempted") is True
                        for row in admission.get("reservations", {}).values()
                    ):
                        raise DispatchRefused(
                            "corrected retry requires durable rejection classification",
                        )
                scheduled = admission.get("durable_continuation")
                if purpose == "recovery" and (
                    admission.get("continuation_used")
                    or isinstance(scheduled, dict) and scheduled.get("state") == "scheduled"
                ):
                    raise DispatchRefused("second dispatch is assigned to continuation")
                if purpose == "continuation" and (
                    admission.get("recovery_used")
                    or not isinstance(scheduled, dict)
                    or scheduled.get("state") != "scheduled"
                ):
                    raise DispatchRefused("second dispatch is not a scheduled continuation")
                composing_nonce = hashlib.sha256(
                    f"{nonce}:composing".encode("utf-8"),
                ).hexdigest()[:32]
                draft = self.drafts / f"{key.inbound_id}.{nonce}.txt"
                admission["paid_dispatches_total"] = total + 1
                subtype = {
                    "initial": "paid_initial_dispatches_total",
                    "recovery": "paid_recoveries_total",
                    "continuation": "paid_continuations_total",
                }[purpose]
                admission[subtype] = int(admission.get(subtype, 0)) + 1
                if purpose != "initial":
                    admission[f"{purpose}_used"] = True
                if purpose == "continuation":
                    scheduled["state"] = "reserved"
                    scheduled["dispatch_nonce"] = nonce
                budgets_digest = hashlib.sha256(
                    _canonical(requested_budgets).encode("utf-8"),
                ).hexdigest()
                reservation = {
                    "purpose": purpose,
                    "state": "reserved",
                    "at": self.now(),
                    "scoped_revision": resolution.scoped_revision,
                    "draft_path": str(draft),
                    "composing_nonce": composing_nonce,
                    "paid_dispatches_total": total + 1,
                    "budgets": requested_budgets,
                    "budgets_digest": budgets_digest,
                    "request_digest": request_digest,
                    "reserved_fence": self.fence,
                }
                admission["reservations"][nonce] = reservation
                ledger["dispatch_nonces"][nonce] = {
                    "key_digest": key.digest,
                    "request_digest": request_digest,
                }
                self._append(
                    ledger,
                    "DISPATCH_RESERVED",
                    scope=key.inbound_id,
                    key_digest=key.digest,
                    data={
                        "nonce": nonce,
                        "purpose": purpose,
                        "total": total + 1,
                        "budgets_digest": budgets_digest,
                    },
                )
                self._write(ledger)
                permit = self._permit_from_reservation(key.digest, nonce, reservation)
        permit.draft_path.parent.mkdir(parents=True, exist_ok=True)
        return permit

    def next_dispatch_purpose(self, key: ObligationKey) -> str | None:
        try:
            with self.store._exclusive_lock(
                self.path.with_suffix(".lock"),
                timeout=10.0,
            ):
                ledger = self._load(create=False)
                admission = ledger["obligations"].get(key.digest)
                if not isinstance(admission, dict) or admission.get("state") != "open":
                    return None
                if admission.get("fence") != self.fence or not self._live_dispatch_fence_owned():
                    return None
                reservations = admission.get("reservations", {})
                pending = [
                    row
                    for row in reservations.values()
                    if isinstance(row, dict) and row.get("state") == "reserved"
                ]
                if len(pending) == 1:
                    return str(pending[0].get("purpose"))
                changed = False
                for nonce, row in reservations.items():
                    if not isinstance(row, dict) or row.get("state") != "dispatching":
                        continue
                    if row.get("dispatch_fence") == self.fence:
                        continue
                    if not isinstance(row.get("dispatch_fence"), str):
                        return None
                    row["state"] = "dispatch_result_missing"
                    row["result_missing_at"] = self.now()
                    admission["first_dispatch_classified"] = True
                    admission["owed_action_missing_seen"] = True
                    admission["last_exhaustion_class"] = "infrastructure"
                    self._append(
                        ledger,
                        "DISPATCH_RESULT_MISSING",
                        key_digest=key.digest,
                        data={"nonce": nonce, "purpose": row.get("purpose")},
                    )
                    changed = True
                if changed:
                    self._write(ledger)
                if any(
                    isinstance(row, dict) and row.get("state") == "dispatching"
                    for row in reservations.values()
                ):
                    return None
                if isinstance(admission.get("broadcast_policy_close_pending"), dict):
                    self._complete_pending_broadcast_close_locked(
                        ledger,
                        admission,
                        key,
                    )
                    self._write(ledger)
                    return None
                if any(
                    isinstance(row, dict) and row.get("state") == "action_infra"
                    for row in reservations.values()
                ):
                    return None
                total = int(admission.get("paid_dispatches_total", 0))
                if total == 0:
                    return "initial"
                if total != 1 or admission.get("first_dispatch_classified") is not True:
                    return None
                if any(
                    isinstance(row, dict)
                    and row.get("state") == "completed"
                    and row.get("action_attempted") is True
                    for row in reservations.values()
                ):
                    return None
                continuation = admission.get("durable_continuation")
                if (
                    isinstance(continuation, dict)
                    and continuation.get("state") == "scheduled"
                ):
                    return "continuation"
                if not admission.get("continuation_used") and not admission.get(
                    "recovery_used",
                ):
                    return "recovery"
                return None
        except LedgerUnreadable:
            return None

    def dispatch_record(self, record: dict, permit: DispatchPermit) -> dict:
        """Durably arm one permit, then add wrapper-owned transport facts."""
        decorated = dict(record)
        executable = self._pinned_executable()
        with self.store._config_lock(timeout=10.0):
            cfg = self.store.load_config()
            roster = _roster_snapshot(cfg)
            recipient = cfg.get("operator_facing")
            if not isinstance(recipient, str) or recipient not in roster[
                "authorized_liaisons"
            ]:
                candidates = [
                    candidate for candidate in roster["authorized_liaisons"]
                    if candidate != self.agent
                ]
                recipient = candidates[0] if len(candidates) == 1 else None
            with self.store._exclusive_lock(
                self.path.with_suffix(".lock"), timeout=10.0
            ):
                ledger = self._load(create=False)
                admission = ledger["obligations"].get(permit.key_digest)
                if not isinstance(admission, dict):
                    raise DispatchRefused("dispatch admission disappeared")
                key = self._key_from(admission["key"])
                current_revision = int(
                    ledger["scoped_revisions"].get(key.inbound_id, 0),
                )
                self._validate_dispatch_authority(
                    ledger,
                    admission,
                    key,
                    Resolution(
                        ResolverState.OWED_UNSATISFIED,
                        "dispatch permit revalidation",
                        key,
                        scoped_revision=current_revision,
                        activation_generation=str(admission.get("activation_generation")),
                        readiness_generation=str(admission.get("readiness_generation")),
                    ),
                )
                reservation = admission.get("reservations", {}).get(permit.nonce)
                if (
                    not isinstance(reservation, dict)
                    or reservation.get("state") != "reserved"
                    or reservation.get("purpose") != permit.purpose
                    or reservation.get("composing_nonce") != permit.composing_nonce
                    or reservation.get("budgets_digest") != permit.budgets_digest
                    or Path(str(reservation.get("draft_path", ""))) != permit.draft_path
                    or int(reservation.get("paid_dispatches_total", 0))
                    != permit.paid_dispatches_total
                ):
                    raise DispatchRefused(
                        "dispatch permit draft path or authority does not match its reservation",
                    )
                obligation_class = admission.get("obligation_class")
                record_meta = (
                    record.get("meta") if isinstance(record.get("meta"), dict) else {}
                )
                if obligation_class == "human_escalation":
                    if not isinstance(recipient, str) or recipient == self.agent:
                        raise DispatchRefused(
                            "human escalation has no external operator target"
                        )
                    operation_intent = {
                        "operation": "terminal",
                        "kind": "question",
                        "recipient": recipient,
                        "in_reply_to": record.get("id"),
                        "request_id": f"esc-{permit.nonce[:12]}",
                        "broadcast_id": None,
                        "origin_request_id": record.get("correlation_id"),
                        "origin_inbound_id": record.get("id"),
                        "origin_obligation_key_digest": key.digest,
                        "expected_roster_revision": roster["revision"],
                    }
                else:
                    operation_intent = {
                        "operation": "terminal",
                        "kind": "message",
                        "recipient": key.requester,
                        "in_reply_to": record.get("id"),
                        "request_id": record_meta.get("request_id"),
                        "broadcast_id": record_meta.get("broadcast_id"),
                        "origin_request_id": None,
                        "origin_inbound_id": None,
                    }
                reservation["state"] = "dispatching"
                reservation["dispatch_started_at"] = self.now()
                reservation["dispatch_fence"] = self.fence
                reservation["dispatch_owner_pid"] = os.getpid()
                reservation["operation_intent"] = operation_intent
                self._append(
                    ledger,
                    "DISPATCH_ATTEMPT_STARTED",
                    key_digest=permit.key_digest,
                    data={"nonce": permit.nonce, "purpose": permit.purpose},
                )
                self._write(ledger)
                requested_budgets = dict(reservation["budgets"])
        if obligation_class == "human_escalation":
            argv = [
                executable,
                "-m",
                "agenttalk",
                "escalate",
                "--from",
                self.agent,
                "--to",
                str(operation_intent["recipient"]),
                "--origin-request",
                str(record.get("correlation_id")),
                "--origin-id",
                str(record.get("id")),
                "--meta",
                f"request_id={operation_intent['request_id']}",
                "--meta",
                (
                    "expected_roster_revision="
                    f"{operation_intent['expected_roster_revision']}"
                ),
                "--meta",
                (
                    "origin_obligation_key_digest="
                    f"{operation_intent['origin_obligation_key_digest']}"
                ),
                "--operation-nonce",
                permit.nonce,
                "--file",
                str(permit.draft_path),
            ]
        else:
            argv = [
                executable,
                "-m",
                "agenttalk",
                "reply",
                "--from",
                self.agent,
                "--to-id",
                str(record.get("id")),
                "--operation-nonce",
                permit.nonce,
                "--file",
                str(permit.draft_path),
            ]
        decorated["owed_action"] = {
            "grade": "detection",
            "obligation_key_digest": permit.key_digest,
            "dispatch_nonce": permit.nonce,
            "terminal_operation_nonce": permit.nonce,
            "composing_operation_nonce": permit.composing_nonce,
            "purpose": permit.purpose,
            "budgets": requested_budgets,
            "exact_inbound_id": record.get("id"),
            "draft_path": str(permit.draft_path),
            "argv": argv,
            "composing_argv": [
                executable,
                "-m",
                "agenttalk",
                "composing",
                "--from",
                self.agent,
                "--to-request",
                str(record.get("correlation_id")),
                "--meta",
                f"in_reply_to={record.get('id')}",
                "--operation-nonce",
                permit.composing_nonce,
            ],
            "body_transport": "structured-write-then-file",
        }
        return decorated

    @staticmethod
    def _pinned_executable() -> str:
        executable = Path(os.environ.get("AGENTTALK_PY") or os.sys.executable).expanduser()
        if not executable.is_absolute():
            raise DispatchRefused("AGENTTALK_PY must name an absolute executable")
        try:
            resolved = executable.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise DispatchRefused("AGENTTALK_PY executable is unavailable") from exc
        # POSIX Python launchers are commonly symlinks. Returning the canonical
        # regular-file path keeps dispatch argv stable if the launcher is retargeted.
        if not resolved.is_file():
            raise DispatchRefused("AGENTTALK_PY must resolve to a regular file")
        return str(resolved)

    def dispatch_exhausted(self, key: ObligationKey) -> bool:
        try:
            ledger = self._load(create=False)
        except LedgerUnreadable:
            return False
        admission = ledger["obligations"].get(key.digest)
        return bool(
            isinstance(admission, dict)
            and int(admission.get("paid_dispatches_total", 0))
            >= MAX_PAID_DISPATCHES_TOTAL
        )

    def _breaker_state(self, ledger: dict) -> dict:
        breaker = ledger["breakers"].setdefault(self.agent, {
            "generation": 0,
            "owed_action_cap_exhaustions_consecutive": 0,
            "proof_infra_exhaustions_consecutive": 0,
            "compliance_exhaustion_references": [],
            "tripped": False,
            "config_blocked": False,
            "config_blocked_reason": None,
            "alerted_generation": None,
            "alerts": {},
        })
        breaker.setdefault("alerts", {})
        return breaker

    def _project_compliance_breaker_hold(self) -> None:
        self.store.write_config_blocked_hold(
            self.agent,
            summary="owed_action_compliance_breaker",
        )

    def _clear_compliance_breaker_hold(self) -> None:
        hold = self.store.read_config_blocked_hold(self.agent)
        if (
            isinstance(hold, dict)
            and hold.get("summary") == "owed_action_compliance_breaker"
        ):
            self.store.clear_config_blocked_hold(self.agent)

    def _reconcile_compliance_breaker_alert(self) -> bool:
        """Publish and acknowledge one durable alert outbox row idempotently."""
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            ledger = self._load(create=False)
            breaker = ledger["breakers"].get(self.agent)
            if not isinstance(breaker, dict) or breaker.get("tripped") is not True:
                return False
            generation = int(breaker.get("generation", 0))
            alerts = breaker.get("alerts")
            alert = alerts.get(str(generation)) if isinstance(alerts, dict) else None
            if not isinstance(alert, dict):
                return False
            if alert.get("state") == "delivered":
                return True
            nonce = str(alert.get("nonce", ""))
            body = str(alert.get("body", ""))
            request_id = str(alert.get("request_id", ""))
            pinned_target = alert.get("recipient")
        candidate = None
        if not isinstance(pinned_target, str) or not pinned_target:
            candidate = self.store.operator_facing() or self.store.sole_lead()

        # Route the outbox row once, before publication. A crash after the bus
        # append must retry the same nonce *and* the same operation identity,
        # even if liaison routing changes before the next wrapper generation.
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            ledger = self._load(create=False)
            breaker = ledger["breakers"].get(self.agent)
            if not isinstance(breaker, dict) or breaker.get("tripped") is not True:
                return False
            alerts = breaker.get("alerts")
            current = alerts.get(str(generation)) if isinstance(alerts, dict) else None
            if not isinstance(current, dict) or current.get("nonce") != nonce:
                return False
            if current.get("state") == "delivered":
                return True
            target = current.get("recipient")
            if not isinstance(target, str) or not target:
                target = candidate
            if not isinstance(target, str) or not target or target == self.agent:
                return False
            digest = operation_payload_digest(
                operation="terminal",
                body=body,
                kind="question",
                recipient=target,
                request_id=request_id,
            )
            stored_digest = current.get("operation_digest")
            if isinstance(stored_digest, str) and stored_digest != digest:
                return False
            if current.get("recipient") != target or stored_digest != digest:
                current["recipient"] = target
                current["operation_digest"] = digest
                self._append(
                    ledger,
                    "COMPLIANCE_BREAKER_ALERT_ROUTED",
                    data={
                        "agent": self.agent,
                        "generation": generation,
                        "recipient": target,
                        "operation_digest": digest,
                    },
                )
                self._write(ledger)
        meta = {
            "needs_operator": "true",
            "request_id": request_id,
            "compliance_breaker_alert_generation": generation,
            "compliance_breaker_agent": self.agent,
        }
        try:
            message, _published = self.store.send_operation(
                sender=self.agent,
                recipient=target,
                body=body,
                kind="question",
                subject=f"owed-action compliance breaker: {self.agent}",
                meta=meta,
                operation_nonce=nonce,
                operation_digest=digest,
            )
        except (OSError, TimeoutError, ValueError):
            return False
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            ledger = self._load(create=False)
            breaker = ledger["breakers"].get(self.agent)
            alerts = breaker.get("alerts") if isinstance(breaker, dict) else None
            current = alerts.get(str(generation)) if isinstance(alerts, dict) else None
            if not isinstance(current, dict) or current.get("nonce") != nonce:
                return False
            if current.get("state") != "delivered":
                current["state"] = "delivered"
                current["message_id"] = message.id
                current["delivered_at"] = self.now()
                current["recipient"] = target
                current["operation_digest"] = digest
                breaker["alerted_generation"] = generation
                self._append(
                    ledger,
                    "COMPLIANCE_BREAKER_ALERT",
                    data={
                        "agent": self.agent,
                        "generation": generation,
                        "message_id": message.id,
                    },
                )
                self._write(ledger)
            return True

    def _reset_compliance_streak_locked(
        self,
        ledger: dict,
        key: ObligationKey,
    ) -> bool:
        breaker = ledger["breakers"].get(self.agent)
        if not isinstance(breaker, dict) or breaker.get("tripped") is True:
            return False
        if (
            int(breaker.get("owed_action_cap_exhaustions_consecutive", 0)) == 0
            and not breaker.get("compliance_exhaustion_references")
        ):
            return False
        breaker["owed_action_cap_exhaustions_consecutive"] = 0
        breaker["compliance_exhaustion_references"] = []
        self._append(
            ledger,
            "COMPLIANCE_STREAK_RESET",
            key_digest=key.digest,
        )
        return True

    def mark_satisfied(self, key: ObligationKey) -> None:
        """A successful delivery resets only the compliance-dominant streak."""
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            ledger = self._load()
            if self._reset_compliance_streak_locked(ledger, key):
                self._write(ledger)

    def reset_compliance_breaker(self, *, actor: str, reason: str) -> None:
        if actor not in {self.store.operator_facing(), self.store.sole_lead()}:
            raise PermissionError("breaker reset requires the liaison or lead")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("breaker reset requires an audit reason")
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            ledger = self._load()
            breaker = self._breaker_state(ledger)
            breaker["generation"] = int(breaker.get("generation", 0)) + 1
            breaker["tripped"] = False
            breaker["config_blocked"] = False
            breaker["config_blocked_reason"] = None
            breaker["owed_action_cap_exhaustions_consecutive"] = 0
            breaker["compliance_exhaustion_references"] = []
            breaker["reset_at"] = self.now()
            breaker["reset_by"] = actor
            self._append(
                ledger,
                "COMPLIANCE_BREAKER_RESET",
                data={
                    "agent": self.agent,
                    "actor": actor,
                    "reason": reason.strip(),
                    "generation": breaker["generation"],
                },
            )
            self._write(ledger)
        self._clear_compliance_breaker_hold()

    def mark_dispatch_result(
        self,
        permit: DispatchPermit,
        *,
        action_attempted: bool,
        action_rejected: bool = False,
        action_infra: bool = False,
    ) -> None:
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            ledger = self._load()
            admission = ledger["obligations"].get(permit.key_digest)
            if not isinstance(admission, dict):
                raise GateError("dispatch admission disappeared")
            reservation = admission["reservations"].get(permit.nonce)
            if not isinstance(reservation, dict):
                raise GateError("dispatch reservation disappeared")
            if reservation.get("state") != "dispatching":
                raise GateError("dispatch result has no durable attempt barrier")
            reservation["state"] = "completed"
            reservation["action_attempted"] = action_attempted
            admission["first_dispatch_classified"] = True
            if action_infra:
                transition = "ACTION_ATTEMPT_INFRA"
                admission["last_exhaustion_class"] = "infrastructure"
                payload_digest = self._captured_payload_digest(reservation, permit.nonce)
                if payload_digest is not None:
                    reservation["state"] = "action_infra"
                    reservation["operation_payload_digest"] = payload_digest
                    admission["operation_infra_attempts"] = max(
                        1,
                        int(admission.get("operation_infra_attempts", 0)),
                    )
                    admission["operation_infra_first_at"] = (
                        admission.get("operation_infra_first_at") or self.now()
                    )
                else:
                    reservation["state"] = "uncaptured_infra"
            elif action_rejected:
                transition = "ACTION_REJECTED"
                reservation["state"] = "action_rejected"
                admission["owed_action_missing_seen"] = True
                admission["last_exhaustion_class"] = "compliance"
            else:
                transition = "ACTION_ATTEMPTED" if action_attempted else "OWED_ACTION_MISSING"
                admission["last_exhaustion_class"] = "compliance"
                if not action_attempted:
                    admission["owed_action_missing_seen"] = True
            self._append(ledger, transition, key_digest=permit.key_digest)
            if isinstance(admission.get("broadcast_policy_close_pending"), dict):
                key = self._key_from(admission["key"])
                self._complete_pending_broadcast_close_locked(
                    ledger,
                    admission,
                    key,
                )
            self._write(ledger)

    def mark_unsatisfied_attempt(self, permit: DispatchPermit, *, reason: str) -> None:
        """Classify an attempted operation that could not legally close this obligation."""
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            ledger = self._load()
            admission = ledger["obligations"].get(permit.key_digest)
            row = admission.get("reservations", {}).get(permit.nonce) if isinstance(
                admission, dict
            ) else None
            if not isinstance(admission, dict) or not isinstance(row, dict):
                raise GateError("dispatch reservation disappeared")
            if row.get("state") == "action_rejected":
                return
            if row.get("state") != "completed":
                raise GateError("unsatisfied action is not durably classified")
            admission["owed_action_missing_seen"] = True
            admission["last_exhaustion_class"] = "compliance"
            row["state"] = "action_rejected"
            row["rejection_reason"] = reason
            self._append(
                ledger,
                "ACTION_REJECTED",
                key_digest=permit.key_digest,
                data={"reason": reason},
            )
            self._write(ledger)

    def captured_operation(self, key: ObligationKey) -> DispatchPermit | None:
        try:
            ledger = self._load(create=False)
        except LedgerUnreadable:
            return None
        admission = ledger["obligations"].get(key.digest)
        if not isinstance(admission, dict) or admission.get("state") != "open":
            return None
        for nonce, row in admission.get("reservations", {}).items():
            if (
                not isinstance(row, dict)
                or row.get("state") != "action_infra"
                or not isinstance(row.get("operation_payload_digest"), str)
            ):
                continue
            return DispatchPermit(
                key.digest,
                nonce,
                str(row.get("composing_nonce", "")),
                row.get("purpose", "recovery"),
                int(admission.get("paid_dispatches_total", 0)),
                Path(row["draft_path"]),
                str(row.get("budgets_digest", "")),
            )
        return None

    def mark_captured_operation_succeeded(self, permit: DispatchPermit) -> None:
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            ledger = self._load()
            admission = ledger["obligations"].get(permit.key_digest)
            row = admission.get("reservations", {}).get(permit.nonce) if isinstance(admission, dict) else None
            if (
                not isinstance(row, dict)
                or row.get("state") != "action_infra"
                or admission.get("operation_infra_retry_inflight") is not True
            ):
                raise GateError("captured operation disappeared")
            row["state"] = "completed"
            admission["operation_infra_retry_inflight"] = False
            self._append(ledger, "CAPTURED_OPERATION_LANDED", key_digest=permit.key_digest)
            self._write(ledger)

    @staticmethod
    def cleanup_permit(permit: DispatchPermit) -> None:
        try:
            permit.draft_path.unlink(missing_ok=True)
        except OSError:
            pass

    def retry_captured_operation(self, permit: DispatchPermit, record: dict) -> bool:
        """Retry the fixed file-based reply operation without another model call."""
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            ledger = self._load(create=False)
            admission = ledger["obligations"].get(permit.key_digest)
            if not isinstance(admission, dict):
                raise DispatchRefused("captured operation admission disappeared")
            key = self._key_from(admission["key"])
            self._validate_dispatch_authority(
                ledger,
                admission,
                key,
                Resolution(
                    ResolverState.OWED_UNSATISFIED,
                    "captured operation revalidation",
                    key,
                    activation_generation=str(admission.get("activation_generation")),
                    readiness_generation=str(admission.get("readiness_generation")),
                ),
            )
            row = admission.get("reservations", {}).get(permit.nonce)
            if (
                not isinstance(row, dict)
                or row.get("state") != "action_infra"
                or row.get("budgets_digest") != permit.budgets_digest
                or admission.get("operation_infra_retry_inflight") is not True
                or not isinstance(row.get("operation_payload_digest"), str)
                or Path(str(row.get("draft_path", ""))) != permit.draft_path
                or not isinstance(row.get("operation_intent"), dict)
            ):
                raise DispatchRefused("captured operation retry is not durably reserved")
            expected_payload_digest = row["operation_payload_digest"]
            operation_intent = dict(row["operation_intent"])
            obligation_class = admission.get("obligation_class")
        draft = Path(str(row["draft_path"]))
        try:
            resolved = draft.resolve(strict=True)
            root = self.drafts.resolve(strict=True)
            resolved.relative_to(root)
            if draft.is_symlink() or resolved.stat().st_size > 1024 * 1024:
                return False
            resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError):
            return False
        if self._captured_payload_digest(row, permit.nonce) != expected_payload_digest:
            return False
        executable = self._pinned_executable()
        if obligation_class == "human_escalation":
            argv = [
                executable,
                "-m",
                "agenttalk",
                "escalate",
                "--from",
                self.agent,
                "--to",
                str(operation_intent["recipient"]),
                "--origin-request",
                str(operation_intent["origin_request_id"]),
                "--origin-id",
                str(operation_intent["origin_inbound_id"]),
                "--meta",
                f"request_id={operation_intent['request_id']}",
                "--meta",
                (
                    "expected_roster_revision="
                    f"{operation_intent['expected_roster_revision']}"
                ),
                "--meta",
                (
                    "origin_obligation_key_digest="
                    f"{operation_intent['origin_obligation_key_digest']}"
                ),
                "--operation-nonce",
                permit.nonce,
                "--file",
                str(resolved),
            ]
        else:
            argv = [
                executable,
                "-m",
                "agenttalk",
                "reply",
                "--from",
                self.agent,
                "--to-id",
                str(operation_intent["in_reply_to"]),
                "--operation-nonce",
                permit.nonce,
                "--file",
                str(resolved),
            ]
        completed = subprocess.run(  # noqa: S603  # nosec B603
            argv,
            cwd=self.store.root,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30.0,
            check=False,
            shell=False,
        )
        return completed.returncode == 0

    def record_retry_barrier(
        self,
        key: ObligationKey,
        *,
        category: str,
        expected_revision: int,
        completed_attempt: bool = False,
    ) -> bool:
        """Durably count a non-paid retry before allowing it to occur."""
        if category not in {"operation_infra", "finalization"}:
            raise ValueError("unknown retry category")
        # Store.send() holds this same publication lock through its eager ledger
        # hook.  Taking it before replay closes the terminal-append versus retry
        # reservation window without inverting the publication -> ledger order.
        with self.store._message_publication_lock():
            try:
                self._validated_messages()
            except LedgerUnreadable:
                return False
            with self.store._exclusive_lock(
                self.path.with_suffix(".lock"), timeout=10.0,
            ):
                ledger = self._load()
                admission = ledger["obligations"].get(key.digest)
                if not isinstance(admission, dict):
                    raise GateError("retry admission missing")
                if any((
                    admission.get("state") != "open",
                    admission.get("fence") != self.fence,
                    not self._live_dispatch_fence_owned(),
                    not self._dispatch_head_is_current(admission, key),
                    int(ledger["scoped_revisions"].get(key.inbound_id, 0))
                    != expected_revision,
                )):
                    return False
                count_name = (
                    f"{category}_attempts"
                    if category == "operation_infra"
                    else "finalization_misses"
                )
                first_name = f"{category}_first_at"
                inflight_name = f"{category}_retry_inflight"
                count = int(admission.get(count_name, 0))
                now_text = self.now()
                first = _epoch(admission.get(first_name))
                now = _epoch(now_text)
                elapsed = 0.0 if first is None or now is None else max(0.0, now - first)
                limit = (
                    MAX_OPERATION_INFRA_ATTEMPTS
                    if category == "operation_infra"
                    else MAX_FINALIZATION_MISSES
                )
                elapsed_limit = (
                    MAX_OPERATION_INFRA_SECONDS
                    if category == "operation_infra"
                    else MAX_FINALIZATION_SECONDS
                )
                changed = False
                if admission.get(inflight_name):
                    if not completed_attempt:
                        self._append(
                            ledger,
                            "OPERATION_RETRY_OUTCOME_MISSING"
                            if category == "operation_infra"
                            else "FINALIZATION_RETRY_OUTCOME_MISSING",
                            key_digest=key.digest,
                            data={"prior_count": count},
                        )
                    admission[inflight_name] = False
                    changed = True
                if count >= limit or elapsed >= elapsed_limit:
                    if changed:
                        self._write(ledger)
                    return False
                admission[count_name] = count + 1
                admission[first_name] = admission.get(first_name) or now_text
                admission[inflight_name] = True
                self._append(
                    ledger,
                    "OPERATION_RETRY_RECORDED"
                    if category == "operation_infra"
                    else "FINALIZATION_MISS_RECORDED",
                    key_digest=key.digest,
                    data={"count": admission[count_name]},
                )
                self._write(ledger)
                return True

    def complete_retry_barrier(self, key: ObligationKey, *, category: str) -> None:
        if category not in {"operation_infra", "finalization"}:
            raise ValueError("unknown retry category")
        inflight_name = f"{category}_retry_inflight"
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            ledger = self._load()
            admission = ledger["obligations"].get(key.digest)
            if not isinstance(admission, dict):
                raise GateError("retry admission missing")
            if admission.get(inflight_name):
                admission[inflight_name] = False
                self._append(
                    ledger,
                    "RETRY_ATTEMPT_COMPLETED",
                    key_digest=key.digest,
                    data={"category": category},
                )
                self._write(ledger)

    def retry_bound_exhausted(self, key: ObligationKey, *, category: str) -> bool:
        """Distinguish a spent durable bound from a stale retry reservation."""
        if category not in {"operation_infra", "finalization"}:
            raise ValueError("unknown retry category")
        count_name = (
            f"{category}_attempts"
            if category == "operation_infra"
            else "finalization_misses"
        )
        first_name = f"{category}_first_at"
        limit = (
            MAX_OPERATION_INFRA_ATTEMPTS
            if category == "operation_infra"
            else MAX_FINALIZATION_MISSES
        )
        elapsed_limit = (
            MAX_OPERATION_INFRA_SECONDS
            if category == "operation_infra"
            else MAX_FINALIZATION_SECONDS
        )
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            admission = self._load()["obligations"].get(key.digest)
            if not isinstance(admission, dict):
                return False
            first = _epoch(admission.get(first_name))
            now = _epoch(self.now())
            elapsed = 0.0 if first is None or now is None else max(0.0, now - first)
            return int(admission.get(count_name, 0)) >= limit or elapsed >= elapsed_limit

    def mark_blocked(self, key: ObligationKey, *, reason: str) -> Resolution:
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            ledger = self._load()
            admission = ledger["obligations"].get(key.digest)
            if not isinstance(admission, dict):
                raise GateError("blocked obligation admission missing")
            if admission.get("state") == "blocked":
                return Resolution(
                    ResolverState.BLOCKED,
                    str(admission.get("blocked_reason") or reason),
                    key,
                )
            if admission.get("state") != "open":
                raise GateError("blocked obligation is no longer open")
            admission["state"] = "blocked"
            admission["blocked_reason"] = reason
            self._append(
                ledger,
                "OBLIGATION_BLOCKED",
                scope=key.inbound_id,
                key_digest=key.digest,
                data={"reason": reason},
            )
            self._write(ledger)
        return Resolution(ResolverState.BLOCKED, reason, key)

    def _record_disposition_block(self, *, reason: str) -> None:
        now_text = self.now()
        lock = self.proof_health_path.with_suffix(".lock")
        with self.store._exclusive_lock(lock, timeout=10.0):
            try:
                health = json.loads(
                    self.proof_health_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError, json.JSONDecodeError):
                health = {}
            if not isinstance(health, dict):
                health = {}
            health.update({
                "state": "blocked",
                "disposition_block": True,
                "reason": reason,
                "last_failure_at": now_text,
                "alerted": True,
            })
            health.setdefault("first_failure_at", now_text)
            self.proof_health_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(
                self.proof_health_path,
                json.dumps(health, indent=2, ensure_ascii=False),
            )

    def _block_retry_exhaustion(
        self,
        key: ObligationKey,
        *,
        reason: str,
    ) -> Resolution:
        try:
            return self.mark_blocked(key, reason=reason)
        except (GateError, OSError, TimeoutError):
            self._record_disposition_block(reason=reason)
            return Resolution(ResolverState.BLOCKED, reason, key)

    def fail_delivery_or_block(
        self,
        record: dict,
        key: ObligationKey,
        *,
        reason: str,
        expected_revision: int,
    ) -> Resolution:
        """Commit exact-head failed delivery, or make inability to do so visible."""
        try:
            return self.delivery_failed(
                record,
                key,
                reason=reason,
                expected_revision=expected_revision,
            )
        except (GateError, OSError, TimeoutError, ValueError, RuntimeError) as exc:
            failure = exc
        try:
            replayed = self.resolve(record)
        except (GateError, OSError, TimeoutError, ValueError, RuntimeError) as exc:
            return self._block_retry_exhaustion(
                key,
                reason=f"failed-delivery disposition unavailable: {type(exc).__name__}",
            )
        if replayed.terminal:
            try:
                finalized = self.finalize(
                    record,
                    replayed,
                    expected_revision=replayed.scoped_revision,
                )
            except (GateError, OSError, TimeoutError, ValueError, RuntimeError):
                return replayed
            return replayed if finalized.state == ResolverState.INDETERMINATE else finalized
        if replayed.state == ResolverState.OWED_UNSATISFIED and replayed.key == key:
            try:
                # A benign correlated append may have invalidated only the caller's
                # revision.  Retry the exact-head disposition once at the freshly
                # replayed revision; publication locking inside delivery_failed()
                # still gives a concurrently published terminal precedence.
                return self.delivery_failed(
                    record,
                    key,
                    reason=reason,
                    expected_revision=replayed.scoped_revision,
                )
            except (GateError, OSError, TimeoutError, ValueError, RuntimeError) as exc:
                failure = exc
        return self._block_retry_exhaustion(
            key,
            reason=f"failed-delivery disposition unavailable: {type(failure).__name__}",
        )

    def fail_head_local_corruption_or_block(
        self,
        record: dict,
        key: ObligationKey,
        permit: DispatchPermit,
        *,
        reason: str,
        expected_revision: int,
    ) -> Resolution:
        """Dispose only a demonstrably corrupt, exact-head captured artifact.

        A missing/unreadable artifact is ambiguous infrastructure and therefore
        blocks.  Head-local failed delivery is allowed only when canonical replay
        is healthy and readable bytes bound to this reservation disagree with the
        durable operation proof.
        """
        proof: dict | None = None
        with ExitStack() as locks:
            locks.enter_context(self.store._message_publication_lock())
            try:
                messages, _ = self._validated_messages()
            except LedgerUnreadable as exc:
                return self._block_retry_exhaustion(
                    key,
                    reason=f"head-local proof unavailable: {exc}",
                )
            locks.enter_context(
                self.store._exclusive_lock(
                    self.path.with_suffix(".lock"), timeout=10.0,
                )
            )
            ledger = self._load()
            admission = ledger["obligations"].get(key.digest)
            if isinstance(admission, dict):
                replayed = self._resolve_replay(
                    record,
                    messages,
                    ledger,
                    admission=admission,
                )
                if replayed.terminal or replayed.state in {
                    ResolverState.BLOCKED,
                    ResolverState.BLOCKED_POLICY,
                    ResolverState.BLOCKED_COMPLIANCE,
                }:
                    return replayed

            def block_locked(block_reason: str) -> Resolution:
                if isinstance(admission, dict) and admission.get("state") == "open":
                    admission["state"] = "blocked"
                    admission["blocked_reason"] = block_reason
                    self._append(
                        ledger,
                        "OBLIGATION_BLOCKED",
                        scope=key.inbound_id,
                        key_digest=key.digest,
                        data={"reason": block_reason},
                    )
                    self._write(ledger)
                else:
                    self._record_disposition_block(reason=block_reason)
                return Resolution(ResolverState.BLOCKED, block_reason, key)

            row = (
                admission.get("reservations", {}).get(permit.nonce)
                if isinstance(admission, dict)
                else None
            )
            if (
                not isinstance(admission, dict)
                or admission.get("state") != "open"
                or admission.get("fence") != self.fence
                or not self._live_dispatch_fence_owned()
                or not self._dispatch_head_is_current(admission, key)
                or int(ledger["scoped_revisions"].get(key.inbound_id, 0))
                != expected_revision
                or not isinstance(row, dict)
                or row.get("state") != "action_infra"
                or permit.key_digest != key.digest
                or row.get("purpose") != permit.purpose
                or row.get("composing_nonce") != permit.composing_nonce
                or row.get("budgets_digest") != permit.budgets_digest
                or int(row.get("paid_dispatches_total", 0))
                != permit.paid_dispatches_total
                or Path(str(row.get("draft_path", ""))) != permit.draft_path
                or not isinstance(row.get("operation_intent"), dict)
                or not isinstance(row.get("operation_payload_digest"), str)
            ):
                return block_locked(
                    "head-local corruption authority is stale or incomplete",
                )
            marker = self.store.read_operation_intent(self.agent, permit.nonce)
            try:
                resolved = permit.draft_path.resolve(strict=True)
                resolved.relative_to(self.drafts.resolve(strict=True))
                if permit.draft_path.is_symlink() or resolved.stat().st_size > 1024 * 1024:
                    raise ValueError("captured draft identity is unsafe")
                payload = resolved.read_bytes()
                body = payload.decode("utf-8")
            except (OSError, UnicodeError, ValueError):
                return block_locked(
                    "head-local artifact is unreadable; corruption is unproven",
                )
            intent = row["operation_intent"]
            observed_payload_sha256 = hashlib.sha256(payload).hexdigest()
            observed_operation_digest = operation_payload_digest(
                operation=str(intent.get("operation", "")),
                body=body,
                kind=str(intent.get("kind", "")),
                recipient=str(intent.get("recipient", "")),
                in_reply_to=intent.get("in_reply_to"),
                request_id=intent.get("request_id"),
                broadcast_id=intent.get("broadcast_id"),
                origin_request_id=intent.get("origin_request_id"),
                origin_inbound_id=intent.get("origin_inbound_id"),
                origin_obligation_key_digest=intent.get(
                    "origin_obligation_key_digest"
                ),
                expected_roster_revision=intent.get("expected_roster_revision"),
            )
            if not isinstance(marker, dict):
                return block_locked(
                    "head-local durable operation proof is unavailable",
                )
            stable_operation_digest = row["operation_payload_digest"]
            durable_proofs_agree = all((
                marker.get("operation_digest") == stable_operation_digest,
                marker.get("intent_digest")
                == self.store._operation_intent_digest(intent),
            ))
            artifact_disagrees = all((
                observed_operation_digest != stable_operation_digest,
                observed_payload_sha256 != marker.get("payload_sha256"),
            ))
            if not durable_proofs_agree:
                return block_locked(
                    "head-local durable operation proofs disagree",
                )
            if not artifact_disagrees:
                return block_locked(
                    "head-local artifact corruption is not proven",
                )
            proof = {
                "kind": "HEAD_LOCAL_PROOF_FAILURE",
                "inbound_id": key.inbound_id,
                "key_digest": key.digest,
                "operation_nonce": permit.nonce,
                "draft_path": str(permit.draft_path),
                "expected_payload_sha256": marker.get("payload_sha256"),
                "observed_payload_sha256": observed_payload_sha256,
                "expected_operation_digest": row.get("operation_payload_digest"),
                "observed_operation_digest": observed_operation_digest,
            }
            admission["head_local_proof_failure"] = proof
            self._append(
                ledger,
                "HEAD_LOCAL_PROOF_FAILURE",
                scope=key.inbound_id,
                key_digest=key.digest,
                data=proof,
            )
            self._write(ledger)
            revision = int(ledger["scoped_revisions"].get(key.inbound_id, 0))
        return self.fail_delivery_or_block(
            record,
            key,
            reason=reason,
            expected_revision=revision,
        )

    def settle_retry_exhaustion(
        self,
        record: dict,
        key: ObligationKey,
        *,
        category: str,
        reason: str,
        permit: DispatchPermit | None = None,
    ) -> Resolution:
        """Replay once at the bound, then choose terminal, local failure, or BLOCKED."""
        if category not in {"operation_infra", "finalization"}:
            raise ValueError("unknown retry category")
        latest = self.resolve(record)
        if latest.terminal:
            finalized = self.finalize(
                record,
                latest,
                expected_revision=latest.ledger_revision,
            )
            if finalized.state != ResolverState.INDETERMINATE:
                return finalized
            raced = self.resolve(record)
            if raced.terminal:
                # Keep the canonical terminal authoritative.  The next poll can
                # retry only its cursor CAS; synthesizing BLOCKED here would mask
                # the very terminal the exhaustion replay was required to honor.
                return raced
            return self._block_retry_exhaustion(
                key,
                reason=f"{category} exhaustion raced its final replay",
            )
        if latest.state in {
            ResolverState.BLOCKED,
            ResolverState.BLOCKED_POLICY,
            ResolverState.BLOCKED_COMPLIANCE,
            ResolverState.INDETERMINATE,
        }:
            return self._block_retry_exhaustion(
                key,
                reason=f"{category} exhaustion: {latest.reason}",
            )
        if category == "operation_infra":
            if permit is None:
                return self._block_retry_exhaustion(
                    key,
                    reason="operation exhaustion locality proof is unavailable",
                )
            return self.fail_head_local_corruption_or_block(
                record,
                key,
                permit,
                reason=reason,
                expected_revision=latest.scoped_revision,
            )
        return self.fail_delivery_or_block(
            record,
            key,
            reason=reason,
            expected_revision=latest.scoped_revision,
        )

    def _record_finalization_miss_locked(
        self,
        ledger: dict,
        owner: dict,
        *,
        inbound_id: str,
        key_digest: str | None,
    ) -> bool:
        """Persist an observed CAS miss before returning control to the loop."""
        now_text = self.now()
        owner["finalization_misses"] = int(owner.get("finalization_misses", 0)) + 1
        owner["finalization_first_at"] = owner.get("finalization_first_at") or now_text
        owner["finalization_retry_inflight"] = False
        first = _epoch(owner.get("finalization_first_at"))
        now = _epoch(now_text)
        elapsed = 0.0 if first is None or now is None else max(0.0, now - first)
        exhausted = (
            int(owner["finalization_misses"]) >= MAX_FINALIZATION_MISSES
            or elapsed >= MAX_FINALIZATION_SECONDS
        )
        self._append(
            ledger,
            "FINALIZATION_MISS_RECORDED",
            scope=inbound_id,
            key_digest=key_digest,
            data={
                "count": owner["finalization_misses"],
                "elapsed_seconds": elapsed,
                "exhausted": exhausted,
            },
        )
        return exhausted

    def _advance_record_cursor(self, record: dict) -> None:
        if record.get("mode") == "scoped":
            self.store.mark_thread_seen(
                self.agent,
                record["scoped"]["request_id"],
                record["id"],
            )
        else:
            self.store.advance_cursor(self.agent, record["id"])

    def _cursor_projection_is_complete(self, record: dict) -> bool:
        inbound_id = record.get("id")
        if not isinstance(inbound_id, str):
            return False
        if record.get("mode") == "scoped":
            scoped = record.get("scoped")
            if not isinstance(scoped, dict) or not isinstance(
                scoped.get("request_id"), str,
            ):
                return False
            return max(
                self.store.cursor(self.agent),
                self.store.thread_seen(self.agent, scoped["request_id"]),
            ) >= inbound_id
        return self.store.cursor(self.agent) >= inbound_id

    def _cursor_projection_exhausted(self, owner: dict) -> tuple[bool, float]:
        first = _epoch(owner.get("cursor_projection_first_at"))
        now = _epoch(self.now())
        elapsed = 0.0 if first is None or now is None else max(0.0, now - first)
        return (
            int(owner.get("cursor_projection_misses", 0))
            >= MAX_FINALIZATION_MISSES
            or elapsed >= MAX_FINALIZATION_SECONDS,
            elapsed,
        )

    def _record_cursor_projection_miss_locked(
        self,
        ledger: dict,
        owner: dict,
        *,
        inbound_id: str,
        key_digest: str | None,
        reason: str,
    ) -> bool:
        """Persist a projection miss before another projection may be tried."""
        now_text = self.now()
        owner["cursor_projection_misses"] = int(
            owner.get("cursor_projection_misses", 0)
        ) + 1
        owner["cursor_projection_first_at"] = (
            owner.get("cursor_projection_first_at")
            or owner.get("cursor_projection_reserved_at")
            or now_text
        )
        owner["cursor_projection_inflight"] = False
        owner["cursor_projection_reserved_at"] = None
        exhausted, elapsed = self._cursor_projection_exhausted(owner)
        self._append(
            ledger,
            "CURSOR_PROJECTION_MISS_RECORDED",
            scope=inbound_id,
            key_digest=key_digest,
            data={
                "count": owner["cursor_projection_misses"],
                "elapsed_seconds": elapsed,
                "exhausted": exhausted,
                "reason": reason,
            },
        )
        return exhausted

    def _block_cursor_projection_locked(
        self,
        ledger: dict,
        owner: dict,
        *,
        inbound_id: str,
        key_digest: str | None,
    ) -> str:
        reason = "cursor projection retry bound exhausted"
        owner["cursor_projection_blocked"] = True
        owner["cursor_projection_blocked_reason"] = reason
        owner["cursor_projection_inflight"] = False
        owner["cursor_projection_reserved_at"] = None
        if not any(
            event.get("transition") == "CURSOR_PROJECTION_BLOCKED"
            and event.get("key_digest") == key_digest
            and event.get("scope") == inbound_id
            for event in ledger["transitions"]
        ):
            self._append(
                ledger,
                "CURSOR_PROJECTION_BLOCKED",
                scope=inbound_id,
                key_digest=key_digest,
                data={"reason": reason},
            )
        return reason

    def _reserve_cursor_projection_locked(
        self,
        ledger: dict,
        owner: dict,
        *,
        inbound_id: str,
        key_digest: str | None,
    ) -> str | None:
        """Reserve the physical cursor side effect, reconciling a crashed attempt."""
        if owner.get("cursor_projection_blocked") is True:
            return str(
                owner.get("cursor_projection_blocked_reason")
                or "cursor projection retry bound exhausted"
            )
        if owner.get("cursor_projection_inflight") is True:
            exhausted = self._record_cursor_projection_miss_locked(
                ledger,
                owner,
                inbound_id=inbound_id,
                key_digest=key_digest,
                reason="previous cursor projection outcome is missing",
            )
            if exhausted:
                return self._block_cursor_projection_locked(
                    ledger,
                    owner,
                    inbound_id=inbound_id,
                    key_digest=key_digest,
                )
        exhausted, _elapsed = self._cursor_projection_exhausted(owner)
        if exhausted:
            return self._block_cursor_projection_locked(
                ledger,
                owner,
                inbound_id=inbound_id,
                key_digest=key_digest,
            )
        owner["cursor_projection_inflight"] = True
        owner["cursor_projection_reserved_at"] = self.now()
        self._append(
            ledger,
            "CURSOR_PROJECTION_RESERVED",
            scope=inbound_id,
            key_digest=key_digest,
            data={"attempt": int(owner.get("cursor_projection_misses", 0)) + 1},
        )
        return None

    @staticmethod
    def _clear_cursor_projection_reservation_locked(owner: dict) -> None:
        owner["cursor_projection_inflight"] = False
        owner["cursor_projection_reserved_at"] = None

    def _revalidate_no_admission_projection_policy(
        self,
        observed: PolicySnapshot,
        claim: dict,
    ) -> Resolution | None:
        """Gate pending projection, without un-winning a zero-work terminal."""
        current = self._current_policy()
        if self._policy_blocks_durable_terminal_projection(current):
            return Resolution(current.status, current.reason)
        if claim.get("state") == "finalized" and self._claim_has_no_legacy_work(claim):
            return None
        if (
            current.status == observed.status
            and current.generation == observed.generation
            and current.grade == observed.grade
            and current.agent == observed.agent
        ):
            return None
        return Resolution(
            ResolverState.BLOCKED_POLICY,
            "operator policy changed before legacy cursor projection",
        )

    def _defer_cursor_projection_for_policy(
        self,
        record: dict,
        *,
        key_digest: str | None,
        reserved_at: object,
    ) -> None:
        """Cancel our reservation when policy, rather than the cursor write, blocks."""
        if not isinstance(reserved_at, str):
            return
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            ledger = self._load()
            owner = (
                ledger["obligations"].get(key_digest)
                if key_digest is not None
                else ledger["no_admission_claims"].get(record.get("id"))
            )
            if (
                isinstance(owner, dict)
                and owner.get("cursor_projection_inflight") is True
                and owner.get("cursor_projection_reserved_at") == reserved_at
            ):
                self._clear_cursor_projection_reservation_locked(owner)
                self._write(ledger)

    def validate_no_admission_authority(
        self,
        record: dict,
        resolution: Resolution,
        *,
        side_effect: Callable[[], None] | None = None,
    ) -> Resolution:
        """Confirm authority and optionally linearize its side effect with replay."""
        if not resolution.allows_legacy_commit:
            raise GateError("only no-admission resolutions have legacy authority")
        inbound_id = record.get("id")
        if not isinstance(inbound_id, str):
            raise GateError("no-admission authority requires an exact inbound")
        terminal_replay: Resolution | None = None
        terminal_has_started_work = False
        with self.store._message_publication_lock():
            try:
                messages, _ = self._validated_messages()
            except LedgerUnreadable as exc:
                return Resolution(ResolverState.BLOCKED, str(exc))
            inbound = self._record_message(record, messages)
            if inbound is None:
                return Resolution(
                    ResolverState.INDETERMINATE,
                    "no-admission authority inbound is absent from validated replay",
                )
            current_policy = self._current_policy()
            if self._policy_blocks_durable_terminal_projection(current_policy):
                return Resolution(current_policy.status, current_policy.reason)
            eligibility, detail, _ = self._eligibility(
                record,
                policy=current_policy,
            )
            with self.store._exclusive_lock(
                self.path.with_suffix(".lock"), timeout=10.0,
            ):
                policy_block = self._revalidate_admission_policy(current_policy)
                if policy_block is not None:
                    return policy_block
                ledger = self._load()
                replayed = self._resolve_replay(
                    record,
                    messages,
                    ledger,
                    admission=None,
                )
                if replayed.state in TERMINAL_STATES:
                    terminal_replay = replayed
                    terminal_claim = ledger["no_admission_claims"].get(inbound_id)
                    terminal_has_started_work = bool(
                        isinstance(terminal_claim, dict)
                        and terminal_claim.get("drive_started_at")
                    )
                else:
                    if eligibility in {
                        ResolverState.BLOCKED,
                        ResolverState.BLOCKED_POLICY,
                        ResolverState.BLOCKED_COMPLIANCE,
                    }:
                        return Resolution(eligibility, detail)
                    if (
                        eligibility != resolution.state
                        or current_policy.generation
                        != resolution.activation_generation
                    ):
                        return self._policy_authority_failure(
                            current_policy,
                            reason=(
                                "no-admission policy changed before legacy side effect"
                            ),
                        )
                    if resolution.ledger_revision is None:
                        policy_block = self._revalidate_admission_policy(
                            current_policy,
                        )
                        if policy_block is not None:
                            return policy_block
                        if side_effect is not None:
                            side_effect()
                        return Resolution(
                            resolution.state,
                            resolution.reason,
                            scoped_revision=resolution.scoped_revision,
                            activation_generation=current_policy.generation,
                            readiness_generation=current_policy.generation,
                        )
                    claim = ledger["no_admission_claims"].get(inbound_id)
                    if not isinstance(claim, dict):
                        return Resolution(
                            ResolverState.INDETERMINATE,
                            "no-admission authority claim missing",
                        )
                    if (
                        not self._claim_matches_policy(claim, current_policy)
                        or claim.get("policy_generation")
                        != resolution.activation_generation
                    ):
                        return self._policy_authority_failure(
                            current_policy,
                            reason="no-admission side-effect policy changed",
                        )
                    if claim.get("resolution") != resolution.state.value:
                        return Resolution(
                            ResolverState.INDETERMINATE,
                            "no-admission authority conflicts with replay",
                        )
                    if (
                        claim.get("state") != "open"
                        or claim.get("fence") != self.fence
                    ):
                        return Resolution(
                            ResolverState.INDETERMINATE,
                            "no-admission authority is not open for this fence",
                        )
                    if int(ledger["scoped_revisions"].get(inbound_id, 0)) != (
                        resolution.ledger_revision
                    ):
                        return Resolution(
                            ResolverState.INDETERMINATE,
                            "no-admission authority scoped CAS miss",
                        )
                    policy_block = self._revalidate_admission_policy(current_policy)
                    if policy_block is not None:
                        return policy_block
                    revision = int(
                        ledger["scoped_revisions"].get(inbound_id, 0)
                    )
                    if side_effect is not None:
                        side_effect()
            if terminal_replay is not None and not terminal_has_started_work:
                return self._normalize_pre_admission_terminal(
                    record,
                    inbound,
                    terminal_replay,
                    current_policy,
                )
        if terminal_replay is not None:
            return self.admit_or_finalize(record)
        return Resolution(
            resolution.state,
            resolution.reason,
            scoped_revision=revision,
            ledger_revision=revision,
            activation_generation=current_policy.generation,
            readiness_generation=current_policy.generation,
        )

    def authorize_no_admission_drive(
        self,
        record: dict,
        resolution: Resolution,
    ) -> Resolution:
        """Replay, then durably pin policy authority immediately before legacy work."""
        if not resolution.allows_legacy_commit:
            raise GateError("only no-admission resolutions authorize legacy work")
        inbound_id = record.get("id")
        if not isinstance(inbound_id, str):
            raise GateError("no-admission drive requires an exact inbound")
        terminal_replay: Resolution | None = None
        terminal_has_started_work = False
        with self.store._message_publication_lock():
            try:
                messages, _ = self._validated_messages()
            except LedgerUnreadable as exc:
                return Resolution(ResolverState.BLOCKED, str(exc))
            inbound = self._record_message(record, messages)
            if inbound is None:
                return Resolution(
                    ResolverState.INDETERMINATE,
                    "no-admission drive inbound is absent from validated replay",
                )
            current_policy = self._current_policy()
            if self._policy_blocks_durable_terminal_projection(current_policy):
                return Resolution(current_policy.status, current_policy.reason)
            eligibility, detail, _ = self._eligibility(
                record,
                policy=current_policy,
            )
            with self.store._exclusive_lock(
                self.path.with_suffix(".lock"), timeout=10.0,
            ):
                policy_block = self._revalidate_admission_policy(current_policy)
                if policy_block is not None:
                    return policy_block
                ledger = self._load()
                replayed = self._resolve_replay(
                    record,
                    messages,
                    ledger,
                    admission=None,
                )
                if replayed.state in TERMINAL_STATES:
                    terminal_replay = replayed
                    terminal_claim = ledger["no_admission_claims"].get(inbound_id)
                    terminal_has_started_work = bool(
                        isinstance(terminal_claim, dict)
                        and terminal_claim.get("drive_started_at")
                    )
                else:
                    if eligibility in {
                        ResolverState.BLOCKED,
                        ResolverState.BLOCKED_POLICY,
                        ResolverState.BLOCKED_COMPLIANCE,
                    }:
                        return Resolution(eligibility, detail)
                    if (
                        eligibility != resolution.state
                        or current_policy.generation
                        != resolution.activation_generation
                    ):
                        return self._policy_authority_failure(
                            current_policy,
                            reason="no-admission policy changed before legacy drive",
                        )
                    if resolution.ledger_revision is None:
                        policy_block = self._revalidate_admission_policy(
                            current_policy,
                        )
                        if policy_block is not None:
                            return policy_block
                        return Resolution(
                            resolution.state,
                            resolution.reason,
                            scoped_revision=resolution.scoped_revision,
                            activation_generation=current_policy.generation,
                            readiness_generation=current_policy.generation,
                        )
                    claim = ledger["no_admission_claims"].get(inbound_id)
                    if not isinstance(claim, dict):
                        return Resolution(
                            ResolverState.INDETERMINATE,
                            "no-admission drive claim missing",
                        )
                    if (
                        not self._claim_matches_policy(claim, current_policy)
                        or claim.get("policy_generation")
                        != resolution.activation_generation
                    ):
                        return self._policy_authority_failure(
                            current_policy,
                            reason="no-admission drive claim policy changed",
                        )
                    if claim.get("resolution") != resolution.state.value:
                        return Resolution(
                            ResolverState.INDETERMINATE,
                            "no-admission drive claim conflicts with replay",
                        )
                    if (
                        claim.get("state") != "open"
                        or claim.get("fence") != self.fence
                    ):
                        return Resolution(
                            ResolverState.INDETERMINATE,
                            "no-admission drive claim is not open for this fence",
                        )
                    if int(ledger["scoped_revisions"].get(inbound_id, 0)) != (
                        resolution.ledger_revision
                    ):
                        return Resolution(
                            ResolverState.INDETERMINATE,
                            "no-admission drive scoped CAS miss",
                        )
                    claim["drive_started_at"] = (
                        claim.get("drive_started_at") or self.now()
                    )
                    claim["drive_attempts"] = int(
                        claim.get("drive_attempts", 0)
                    ) + 1
                    self._append(
                        ledger,
                        "PRE_ADMISSION_DRIVE_AUTHORIZED",
                        scope=inbound_id,
                        source_id=inbound_id,
                        data={
                            "attempt": claim["drive_attempts"],
                            "policy_generation": current_policy.generation,
                        },
                    )
                    self._write(ledger)
                    policy_block = self._revalidate_admission_policy(current_policy)
                    if policy_block is not None:
                        return policy_block
                    revision = int(
                        ledger["scoped_revisions"].get(inbound_id, 0)
                    )
            if terminal_replay is not None and not terminal_has_started_work:
                return self._normalize_pre_admission_terminal(
                    record,
                    inbound,
                    terminal_replay,
                    current_policy,
                )
        if terminal_replay is not None:
            return self.admit_or_finalize(record)
        return Resolution(
            resolution.state,
            resolution.reason,
            scoped_revision=revision,
            ledger_revision=revision,
            activation_generation=current_policy.generation,
            readiness_generation=current_policy.generation,
        )

    def record_no_admission_success(
        self,
        record: dict,
        resolution: Resolution,
    ) -> Resolution:
        """Durably retain a successful legacy turn before its cursor CAS.

        A finalization CAS miss must retry only the cursor disposition.  Without
        this write-ahead marker the next poll would invoke the model again even
        though the preceding turn already completed successfully.
        """
        if not resolution.allows_legacy_commit:
            raise GateError("only no-admission resolutions can retain legacy success")
        inbound_id = record.get("id")
        if not isinstance(inbound_id, str):
            raise GateError("no-admission success requires an exact inbound")
        with ExitStack() as locks:
            locks.enter_context(self.store._message_publication_lock())
            try:
                messages, _ = self._validated_messages()
            except LedgerUnreadable as exc:
                return Resolution(ResolverState.BLOCKED, str(exc))
            current_policy = self._current_policy()
            current_eligibility, detail, _ = self._eligibility(
                record,
                policy=current_policy,
            )
            if current_policy.generation != resolution.activation_generation:
                return self._policy_authority_failure(
                    current_policy,
                    reason="no-admission policy changed during legacy drive",
                )
            if current_eligibility != resolution.state:
                if current_eligibility in {
                    ResolverState.BLOCKED,
                    ResolverState.BLOCKED_POLICY,
                    ResolverState.BLOCKED_COMPLIANCE,
                }:
                    return Resolution(current_eligibility, detail)
                return Resolution(
                    ResolverState.INDETERMINATE,
                    "no-admission success classification changed during drive",
                )
            locks.enter_context(
                self.store._exclusive_lock(
                    self.path.with_suffix(".lock"), timeout=10.0,
                )
            )
            ledger = self._load()
            claim = ledger["no_admission_claims"].get(inbound_id)
            if not isinstance(claim, dict):
                return Resolution(
                    ResolverState.INDETERMINATE,
                    "no-admission success claim missing",
                )
            if (
                not self._claim_matches_policy(claim, current_policy)
                or claim.get("policy_generation") != resolution.activation_generation
            ):
                return self._policy_authority_failure(
                    current_policy,
                    reason="no-admission claim policy changed during legacy drive",
                )
            if claim.get("resolution") != resolution.state.value:
                return Resolution(
                    ResolverState.INDETERMINATE,
                    "no-admission success replay mismatch",
                )
            if claim.get("state") == "finalized":
                return resolution
            if claim.get("state") == "blocked":
                return Resolution(
                    ResolverState.BLOCKED,
                    str(claim.get("blocked_reason") or "finalization blocked"),
                )
            if claim.get("state") == "finalization_pending":
                return Resolution(
                    resolution.state,
                    "no_admission_finalization_pending",
                    scoped_revision=int(
                        ledger["scoped_revisions"].get(inbound_id, 0)
                    ),
                    ledger_revision=int(
                        ledger["scoped_revisions"].get(inbound_id, 0)
                    ),
                    activation_generation=current_policy.generation,
                    readiness_generation=current_policy.generation,
                )
            replayed = self._resolve_replay(
                record,
                messages,
                ledger,
                admission=None,
            )
            if replayed.terminal:
                if claim.get("state") != "open" or claim.get("fence") != self.fence:
                    return Resolution(
                        ResolverState.INDETERMINATE,
                        "legacy terminal retention fence mismatch",
                    )
                self._retain_same_generation_legacy_terminal_locked(
                    ledger,
                    claim,
                    record,
                    replayed,
                    current_policy,
                )
                self._write(ledger)
                policy_block = self._revalidate_admission_policy(current_policy)
                if policy_block is not None:
                    return policy_block
                revision = int(ledger["scoped_revisions"].get(inbound_id, 0))
                return Resolution(
                    replayed.state,
                    "no_admission_finalization_pending",
                    evidence_id=replayed.evidence_id,
                    scoped_revision=revision,
                    ledger_revision=revision,
                    activation_generation=current_policy.generation,
                    readiness_generation=current_policy.generation,
                )
            if claim.get("state") != "open" or claim.get("fence") != self.fence:
                return Resolution(
                    ResolverState.INDETERMINATE,
                    "no-admission success fence mismatch",
                )
            claim["state"] = "finalization_pending"
            claim["drive_succeeded_at"] = self.now()
            self._append(
                ledger,
                "PRE_ADMISSION_SUCCESS_RETAINED",
                scope=inbound_id,
                source_id=inbound_id,
                data={"state": resolution.state.value},
            )
            self._write(ledger)
            policy_block = self._revalidate_admission_policy(current_policy)
            if policy_block is not None:
                return policy_block
            revision = int(ledger["scoped_revisions"].get(inbound_id, 0))
        return Resolution(
            resolution.state,
            "no_admission_finalization_pending",
            scoped_revision=revision,
            ledger_revision=revision,
            activation_generation=current_policy.generation,
            readiness_generation=current_policy.generation,
        )

    def finalize(
        self,
        record: dict,
        resolution: Resolution,
        *,
        expected_revision: int | None = None,
    ) -> Resolution:
        if not (resolution.terminal or resolution.allows_legacy_commit):
            raise GateError("nonterminal resolution cannot finalize")
        projection_block: str | None = None
        projection_reserved_at: object = None
        durable_zero_work_terminal = False
        key = resolution.key
        no_admission_policy: PolicySnapshot | None = None
        no_admission_messages: list[Message] | None = None
        if key is not None and any((
            key.responder != self.agent,
            record.get("id") != key.inbound_id,
            record.get("to") != key.responder,
            record.get("from") != key.requester,
            record.get("correlation_id") != key.correlation_id,
        )):
            raise GateError("finalizer record does not match the exact inbound key")
        inbound_id = str(key.inbound_id if key is not None else record.get("id"))
        key_digest = key.digest if key is not None else None
        if key is None:
            try:
                no_admission_messages, _ = self._validated_messages()
            except LedgerUnreadable as exc:
                return Resolution(ResolverState.BLOCKED, str(exc))
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            ledger = self._load()
            write_required = True
            if key is not None:
                admission = ledger["obligations"].get(key.digest)
                if not isinstance(admission, dict):
                    return Resolution(ResolverState.INDETERMINATE, "finalizer admission missing")
                disposition_state = (
                    "delivery_failed"
                    if resolution.state == ResolverState.DELIVERY_EXHAUSTED
                    else resolution.state.value
                )
                persisted_admission_state = (
                    "delivery_failed"
                    if resolution.state == ResolverState.DELIVERY_EXHAUSTED
                    else "operator_resolved"
                    if resolution.state == ResolverState.OPERATOR_RESOLVED
                    else "transferred"
                    if resolution.state == ResolverState.TRANSFERRED
                    else "broadcast_policy_satisfied"
                    if resolution.state == ResolverState.BROADCAST_POLICY_SATISFIED
                    else "finalized"
                )
                disposition = ledger["cursor_dispositions"].get(self.agent)
                already_disposed = (
                    admission.get("state") == persisted_admission_state
                    and admission.get("terminal_state") == resolution.state.value
                    and isinstance(disposition, dict)
                    and disposition.get("inbound_id") == record.get("id")
                    and disposition.get("mode") == record.get("mode", "global")
                    and disposition.get("state") == disposition_state
                )
                if already_disposed:
                    if (
                        resolution.state == ResolverState.DELIVERY_EXHAUSTED
                        and not self._delivery_transaction_intact(
                            ledger,
                            admission,
                            key,
                            record,
                        )
                    ):
                        reason = "failed-delivery transaction is structurally torn"
                        admission["state"] = "blocked"
                        admission["blocked_reason"] = reason
                        self._append(
                            ledger,
                            "OBLIGATION_BLOCKED",
                            scope=key.inbound_id,
                            key_digest=key.digest,
                            data={"reason": reason},
                        )
                        self._write(ledger)
                        return Resolution(ResolverState.BLOCKED, reason, key)
                    write_required = False
                else:
                    policy_or_transfer_preclosed = bool(
                        resolution.state in {
                            ResolverState.TRANSFERRED,
                            ResolverState.BROADCAST_POLICY_SATISFIED,
                        }
                        and admission.get("state") == persisted_admission_state
                        and admission.get("terminal_state") == resolution.state.value
                    )
                    if admission.get("state") != "open" and not policy_or_transfer_preclosed:
                        return Resolution(
                            ResolverState.INDETERMINATE,
                            "finalized cursor disposition is torn",
                            key,
                        )
                    if not policy_or_transfer_preclosed and int(
                        ledger["scoped_revisions"].get(key.inbound_id, 0)
                    ) != (
                        resolution.scoped_revision
                    ):
                        exhausted = self._record_finalization_miss_locked(
                            ledger,
                            admission,
                            inbound_id=key.inbound_id,
                            key_digest=key.digest,
                        )
                        self._write(ledger)
                        return Resolution(
                            ResolverState.INDETERMINATE,
                            (
                                "finalization CAS contention exhausted"
                                if exhausted
                                else "finalizer scoped CAS miss"
                            ),
                            key,
                        )
                    admission["state"] = persisted_admission_state
                    admission["finalization_retry_inflight"] = False
                    admission["terminal_state"] = resolution.state.value
                    if not policy_or_transfer_preclosed:
                        admission["terminal_evidence_id"] = resolution.evidence_id
                    admission["finalized_at"] = self.now()
                    self._append(
                        ledger,
                        "CURSOR_FINALIZED",
                        scope=key.inbound_id,
                        key_digest=key.digest,
                        data={"state": resolution.state.value},
                    )
                    if resolution.compliance_success:
                        self._reset_compliance_streak_locked(ledger, key)
                    if resolution.state == ResolverState.SATISFIED:
                        self._apply_broadcast_policy_locked(
                            ledger,
                            broadcast_id=admission.get("broadcast_id"),
                            broadcast_generation=admission.get("broadcast_generation"),
                        )
                owner = admission
            else:
                claim = ledger["no_admission_claims"].get(record.get("id"))
                if not isinstance(claim, dict):
                    return Resolution(
                        ResolverState.INDETERMINATE,
                        "no-admission finalizer claim missing",
                    )
                current_policy = self._current_policy()
                no_admission_policy = current_policy
                if self._policy_blocks_durable_terminal_projection(current_policy):
                    return Resolution(current_policy.status, current_policy.reason)
                terminal_authority = self._recognized_zero_work_terminal_authority(
                    record,
                    no_admission_messages or [],
                    ledger,
                    claim,
                )
                if (
                    terminal_authority is not None
                    and terminal_authority.state == ResolverState.INDETERMINATE
                ):
                    return terminal_authority
                terminal_immune = bool(
                    terminal_authority is not None
                    and terminal_authority.state == resolution.state
                    and terminal_authority.evidence_id == resolution.evidence_id
                )
                if terminal_authority is not None and not terminal_immune:
                    return Resolution(
                        ResolverState.INDETERMINATE,
                        "no-admission finalizer terminal authority changed",
                    )
                if claim.get("resolution") != resolution.state.value:
                    return Resolution(
                        ResolverState.INDETERMINATE,
                        "no-admission finalizer replay mismatch",
                    )
                if claim.get("state") == "blocked":
                    return Resolution(
                        ResolverState.BLOCKED,
                        str(claim.get("blocked_reason") or "finalization blocked"),
                    )
                if claim.get("state") == "finalized":
                    disposition = ledger["cursor_dispositions"].get(self.agent)
                    disposition_state = (
                        "delivery_failed"
                        if resolution.state == ResolverState.DELIVERY_EXHAUSTED
                        else resolution.state.value
                    )
                    if not isinstance(disposition, dict) or any((
                        disposition.get("inbound_id") != record.get("id"),
                        disposition.get("mode") != record.get("mode", "global"),
                        disposition.get("state") != disposition_state,
                        claim.get("terminal_evidence_id") != resolution.evidence_id,
                    )):
                        return Resolution(
                            ResolverState.INDETERMINATE,
                            "no-admission finalized disposition is torn",
                        )
                    if not terminal_immune and not self._claim_has_no_legacy_work(
                        claim
                    ) and (
                        not self._claim_matches_policy(claim, current_policy)
                        or resolution.activation_generation
                        not in {None, current_policy.generation}
                    ):
                        return self._policy_authority_failure(
                            current_policy,
                            reason="no-admission policy changed before finalization",
                        )
                    write_required = (
                        self._rebind_zero_work_terminal_authority_locked(
                            ledger,
                            claim,
                            record,
                            terminal_authority,
                            current_policy,
                        )
                        if terminal_immune and terminal_authority is not None
                        else False
                    )
                else:
                    if not terminal_immune and (
                        not self._claim_matches_policy(claim, current_policy)
                        or resolution.activation_generation
                        not in {None, current_policy.generation}
                    ):
                        return self._policy_authority_failure(
                            current_policy,
                            reason="no-admission policy changed before finalization",
                        )
                    if claim.get("state") not in {
                        "open",
                        "finalization_pending",
                    } or (
                        claim.get("fence") != self.fence
                        and (
                            not terminal_immune
                            or not self._can_reassign_no_admission_claim(claim)
                        )
                    ):
                        return Resolution(
                            ResolverState.INDETERMINATE,
                            "no-admission finalizer fence mismatch",
                        )
                    if expected_revision is None:
                        return Resolution(
                            ResolverState.INDETERMINATE,
                            "no-admission finalizer requires a scoped revision",
                        )
                    if int(ledger["scoped_revisions"].get(record.get("id"), 0)) != (
                        expected_revision
                    ):
                        exhausted = self._record_finalization_miss_locked(
                            ledger,
                            claim,
                            inbound_id=str(record.get("id")),
                            key_digest=None,
                        )
                        if exhausted:
                            reason = "no-admission finalization CAS contention exhausted"
                            claim["state"] = "blocked"
                            claim["blocked_reason"] = reason
                            self._append(
                                ledger,
                                "NO_ADMISSION_FINALIZATION_BLOCKED",
                                scope=str(record.get("id")),
                                data={"reason": reason},
                            )
                            self._write(ledger)
                            return Resolution(ResolverState.BLOCKED, reason)
                        self._write(ledger)
                        return Resolution(
                            ResolverState.INDETERMINATE,
                            "no-admission finalizer CAS miss",
                        )
                    if terminal_immune and terminal_authority is not None:
                        self._rebind_zero_work_terminal_authority_locked(
                            ledger,
                            claim,
                            record,
                            terminal_authority,
                            current_policy,
                        )
                    self._append(
                        ledger,
                        "PRE_ADMISSION_FINALIZED",
                        scope=record.get("id"),
                        source_id=resolution.evidence_id,
                        data={"state": resolution.state.value},
                    )
                    claim["state"] = "finalized"
                    claim["finalized_at"] = self.now()
                    claim["terminal_evidence_id"] = resolution.evidence_id
                    if resolution.state == ResolverState.SATISFIED:
                        indexed = ledger["messages"].get(record.get("id"), {})
                        self._apply_broadcast_policy_locked(
                            ledger,
                            broadcast_id=indexed.get("broadcast_id"),
                            broadcast_generation=indexed.get("broadcast_generation"),
                        )
                owner = claim
            if write_required:
                disposition_state = (
                    "delivery_failed"
                    if resolution.state == ResolverState.DELIVERY_EXHAUSTED
                    else resolution.state.value
                )
                ledger["cursor_dispositions"][self.agent] = {
                    "inbound_id": record.get("id"),
                    "mode": record.get("mode", "global"),
                    "state": disposition_state,
                    "at": self.now(),
                }
            durable_zero_work_terminal = (
                no_admission_policy is not None
                and owner.get("state") == "finalized"
                and self._claim_has_no_legacy_work(owner)
            )
            if durable_zero_work_terminal and write_required:
                # The terminal and its exact disposition become authoritative before
                # policy is checked for the pending physical projection.  A crash or
                # unreadable policy here therefore leaves no false cursor attempt.
                self._write(ledger)
                write_required = False
                policy_block = self._revalidate_no_admission_projection_policy(
                    no_admission_policy,
                    owner,
                )
                if policy_block is not None:
                    return policy_block
                rebound_policy = self._current_policy()
                if self._policy_blocks_durable_terminal_projection(rebound_policy):
                    return Resolution(rebound_policy.status, rebound_policy.reason)
                recognized = self._recognized_zero_work_terminal_authority(
                    record,
                    no_admission_messages or [],
                    ledger,
                    owner,
                )
                if (
                    recognized is not None
                    and recognized.state == ResolverState.INDETERMINATE
                ):
                    return recognized
                if recognized is not None:
                    no_admission_policy = rebound_policy
                    if self._rebind_zero_work_terminal_authority_locked(
                        ledger,
                        owner,
                        record,
                        recognized,
                        rebound_policy,
                    ):
                        self._write(ledger)
            if self._cursor_projection_is_complete(record):
                projection_was_inflight = owner.get("cursor_projection_inflight") is True
                self._clear_cursor_projection_reservation_locked(owner)
                if write_required or projection_was_inflight:
                    self._write(ledger)
                return resolution
            projection_block = self._reserve_cursor_projection_locked(
                ledger,
                owner,
                inbound_id=inbound_id,
                key_digest=key_digest,
            )
            projection_reserved_at = owner.get("cursor_projection_reserved_at")
            self._write(ledger)
            if (
                no_admission_policy is not None
                and projection_block is None
            ):
                policy_block = self._revalidate_no_admission_projection_policy(
                    no_admission_policy,
                    owner,
                )
                if policy_block is not None:
                    self._clear_cursor_projection_reservation_locked(owner)
                    self._write(ledger)
                    return policy_block
        if projection_block is not None:
            self._record_disposition_block(reason=projection_block)
            return Resolution(ResolverState.INDETERMINATE, projection_block, key)
        # The physical cursor is a projection of the authoritative disposition.
        # Its write-ahead reservation makes a crash an accounted miss before any
        # subsequent retry, while the exact disposition makes the side effect
        # idempotent and prevents duplicate terminal transitions.
        if no_admission_policy is not None:
            policy_block = self._revalidate_no_admission_projection_policy(
                no_admission_policy,
                owner,
            )
            if policy_block is not None:
                self._defer_cursor_projection_for_policy(
                    record,
                    key_digest=key_digest,
                    reserved_at=projection_reserved_at,
                )
                return policy_block
        try:
            self._advance_record_cursor(record)
        except (OSError, TimeoutError, ValueError, RuntimeError) as exc:
            reason = f"cursor projection failed: {type(exc).__name__}"
            exhausted = False
            try:
                with self.store._exclusive_lock(
                    self.path.with_suffix(".lock"), timeout=10.0,
                ):
                    ledger = self._load()
                    owner = (
                        ledger["obligations"].get(key_digest)
                        if key_digest is not None
                        else ledger["no_admission_claims"].get(record.get("id"))
                    )
                    if not isinstance(owner, dict):
                        raise LedgerUnreadable("cursor projection owner is missing")
                    if owner.get("cursor_projection_inflight") is True:
                        exhausted = self._record_cursor_projection_miss_locked(
                            ledger,
                            owner,
                            inbound_id=inbound_id,
                            key_digest=key_digest,
                            reason=reason,
                        )
                    if exhausted:
                        reason = self._block_cursor_projection_locked(
                            ledger,
                            owner,
                            inbound_id=inbound_id,
                            key_digest=key_digest,
                        )
                    self._write(ledger)
            except (OSError, TimeoutError, ValueError, RuntimeError, LedgerUnreadable):
                reason = "cursor projection failure accounting is unavailable"
                exhausted = True
            if exhausted:
                self._record_disposition_block(reason=reason)
            return Resolution(ResolverState.INDETERMINATE, reason, key)
        try:
            with self.store._exclusive_lock(
                self.path.with_suffix(".lock"), timeout=10.0,
            ):
                ledger = self._load()
                owner = (
                    ledger["obligations"].get(key_digest)
                    if key_digest is not None
                    else ledger["no_admission_claims"].get(record.get("id"))
                )
                if isinstance(owner, dict) and owner.get("cursor_projection_inflight"):
                    owner["cursor_projection_inflight"] = False
                    owner["cursor_projection_reserved_at"] = None
                    owner["cursor_projection_completed_at"] = self.now()
                    self._append(
                        ledger,
                        "CURSOR_PROJECTION_COMPLETED",
                        scope=inbound_id,
                        key_digest=key_digest,
                    )
                    self._write(ledger)
        except (OSError, TimeoutError, ValueError, RuntimeError, LedgerUnreadable):
            # The authoritative disposition and its physical projection are both
            # already durable; completion telemetry is best-effort after success.
            pass
        return resolution

    def _broadcast_qualifying_answers_locked(
        self,
        ledger: dict,
        aggregate: dict,
    ) -> list[tuple[int, str, str, str]]:
        """Return canonical answer evidence for the current aggregate generation."""
        descriptor = aggregate.get("descriptor")
        if not isinstance(descriptor, dict):
            return []
        bid = descriptor.get("broadcast_id")
        requester = descriptor.get("requester")
        members = descriptor.get("membership_snapshot")
        if (
            not isinstance(bid, str)
            or not isinstance(requester, str)
            or not isinstance(members, list)
        ):
            return []
        generation = int(aggregate.get("generation", 1))
        qualifying: list[tuple[int, str, str, str]] = []
        for member, inbound_ids in aggregate.get("member_inbounds", {}).items():
            if member not in members or not isinstance(inbound_ids, list):
                continue
            for inbound_id in inbound_ids:
                inbound_row = ledger["messages"].get(inbound_id)
                if not isinstance(inbound_id, str) or not isinstance(inbound_row, dict):
                    continue
                if any((
                    int(inbound_row.get("broadcast_generation", 1)) != generation,
                    inbound_row.get("kind") != "question",
                    inbound_row.get("sender") != requester,
                    inbound_row.get("recipient") != member,
                    inbound_row.get("correlation_id") != bid,
                    inbound_row.get("request_id") != bid,
                    inbound_row.get("broadcast_id") != bid,
                )):
                    continue
                key_id = ledger["inbound_index"].get(inbound_id)
                admission = ledger["obligations"].get(key_id) if key_id else None
                reservations: dict = {}
                if isinstance(admission, dict):
                    try:
                        key = self._key_from(admission.get("key"))
                    except (TypeError, ValueError):
                        continue
                    if any((
                        key.inbound_id != inbound_id,
                        key.correlation_id != bid,
                        key.requester != requester,
                        key.responder != member,
                        admission.get("obligation_class") != "answer",
                        int(admission.get("broadcast_generation") or 1) != generation,
                    )):
                        continue
                    reservations = admission.get("reservations", {})
                    if not isinstance(reservations, dict):
                        continue
                else:
                    claim = ledger["no_admission_claims"].get(inbound_id)
                    evidence_id = (
                        claim.get("terminal_evidence_id")
                        if isinstance(claim, dict)
                        and claim.get("resolution") == ResolverState.SATISFIED.value
                        and claim.get("state") in {"finalization_pending", "finalized"}
                        else None
                    )
                    evidence = ledger["messages"].get(evidence_id)
                    if not isinstance(evidence, dict) or any((
                        int(evidence.get("sequence", 0))
                        <= int(inbound_row.get("sequence", 0)),
                        evidence.get("in_reply_to") != inbound_id,
                        evidence.get("correlation_id") != bid,
                        evidence.get("sender") != member,
                        evidence.get("recipient") != requester,
                        evidence.get("kind") in CONTROL_KINDS,
                    )):
                        continue
                    qualifying.append((
                        int(evidence["sequence"]),
                        str(evidence_id),
                        str(member),
                        inbound_id,
                    ))
                    continue

                for message_id, message_row in ledger["messages"].items():
                    if not isinstance(message_row, dict):
                        continue
                    nonce = message_row.get("operation_nonce")
                    if not (
                        int(message_row.get("sequence", 0))
                        > int(inbound_row.get("sequence", 0))
                        and message_row.get("in_reply_to") == inbound_id
                        and message_row.get("correlation_id") == bid
                        and message_row.get("sender") == member
                        and message_row.get("recipient") == requester
                        and message_row.get("kind") not in CONTROL_KINDS
                        and message_row.get("operation_payload_valid") is True
                        and isinstance(nonce, str)
                        and isinstance(reservations.get(nonce), dict)
                    ):
                        continue
                    qualifying.append((
                        int(message_row["sequence"]),
                        message_id,
                        str(member),
                        inbound_id,
                    ))
                    break
        return sorted(qualifying, key=lambda item: (item[0], item[1]))

    def _apply_broadcast_policy_locked(
        self,
        ledger: dict,
        *,
        broadcast_id: object,
        broadcast_generation: object,
    ) -> None:
        bid = broadcast_id
        if not isinstance(bid, str):
            return
        aggregate = ledger["broadcasts"].get(bid)
        if not isinstance(aggregate, dict) or aggregate.get("state") != "open":
            return
        if int(broadcast_generation or 1) != int(aggregate.get("generation", 1)):
            return
        descriptor = aggregate.get("descriptor")
        if not isinstance(descriptor, dict):
            return
        policy = descriptor.get("response_policy")
        members = descriptor.get("membership_snapshot")
        requester = descriptor.get("requester")
        if (
            policy not in {"each", "any", "quorum"}
            or not isinstance(members, list)
            or not isinstance(requester, str)
        ):
            return
        qualifying = self._broadcast_qualifying_answers_locked(ledger, aggregate)
        threshold = (
            len(members)
            if policy == "each"
            else 1
            if policy == "any"
            else descriptor.get("response_quorum")
        )
        if not isinstance(threshold, int) or threshold < 1:
            return
        earliest_by_member: dict[str, tuple[int, str, str, str]] = {}
        for candidate in qualifying:
            earliest_by_member.setdefault(candidate[2], candidate)
        ordered = sorted(earliest_by_member.values(), key=lambda item: item[0])
        if policy == "each":
            if set(earliest_by_member) != set(members):
                return
            winners = ordered
        else:
            if len(ordered) < threshold:
                return
            winners = ordered[:threshold]
        winning_ids = [candidate[1] for candidate in winners]
        winning_inbounds = {candidate[3] for candidate in winners}
        transaction_id = hashlib.sha256(_canonical({
            "broadcast_descriptor_digest": aggregate.get("descriptor_digest"),
            "winning_ids": winning_ids,
        }).encode("utf-8")).hexdigest()

        affected: list[dict] = []
        close_candidates: list[tuple[str, str | None, dict | None, str]] = []
        pending_candidates: list[tuple[dict, dict]] = []
        conflict: tuple[str, str | None, str] | None = None
        current_generation = int(aggregate.get("generation", 1))
        for member, inbound_ids in sorted(aggregate.get("member_inbounds", {}).items()):
            if member not in members or not isinstance(inbound_ids, list):
                continue
            for inbound_id in inbound_ids:
                inbound_row = ledger["messages"].get(inbound_id)
                if (
                    not isinstance(inbound_id, str)
                    or not isinstance(inbound_row, dict)
                    or int(inbound_row.get("broadcast_generation", 1))
                    != current_generation
                ):
                    continue
                if inbound_id in winning_inbounds:
                    continue
                key_id = ledger["inbound_index"].get(inbound_id)
                admission = ledger["obligations"].get(key_id) if key_id else None
                outcome = "prospective_policy_close"
                if isinstance(admission, dict):
                    if admission.get("state") != "open":
                        continue
                    try:
                        affected_key = self._key_from(admission.get("key"))
                    except (TypeError, ValueError):
                        conflict = conflict or (
                            inbound_id,
                            key_id,
                            "broadcast admission key is invalid",
                        )
                        continue
                    if any((
                        affected_key.inbound_id != inbound_id,
                        affected_key.correlation_id != bid,
                        affected_key.requester != requester,
                        affected_key.responder != member,
                        int(admission.get("broadcast_generation") or 1)
                        != current_generation,
                    )):
                        conflict = conflict or (
                            inbound_id,
                            key_id,
                            "broadcast admission identity conflicts with frozen policy",
                        )
                        continue
                    reservations = admission.get("reservations", {})
                    if not isinstance(reservations, dict):
                        conflict = conflict or (
                            inbound_id,
                            key_id,
                            "broadcast admission reservations are invalid",
                        )
                        continue
                    armed = any(
                        isinstance(row, dict)
                        and row.get("state") in {"reserved", "dispatching", "action_infra"}
                        for row in reservations.values()
                    )
                    if armed:
                        outcome = "reservation_won"
                    else:
                        outcome = "policy_close"
                affected.append({
                    "member": member,
                    "inbound_id": inbound_id,
                    "key_digest": key_id,
                    "scoped_revision": int(
                        ledger["scoped_revisions"].get(inbound_id, 0)
                    ),
                    "outcome": outcome,
                })
                if outcome == "reservation_won" and isinstance(admission, dict):
                    pending_candidates.append((admission, {
                        "transaction_id": transaction_id,
                        "winning_ids": list(winning_ids),
                        "winning_classes": ["answer"] * len(winning_ids),
                        "broadcast_id": bid,
                        "broadcast_generation": current_generation,
                        "policy": policy,
                    }))
                else:
                    close_candidates.append((inbound_id, key_id, admission, member))

        if conflict is not None:
            conflict_inbound, conflict_key, conflict_reason = conflict
            aggregate["state"] = "blocked"
            aggregate["blocked_reason"] = conflict_reason
            self._append(
                ledger,
                "BROADCAST_POLICY_CONFLICT",
                scope=conflict_inbound,
                key_digest=conflict_key,
                data={"broadcast_id": bid, "reason": conflict_reason},
            )
            return

        aggregate_event = self._append(
            ledger,
            "BROADCAST_POLICY_SATISFIED",
            source_id=winning_ids[-1],
            data={
                "aggregate": True,
                "broadcast_id": bid,
                "broadcast_policy_version": descriptor["broadcast_policy_version"],
                "broadcast_generation": aggregate.get("generation", 1),
                "policy": policy,
                "response_quorum": descriptor.get("response_quorum"),
                "winning_ids": winning_ids,
                "winning_classes": ["answer"] * len(winning_ids),
                "affected_member_keys": affected,
                "transaction_id": transaction_id,
                "descriptor_digest": aggregate.get("descriptor_digest"),
            },
        )
        aggregate.update({
            "state": "policy_satisfied",
            "winning_ids": winning_ids,
            "winning_classes": ["answer"] * len(winning_ids),
            "affected_member_keys": affected,
            "transaction_id": transaction_id,
            "aggregate_sequence": aggregate_event["sequence"],
        })
        ledger["delivery_index"].setdefault(bid, []).append({
            "kind": "BROADCAST_POLICY_SATISFIED",
            "sequence": aggregate_event["sequence"],
            "key_digest": (
                ledger["inbound_index"].get(winners[-1][3])
                if winners
                else None
            ),
            "inbound_id": winners[-1][3],
            "question_generation": None,
            "delivery_generation": None,
            "requester": requester,
            "responder": "*",
            "state": ResolverState.BROADCAST_POLICY_SATISFIED.value,
            "aggregate": True,
            "broadcast_id": bid,
            "response_policy": policy,
            "response_quorum": descriptor.get("response_quorum"),
            "broadcast_policy_version": descriptor["broadcast_policy_version"],
            "broadcast_generation": aggregate.get("generation", 1),
            "winning_ids": winning_ids,
            "transaction_id": transaction_id,
        })
        for admission, pending in pending_candidates:
            admission["broadcast_policy_close_pending"] = pending
        for inbound_id, key_id, admission, _member in close_candidates:
            member_event = self._append(
                ledger,
                "BROADCAST_POLICY_SATISFIED",
                scope=inbound_id,
                source_id=winning_ids[-1],
                key_digest=key_id,
                data={
                    "aggregate": False,
                    "broadcast_id": bid,
                    "inbound_id": inbound_id,
                    "winning_ids": winning_ids,
                    "winning_classes": ["answer"] * len(winning_ids),
                    "policy": policy,
                    "broadcast_policy_version": descriptor["broadcast_policy_version"],
                    "broadcast_generation": aggregate.get("generation", 1),
                    "transaction_id": transaction_id,
                },
            )
            if isinstance(admission, dict):
                admission["state"] = "broadcast_policy_satisfied"
                admission["terminal_state"] = (
                    ResolverState.BROADCAST_POLICY_SATISFIED.value
                )
                admission["terminal_evidence_id"] = str(member_event["sequence"])

    def _complete_pending_broadcast_close_locked(
        self,
        ledger: dict,
        admission: dict,
        key: ObligationKey,
    ) -> None:
        """Close a nonwinning member after its already-reserved call is classified."""
        pending = admission.get("broadcast_policy_close_pending")
        if not isinstance(pending, dict) or admission.get("state") != "open":
            return
        bid = pending.get("broadcast_id")
        winning_ids = pending.get("winning_ids")
        aggregate = ledger["broadcasts"].get(bid) if isinstance(bid, str) else None
        descriptor = aggregate.get("descriptor") if isinstance(aggregate, dict) else None
        pending_generation = pending.get("broadcast_generation")
        transaction_id = pending.get("transaction_id")
        descriptor_digest = (
            aggregate.get("descriptor_digest")
            if isinstance(aggregate, dict)
            else None
        )
        expected_transaction_id = (
            hashlib.sha256(_canonical({
                "broadcast_descriptor_digest": descriptor_digest,
                "winning_ids": winning_ids,
            }).encode("utf-8")).hexdigest()
            if isinstance(winning_ids, list)
            else None
        )
        affected = (
            aggregate.get("affected_member_keys")
            if isinstance(aggregate, dict)
            else None
        )
        inbound_row = ledger["messages"].get(key.inbound_id)
        exact_affected = [
            row for row in affected
            if isinstance(row, dict)
            and row.get("member") == key.responder
            and row.get("inbound_id") == key.inbound_id
            and row.get("key_digest") == key.digest
            and row.get("outcome") == "reservation_won"
        ] if isinstance(affected, list) else []
        proof_valid = bool(
            isinstance(bid, str)
            and isinstance(winning_ids, list)
            and winning_ids
            and all(isinstance(message_id, str) for message_id in winning_ids)
            and isinstance(pending_generation, int)
            and isinstance(transaction_id, str)
            and transaction_id
            and isinstance(aggregate, dict)
            and aggregate.get("state") == "policy_satisfied"
            and aggregate.get("generation") == pending_generation
            and aggregate.get("transaction_id") == transaction_id
            and transaction_id == expected_transaction_id
            and aggregate.get("winning_ids") == winning_ids
            and aggregate.get("winning_classes") == pending.get("winning_classes")
            and isinstance(descriptor, dict)
            and descriptor_digest == _broadcast_descriptor_digest(descriptor)
            and descriptor.get("broadcast_id") == bid
            and descriptor.get("requester") == key.requester
            and isinstance(descriptor.get("membership_snapshot"), list)
            and key.responder in descriptor["membership_snapshot"]
            and descriptor.get("response_policy") == pending.get("policy")
            and key.correlation_id == bid
            and admission.get("broadcast_id") == bid
            and admission.get("broadcast_generation") == pending_generation
            and ledger["inbound_index"].get(key.inbound_id) == key.digest
            and ledger["obligations"].get(key.digest) is admission
            and isinstance(inbound_row, dict)
            and inbound_row.get("sender") == key.requester
            and inbound_row.get("recipient") == key.responder
            and inbound_row.get("correlation_id") == bid
            and inbound_row.get("request_id") == bid
            and inbound_row.get("broadcast_id") == bid
            and inbound_row.get("broadcast_generation") == pending_generation
            and len(exact_affected) == 1
        )
        if not proof_valid:
            admission["state"] = "blocked"
            admission["blocked_reason"] = (
                "pending broadcast close does not match the policy-satisfied aggregate"
            )
            self._append(
                ledger,
                "OBLIGATION_BLOCKED",
                scope=key.inbound_id,
                key_digest=key.digest,
                data={"reason": admission["blocked_reason"]},
            )
            admission.pop("broadcast_policy_close_pending", None)
            return
        member_event = self._append(
            ledger,
            "BROADCAST_POLICY_SATISFIED",
            scope=key.inbound_id,
            source_id=winning_ids[-1],
            key_digest=key.digest,
            data={
                "aggregate": False,
                "broadcast_id": bid,
                "inbound_id": key.inbound_id,
                "winning_ids": list(winning_ids),
                "winning_classes": list(pending.get("winning_classes") or []),
                "policy": pending.get("policy"),
                "broadcast_policy_version": 1,
                "broadcast_generation": pending.get("broadcast_generation"),
                "transaction_id": transaction_id,
                "reservation_won": True,
            },
        )
        admission["state"] = "broadcast_policy_satisfied"
        admission["terminal_state"] = ResolverState.BROADCAST_POLICY_SATISFIED.value
        admission["terminal_evidence_id"] = str(member_event["sequence"])
        admission.pop("broadcast_policy_close_pending", None)

        aggregate_sequence = int(aggregate.get("aggregate_sequence", 0))
        already_late = {
            event.get("source_id")
            for event in ledger["transitions"]
            if event.get("transition") == "LATE_RESPONSE"
            and event.get("key_digest") == key.digest
        }
        for message_id, row in ledger["messages"].items():
            if not isinstance(row, dict) or any((
                int(row.get("sequence", 0)) <= aggregate_sequence,
                row.get("in_reply_to") != key.inbound_id,
                row.get("correlation_id") != bid,
                row.get("sender") != key.responder,
                row.get("recipient") != key.requester,
                row.get("kind") in CONTROL_KINDS,
                message_id in winning_ids,
                message_id in already_late,
            )):
                continue
            self._append(
                ledger,
                "LATE_RESPONSE",
                scope=key.inbound_id,
                source_id=message_id,
                key_digest=key.digest,
                data={
                    "broadcast_id": bid,
                    "closed_state": "broadcast_policy_satisfied",
                    "transaction_id": transaction_id,
                },
            )

    def _reconcile_landed_pending_broadcast_close(
        self,
        key: ObligationKey,
    ) -> bool:
        """Make a landed reserved response informational before replay can classify it."""
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            ledger = self._load()
            admission = ledger["obligations"].get(key.digest)
            if (
                not isinstance(admission, dict)
                or not isinstance(admission.get("broadcast_policy_close_pending"), dict)
            ):
                return False
            aggregate = ledger["broadcasts"].get(admission.get("broadcast_id"))
            if not isinstance(aggregate, dict):
                return False
            landed = any(
                candidate[3] == key.inbound_id
                for candidate in self._broadcast_qualifying_answers_locked(
                    ledger,
                    aggregate,
                )
            )
            if not landed:
                return False
            self._complete_pending_broadcast_close_locked(ledger, admission, key)
            self._write(ledger)
            return True

    @staticmethod
    def _delivery_transaction_intact(
        ledger: dict,
        admission: dict,
        key: ObligationKey,
        record: dict,
    ) -> bool:
        failure_sequence = admission.get("delivery_failure_sequence")
        incident_sequence = admission.get("delivery_failure_incident_sequence")
        reference = admission.get("dead_letter_reference")
        if (
            not isinstance(failure_sequence, int)
            or not isinstance(incident_sequence, int)
            or not isinstance(reference, dict)
            or set(reference) != {
                "kind",
                "immutable_inbound_id",
                "admission_key_digest",
                "operation_nonces",
                "incident_sequence",
                "delivery_failure_sequence",
            }
            or reference.get("kind") != "dead_letter_reference"
            or reference.get("immutable_inbound_id") != key.inbound_id
            or reference.get("admission_key_digest") != key.digest
            or reference.get("operation_nonces")
            != sorted(admission.get("reservations", {}))
            or reference.get("incident_sequence") != incident_sequence
            or reference.get("delivery_failure_sequence") != failure_sequence
        ):
            return False
        failure_events = [
            event
            for event in ledger.get("transitions", [])
            if isinstance(event, dict)
            and event.get("sequence") == failure_sequence
            and event.get("transition") == "DELIVERY_FAILED"
            and event.get("key_digest") == key.digest
        ]
        incident_events = [
            event
            for event in ledger.get("transitions", [])
            if isinstance(event, dict)
            and event.get("sequence") == incident_sequence
            and event.get("transition") in {
                "COMPLIANCE_INCIDENT",
                "INFRASTRUCTURE_INCIDENT",
            }
            and event.get("key_digest") == key.digest
        ]
        indexed = [
            row
            for row in ledger.get("delivery_index", {}).get(key.correlation_id, [])
            if isinstance(row, dict)
            and row.get("sequence") == failure_sequence
            and row.get("key_digest") == key.digest
            and row.get("inbound_id") == key.inbound_id
            and row.get("requester") == key.requester
            and row.get("responder") == key.responder
            and row.get("state") == "delivery_failed"
            and row.get("incident_sequence") == incident_sequence
            and row.get("dead_letter_reference_key_digest") == key.digest
        ]
        disposition = ledger.get("cursor_dispositions", {}).get(key.responder)
        return (
            len(failure_events) == 1
            and len(incident_events) == 1
            and len(indexed) == 1
            and isinstance(disposition, dict)
            and disposition.get("inbound_id") == record.get("id")
            and disposition.get("mode") == record.get("mode", "global")
            and disposition.get("state") == "delivery_failed"
        )

    def delivery_failed(
        self,
        record: dict,
        key: ObligationKey,
        *,
        reason: str,
        expected_revision: int,
    ) -> Resolution:
        """Atomically index requester release and dispose the responder assignment."""
        if record.get("id") != key.inbound_id:
            raise GateError("delivery failure does not match the exact inbound")
        liaison = self.store.load_config().get("operator_facing")
        terminal: Resolution | None = None
        blocked: Resolution | None = None
        breaker_tripped = False
        scoped_revision = expected_revision
        canonical_reason = reason
        failure_sequence: int | None = None
        with ExitStack() as locks:
            locks.enter_context(self.store._message_publication_lock())
            messages, _ = self._validated_messages()
            locks.enter_context(
                self.store._exclusive_lock(
                    self.path.with_suffix(".lock"), timeout=10.0,
                )
            )
            ledger = self._load()
            if ledger["inbound_index"].get(key.inbound_id) != key.digest:
                raise GateError("delivery failure admission index is stale")
            admission = ledger["obligations"].get(key.digest)
            if not isinstance(admission, dict):
                raise GateError("delivery admission missing")
            if self._key_from(admission.get("key")) != key:
                raise GateError("delivery failure key does not match persisted admission")
            replayed = self._resolve_replay(
                record,
                messages,
                ledger,
                admission=admission,
            )
            if replayed.terminal and replayed.state != ResolverState.DELIVERY_EXHAUSTED:
                terminal = replayed
            elif replayed.state in {
                ResolverState.BLOCKED,
                ResolverState.BLOCKED_POLICY,
                ResolverState.BLOCKED_COMPLIANCE,
            }:
                blocked = replayed
            elif admission.get("state") == "delivery_failed":
                canonical_reason = str(admission.get("delivery_failure_reason") or reason)
                if not self._delivery_transaction_intact(
                    ledger,
                    admission,
                    key,
                    record,
                ):
                    torn_reason = "failed-delivery transaction is structurally torn"
                    admission["state"] = "blocked"
                    admission["blocked_reason"] = torn_reason
                    self._append(
                        ledger,
                        "OBLIGATION_BLOCKED",
                        scope=key.inbound_id,
                        key_digest=key.digest,
                        data={"reason": torn_reason},
                    )
                    self._write(ledger)
                    blocked = Resolution(ResolverState.BLOCKED, torn_reason, key)
            else:
                if admission.get("state") != "open":
                    raise GateError("delivery admission is no longer open")
                if admission.get("fence") != self.fence or not self._live_dispatch_fence_owned():
                    raise GateError("delivery failure wrapper fence changed")
                if int(ledger["scoped_revisions"].get(key.inbound_id, 0)) != (
                    expected_revision
                ):
                    raise StaleRevision("delivery failure scoped revision changed")
                if not self._dispatch_head_is_current(admission, key):
                    raise StaleRevision("delivery failure no longer owns the exact cursor head")
                admission["state"] = "delivery_failed"
                admission["delivery_failure_reason"] = canonical_reason
                failure_class = (
                    "compliance"
                    if admission.get("last_exhaustion_class") == "compliance"
                    else "infrastructure"
                )
                incident = self._append(
                    ledger,
                    (
                        "COMPLIANCE_INCIDENT"
                        if failure_class == "compliance"
                        else "INFRASTRUCTURE_INCIDENT"
                    ),
                    scope=key.inbound_id,
                    key_digest=key.digest,
                    data={
                        "reason": canonical_reason,
                        "failure_class": failure_class,
                        "requester": key.requester,
                        "responder": key.responder,
                        "liaison": liaison,
                    },
                )
                event = self._append(
                    ledger,
                    "DELIVERY_FAILED",
                    scope=key.inbound_id,
                    key_digest=key.digest,
                    data={
                        "reason": canonical_reason,
                        "requester": key.requester,
                        "responder": key.responder,
                        "correlation_id": key.correlation_id,
                        "incident_sequence": incident["sequence"],
                    },
                )
                dead_letter_reference = {
                    "kind": "dead_letter_reference",
                    "immutable_inbound_id": key.inbound_id,
                    "admission_key_digest": key.digest,
                    "operation_nonces": sorted(admission.get("reservations", {})),
                    "incident_sequence": incident["sequence"],
                    "delivery_failure_sequence": event["sequence"],
                }
                admission["delivery_failure_sequence"] = event["sequence"]
                admission["delivery_failure_incident_sequence"] = incident["sequence"]
                admission["dead_letter_reference"] = dead_letter_reference
                admission["terminal_state"] = ResolverState.DELIVERY_EXHAUSTED.value
                admission["terminal_evidence_id"] = str(event["sequence"])
                admission["exhausted"] = True
                admission["exhausted_at"] = self.now()
                ledger["delivery_index"].setdefault(key.correlation_id, []).append({
                    "kind": "DELIVERY_FAILED",
                    "sequence": event["sequence"],
                    "key_digest": key.digest,
                    "inbound_id": key.inbound_id,
                    "question_generation": key.question_generation,
                    "delivery_generation": key.delivery_generation,
                    "requester": key.requester,
                    "responder": key.responder,
                    "state": "delivery_failed",
                    "reason": canonical_reason,
                    "incident_sequence": incident["sequence"],
                    "dead_letter_reference_key_digest": key.digest,
                    "broadcast_id": admission.get("broadcast_id"),
                    "membership_snapshot": admission.get("membership_snapshot"),
                    "response_policy": admission.get("response_policy"),
                    "response_quorum": admission.get("response_quorum"),
                    "broadcast_policy_version": admission.get(
                        "broadcast_policy_version"
                    ),
                })
                ledger["cursor_dispositions"][self.agent] = {
                    "inbound_id": record.get("id"),
                    "mode": record.get("mode", "global"),
                    "state": "delivery_failed",
                    "at": self.now(),
                }
                self._apply_delivery_exhaustion(ledger, key, admission, event)
                self._write(ledger)
            breaker = ledger["breakers"].get(self.agent, {})
            breaker_tripped = isinstance(breaker, dict) and breaker.get("tripped") is True
            scoped_revision = int(ledger["scoped_revisions"].get(key.inbound_id, 0))
            raw_failure_sequence = admission.get("delivery_failure_sequence")
            failure_sequence = (
                raw_failure_sequence if isinstance(raw_failure_sequence, int) else None
            )
        if terminal is not None:
            return self.finalize(
                record,
                terminal,
                expected_revision=terminal.scoped_revision,
            )
        if blocked is not None:
            return blocked
        if breaker_tripped:
            self._project_compliance_breaker_hold()
            self._reconcile_compliance_breaker_alert()
        return self.finalize(
            record,
            Resolution(
                ResolverState.DELIVERY_EXHAUSTED,
                canonical_reason,
                key,
                evidence_id=(
                    str(failure_sequence) if failure_sequence is not None else None
                ),
                scoped_revision=scoped_revision,
            ),
            expected_revision=scoped_revision,
        )

    def _apply_delivery_exhaustion(
        self,
        ledger: dict,
        key: ObligationKey,
        admission: dict,
        event: dict,
    ) -> bool:
        breaker = self._breaker_state(ledger)
        if admission.get("last_exhaustion_class") != "compliance":
            breaker["proof_infra_exhaustions_consecutive"] = int(
                breaker.get("proof_infra_exhaustions_consecutive", 0),
            ) + 1
            self._append(
                ledger,
                "PROOF_INFRA_EXHAUSTION_RECORDED",
                key_digest=key.digest,
                data={
                    "count": breaker["proof_infra_exhaustions_consecutive"],
                    "delivery_sequence": event.get("sequence"),
                },
            )
            return False
        breaker["owed_action_cap_exhaustions_consecutive"] = int(
            breaker.get("owed_action_cap_exhaustions_consecutive", 0)) + 1
        references = breaker.setdefault("compliance_exhaustion_references", [])
        if not isinstance(references, list):
            raise LedgerUnreadable("compliance exhaustion references are invalid")
        references.append({
            "key_digest": key.digest,
            "delivery_sequence": event.get("sequence"),
        })
        if breaker["owed_action_cap_exhaustions_consecutive"] < COMPLIANCE_BREAKER_TRIP:
            return False
        newly_tripped = False
        if breaker.get("tripped") is not True:
            breaker["generation"] = int(breaker.get("generation", 0)) + 1
            breaker["tripped"] = True
            breaker["config_blocked"] = True
            breaker["config_blocked_reason"] = "owed_action_compliance_breaker"
            newly_tripped = True
            breaker["tripped_at"] = self.now()
            generation = int(breaker["generation"])
            alert_nonce = uuid.uuid4().hex
            alert_request_id = f"esc-owed-breaker-{self.agent}-{generation}"
            alert_body = (
                f"Owed-action compliance breaker tripped for {self.agent} "
                f"at generation {generation}. Paid dispatch is halted. "
                f"Exhaustion references: {_canonical(references[-COMPLIANCE_BREAKER_TRIP:])}"
            )
            breaker.setdefault("alerts", {})[str(generation)] = {
                "state": "pending",
                "nonce": alert_nonce,
                "request_id": alert_request_id,
                "body": alert_body,
                "exhaustion_references": list(references[-COMPLIANCE_BREAKER_TRIP:]),
                "queued_at": self.now(),
            }
            self._append(
                ledger,
                "COMPLIANCE_BREAKER_TRIPPED",
                key_digest=key.digest,
                data={
                    "agent": self.agent,
                    "generation": breaker["generation"],
                    "exhaustion_references": list(references[-COMPLIANCE_BREAKER_TRIP:]),
                },
            )
            self._append(
                ledger,
                "COMPLIANCE_BREAKER_ALERT_QUEUED",
                key_digest=key.digest,
                data={
                    "agent": self.agent,
                    "generation": generation,
                    "nonce": alert_nonce,
                },
            )
        return newly_tripped

    @staticmethod
    def _supported_requester_reask(
        message: Message,
        *,
        request_id: str,
        requester: str,
    ) -> bool:
        if message.kind != "question" or message.sender != requester:
            return False
        meta = message.meta if isinstance(message.meta, dict) else {}
        if _true(meta, "consult") is not False:
            return False
        broadcast_id = meta.get("broadcast_id")
        if broadcast_id is None:
            return meta.get("request_id") == request_id
        members = meta.get("membership_snapshot")
        policy = meta.get("response_policy")
        quorum = meta.get("response_quorum")
        return bool(
            broadcast_id == request_id
            and isinstance(members, list)
            and message.recipient in members
            and policy in {"each", "any", "quorum"}
            and meta.get("broadcast_policy_version") == 1
            and (
                policy != "quorum"
                or isinstance(quorum, int)
                and not isinstance(quorum, bool)
                and 1 <= quorum <= len(members)
            )
        )

    def delivery_status(self, request_id: str, requester: str) -> dict | None:
        try:
            ledger = self._load(create=False)
        except (LedgerUnreadable, OSError, ValueError, TimeoutError, RuntimeError):
            return None
        rows = ledger["delivery_index"].get(request_id, [])
        failures = [
            row for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict) and row.get("requester") == requester
        ]
        if not failures:
            return None
        aggregate_state = ledger["broadcasts"].get(request_id)
        current_broadcast_generation = (
            int(aggregate_state.get("generation", 1))
            if isinstance(aggregate_state, dict)
            else None
        )
        policy_terminals = [
            row for row in failures
            if row.get("state") == ResolverState.BROADCAST_POLICY_SATISFIED.value
            and row.get("aggregate") is True
            and (
                current_broadcast_generation is None
                or int(row.get("broadcast_generation", 1))
                == current_broadcast_generation
            )
        ]
        if policy_terminals:
            return dict(policy_terminals[-1])
        try:
            messages = self.store.publication_ordered_messages()
        except (OSError, ValueError, TimeoutError, RuntimeError):
            return None
        canonical_order = {
            message.id: index for index, message in enumerate(messages)
        }
        current_failures: list[dict] = []
        for row in failures:
            failed_order = canonical_order.get(row.get("inbound_id"), -1)
            superseded = any(
                self._supported_requester_reask(
                    message,
                    request_id=request_id,
                    requester=requester,
                )
                and message.recipient == row.get("responder")
                and canonical_order[message.id] > failed_order
                for message in messages
            )
            if not superseded:
                current_failures.append(row)
        failures = current_failures
        if not failures:
            return None
        admissions: list[tuple[ObligationKey, dict]] = []
        for admission in ledger["obligations"].values():
            if not isinstance(admission, dict):
                continue
            try:
                key = self._key_from(admission.get("key"))
            except (TypeError, ValueError):
                continue
            if key.correlation_id == request_id and key.requester == requester:
                admissions.append((key, admission))
        broadcast = [
            (key, admission) for key, admission in admissions
            if admission.get("broadcast_id") == request_id
            and (
                current_broadcast_generation is None
                or int(admission.get("broadcast_generation") or 1)
                == current_broadcast_generation
            )
        ]
        if broadcast and isinstance(aggregate_state, dict):
            descriptor = aggregate_state.get("descriptor")
            if not isinstance(descriptor, dict) or descriptor.get("requester") != requester:
                return None
            policy = descriptor.get("response_policy")
            members = descriptor.get("membership_snapshot")
            threshold = (
                1
                if policy == "any"
                else descriptor.get("response_quorum")
                if policy == "quorum"
                else len(members)
                if policy == "each" and isinstance(members, list)
                else None
            )
            if isinstance(threshold, int) and threshold > 0:
                qualifying = self._broadcast_qualifying_answers_locked(
                    ledger,
                    aggregate_state,
                )
                satisfied_members = {candidate[2] for candidate in qualifying}
                possible_members: set[str] = set()
                if isinstance(members, list):
                    by_responder: dict[str, list[dict]] = {}
                    for key, admission in broadcast:
                        by_responder.setdefault(key.responder, []).append(admission)
                    for member in members:
                        if member in satisfied_members:
                            continue
                        member_rows = by_responder.get(member, [])
                        if not member_rows or any(
                            row.get("state") == "open" for row in member_rows
                        ):
                            possible_members.add(member)
                # DELIVERY_FAILED is not a qualifying default answer.  The aggregate
                # waiter stays live while the immutable policy can still be met.
                if (
                    len(satisfied_members) >= threshold
                    or len(satisfied_members | possible_members) >= threshold
                ):
                    return None
                result = dict(failures[-1])
                result.update({
                    "aggregate": True,
                    "response_policy": policy,
                    "response_quorum": descriptor.get("response_quorum"),
                })
                return result
        if admissions:
            latest_key, latest = max(
                admissions,
                key=lambda item: (
                    item[0].question_generation,
                    item[0].delivery_generation,
                ),
            )
            terminal_state = latest.get("state")
            if terminal_state not in {"delivery_failed", "operator_resolved"}:
                return None
            for row in reversed(failures):
                if (
                    row.get("key_digest") == latest_key.digest
                    and row.get("state") == terminal_state
                ):
                    return dict(row)
            return None
        return dict(failures[-1])

    def broadcast_status(self, request_id: str, requester: str) -> str | None:
        """Return the normative v1 aggregate state for requester wait projection."""
        try:
            ledger = self._load(create=False)
        except (LedgerUnreadable, OSError, ValueError, TimeoutError, RuntimeError):
            return None
        aggregate = ledger["broadcasts"].get(request_id)
        if not isinstance(aggregate, dict):
            return None
        descriptor = aggregate.get("descriptor")
        if not isinstance(descriptor, dict):
            return None
        return (
            str(aggregate.get("state"))
            if descriptor.get("requester") == requester
            else None
        )

    def schedule_continuation(self, key: ObligationKey, *, producer_token: str) -> str:
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            ledger = self._load()
            admission = ledger["obligations"].get(key.digest)
            if not isinstance(admission, dict) or admission.get("state") != "open":
                raise GateError("continuation admission missing")
            if int(admission.get("paid_dispatches_total", 0)) != 1:
                raise DispatchRefused("continuation requires exactly one paid dispatch")
            if admission.get("recovery_used") or admission.get("continuation_used"):
                raise DispatchRefused("extra dispatch budget already assigned")
            existing = admission.get("durable_continuation")
            if isinstance(existing, dict) and existing.get("state") == "scheduled":
                if admission.get("producer_token") != producer_token:
                    raise DispatchRefused("continuation is already owned by another producer")
                return str(existing["operation_nonce"])
            operation_nonce = uuid.uuid4().hex
            admission["producer_token"] = producer_token
            admission["first_deferred_at"] = admission.get("first_deferred_at") or self.now()
            admission["durable_continuation"] = {
                "operation_nonce": operation_nonce,
                "state": "scheduled",
            }
            self._append(
                ledger,
                "CONTINUATION_SCHEDULED",
                scope=key.inbound_id,
                key_digest=key.digest,
                data={"operation_nonce": operation_nonce},
            )
            self._write(ledger)
        return operation_nonce

    def roster_snapshot(self) -> dict:
        return _roster_snapshot(self.store.load_config())

    def operator_resolve(
        self,
        record: dict,
        key: ObligationKey,
        *,
        actor: str,
        expected_roster_revision: str,
        reason: str,
    ) -> Resolution:
        if not reason.strip():
            raise ValueError("operator resolution requires an audit reason")
        if any((
            key.responder != self.agent,
            record.get("id") != key.inbound_id,
            record.get("from") != key.requester,
            record.get("to") != key.responder,
            record.get("correlation_id") != key.correlation_id,
        )):
            raise GateError(
                "operator resolution record does not match the exact inbound key"
            )
        resolution: Resolution
        with self.store._config_lock(timeout=10.0):
            roster = _roster_snapshot(self.store.load_config())
            if roster["revision"] != expected_roster_revision:
                raise StaleRevision("roster changed before operator resolution")
            if actor not in roster["authorized_liaisons"]:
                raise PermissionError("actor is not an event-time authorized liaison or lead")
            with ExitStack() as locks:
                locks.enter_context(self.store._message_publication_lock())
                messages, _ = self._validated_messages()
                canonical = self._record_message(record, messages)
                if canonical is None or any((
                    canonical.sender != key.requester,
                    canonical.recipient != key.responder,
                    _correlation(canonical.meta) != key.correlation_id,
                )):
                    raise GateError("operator resolution canonical inbound mismatch")
                locks.enter_context(
                    self.store._exclusive_lock(
                        self.path.with_suffix(".lock"), timeout=10.0
                    )
                )
                ledger = self._load()
                admission = ledger["obligations"].get(key.digest)
                if not isinstance(admission, dict) or admission.get("state") not in {
                    "open",
                    "delivery_failed",
                }:
                    raise GateError("operator resolution target is not recoverable")
                replayed = self._resolve_replay(
                    record,
                    messages,
                    ledger,
                    admission=admission,
                )
                if replayed.terminal and replayed.state != ResolverState.DELIVERY_EXHAUSTED:
                    resolution = replayed
                elif replayed.state in {
                    ResolverState.BLOCKED,
                    ResolverState.BLOCKED_POLICY,
                    ResolverState.BLOCKED_COMPLIANCE,
                    ResolverState.INDETERMINATE,
                }:
                    return replayed
                else:
                    admission["state"] = "operator_resolved"
                    admission["terminal_state"] = ResolverState.OPERATOR_RESOLVED.value
                    event = self._append(
                        ledger,
                        "OPERATOR_RESOLUTION",
                        scope=key.inbound_id,
                        source_id=actor,
                        key_digest=key.digest,
                        data={
                            "actor": actor,
                            "reason": reason.strip(),
                            "roster_revision": expected_roster_revision,
                        },
                    )
                    ledger["delivery_index"].setdefault(key.correlation_id, []).append({
                        "kind": "OPERATOR_RESOLUTION",
                        "sequence": event["sequence"],
                        "key_digest": key.digest,
                        "inbound_id": key.inbound_id,
                        "question_generation": key.question_generation,
                        "delivery_generation": key.delivery_generation,
                        "requester": key.requester,
                        "responder": key.responder,
                        "state": "operator_resolved",
                        "reason": reason.strip(),
                        "actor": actor,
                        "roster_revision": expected_roster_revision,
                        "broadcast_id": admission.get("broadcast_id"),
                    })
                    ledger["cursor_dispositions"][self.agent] = {
                        "inbound_id": record.get("id"),
                        "mode": record.get("mode", "global"),
                        "state": ResolverState.OPERATOR_RESOLVED.value,
                        "at": self.now(),
                    }
                    self._write(ledger)
                    scoped_revision = int(
                        ledger["scoped_revisions"].get(key.inbound_id, 0)
                    )
                    resolution = Resolution(
                        ResolverState.OPERATOR_RESOLVED,
                        reason.strip(),
                        key,
                        evidence_id=str(event["sequence"]),
                        scoped_revision=scoped_revision,
                        ledger_revision=scoped_revision,
                    )
        return self.finalize(
            record,
            resolution,
            expected_revision=resolution.scoped_revision,
        )

    def transfer(
        self,
        record: dict,
        key: ObligationKey,
        *,
        destination: str,
        new_inbound_id: str,
        destination_policy: PolicySnapshot,
        actor: str,
        expected_roster_revision: str,
        expected_revision: int,
    ) -> Resolution:
        """Close the source and create one fenced destination generation atomically."""
        if any((
            key.responder != self.agent,
            record.get("id") != key.inbound_id,
            record.get("from") != key.requester,
            record.get("to") != key.responder,
            record.get("correlation_id") != key.correlation_id,
        )):
            raise GateError("transfer record does not match the exact inbound key")
        resolution: Resolution
        with self.store._config_lock(timeout=10.0):
            cfg = self.store.load_config()
            roster = _roster_snapshot(cfg)
            if roster["revision"] != expected_roster_revision:
                raise StaleRevision("roster changed before transfer")
            if actor not in roster["authorized_liaisons"]:
                raise PermissionError("transfer actor is not an authorized liaison or lead")
            if destination not in (cfg.get("agents") or []):
                raise ValueError("transfer destination is not active")
            current_policy = self._current_policy()
            destination_policy_block_state = (
                destination_policy.status
                if destination_policy.status in {
                    ResolverState.BLOCKED,
                    ResolverState.BLOCKED_POLICY,
                    ResolverState.BLOCKED_COMPLIANCE,
                }
                else None
            )
            destination_policy_block_reason = (
                destination_policy.reason
                or "transfer destination policy is unreadable"
            )
            if destination_policy_block_state is None and (
                current_policy.status != ResolverState.ACTIVE
                or current_policy.generation != destination_policy.generation
                or destination_policy.status != ResolverState.ACTIVE
                or destination_policy.agent != destination
                or destination_policy.grade != DETECTION_GRADE
                or not _valid_hex_digest(destination_policy.generation)
            ):
                raise GateError(
                    "transfer destination policy is not detection-grade active"
                )
            if (
                destination_policy_block_state is not None
                and destination_policy.agent not in {None, destination}
            ):
                raise GateError("transfer destination policy is bound to another agent")
            with ExitStack() as locks:
                locks.enter_context(self.store._message_publication_lock())
                messages, projected = self._validated_messages()
                canonical_source = self._record_message(record, messages)
                source_meta = (
                    canonical_source.meta
                    if canonical_source is not None
                    and isinstance(canonical_source.meta, dict)
                    else {}
                )
                if canonical_source is None or any((
                    canonical_source.id != record.get("id"),
                    canonical_source.ts != record.get("ts"),
                    canonical_source.sender != record.get("from"),
                    canonical_source.recipient != record.get("to"),
                    canonical_source.kind != record.get("kind"),
                    canonical_source.subject != record.get("subject"),
                    canonical_source.body != record.get("body"),
                    source_meta != record.get("meta"),
                    _correlation(source_meta) != key.correlation_id,
                )):
                    raise GateError("transfer canonical source record mismatch")
                projected_source = projected["obligations"].get(key.digest)
                if (
                    isinstance(projected_source, dict)
                    and self._key_from(projected_source.get("key")) == key
                ):
                    published_source = self._resolve_replay(
                        record,
                        messages,
                        projected,
                        admission=projected_source,
                    )
                    if (
                        published_source.terminal
                        and published_source.state
                        != ResolverState.DELIVERY_EXHAUSTED
                    ):
                        return self.finalize(
                            record,
                            published_source,
                            expected_revision=published_source.scoped_revision,
                        )
                next_inbound = next(
                    (message for message in messages if message.id == new_inbound_id),
                    None,
                )
                meta = (
                    next_inbound.meta
                    if next_inbound is not None and isinstance(next_inbound.meta, dict)
                    else {}
                )
                if (
                    next_inbound is None
                    or next_inbound.kind != "question"
                    or next_inbound.sender != key.requester
                    or next_inbound.recipient != destination
                    or _correlation(meta) != key.correlation_id
                    or _true(meta, "consult") is not False
                    or meta.get("transfer_from_key_digest") != key.digest
                    or meta.get("transfer_policy_generation")
                    != destination_policy.generation
                    or (
                        _true(meta, "escalation_required") is True
                        if key.obligation_class == "answer"
                        else _true(meta, "escalation_required") is not True
                    )
                ):
                    raise ValueError(
                        "transfer destination inbound is missing or semantically unadmittable"
                    )
                source_descriptor = _broadcast_descriptor(
                    source_meta,
                    requester=canonical_source.sender,
                )
                target_descriptor = _broadcast_descriptor(
                    meta,
                    requester=next_inbound.sender,
                )
                if (
                    (source_descriptor is None) != (target_descriptor is None)
                    or source_descriptor is not None
                    and source_descriptor != target_descriptor
                    or target_descriptor is not None
                    and destination not in target_descriptor["membership_snapshot"]
                ):
                    raise ValueError("transfer destination changed frozen broadcast policy")
                locks.enter_context(
                    self.store._exclusive_lock(
                        self.path.with_suffix(".lock"), timeout=10.0
                    )
                )
                ledger = self._load()
                old = ledger["obligations"].get(key.digest)
                if not isinstance(old, dict) or self._key_from(old.get("key")) != key:
                    raise GateError("transfer source admission is missing or mismatched")
                if old.get("state") not in {"open", "delivery_failed"}:
                    raise GateError("transfer source is not recoverable")
                if int(ledger["scoped_revisions"].get(key.inbound_id, 0)) != (
                    expected_revision
                ):
                    raise StaleRevision("transfer source scoped revision changed")
                if old.get("state") == "open" and (
                    old.get("fence") != self.fence
                    or not self._live_dispatch_fence_owned()
                    or not self._dispatch_head_is_current(old, key)
                ):
                    raise GateError("transfer source wrapper no longer owns the exact head")
                old.pop("transfer_blocked_state", None)
                old.pop("transfer_blocked_reason", None)
                source_replay = self._resolve_replay(
                    record,
                    messages,
                    ledger,
                    admission=old,
                )
                if (
                    source_replay.terminal
                    and source_replay.state != ResolverState.DELIVERY_EXHAUSTED
                ):
                    return source_replay
                if destination_policy_block_state is not None:
                    old["transfer_blocked_state"] = destination_policy_block_state.value
                    old["transfer_blocked_reason"] = destination_policy_block_reason
                    self._append(
                        ledger,
                        "TRANSFER_DESTINATION_BLOCKED",
                        scope=key.inbound_id,
                        key_digest=key.digest,
                        data={
                            "state": destination_policy_block_state.value,
                            "reason": destination_policy_block_reason,
                        },
                    )
                    self._write(ledger)
                    return Resolution(
                        destination_policy_block_state,
                        destination_policy_block_reason,
                        key,
                        scoped_revision=int(
                            ledger["scoped_revisions"].get(key.inbound_id, 0)
                        ),
                    )
                if target_descriptor is not None:
                    aggregate = ledger["broadcasts"].get(
                        target_descriptor["broadcast_id"]
                    )
                    if (
                        isinstance(aggregate, dict)
                        and aggregate.get("state") == "blocked"
                    ):
                        reason = "transfer destination broadcast aggregate is blocked"
                        old["transfer_blocked_state"] = ResolverState.BLOCKED.value
                        old["transfer_blocked_reason"] = reason
                        self._append(
                            ledger,
                            "TRANSFER_DESTINATION_BLOCKED",
                            scope=key.inbound_id,
                            key_digest=key.digest,
                            data={"state": ResolverState.BLOCKED.value, "reason": reason},
                        )
                        self._write(ledger)
                        scoped_revision = int(
                            ledger["scoped_revisions"].get(key.inbound_id, 0)
                        )
                        return Resolution(
                            ResolverState.BLOCKED,
                            reason,
                            key,
                            scoped_revision=scoped_revision,
                            ledger_revision=scoped_revision,
                        )
                destination_breaker = ledger["breakers"].get(destination, {})
                if isinstance(destination_breaker, dict) and (
                    destination_breaker.get("tripped") is True
                    or destination_breaker.get("config_blocked") is True
                ):
                    reason = "transfer destination compliance breaker is tripped"
                    old["transfer_blocked_state"] = (
                        ResolverState.BLOCKED_COMPLIANCE.value
                    )
                    old["transfer_blocked_reason"] = reason
                    self._append(
                        ledger,
                        "TRANSFER_DESTINATION_BLOCKED",
                        scope=key.inbound_id,
                        key_digest=key.digest,
                        data={
                            "state": ResolverState.BLOCKED_COMPLIANCE.value,
                            "reason": reason,
                        },
                    )
                    self._write(ledger)
                    scoped_revision = int(
                        ledger["scoped_revisions"].get(key.inbound_id, 0)
                    )
                    return Resolution(
                        ResolverState.BLOCKED_COMPLIANCE,
                        reason,
                        key,
                        scoped_revision=scoped_revision,
                        ledger_revision=scoped_revision,
                    )
                if source_replay.state in {
                    ResolverState.BLOCKED,
                    ResolverState.BLOCKED_POLICY,
                    ResolverState.BLOCKED_COMPLIANCE,
                    ResolverState.INDETERMINATE,
                }:
                    raise GateError(
                        f"transfer source replay is {source_replay.state.value}"
                    )
                if (
                    new_inbound_id in ledger["inbound_index"]
                    or new_inbound_id in ledger["no_admission_claims"]
                ):
                    raise StaleRevision("transfer destination was already claimed")
                indexed = ledger["messages"].get(new_inbound_id)
                if not isinstance(indexed, dict):
                    raise GateError("transfer destination is absent from canonical replay")
                target_record = {
                    "id": next_inbound.id,
                    "ts": next_inbound.ts,
                    "from": next_inbound.sender,
                    "to": next_inbound.recipient,
                    "kind": next_inbound.kind,
                    "subject": next_inbound.subject,
                    "body": next_inbound.body,
                    "meta": dict(meta),
                    "request_id": meta.get("request_id"),
                    "broadcast_id": meta.get("broadcast_id"),
                    "correlation_id": _correlation(meta),
                    "mode": "global",
                }
                destination_gate = DetectionCommitGate(
                    self.store,
                    destination,
                    destination_policy,
                    fence="transfer-pre-admission-validation",
                    now=self.now,
                )
                prospective = destination_gate._resolve_replay(
                    target_record,
                    messages,
                    ledger,
                    admission=None,
                )
                if prospective.state in TERMINAL_STATES:
                    raise GateError(
                        "transfer destination already terminal before admission"
                    )
                if prospective.state != ResolverState.OWED_UNSATISFIED:
                    raise GateError(
                        "transfer destination replay is not safely admissible"
                    )

                next_key = ObligationKey(
                    store_epoch=key.store_epoch,
                    inbound_id=new_inbound_id,
                    correlation_id=key.correlation_id,
                    requester=key.requester,
                    responder=destination,
                    question_generation=key.question_generation,
                    delivery_generation=key.delivery_generation + 1,
                    obligation_class=key.obligation_class,
                    reducer_version=key.reducer_version,
                    participant_capabilities_digest=key.participant_capabilities_digest,
                )
                next_admission = {
                    "key": next_key.to_dict(),
                    "correlation_id": key.correlation_id,
                    "obligation_class": key.obligation_class,
                    "state": "open",
                    "watermark_sequence": ledger["append_sequence"],
                    "scoped_revision": int(
                        ledger["scoped_revisions"].get(new_inbound_id, 0)
                    ),
                    "activation_generation": destination_policy.generation,
                    "readiness_generation": destination_policy.generation,
                    "fence": "unclaimed",
                    "owner_pid": None,
                    "delivery_mode": old.get("delivery_mode", "global"),
                    "scoped_request_id": old.get("scoped_request_id"),
                    "paid_dispatches_total": 0,
                    "paid_initial_dispatches_total": 0,
                    "paid_recoveries_total": 0,
                    "paid_continuations_total": 0,
                    "continuation_used": False,
                    "recovery_used": False,
                    "first_dispatch_classified": False,
                    "last_exhaustion_class": None,
                    "reservations": {},
                    "operation_infra_attempts": 0,
                    "operation_infra_first_at": None,
                    "operation_infra_retry_inflight": False,
                    "finalization_misses": 0,
                    "finalization_first_at": None,
                    "finalization_retry_inflight": False,
                    "cursor_projection_misses": 0,
                    "cursor_projection_first_at": None,
                    "cursor_projection_inflight": False,
                    "cursor_projection_reserved_at": None,
                    "owed_action_missing_seen": False,
                    "created_at": self.now(),
                    "broadcast_id": meta.get("broadcast_id"),
                    "membership_snapshot": meta.get("membership_snapshot"),
                    "response_policy": meta.get("response_policy"),
                    "response_quorum": meta.get("response_quorum"),
                    "broadcast_policy_version": meta.get("broadcast_policy_version"),
                    "broadcast_generation": indexed.get("broadcast_generation"),
                    "exhausted": False,
                    "exhausted_at": None,
                    "delivery_failure_reason": None,
                    "delivery_failure_sequence": None,
                    "delivery_failure_incident_sequence": None,
                    "dead_letter_reference": None,
                }
                transaction_id = uuid.uuid4().hex
                ledger["obligations"][next_key.digest] = next_admission
                ledger["inbound_index"][new_inbound_id] = next_key.digest
                self._append(
                    ledger,
                    "OBLIGATION_ADMITTED",
                    scope=new_inbound_id,
                    source_id=new_inbound_id,
                    key_digest=next_key.digest,
                    data={
                        "key": next_key.to_dict(),
                        "transfer_from": key.digest,
                        "transaction_id": transaction_id,
                        "destination_policy_generation": destination_policy.generation,
                    },
                )
                transfer_event = self._append(
                    ledger,
                    "TRANSFERRED",
                    scope=key.inbound_id,
                    source_id=actor,
                    key_digest=key.digest,
                    data={
                        "destination": destination,
                        "new_key": next_key.to_dict(),
                        "transaction_id": transaction_id,
                        "roster_revision": expected_roster_revision,
                    },
                )
                old["state"] = "transferred"
                old["terminal_state"] = ResolverState.TRANSFERRED.value
                old["terminal_evidence_id"] = str(transfer_event["sequence"])
                old["transferred_at"] = self.now()
                ledger["cursor_dispositions"][self.agent] = {
                    "inbound_id": record.get("id"),
                    "mode": record.get("mode", "global"),
                    "state": ResolverState.TRANSFERRED.value,
                    "at": self.now(),
                }
                self._write(ledger)
                scoped_revision = int(
                    ledger["scoped_revisions"].get(key.inbound_id, 0)
                )
                resolution = Resolution(
                    ResolverState.TRANSFERRED,
                    "transferred",
                    key,
                    str(transfer_event["sequence"]),
                    scoped_revision,
                )
        return self.finalize(record, resolution, expected_revision=scoped_revision)

    def close_broadcast_members(self, winning_key: ObligationKey,
                                remaining: list[ObligationKey], *, winning_ids: list[str]) -> None:
        del winning_key, remaining, winning_ids
        raise GateError("direct broadcast closure is forbidden; use canonical replay")

    def status(self) -> dict:
        policy = self._current_policy()
        result = {
            "agent": self.agent,
            "grade": policy.grade,
            "policy_generation": policy.generation,
            "status": (
                "ACTIVE (detection-grade)"
                if policy.status == ResolverState.ACTIVE
                else policy.status.value.upper()
            ),
            "reason": policy.reason,
            "security_grade": False,
        }
        if self.proof_health_path.exists():
            try:
                proof_health = json.loads(
                    self.proof_health_path.read_text(encoding="utf-8")
                )
                if not isinstance(proof_health, dict):
                    raise ValueError("proof health is not an object")
            except (OSError, ValueError, json.JSONDecodeError):
                proof_health = {"state": "blocked", "unreadable": True}
            result["proof_health"] = proof_health
            if (
                policy.status == ResolverState.ACTIVE
                and proof_health.get("state") == "blocked"
            ):
                result["status"] = ResolverState.BLOCKED.value.upper()
                result["reason"] = (
                    "canonical replay proof health is unreadable"
                    if proof_health.get("unreadable") is True
                    else "canonical replay proof is blocked pending a successful replay"
                )
        if not self.path.exists() and not self.epoch_anchor_path.exists():
            result["store_epoch"] = None
            result["append_sequence"] = 0
            result["breaker"] = {}
            result["open_obligations"] = 0
            result["legacy_broadcast"] = {
                "enforcement": "none",
                "unenforced_total": 0,
            }
            return result
        try:
            ledger = self._load(create=False)
        except LedgerUnreadable as exc:
            if policy.status == ResolverState.ACTIVE:
                result["status"] = ResolverState.BLOCKED.value.upper()
                result["reason"] = str(exc)
            result["ledger_error"] = str(exc)
            return result
        result["store_epoch"] = ledger["store_epoch"]
        result["append_sequence"] = ledger["append_sequence"]
        result["legacy_broadcast"] = {
            "enforcement": "none",
            "unenforced_total": int(
                ledger["telemetry"].get("legacy_broadcast_unenforced_total", 0)
            ),
        }
        breaker = ledger["breakers"].get(self.agent, {})
        result["breaker"] = breaker
        if (
            policy.status == ResolverState.ACTIVE
            and isinstance(breaker, dict)
            and breaker.get("tripped") is True
        ):
            result["status"] = ResolverState.BLOCKED_COMPLIANCE.value.upper()
            result["reason"] = "owed_action_compliance_breaker"
            self._project_compliance_breaker_hold()
            self._reconcile_compliance_breaker_alert()
        elif isinstance(breaker, dict) and breaker.get("config_blocked") is False:
            self._clear_compliance_breaker_hold()
        result["open_obligations"] = sum(
            1 for row in ledger["obligations"].values()
            if isinstance(row, dict) and row.get("state") == "open"
        )
        return result


def note_bus_message(store, message: Message) -> None:
    """Best-effort eager indexing; authoritative replay repairs missed hooks."""
    gate = DetectionCommitGate(
        store,
        message.recipient,
        PolicySnapshot.inactive("append service"),
        fence="append-service",
    )
    if not gate.path.exists():
        return
    # Re-read the immutable publication stream so a prior failed eager hook is
    # repaired before the newest message.  Indexing only ``message`` could invert
    # canonical winner order when the earlier projection was delayed or missed.
    gate._index_messages(
        store.publication_ordered_messages(),
        invalid_records=store.list_invalid_messages(),
    )


def note_manual_close(store, agent: str, request_id: str) -> None:
    """Canonicalize a no-message ACK/manual close for admitted generations."""
    gate = DetectionCommitGate(
        store, agent, PolicySnapshot.inactive("append service"), fence="append-service",
    )
    messages = store.publication_ordered_messages()
    inbounds = [
        message for message in messages
        if message.kind == "question"
        and message.recipient == agent
        and _correlation(message.meta) == request_id
    ]
    if not inbounds:
        return
    gate._index_messages(messages, invalid_records=store.list_invalid_messages())
    with store._exclusive_lock(gate.path.with_suffix(".lock"), timeout=10.0):
        ledger = gate._load()
        changed = False
        for inbound in inbounds:
            key_id = ledger["inbound_index"].get(inbound.id)
            admission = ledger["obligations"].get(key_id) if key_id else None
            if isinstance(admission, dict) and admission.get("state") != "open":
                continue
            duplicate = any(
                event.get("transition") == "MANUAL_CLOSE"
                and (
                    event.get("key_digest") == key_id
                    if key_id
                    else isinstance(event.get("data"), dict)
                    and event["data"].get("inbound_id") == inbound.id
                )
                for event in ledger["transitions"]
            )
            if duplicate:
                continue
            gate._append(
                ledger,
                "MANUAL_CLOSE",
                scope=inbound.id,
                source_id=inbound.id,
                key_digest=key_id,
                data={
                    "agent": agent,
                    "request_id": request_id,
                    "inbound_id": inbound.id,
                },
            )
            changed = True
        if changed:
            gate._write(ledger)


def requester_terminal_for(store, request_id: str, requester: str) -> dict | None:
    gate = DetectionCommitGate(
        store,
        requester,
        PolicySnapshot.inactive("delivery index read"),
        fence="read-only",
    )
    return gate.delivery_status(request_id, requester)


def requester_broadcast_policy_state(
    store,
    request_id: str,
    requester: str,
) -> str | None:
    gate = DetectionCommitGate(
        store,
        requester,
        PolicySnapshot.inactive("broadcast state read"),
        fence="read-only",
    )
    return gate.broadcast_status(request_id, requester)


def delivery_failed_for(store, request_id: str, requester: str) -> dict | None:
    terminal = requester_terminal_for(store, request_id, requester)
    if terminal is None or terminal.get("state") != "delivery_failed":
        return None
    return terminal
