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
from agenttalk.store import PROC_DEAD, Message, _process_liveness

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

    @classmethod
    def inactive(cls, reason: str = "no policy configured") -> "PolicySnapshot":
        return cls(ResolverState.NOT_OWED, "inactive", reason=reason)

    @classmethod
    def from_mapping(cls, raw: object, agent: str) -> "PolicySnapshot":
        if not isinstance(raw, dict) or raw.get("schema_version") != POLICY_SCHEMA_VERSION:
            return cls(ResolverState.BLOCKED_POLICY, "unreadable", reason="policy schema invalid")
        agents = raw.get("agents")
        if not isinstance(agents, dict):
            return cls(ResolverState.BLOCKED_POLICY, "unreadable", reason="policy agents invalid")
        canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        generation = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        entry = agents.get(agent)
        if entry is None:
            return cls(ResolverState.NOT_OWED, generation, reason="agent has no configured grade")
        if not isinstance(entry, dict) or not isinstance(entry.get("grade"), str):
            return cls(
                ResolverState.BLOCKED_POLICY,
                generation,
                reason="configured grade is invalid",
            )
        grade = entry.get("grade")
        if grade not in {DETECTION_GRADE, SECURITY_GRADE}:
            return cls(
                ResolverState.BLOCKED_POLICY,
                generation,
                reason="configured grade is unsupported",
            )
        enabled = entry.get("enabled", True)
        if enabled is not True and enabled is not False:
            return cls(
                ResolverState.BLOCKED_POLICY,
                generation,
                reason="configured enabled flag is invalid",
            )
        if enabled is False:
            return cls(
                ResolverState.INACTIVE,
                generation,
                grade,
                "operator disabled gate",
            )
        if grade == SECURITY_GRADE:
            return cls(
                ResolverState.BLOCKED,
                generation,
                SECURITY_GRADE,
                "security-grade prerequisites are not available in this build",
            )
        return cls(ResolverState.ACTIVE, generation, DETECTION_GRADE, "policy ready")

    @classmethod
    def from_path(cls, path: Path, agent: str) -> "PolicySnapshot":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return cls(
                ResolverState.BLOCKED_POLICY,
                "unreadable",
                reason=f"operator policy unreadable: {type(exc).__name__}",
            )
        return cls.from_mapping(raw, agent)

    @classmethod
    def from_environment(cls, agent: str) -> "PolicySnapshot":
        configured = os.environ.get(POLICY_ENV)
        if not configured:
            return cls.inactive()
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
        "cursor_dispositions": {},
        "telemetry": {},
    }


def _validate_ledger(raw: object) -> dict:
    if not isinstance(raw, dict) or raw.get("schema_version") != LEDGER_SCHEMA_VERSION:
        raise LedgerUnreadable("obligation ledger schema invalid")
    raw.setdefault("dispatch_nonces", {})
    required = (
        "store_epoch", "append_sequence", "revision", "messages", "transitions",
        "scoped_revisions", "obligations", "inbound_index", "breakers",
        "no_admission_claims", "dispatch_nonces", "delivery_index", "telemetry",
        "cursor_dispositions",
    )
    if not isinstance(raw.get("store_epoch"), str):
        raise LedgerUnreadable("obligation ledger epoch invalid")
    if not all(name in raw for name in required):
        raise LedgerUnreadable("obligation ledger fields missing")
    if not all(isinstance(raw.get(name), dict) for name in (
        "messages", "scoped_revisions", "obligations", "inbound_index",
        "no_admission_claims", "dispatch_nonces", "breakers", "delivery_index", "telemetry",
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
                ledger["messages"][message.id] = {
                    "sequence": event["sequence"],
                    "correlation_id": rid,
                    "kind": message.kind,
                    "sender": message.sender,
                    "recipient": message.recipient,
                    "in_reply_to": meta.get("in_reply_to"),
                    "broadcast_id": meta.get("broadcast_id"),
                    "membership_snapshot": meta.get("membership_snapshot"),
                    "response_policy": meta.get("response_policy"),
                    "response_quorum": meta.get("response_quorum"),
                    "broadcast_policy_version": meta.get("broadcast_policy_version"),
                    "roster_revision": meta.get("roster_revision"),
                    "authorized_liaisons": meta.get("authorized_liaisons"),
                }
                revisions = ledger["scoped_revisions"]
                for inbound_id in set(related_inbounds):
                    revisions[inbound_id] = int(revisions.get(inbound_id, 0)) + 1
                if isinstance(anchor, str) and anchor:
                    key_id = ledger["inbound_index"].get(anchor)
                    admission = ledger["obligations"].get(key_id) if key_id else None
                    if isinstance(admission, dict) and admission.get("state") != "open":
                        self._append(
                            ledger,
                            "LATE_RESPONSE",
                            scope=anchor,
                            source_id=message.id,
                            key_digest=key_id,
                            data={"closed_state": admission.get("state")},
                        )
                if (
                    message.kind == "question"
                    and meta.get("broadcast_id")
                    and not (
                        isinstance(meta.get("membership_snapshot"), list)
                        and meta.get("response_policy") in {"each", "any", "quorum"}
                    )
                ):
                    telemetry = ledger["telemetry"]
                    telemetry["legacy_broadcast_records"] = int(
                        telemetry.get("legacy_broadcast_records", 0)
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
            messages = self.store.valid_messages()
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
            self.proof_health_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(
                self.proof_health_path,
                json.dumps(health, indent=2, ensure_ascii=False),
            )
            return health

    def clear_proof_failure(self) -> None:
        try:
            self.proof_health_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _eligibility(self, record: dict) -> tuple[ResolverState, str, str | None]:
        if self.policy.status in {
            ResolverState.BLOCKED,
            ResolverState.BLOCKED_POLICY,
            ResolverState.BLOCKED_COMPLIANCE,
        }:
            return self.policy.status, self.policy.reason, None
        if self.policy.status == ResolverState.INACTIVE:
            return ResolverState.INACTIVE, self.policy.reason, None
        if self.policy.status != ResolverState.ACTIVE:
            return ResolverState.NOT_OWED, self.policy.reason, None
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
            members = meta.get("membership_snapshot")
            policy = meta.get("response_policy")
            if not isinstance(members, list) or self.agent not in members or policy not in {
                "each", "any", "quorum",
            }:
                return ResolverState.NOT_OWED, "legacy broadcast log-only", rid
            if meta.get("broadcast_policy_version") != 1:
                return ResolverState.CLASSIFICATION_UNKNOWN, "broadcast policy version unsupported", rid
            quorum = meta.get("response_quorum")
            if policy == "quorum" and (
                not isinstance(quorum, int) or quorum < 1 or quorum > len(members)
            ):
                return ResolverState.CLASSIFICATION_UNKNOWN, "broadcast quorum invalid", rid
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
                return Resolution(
                    ResolverState.SUPERSEDED,
                    "requester rescinded",
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
                self._validated_messages()
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
                self._validated_messages()
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
            row["state"] = "action_infra"
            row["operation_payload_digest"] = digest
            row["captured_at"] = self.now()
            admission["first_dispatch_classified"] = True
            admission["last_exhaustion_class"] = "infrastructure"
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
                if admission.get("activation_generation") != self.policy.generation:
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

    def admit_or_finalize(self, record: dict) -> Resolution:
        eligibility, detail, rid = self._eligibility(record)
        if eligibility != ResolverState.ACTIVE:
            if eligibility in {
                ResolverState.BLOCKED,
                ResolverState.BLOCKED_POLICY,
                ResolverState.BLOCKED_COMPLIANCE,
            } or self.policy.generation == "inactive":
                return Resolution(eligibility, detail)
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
                    if claim.get("resolution") != eligibility.value or claim.get(
                        "state"
                    ) not in {"open", "finalized"}:
                        return Resolution(
                            ResolverState.INDETERMINATE,
                            "no-admission claim conflicts with replay",
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
                            source_id=inbound.id,
                            data={
                                "previous_fence": previous_fence,
                                "fence": self.fence,
                            },
                        )
                        self._write(current)
                else:
                    current_telemetry = current["telemetry"]
                    current_telemetry[name] = int(current_telemetry.get(name, 0)) + 1
                    self._append(
                        current,
                        transition,
                        scope=inbound.id,
                        source_id=inbound.id,
                        data={"reason": detail},
                    )
                    current["no_admission_claims"][inbound.id] = {
                        "fence": self.fence,
                        "owner_pid": os.getpid(),
                        "state": "open",
                        "resolution": eligibility.value,
                    }
                    self._write(current)
                scope_revision = int(current["scoped_revisions"].get(inbound.id, 0))
            return Resolution(
                eligibility,
                detail,
                scoped_revision=scope_revision,
                ledger_revision=scope_revision,
            )
        try:
            messages, ledger = self._validated_messages()
        except LedgerUnreadable as exc:
            return Resolution(ResolverState.BLOCKED, str(exc))
        inbound = self._record_message(record, messages)
        if inbound is None:
            return Resolution(ResolverState.INDETERMINATE, "inbound absent from validated replay")
        existing_id = ledger["inbound_index"].get(inbound.id)
        if existing_id:
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
        before = self._resolve_replay(record, messages, ledger, admission=None)
        if before.state in TERMINAL_STATES:
            with self.store._exclusive_lock(
                self.path.with_suffix(".lock"), timeout=10.0,
            ):
                current = self._load()
                if int(current["scoped_revisions"].get(inbound.id, 0)) != before.scoped_revision:
                    return Resolution(ResolverState.INDETERMINATE, "normalization CAS miss")
                if inbound.id in current["inbound_index"]:
                    return Resolution(
                        ResolverState.INDETERMINATE,
                        "concurrent admission won before terminal normalization",
                    )
                claim = current["no_admission_claims"].get(inbound.id)
                if isinstance(claim, dict):
                    if claim.get("resolution") != before.state.value or claim.get(
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
                            source_id=before.evidence_id,
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
                        "resolution": before.state.value,
                    }
                    self._append(
                        current,
                        "PRE_ADMISSION_TERMINAL_NORMALIZED",
                        scope=inbound.id,
                        source_id=before.evidence_id,
                        data={"inbound_id": inbound.id, "state": before.state.value},
                    )
                    self._write(current)
                scoped_revision = int(current["scoped_revisions"].get(inbound.id, 0))
            return Resolution(
                before.state,
                before.reason,
                evidence_id=before.evidence_id,
                scoped_revision=scoped_revision,
                ledger_revision=scoped_revision,
            )
        if before.state not in {ResolverState.OWED_UNSATISFIED}:
            return before
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            current = self._load()
            if int(current["scoped_revisions"].get(inbound.id, 0)) != before.scoped_revision:
                return Resolution(ResolverState.INDETERMINATE, "admission CAS miss")
            if inbound.id in current["inbound_index"]:
                return Resolution(ResolverState.INDETERMINATE, "concurrent admission won")
            if inbound.id in current["no_admission_claims"]:
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
                "activation_generation": self.policy.generation,
                "readiness_generation": self.policy.generation,
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
                "owed_action_missing_seen": False,
                "created_at": self.now(),
                "broadcast_id": (inbound.meta or {}).get("broadcast_id"),
                "membership_snapshot": (inbound.meta or {}).get("membership_snapshot"),
                "response_policy": (inbound.meta or {}).get("response_policy"),
                "response_quorum": (inbound.meta or {}).get("response_quorum"),
                "broadcast_policy_version": (inbound.meta or {}).get(
                    "broadcast_policy_version"
                ),
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
            activation_generation=self.policy.generation,
            readiness_generation=self.policy.generation,
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
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
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
            record_meta = record.get("meta") if isinstance(record.get("meta"), dict) else {}
            if obligation_class == "human_escalation":
                recipient = self.store.operator_facing() or self.store.sole_lead()
                if not isinstance(recipient, str) or recipient == self.agent:
                    raise DispatchRefused("human escalation has no external operator target")
                operation_intent = {
                    "operation": "terminal",
                    "kind": "question",
                    "recipient": recipient,
                    "in_reply_to": record.get("id"),
                    "request_id": f"esc-{permit.nonce[:12]}",
                    "broadcast_id": None,
                    "origin_request_id": record.get("correlation_id"),
                    "origin_inbound_id": record.get("id"),
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
        except OSError as exc:
            raise DispatchRefused("AGENTTALK_PY executable is unavailable") from exc
        if executable.is_symlink() or not resolved.is_file():
            raise DispatchRefused("AGENTTALK_PY must name a regular non-symlink file")
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
        if not isinstance(admission, dict):
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
        completed_attempt: bool = False,
    ) -> bool:
        """Durably count a non-paid retry before allowing it to occur."""
        if category not in {"operation_infra", "finalization"}:
            raise ValueError("unknown retry category")
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            ledger = self._load()
            admission = ledger["obligations"].get(key.digest)
            if not isinstance(admission, dict):
                raise GateError("retry admission missing")
            count_name = f"{category}_attempts" if category == "operation_infra" else "finalization_misses"
            first_name = f"{category}_first_at"
            inflight_name = f"{category}_retry_inflight"
            count = int(admission.get(count_name, 0))
            now_text = self.now()
            first = _epoch(admission.get(first_name))
            now = _epoch(now_text)
            elapsed = 0.0 if first is None or now is None else now - first
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
            if count >= limit or elapsed >= elapsed_limit:
                return False
            if admission.get(inflight_name) and not completed_attempt:
                self._append(
                    ledger,
                    "OPERATION_RETRY_OUTCOME_MISSING"
                    if category == "operation_infra"
                    else "FINALIZATION_RETRY_OUTCOME_MISSING",
                    key_digest=key.digest,
                    data={"prior_count": int(admission.get(count_name, 0))},
                )
                admission[inflight_name] = False
            elif completed_attempt:
                admission[inflight_name] = False
            if not admission.get(inflight_name):
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
                allowed = admission[count_name] < limit and elapsed < elapsed_limit
                if not allowed:
                    admission[inflight_name] = False
                self._write(ledger)
            return admission[count_name] < limit and elapsed < elapsed_limit

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

    def mark_blocked(self, key: ObligationKey, *, reason: str) -> Resolution:
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            ledger = self._load()
            admission = ledger["obligations"].get(key.digest)
            if not isinstance(admission, dict):
                raise GateError("blocked obligation admission missing")
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

    def _advance_record_cursor(self, record: dict) -> None:
        if record.get("mode") == "scoped":
            self.store.mark_thread_seen(
                self.agent,
                record["scoped"]["request_id"],
                record["id"],
            )
        else:
            self.store.advance_cursor(self.agent, record["id"])

    def finalize(
        self,
        record: dict,
        resolution: Resolution,
        *,
        expected_revision: int | None = None,
    ) -> Resolution:
        if not (resolution.terminal or resolution.allows_legacy_commit):
            raise GateError("nonterminal resolution cannot finalize")
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            ledger = self._load()
            key = resolution.key
            write_required = True
            if key is not None:
                admission = ledger["obligations"].get(key.digest)
                if not isinstance(admission, dict):
                    return Resolution(ResolverState.INDETERMINATE, "finalizer admission missing")
                if int(ledger["scoped_revisions"].get(key.inbound_id, 0)) != resolution.scoped_revision:
                    return Resolution(ResolverState.INDETERMINATE, "finalizer scoped CAS miss", key)
                admission["state"] = "finalized"
                admission["finalization_retry_inflight"] = False
                admission["terminal_state"] = resolution.state.value
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
                    self._apply_broadcast_policy_locked(ledger, key, admission)
            else:
                claim = ledger["no_admission_claims"].get(record.get("id"))
                if not isinstance(claim, dict):
                    return Resolution(
                        ResolverState.INDETERMINATE,
                        "no-admission finalizer claim missing",
                    )
                if claim.get("resolution") != resolution.state.value:
                    return Resolution(
                        ResolverState.INDETERMINATE,
                        "no-admission finalizer replay mismatch",
                    )
                if claim.get("state") == "finalized":
                    disposition = ledger["cursor_dispositions"].get(self.agent)
                    if not isinstance(disposition, dict) or any((
                        disposition.get("inbound_id") != record.get("id"),
                        disposition.get("state") != resolution.state.value,
                    )):
                        return Resolution(
                            ResolverState.INDETERMINATE,
                            "no-admission finalized disposition is torn",
                        )
                    write_required = False
                else:
                    if claim.get("state") != "open" or claim.get("fence") != self.fence:
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
                        return Resolution(
                            ResolverState.INDETERMINATE,
                            "no-admission finalizer CAS miss",
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
            if write_required:
                ledger["cursor_dispositions"][self.agent] = {
                    "inbound_id": record.get("id"),
                    "mode": record.get("mode", "global"),
                    "state": resolution.state.value,
                    "at": self.now(),
                }
                self._write(ledger)
        # The physical cursor is a projection of the authoritative disposition.
        # A crash here replays this idempotently; the reverse ordering could lose
        # a message with no canonical terminal.
        self._advance_record_cursor(record)
        return resolution

    def _apply_broadcast_policy_locked(
        self,
        ledger: dict,
        winning_key: ObligationKey,
        winning_admission: dict,
    ) -> None:
        bid = winning_admission.get("broadcast_id")
        policy = winning_admission.get("response_policy")
        members = winning_admission.get("membership_snapshot")
        if not isinstance(bid, str) or policy == "each" or not isinstance(members, list):
            return
        threshold = 1 if policy == "any" else winning_admission.get("response_quorum")
        if not isinstance(threshold, int) or threshold < 1:
            return
        winners = [
            (key_id, row)
            for key_id, row in ledger["obligations"].items()
            if isinstance(row, dict)
            and row.get("broadcast_id") == bid
            and row.get("terminal_state") == ResolverState.SATISFIED.value
            and isinstance(row.get("terminal_evidence_id"), str)
        ]
        if len(winners) < threshold:
            return
        winners.sort(key=lambda item: int(
            ledger["messages"].get(item[1]["terminal_evidence_id"], {}).get(
                "sequence", 0
            )
        ))
        winning_ids = [row["terminal_evidence_id"] for _, row in winners[:threshold]]
        for inbound_id, message_row in ledger["messages"].items():
            if not isinstance(message_row, dict):
                continue
            if (
                message_row.get("kind") != "question"
                or message_row.get("broadcast_id") != bid
                or message_row.get("recipient") not in members
            ):
                continue
            key_id = ledger["inbound_index"].get(inbound_id)
            admission = ledger["obligations"].get(key_id) if key_id else None
            if key_id == winning_key.digest:
                continue
            if isinstance(admission, dict):
                if admission.get("state") != "open":
                    continue
                admission["state"] = "broadcast_policy_satisfied"
                admission["terminal_state"] = (
                    ResolverState.BROADCAST_POLICY_SATISFIED.value
                )
            elif any(
                event.get("transition") == "BROADCAST_POLICY_SATISFIED"
                and isinstance(event.get("data"), dict)
                and event["data"].get("inbound_id") == inbound_id
                for event in ledger["transitions"]
            ):
                continue
            self._append(
                ledger,
                "BROADCAST_POLICY_SATISFIED",
                scope=inbound_id,
                source_id=winning_ids[-1],
                key_digest=key_id,
                data={
                    "broadcast_id": bid,
                    "inbound_id": inbound_id,
                    "winning_ids": winning_ids,
                    "policy": policy,
                },
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
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            ledger = self._load()
            if ledger["inbound_index"].get(key.inbound_id) != key.digest:
                raise GateError("delivery failure admission index is stale")
            admission = ledger["obligations"].get(key.digest)
            if not isinstance(admission, dict):
                raise GateError("delivery admission missing")
            if self._key_from(admission.get("key")) != key:
                raise GateError("delivery failure key does not match persisted admission")
            if admission.get("state") == "delivery_failed":
                canonical_reason = str(admission.get("delivery_failure_reason") or reason)
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
                canonical_reason = reason
                admission["state"] = "delivery_failed"
                admission["delivery_failure_reason"] = canonical_reason
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
                    },
                )
                admission["delivery_failure_sequence"] = event["sequence"]
                ledger["delivery_index"].setdefault(key.correlation_id, []).append({
                    "sequence": event["sequence"],
                    "key_digest": key.digest,
                    "inbound_id": key.inbound_id,
                    "delivery_generation": key.delivery_generation,
                    "requester": key.requester,
                    "responder": key.responder,
                    "state": "delivery_failed",
                    "reason": canonical_reason,
                })
                ledger["cursor_dispositions"][self.agent] = {
                    "inbound_id": record.get("id"),
                    "mode": record.get("mode", "global"),
                    "state": "delivery_failed",
                    "at": self.now(),
                }
                self._apply_delivery_exhaustion(
                    ledger,
                    key,
                    admission,
                    event,
                )
                self._write(ledger)
            breaker = ledger["breakers"].get(self.agent, {})
            breaker_tripped = isinstance(breaker, dict) and breaker.get("tripped") is True
            scoped_revision = int(ledger["scoped_revisions"].get(key.inbound_id, 0))
        if breaker_tripped:
            self._project_compliance_breaker_hold()
            self._reconcile_compliance_breaker_alert()
        self._advance_record_cursor(record)
        return Resolution(
            ResolverState.DELIVERY_EXHAUSTED,
            canonical_reason,
            key,
            scoped_revision=scoped_revision,
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

    def delivery_status(self, request_id: str, requester: str) -> dict | None:
        try:
            ledger = self._load(create=False)
        except LedgerUnreadable:
            return None
        rows = ledger["delivery_index"].get(request_id, [])
        for row in reversed(rows if isinstance(rows, list) else []):
            if isinstance(row, dict) and row.get("requester") == requester:
                return dict(row)
        return None

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
        with self.store._config_lock(timeout=10.0):
            roster = _roster_snapshot(self.store.load_config())
            if roster["revision"] != expected_roster_revision:
                raise StaleRevision("roster changed before operator resolution")
            if actor not in roster["authorized_liaisons"]:
                raise PermissionError("actor is not an event-time authorized liaison or lead")
            with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
                ledger = self._load()
                admission = ledger["obligations"].get(key.digest)
                if not isinstance(admission, dict) or admission.get("state") != "open":
                    raise GateError("operator resolution target is not open")
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
                ledger["cursor_dispositions"][self.agent] = {
                    "inbound_id": record.get("id"),
                    "mode": record.get("mode", "global"),
                    "state": ResolverState.OPERATOR_RESOLVED.value,
                    "at": self.now(),
                }
                self._write(ledger)
        self._advance_record_cursor(record)
        return Resolution(
            ResolverState.OPERATOR_RESOLVED,
            reason.strip(),
            key,
            evidence_id=str(event["sequence"]),
            scoped_revision=int(ledger["scoped_revisions"].get(key.inbound_id, 0)),
        )

    def transfer(self, key: ObligationKey, *, destination: str, new_inbound_id: str) -> None:
        """Atomically validate destination, close old delivery, and create the next."""
        if destination not in self.store.load_config().get("agents", []):
            raise ValueError("transfer destination is not active")
        messages, _ = self._validated_messages()
        next_inbound = next(
            (message for message in messages if message.id == new_inbound_id),
            None,
        )
        if (
            next_inbound is None
            or next_inbound.kind != "question"
            or next_inbound.sender != key.requester
            or next_inbound.recipient != destination
            or _correlation(next_inbound.meta) != key.correlation_id
        ):
            raise ValueError("transfer destination inbound is missing or unadmittable")
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            ledger = self._load()
            old = ledger["obligations"].get(key.digest)
            if not isinstance(old, dict) or old.get("state") != "open":
                raise GateError("transfer source is not open")
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
            next_admission = dict(old)
            next_admission.update({
                "key": next_key.to_dict(),
                "state": "open",
                "fence": "unclaimed",
                "paid_dispatches_total": 0,
                "paid_initial_dispatches_total": 0,
                "paid_recoveries_total": 0,
                "paid_continuations_total": 0,
                "reservations": {},
                "continuation_used": False,
                "recovery_used": False,
                "first_dispatch_classified": False,
                "last_exhaustion_class": None,
                "operation_infra_attempts": 0,
                "operation_infra_first_at": None,
                "operation_infra_retry_inflight": False,
                "finalization_misses": 0,
                "finalization_first_at": None,
                "finalization_retry_inflight": False,
                "owed_action_missing_seen": False,
            })
            old["state"] = "transferred"
            ledger["obligations"][next_key.digest] = next_admission
            ledger["inbound_index"][new_inbound_id] = next_key.digest
            self._append(
                ledger,
                "TRANSFERRED",
                scope=key.inbound_id,
                key_digest=key.digest,
                data={"destination": destination, "new_key": next_key.to_dict()},
            )
            self._append(
                ledger,
                "OBLIGATION_ADMITTED",
                scope=new_inbound_id,
                source_id=new_inbound_id,
                key_digest=next_key.digest,
                data={"key": next_key.to_dict(), "transfer_from": key.digest},
            )
            self._write(ledger)

    def close_broadcast_members(self, winning_key: ObligationKey,
                                remaining: list[ObligationKey], *, winning_ids: list[str]) -> None:
        with self.store._exclusive_lock(self.path.with_suffix(".lock"), timeout=10.0):
            ledger = self._load()
            # DELIVERY_FAILED is intentionally not an answer winner.
            winner = ledger["obligations"].get(winning_key.digest)
            if not isinstance(winner, dict) or winner.get("terminal_state") != "satisfied":
                raise GateError("broadcast winner is not an answer terminal")
            for key in remaining:
                admission = ledger["obligations"].get(key.digest)
                if not isinstance(admission, dict) or admission.get("state") != "open":
                    continue
                admission["state"] = "broadcast_policy_satisfied"
                self._append(
                    ledger,
                    "BROADCAST_POLICY_SATISFIED",
                    scope=key.inbound_id,
                    key_digest=key.digest,
                    data={"winning_ids": list(winning_ids)},
                )
            self._write(ledger)

    def status(self) -> dict:
        result = {
            "agent": self.agent,
            "grade": self.policy.grade,
            "policy_generation": self.policy.generation,
            "status": (
                "ACTIVE (detection-grade)"
                if self.policy.status == ResolverState.ACTIVE
                else self.policy.status.value.upper()
            ),
            "reason": self.policy.reason,
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
                self.policy.status == ResolverState.ACTIVE
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
            return result
        try:
            ledger = self._load(create=False)
        except LedgerUnreadable as exc:
            if self.policy.status == ResolverState.ACTIVE:
                result["status"] = ResolverState.BLOCKED.value.upper()
                result["reason"] = str(exc)
            result["ledger_error"] = str(exc)
            return result
        result["store_epoch"] = ledger["store_epoch"]
        result["append_sequence"] = ledger["append_sequence"]
        breaker = ledger["breakers"].get(self.agent, {})
        result["breaker"] = breaker
        if isinstance(breaker, dict) and breaker.get("tripped") is True:
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
    gate._index_messages([message])


def note_manual_close(store, agent: str, request_id: str) -> None:
    """Canonicalize a no-message ACK/manual close for admitted generations."""
    gate = DetectionCommitGate(
        store, agent, PolicySnapshot.inactive("append service"), fence="append-service",
    )
    messages = store.valid_messages()
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


def delivery_failed_for(store, request_id: str, requester: str) -> dict | None:
    gate = DetectionCommitGate(
        store,
        requester,
        PolicySnapshot.inactive("delivery index read"),
        fence="read-only",
    )
    return gate.delivery_status(request_id, requester)
