"""Watched-trial OVH/Qwen gateway policy and durable spend accounting.

This module deliberately has no LiteLLM dependency.  The public front reserves
money here before it sends a byte to the loopback LiteLLM process, then settles
from the completed Anthropic response stream.  LiteLLM callbacks are not an
accounting authority.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping

from .ovh_gateway_reasoning import ReasoningValue, render_extra_body_lines


MODEL_ALIAS = "Qwen3.8-27B"
POLICY_SOURCE = "https://www.ovhcloud.com/en/public-cloud/ai-endpoints/catalog/"
POLICY_OBSERVED_DATE = "2026-09-22"
POLICY_CURRENCY = "EUR"
TOKENS_PER_RATE_UNIT = 1_000_000
INPUT_RATE_MICRO_EUR = 400_000
OUTPUT_RATE_MICRO_EUR = 2_700_000
RESERVE_INPUT_RATE_MICRO_EUR = 480_000
RESERVE_OUTPUT_RATE_MICRO_EUR = 3_240_000
MAX_CONTEXT_TOKENS = 262_144
# Not confirmed from local LiteLLM/OVH data: no "Qwen3.8-27B" entry exists in
# this venv's model_prices_and_context_window_backup.json for any provider,
# let alone ovhcloud specifically - the closest OVH entry (ovhcloud/Qwen3-32B,
# a different model generation) shows max_output_tokens=32000 tied 1:1 to its
# own max_input_tokens, not a documented output sub-ceiling. Using the
# lead's own fallback value per their explicit instruction.
MAX_OUTPUT_TOKENS = 32_768
TRIAL_CUTOFF_MICRO_EUR = 95_000_000
SOFT_STOP_MICRO_EUR = 90_000_000
EXTERNAL_CEILING_MICRO_EUR = 100_000_000
CANARY_TOLERANCE_BPS = 1_000
LEDGER_SCHEMA_VERSION = 3
# A child-cap schema-3 ledger keeps ledger schema 2. Every writer from before quota
# lease binding requires ledger schema 2 in both the install marker and the database
# on each connection, so moving both to 3 fences every one of them out.
LEDGER_LEGACY_SCHEMA_VERSION = 2
INSTALL_MARKER_SCHEMA_VERSION = 1
CHILD_CAP_SCHEMA_VERSION = 4
# Schema 3 is the child-cap schema before quota lease binding. It is still served
# exactly as before; only the explicit install_child_cap_binding moves a ledger to 4.
CHILD_CAP_LEGACY_SCHEMA_VERSION = 3
# The one child-cap schema each ledger schema may hold.
_CHILD_CAP_SCHEMA_FOR_LEDGER = {
    LEDGER_LEGACY_SCHEMA_VERSION: CHILD_CAP_LEGACY_SCHEMA_VERSION,
    LEDGER_SCHEMA_VERSION: CHILD_CAP_SCHEMA_VERSION,
}
CHILD_TURN_MAX_CALLS = 100_000
CHILD_TURN_MAX_MICRO_EUR = 95_000_000
CHILD_TURN_MAX_SECONDS = 86_400
QUOTA_LEASE_BINDING = "quota_lease_v1"
CHILD_STATES = ("open", "capped", "expired", "fenced")
CHILD_OUTCOMES = ("completed", "cancelled", "failed", "provider_limit")
# 2: every receipt row on a page carries its charge_period.
CHILD_RECEIPT_REPORT_VERSION = 2
RECEIPT_ENVELOPE_VERSION = 1
GATEWAY_REPORT_VERSION = 1
RECEIPT_PAGE_MAX_LIMIT = 1_000
RECEIPT_MAX_SEQ = 2**63 - 1
RECEIPT_MAX_CALLS = 1_000_000
RECEIPT_MAX_AMOUNT = 10**12
BACKEND_PROFILE = "ovh-qwen"
EXTERNAL_WORKER = "external-worker"
PUBLIC_HOST = "127.0.0.1"
PUBLIC_PORT = 4000
INTERNAL_HOST = "127.0.0.1"
INTERNAL_PORT = 4001
# Four times the observed approximately 120 KiB Claude Code system-and-tools
# payload, while retaining a bounded per-request allocation.
MAX_REQUEST_BYTES = 512 * 1024
DEFAULT_LOCAL_DIRNAME = "agenttalk-ovh"
DEFAULT_SPEND_DIRNAME = "agenttalk-ovh-spend"
MAX_OPENING_EVIDENCE_LENGTH = 512

_ATTEMPT_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_PERIOD_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")
_TERMINAL_ATTEMPT_STATES = frozenset({"settled", "reconciled"})
_UNRESOLVED_ATTEMPT_STATES = frozenset({"reserved", "uncertain"})
# The closed words for a service hold; the hold's own text is never shown.
_HOLD_OVER_RESERVATION_RE = re.compile(r"attempt [a-f0-9]{32} exceeded the reserved policy")
HOLD_REASON_WORDS = ("attempt_over_reservation", "dashboard_canary_mismatch", "manual", "other")
_FRONT_TOKEN_RE = re.compile(r"^atgw-[A-Za-z0-9_-]{43}$")
_CHILD_CAPABILITY_RE = re.compile(r"^atgw-child-[A-Za-z0-9_-]{43}$")
_CHILD_CAP_TABLES = (
    "child_turns",
    "child_capabilities",
    "child_attempts",
)
# Schema 4 adds the receipts (also named child_*) and the pending notes.
_CHILD_CAP_TABLES_V4 = _CHILD_CAP_TABLES + ("child_receipts",)
_RECEIPT_TABLES = ("child_receipts", "receipt_pending")
_QUOTA_LEASE_REF_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SHA256_HEX_RE = re.compile(r"^[a-f0-9]{64}$")
_LEDGER_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")
_CHILD_CAP_GUARDS = (
    "child_turns_ending_frozen",
    "child_turns_binding_immutable",
    "child_turns_no_replace",
    "child_turns_bound_no_delete",
    "child_receipts_no_delete",
    "child_receipts_no_update",
    "child_receipts_no_replace",
    "receipt_pending_no_delete",
    "receipt_pending_resolve_only",
    "receipt_pending_no_replace",
)
_CHILD_TURN_COLUMNS_V3 = (
    "agent",
    "message_id",
    "request_id",
    "state",
    "max_calls",
    "max_micro_eur",
    "opened_at",
    "expires_at",
    "updated_at",
    "reason",
)
_CHILD_TURN_COLUMNS_V4 = _CHILD_TURN_COLUMNS_V3 + (
    "quota_lease_ref_sha256",
    "terminal_outcome",
    "terminal_at",
    "terminal_source",
)
_RECEIPT_COLUMNS = (
    "seq",
    "agent",
    "message_id",
    "quota_lease_ref_sha256",
    "outcome",
    "calls",
    "input_tokens",
    "output_tokens",
    "actual_micro_eur",
    "closed_at",
)
_RECEIPT_PENDING_COLUMNS = ("agent", "message_id", "created_at", "resolved_at")


class GatewayError(RuntimeError):
    """Base class for stable gateway failures."""


class GatewayConfigError(GatewayError):
    """Static configuration is invalid or incomplete."""


class LedgerError(GatewayError):
    """The durable ledger could not prove a safe accounting state."""


class LedgerBlocked(LedgerError):
    """Accounting failed closed before transport."""


class LedgerHold(LedgerError):
    """An unresolved or explicitly held attempt blocks further transport."""


class PolicyBlocked(LedgerError):
    """The configured trial cutoff refuses a new reservation."""


class ChildTurnCapBlocked(LedgerBlocked):
    """A request lacks a valid durable child-turn capability."""


class ChildTurnCapExceeded(ChildTurnCapBlocked):
    """A durable child-turn budget has reached a hard gateway ceiling."""


class QuotaLeaseReferenceMismatch(ChildTurnCapBlocked):
    """A bound child turn was closed with a different quota lease reference.

    Permanent: the message names no reference, only the closed word."""


class QuotaLeaseBindingRequired(LedgerHold):
    """The binding flag is on and an open came without a quota lease reference.

    A hold, not a block: the caller waits until the operator turns the flag off or
    the open brings its admission."""


class ReceiptPageRefused(LedgerBlocked):
    """The receipt page cannot be given exactly: a gap, damage or a value that does
    not fit the page's bounds. Nothing is rounded or dropped to make it fit."""


class GatewayReportRefused(LedgerBlocked):
    """The ledger report cannot be given exactly: a value outside the report's
    closed shape or bounds. Nothing is rounded or dropped to make it fit."""


def _charged(state: str, actual_micro_eur: int | None, reconcile_outcome: str | None) -> bool:
    """Whether an attempt's recorded actual is money spent: settled; reconciled as
    charge-reserve (a no-send reconciliation sent nothing and records 0); or
    uncertain with a recorded actual (settlement went over the reservation). A
    receipt's charge period and the report's unreceipted money use this one rule."""
    if state == "settled":
        return True
    if state == "uncertain":
        return actual_micro_eur is not None
    return state == "reconciled" and reconcile_outcome == "charge-reserve"


def _hold_word(value: str) -> str | None:
    if not value:
        return None
    if _HOLD_OVER_RESERVATION_RE.fullmatch(value):
        return "attempt_over_reservation"
    if value == "dashboard_canary_mismatch":
        return "dashboard_canary_mismatch"
    if value.startswith("manual: "):
        return "manual"
    return "other"


def _ceil_cost(tokens: int, rate_micro_eur: int) -> int:
    if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
        raise ValueError("token counts must be non-negative integers")
    return (tokens * rate_micro_eur + TOKENS_PER_RATE_UNIT - 1) // TOKENS_PER_RATE_UNIT


def settlement_cost_micro_eur(input_tokens: int, output_tokens: int) -> int:
    """Exact fixed-point settlement cost, rounded upward per component."""
    return (
        _ceil_cost(input_tokens, INPUT_RATE_MICRO_EUR)
        + _ceil_cost(output_tokens, OUTPUT_RATE_MICRO_EUR)
    )


def reservation_cost_micro_eur() -> int:
    """Conservative one-attempt hold at full context and bounded output."""
    return (
        _ceil_cost(MAX_CONTEXT_TOKENS, RESERVE_INPUT_RATE_MICRO_EUR)
        + _ceil_cost(MAX_OUTPUT_TOKENS, RESERVE_OUTPUT_RATE_MICRO_EUR)
    )


def price_policy(
    *,
    trial_cutoff_micro_eur: int = TRIAL_CUTOFF_MICRO_EUR,
    soft_stop_micro_eur: int = SOFT_STOP_MICRO_EUR,
    external_ceiling_micro_eur: int = EXTERNAL_CEILING_MICRO_EUR,
) -> dict:
    """Canonical non-secret policy object whose digest binds persisted state.

    The three envelope figures are parameters, not bare constants: each
    ledger pins its OWN chosen envelope at ``initialize()`` time (operator
    ``gateway init --cutoff-eur``/``--soft-stop-eur``/``--ceiling-eur``), and
    every caller after that reads the envelope back from that ledger's own
    metadata, never from the module defaults below - the defaults exist only
    so that an unchanged call site (and an unchanged CLI invocation) keeps
    producing today's hash.
    """
    return {
        "schema_version": 1,
        "model": MODEL_ALIAS,
        "source": POLICY_SOURCE,
        "observed_date": POLICY_OBSERVED_DATE,
        "billing_currency": POLICY_CURRENCY,
        "tokens_per_rate_unit": TOKENS_PER_RATE_UNIT,
        "settlement": {
            "input_micro_eur": INPUT_RATE_MICRO_EUR,
            "output_micro_eur": OUTPUT_RATE_MICRO_EUR,
        },
        "reservation": {
            "input_micro_eur": RESERVE_INPUT_RATE_MICRO_EUR,
            "output_micro_eur": RESERVE_OUTPUT_RATE_MICRO_EUR,
            "margin_percent": 20,
            "max_context_tokens": MAX_CONTEXT_TOKENS,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "worst_case_micro_eur": reservation_cost_micro_eur(),
        },
        "trial_cutoff_micro_eur": trial_cutoff_micro_eur,
        "soft_stop_micro_eur": soft_stop_micro_eur,
        "external_ceiling_micro_eur": external_ceiling_micro_eur,
        "dashboard_canary": {
            "requires_nonzero_delta": True,
            "tolerance_basis_points": CANARY_TOLERANCE_BPS,
        },
    }


def price_policy_hash(
    *,
    trial_cutoff_micro_eur: int = TRIAL_CUTOFF_MICRO_EUR,
    soft_stop_micro_eur: int = SOFT_STOP_MICRO_EUR,
    external_ceiling_micro_eur: int = EXTERNAL_CEILING_MICRO_EUR,
) -> str:
    encoded = json.dumps(
        price_policy(
            trial_cutoff_micro_eur=trial_cutoff_micro_eur,
            soft_stop_micro_eur=soft_stop_micro_eur,
            external_ceiling_micro_eur=external_ceiling_micro_eur,
        ),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def child_cap_policy(
    *,
    child_turn_max_micro_eur: int = CHILD_TURN_MAX_MICRO_EUR,
    schema_version: int = CHILD_CAP_SCHEMA_VERSION,
) -> dict:
    """Canonical separately-versioned child-turn admission policy.

    ``child_turn_max_micro_eur`` is a parameter for the same reason the three
    ``price_policy`` envelope figures are: it is chosen once per ledger (by
    default, equal to that ledger's own trial cutoff) and read back from
    metadata thereafter, never from the module default below.
    ``schema_version`` 3 gives the policy of a ledger not yet migrated to quota
    lease binding, unchanged, so its stored hash still verifies.
    """
    policy: dict = {
        "schema_version": schema_version,
        "max_calls": CHILD_TURN_MAX_CALLS,
        "max_micro_eur": child_turn_max_micro_eur,
        "max_seconds": CHILD_TURN_MAX_SECONDS,
        "reservation_micro_eur": reservation_cost_micro_eur(),
    }
    if schema_version >= 4:
        policy["binding"] = QUOTA_LEASE_BINDING
        policy["child_states"] = list(CHILD_STATES)
    return policy


def child_cap_policy_hash(
    *,
    child_turn_max_micro_eur: int = CHILD_TURN_MAX_MICRO_EUR,
    schema_version: int = CHILD_CAP_SCHEMA_VERSION,
) -> str:
    encoded = json.dumps(
        child_cap_policy(
            child_turn_max_micro_eur=child_turn_max_micro_eur,
            schema_version=schema_version,
        ),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def quota_lease_ref_sha256(reference: object) -> str:
    """The stored identity of a quota lease reference: the SHA-256 of its ASCII bytes,
    as lowercase hex. The reference itself is never stored, logged or echoed."""
    if not isinstance(reference, str) or not _QUOTA_LEASE_REF_RE.fullmatch(reference):
        raise ChildTurnCapBlocked("quota lease reference is malformed")
    return hashlib.sha256(reference.encode("ascii")).hexdigest()


def _local_appdata() -> Path:
    raw = os.environ.get("LOCALAPPDATA")
    if raw:
        return Path(raw)
    return Path.home() / ".local" / "share"


def default_secret_dir() -> Path:
    return _local_appdata() / DEFAULT_LOCAL_DIRNAME


def default_spend_dir() -> Path:
    return _local_appdata() / DEFAULT_SPEND_DIRNAME


def default_key_path() -> Path:
    return default_secret_dir() / "api_key.txt"


def default_front_token_path() -> Path:
    return default_secret_dir() / "front_token.txt"


def default_internal_token_path() -> Path:
    return default_secret_dir() / "internal_token.txt"


def default_ledger_path() -> Path:
    return default_spend_dir() / "ledger.sqlite3"


def default_install_marker_path() -> Path:
    return default_spend_dir() / "install.json"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("a timezone-aware UTC datetime is required")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise LedgerBlocked("ledger timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LedgerBlocked("ledger timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LedgerBlocked("ledger timestamp lacks a UTC offset")
    return parsed.astimezone(timezone.utc)


def _period(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m")


def _next_period(value: str) -> str:
    if not _PERIOD_RE.fullmatch(value):
        raise LedgerBlocked("ledger period is invalid")
    year, month = (int(part) for part in value.split("-", 1))
    if month == 12:
        return f"{year + 1:04d}-01"
    return f"{year:04d}-{month + 1:02d}"


def _flush_file(path: Path) -> None:
    # CPython's Windows fsync delegates to _commit(), which rejects a
    # read-only CRT descriptor. The ledger and its journal are service-owned
    # writable files, so open read/write before the required FlushFileBuffers.
    flags = os.O_RDWR if os.name == "nt" else os.O_RDONLY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
        if os.name == "nt":
            import msvcrt

            handle = ctypes.c_void_p(msvcrt.get_osfhandle(fd))
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.FlushFileBuffers.argtypes = [ctypes.c_void_p]
            kernel32.FlushFileBuffers.restype = ctypes.c_int
            if not kernel32.FlushFileBuffers(handle):
                raise OSError(ctypes.get_last_error(), "FlushFileBuffers failed")
    finally:
        os.close(fd)


def _flush_parent(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _durable_replace(source: Path, target: Path) -> None:
    _flush_file(source)
    if os.name == "nt":
        move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move_file.restype = ctypes.c_int
        replace_existing = 0x1
        write_through = 0x8
        if not move_file(str(source), str(target), replace_existing | write_through):
            raise OSError(ctypes.get_last_error(), "MoveFileExW durable replace failed")
        return
    os.replace(source, target)
    _flush_parent(target)


def _durable_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _durable_replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _durable_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        _durable_replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _strict_json_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LedgerBlocked(f"cannot read {path.name}") from exc
    if not isinstance(value, dict):
        raise LedgerBlocked(f"{path.name} must contain a JSON object")
    return value


def _binding_migration_checkpoint(conn: sqlite3.Connection, step: str) -> None:
    """Called after each step of the schema 3 to 4 migration (begin, copied, renamed,
    guards, metadata, foreign_keys_on). It does nothing; tests replace it to inject
    a fault after a step and prove the ledger stays whole."""


@dataclass(frozen=True)
class Reservation:
    attempt_id: str
    period: str
    reserved_micro_eur: int
    admitted_at: str


@dataclass(frozen=True)
class ChildTurnCredential:
    token: str
    agent: str
    message_id: str
    expires_at: str


class SpendLedger:
    """Fail-closed monthly accounting with durable unresolved reservations."""

    def __init__(
        self,
        db_path: Path | None = None,
        marker_path: Path | None = None,
        *,
        now: Callable[[], datetime] = _utc_now,
        busy_timeout_seconds: float = 5.0,
        durability_barrier: Callable[[Path], None] | None = None,
    ) -> None:
        self.db_path = Path(db_path or default_ledger_path()).resolve()
        self.marker_path = Path(marker_path or default_install_marker_path()).resolve()
        self.now = now
        self.busy_timeout_seconds = max(0.05, float(busy_timeout_seconds))
        self._durability_barrier = durability_barrier or self._default_barrier

    @staticmethod
    def _default_barrier(db_path: Path) -> None:
        _flush_file(db_path)
        journal = Path(f"{db_path}-journal")
        if journal.exists():
            _flush_file(journal)

    def installation_state(self) -> str:
        db_exists = self.db_path.exists()
        marker_exists = self.marker_path.exists()
        if db_exists and marker_exists:
            return "complete"
        if not db_exists and not marker_exists:
            return "absent"
        return "partial"

    @staticmethod
    def _opening_evidence(value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("opening_evidence must be a string")
        normalized = value.strip()
        if (
            not normalized
            or len(normalized) > MAX_OPENING_EVIDENCE_LENGTH
            or "\r" in normalized
            or "\n" in normalized
            or any(ord(char) < 32 for char in normalized)
        ):
            raise ValueError(
                "opening_evidence must be a non-empty single line of at most "
                f"{MAX_OPENING_EVIDENCE_LENGTH} characters"
            )
        return normalized

    @staticmethod
    def _opening_amount(value: object) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("opening_micro_eur must be a non-negative integer")
        return value

    @staticmethod
    def _assert_external_envelope(
        opening_micro_eur: int,
        *,
        trial_cutoff_micro_eur: int,
        external_ceiling_micro_eur: int,
    ) -> None:
        projected = (
            opening_micro_eur
            + trial_cutoff_micro_eur
            + reservation_cost_micro_eur()
        )
        if projected > external_ceiling_micro_eur:
            raise PolicyBlocked(
                "opening balance plus trial cutoff and one reservation exceeds "
                "the external account ceiling"
            )

    @staticmethod
    def _validated_envelope(
        trial_cutoff_micro_eur: object,
        soft_stop_micro_eur: object,
        external_ceiling_micro_eur: object,
    ) -> tuple[int, int, int]:
        parsed: dict[str, int] = {}
        for name, value in (
            ("cutoff", trial_cutoff_micro_eur),
            ("soft-stop", soft_stop_micro_eur),
            ("ceiling", external_ceiling_micro_eur),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise PolicyBlocked(f"envelope {name} must be a non-negative integer")
            parsed[name] = value
        if not parsed["soft-stop"] < parsed["cutoff"] <= parsed["ceiling"]:
            raise PolicyBlocked(
                "envelope must satisfy soft-stop < cutoff <= ceiling"
            )
        return parsed["cutoff"], parsed["soft-stop"], parsed["ceiling"]

    @staticmethod
    def _parse_envelope_int(metadata: dict[str, str], key: str) -> int:
        raw = metadata.get(key)
        try:
            value = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise LedgerBlocked(f"ledger metadata {key} is invalid") from exc
        if str(value) != raw or value < 0:
            raise LedgerBlocked(f"ledger metadata {key} is invalid")
        return value

    def initialize(
        self,
        *,
        opening_micro_eur: int,
        opening_evidence: str,
        generation: str | None = None,
        child_cap_issuer_token: str | None = None,
        trial_cutoff_micro_eur: int = TRIAL_CUTOFF_MICRO_EUR,
        soft_stop_micro_eur: int = SOFT_STOP_MICRO_EUR,
        external_ceiling_micro_eur: int = EXTERNAL_CEILING_MICRO_EUR,
        child_turn_max_micro_eur: int | None = None,
    ) -> dict:
        if self.installation_state() != "absent":
            raise LedgerBlocked(
                "ledger initialization requires both database and install marker to be absent"
            )
        opening_micro_eur = self._opening_amount(opening_micro_eur)
        opening_evidence = self._opening_evidence(opening_evidence)
        issuer_hash = self._child_cap_issuer_hash(child_cap_issuer_token)
        trial_cutoff_micro_eur, soft_stop_micro_eur, external_ceiling_micro_eur = (
            self._validated_envelope(
                trial_cutoff_micro_eur, soft_stop_micro_eur, external_ceiling_micro_eur
            )
        )
        # By design (unchanged from the historical single-envelope constants):
        # the per-turn cap defaults to the chosen trial cutoff, so the
        # ledger's own cutoff/ceiling stay the only limits a turn can hit
        # live. A caller MAY decouple it explicitly (e.g. a test that wants
        # to reach the per-turn ceiling without exhausting the whole trial
        # cutoff first) - the CLI never does.
        if child_turn_max_micro_eur is None:
            child_turn_max_micro_eur = trial_cutoff_micro_eur
        elif (
            not isinstance(child_turn_max_micro_eur, int)
            or isinstance(child_turn_max_micro_eur, bool)
            or child_turn_max_micro_eur < 0
        ):
            raise PolicyBlocked(
                "envelope child turn cap must be a non-negative integer"
            )
        self._assert_external_envelope(
            opening_micro_eur,
            trial_cutoff_micro_eur=trial_cutoff_micro_eur,
            external_ceiling_micro_eur=external_ceiling_micro_eur,
        )
        now = self.now().astimezone(timezone.utc)
        observed_at = _iso_utc(now)
        opening_period = _period(now)
        evidence_hash = hashlib.sha256(opening_evidence.encode("utf-8")).hexdigest()
        generation = generation or uuid.uuid4().hex
        if not _ATTEMPT_ID_RE.fullmatch(generation):
            raise ValueError("generation must be 32 lowercase hexadecimal characters")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        temp_db = self.db_path.with_name(f".{self.db_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            conn = sqlite3.connect(temp_db)
            try:
                self._configure(conn)
                self._create_schema(conn)
                values = {
                    "schema_version": str(LEDGER_SCHEMA_VERSION),
                    "generation": generation,
                    "price_policy_hash": price_policy_hash(
                        trial_cutoff_micro_eur=trial_cutoff_micro_eur,
                        soft_stop_micro_eur=soft_stop_micro_eur,
                        external_ceiling_micro_eur=external_ceiling_micro_eur,
                    ),
                    "currency": POLICY_CURRENCY,
                    "trial_cutoff_micro_eur": str(trial_cutoff_micro_eur),
                    "soft_stop_micro_eur": str(soft_stop_micro_eur),
                    "external_ceiling_micro_eur": str(external_ceiling_micro_eur),
                    "opening_micro_eur": str(opening_micro_eur),
                    "opening_evidence": opening_evidence,
                    "opening_observed_at": observed_at,
                    "opening_period": opening_period,
                    "initialized_at": observed_at,
                    "last_accepted_utc": observed_at,
                    "last_accepted_period": opening_period,
                    "service_hold": "",
                    "child_cap_schema_version": str(CHILD_CAP_SCHEMA_VERSION),
                    "child_cap_policy_hash": child_cap_policy_hash(
                        child_turn_max_micro_eur=child_turn_max_micro_eur
                    ),
                    "child_turn_max_micro_eur": str(child_turn_max_micro_eur),
                    "child_cap_issuer_sha256": issuer_hash,
                    # off until the operator's explicit binding-required --on
                    "quota_lease_binding_required": "0",
                }
                conn.execute("BEGIN IMMEDIATE")
                conn.executemany(
                    "INSERT INTO metadata(key, value) VALUES (?, ?)", values.items()
                )
                conn.execute(
                    "INSERT INTO periods(period, committed_micro_eur) VALUES (?, ?)",
                    (opening_period, opening_micro_eur),
                )
                conn.commit()
            finally:
                conn.close()
            self._durability_barrier(temp_db)
            _durable_replace(temp_db, self.db_path)
            marker = {
                "schema_version": INSTALL_MARKER_SCHEMA_VERSION,
                "ledger_schema_version": LEDGER_SCHEMA_VERSION,
                "generation": generation,
                "price_policy_hash": price_policy_hash(
                    trial_cutoff_micro_eur=trial_cutoff_micro_eur,
                    soft_stop_micro_eur=soft_stop_micro_eur,
                    external_ceiling_micro_eur=external_ceiling_micro_eur,
                ),
                "opening_micro_eur": opening_micro_eur,
                "opening_evidence_sha256": evidence_hash,
                "opening_observed_at": observed_at,
                "opening_period": opening_period,
                "initialized_at": observed_at,
            }
            _durable_write_json(self.marker_path, marker)
            return marker
        finally:
            for temporary in (
                temp_db,
                Path(f"{temp_db}-journal"),
                Path(f"{temp_db}-wal"),
                Path(f"{temp_db}-shm"),
            ):
                with contextlib.suppress(FileNotFoundError):
                    temporary.unlink()

    @staticmethod
    def _configure(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=PERSIST")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA fullfsync=ON")
        conn.execute("PRAGMA checkpoint_fullfsync=ON")
        conn.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE periods (
                period TEXT PRIMARY KEY,
                committed_micro_eur INTEGER NOT NULL CHECK (committed_micro_eur >= 0)
            );
            CREATE TABLE attempts (
                attempt_id TEXT PRIMARY KEY,
                period TEXT NOT NULL REFERENCES periods(period),
                model TEXT NOT NULL,
                policy_hash TEXT NOT NULL,
                reserved_micro_eur INTEGER NOT NULL CHECK (reserved_micro_eur >= 0),
                state TEXT NOT NULL CHECK (
                    state IN ('reserved', 'uncertain', 'settled', 'reconciled')
                ),
                input_tokens INTEGER,
                output_tokens INTEGER,
                actual_micro_eur INTEGER,
                admitted_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE reconciliations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
                outcome TEXT NOT NULL,
                prior_state TEXT NOT NULL,
                charged_micro_eur INTEGER NOT NULL,
                reason TEXT NOT NULL,
                reconciled_at TEXT NOT NULL
            );
            """
        )
        SpendLedger._create_child_cap_schema(conn)

    @staticmethod
    def _create_child_turns_table(conn: sqlite3.Connection, name: str) -> None:
        """The schema-4 child_turns table, under ``name`` (the migration builds it
        beside the old one, then renames it)."""
        conn.execute(
            f"""CREATE TABLE {name} (
                agent TEXT NOT NULL,
                message_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('open', 'capped', 'expired', 'fenced')
                ),
                max_calls INTEGER NOT NULL,
                max_micro_eur INTEGER NOT NULL,
                opened_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                quota_lease_ref_sha256 TEXT CHECK (
                    quota_lease_ref_sha256 IS NULL OR (
                        length(quota_lease_ref_sha256) = 64
                        AND quota_lease_ref_sha256 NOT GLOB '*[^0-9a-f]*'
                    )
                ),
                terminal_outcome TEXT CHECK (
                    terminal_outcome IS NULL OR terminal_outcome IN (
                        'completed', 'cancelled', 'failed', 'provider_limit'
                    )
                ),
                terminal_at TEXT,
                terminal_source TEXT CHECK (
                    terminal_source IS NULL
                    OR terminal_source IN ('recorded', 'legacy_fallback')
                ),
                CHECK (
                    (terminal_outcome IS NULL) = (terminal_at IS NULL)
                    AND (terminal_at IS NULL) = (terminal_source IS NULL)
                ),
                CHECK ((state = 'open') = (terminal_outcome IS NULL)),
                CHECK (
                    (
                        state = 'fenced'
                        AND max_calls = 0
                        AND max_micro_eur = 0
                        AND expires_at = opened_at
                        AND quota_lease_ref_sha256 IS NOT NULL
                    )
                    OR (
                        state != 'fenced'
                        AND max_calls > 0
                        AND max_micro_eur > 0
                        AND expires_at > opened_at
                    )
                ),
                PRIMARY KEY(agent, message_id)
            ) WITHOUT ROWID"""
        )

    @staticmethod
    def _create_child_turn_guards(conn: sqlite3.Connection) -> None:
        """The database guards of child_turns (schema 4): an ending is written once,
        and the binding and caps never change after open. The one exception is the
        repair of a referenced row that has a terminal state but no recorded ending
        (from a restored backup or a hand edit): empty to filled, with the fallback
        source, and the state and reason left as they are."""
        conn.execute(
            """CREATE UNIQUE INDEX child_turns_quota_lease_ref
            ON child_turns(quota_lease_ref_sha256)
            WHERE quota_lease_ref_sha256 IS NOT NULL"""
        )
        conn.execute(
            """CREATE TRIGGER child_turns_ending_frozen
            BEFORE UPDATE OF state, reason, terminal_outcome, terminal_at, terminal_source
            ON child_turns
            WHEN OLD.state != 'open' AND NOT (
                OLD.terminal_outcome IS NULL
                AND OLD.terminal_at IS NULL
                AND OLD.terminal_source IS NULL
                AND NEW.terminal_source = 'legacy_fallback'
                AND NEW.state = OLD.state
                AND NEW.reason = OLD.reason
            )
            BEGIN
                SELECT RAISE(ABORT, 'child turn ending is frozen');
            END"""
        )
        conn.execute(
            """CREATE TRIGGER child_turns_binding_immutable
            BEFORE UPDATE OF agent, message_id, request_id, max_calls, max_micro_eur,
                opened_at, expires_at, quota_lease_ref_sha256
            ON child_turns
            BEGIN
                SELECT RAISE(ABORT, 'child turn binding is immutable');
            END"""
        )
        # INSERT OR REPLACE deletes the conflicting row without firing a delete
        # trigger (recursive triggers are off) and is no UPDATE, so a conflicting
        # insert is refused before it can replace a row.
        conn.execute(
            """CREATE TRIGGER child_turns_no_replace
            BEFORE INSERT ON child_turns
            WHEN EXISTS (
                SELECT 1 FROM child_turns
                WHERE agent = NEW.agent AND message_id = NEW.message_id
            ) OR (
                NEW.quota_lease_ref_sha256 IS NOT NULL AND EXISTS (
                    SELECT 1 FROM child_turns
                    WHERE quota_lease_ref_sha256 = NEW.quota_lease_ref_sha256
                )
            )
            BEGIN
                SELECT RAISE(ABORT, 'a child turn is never replaced');
            END"""
        )
        conn.execute(
            """CREATE TRIGGER child_turns_bound_no_delete
            BEFORE DELETE ON child_turns
            WHEN OLD.quota_lease_ref_sha256 IS NOT NULL
            BEGIN
                SELECT RAISE(ABORT, 'a bound child turn is never deleted');
            END"""
        )

    @staticmethod
    def _create_receipt_tables(conn: sqlite3.Connection) -> None:
        """Receipts and pending notes: written once, kept for the life of the ledger.
        Nothing deletes or compacts them; a pending note only gets its resolved time."""
        statements = (
            """CREATE TABLE child_receipts (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                agent TEXT NOT NULL,
                message_id TEXT NOT NULL,
                quota_lease_ref_sha256 TEXT NOT NULL CHECK (
                    length(quota_lease_ref_sha256) = 64
                    AND quota_lease_ref_sha256 NOT GLOB '*[^0-9a-f]*'
                ),
                outcome TEXT NOT NULL CHECK (
                    outcome IN ('completed', 'cancelled', 'failed', 'provider_limit')
                ),
                calls INTEGER NOT NULL CHECK (calls >= 0),
                input_tokens INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
                output_tokens INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
                actual_micro_eur INTEGER NOT NULL CHECK (actual_micro_eur >= 0),
                closed_at TEXT NOT NULL,
                UNIQUE(agent, message_id),
                FOREIGN KEY(agent, message_id)
                    REFERENCES child_turns(agent, message_id)
            )""",
            """CREATE TABLE receipt_pending (
                agent TEXT NOT NULL,
                message_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                PRIMARY KEY(agent, message_id),
                FOREIGN KEY(agent, message_id)
                    REFERENCES child_turns(agent, message_id)
            ) WITHOUT ROWID""",
            """CREATE TRIGGER child_receipts_no_delete
            BEFORE DELETE ON child_receipts
            BEGIN
                SELECT RAISE(ABORT, 'receipts are permanent');
            END""",
            """CREATE TRIGGER child_receipts_no_update
            BEFORE UPDATE ON child_receipts
            BEGIN
                SELECT RAISE(ABORT, 'receipts are permanent');
            END""",
            # A replacing insert (same number or same child) is refused too: see
            # child_turns_no_replace.
            """CREATE TRIGGER child_receipts_no_replace
            BEFORE INSERT ON child_receipts
            WHEN EXISTS (
                SELECT 1 FROM child_receipts
                WHERE seq = NEW.seq
                   OR (agent = NEW.agent AND message_id = NEW.message_id)
            )
            BEGIN
                SELECT RAISE(ABORT, 'receipts are permanent');
            END""",
            """CREATE TRIGGER receipt_pending_no_delete
            BEFORE DELETE ON receipt_pending
            BEGIN
                SELECT RAISE(ABORT, 'pending receipt notes are permanent');
            END""",
            """CREATE TRIGGER receipt_pending_resolve_only
            BEFORE UPDATE ON receipt_pending
            WHEN NOT (
                OLD.resolved_at IS NULL
                AND NEW.resolved_at IS NOT NULL
                AND NEW.agent = OLD.agent
                AND NEW.message_id = OLD.message_id
                AND NEW.created_at = OLD.created_at
            )
            BEGIN
                SELECT RAISE(ABORT, 'a pending receipt note only gets its resolved time');
            END""",
            """CREATE TRIGGER receipt_pending_no_replace
            BEFORE INSERT ON receipt_pending
            WHEN EXISTS (
                SELECT 1 FROM receipt_pending
                WHERE agent = NEW.agent AND message_id = NEW.message_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'pending receipt notes are permanent');
            END""",
        )
        for statement in statements:
            conn.execute(statement)

    @staticmethod
    def _create_child_cap_schema(conn: sqlite3.Connection) -> None:
        SpendLedger._create_child_turns_table(conn, "child_turns")
        SpendLedger._create_child_turn_guards(conn)
        statements = (
            """CREATE TABLE child_capabilities (
                token_sha256 TEXT PRIMARY KEY,
                agent TEXT NOT NULL,
                message_id TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                FOREIGN KEY(agent, message_id)
                    REFERENCES child_turns(agent, message_id)
            )""",
            """CREATE TABLE child_attempts (
                attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
                agent TEXT NOT NULL,
                message_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL CHECK (ordinal > 0),
                FOREIGN KEY(agent, message_id)
                    REFERENCES child_turns(agent, message_id),
                UNIQUE(agent, message_id, ordinal)
            )""",
        )
        for statement in statements:
            conn.execute(statement)
        SpendLedger._create_receipt_tables(conn)

    def _marker(self, *, ledger_schema_version: int | None = None) -> dict:
        state = self.installation_state()
        if state == "absent":
            raise LedgerBlocked("ledger is not initialized; run explicit ledger init")
        if state == "partial":
            raise LedgerBlocked("ledger installation is partial; explicit recovery is required")
        marker = _strict_json_object(self.marker_path)
        if marker.get("schema_version") != INSTALL_MARKER_SCHEMA_VERSION:
            raise LedgerBlocked("ledger install marker schema_version mismatch")
        # Without an explicit version, either supported ledger schema: 2 (child-cap
        # schema 3, served as before) or 3 (child-cap schema 4).
        accepted = (
            (LEDGER_LEGACY_SCHEMA_VERSION, LEDGER_SCHEMA_VERSION)
            if ledger_schema_version is None
            else (ledger_schema_version,)
        )
        marker_ledger_version = marker.get("ledger_schema_version")
        if (
            not isinstance(marker_ledger_version, int)
            or isinstance(marker_ledger_version, bool)
            or marker_ledger_version not in accepted
        ):
            raise LedgerBlocked("ledger install marker ledger_schema_version mismatch")
        # price_policy_hash depends on this ledger's own chosen envelope,
        # which is not known yet at this point (the database metadata - the
        # envelope's authority - has not been opened). Validate shape here;
        # _verify_metadata cross-checks this value against the database's
        # own recomputed hash once the envelope is readable.
        marker_policy_hash = marker.get("price_policy_hash")
        if not isinstance(marker_policy_hash, str) or not re.fullmatch(
            r"[a-f0-9]{64}", marker_policy_hash
        ):
            raise LedgerBlocked("ledger install marker price_policy_hash mismatch")
        generation = marker.get("generation")
        if not isinstance(generation, str) or not _ATTEMPT_ID_RE.fullmatch(generation):
            raise LedgerBlocked("ledger install marker generation is invalid")
        opening_micro_eur = marker.get("opening_micro_eur")
        if (
            not isinstance(opening_micro_eur, int)
            or isinstance(opening_micro_eur, bool)
            or opening_micro_eur < 0
        ):
            raise LedgerBlocked("ledger install marker opening balance is invalid")
        evidence_hash = marker.get("opening_evidence_sha256")
        if (
            not isinstance(evidence_hash, str)
            or not re.fullmatch(r"[a-f0-9]{64}", evidence_hash)
        ):
            raise LedgerBlocked("ledger install marker opening evidence hash is invalid")
        _parse_utc(marker.get("opening_observed_at"))
        if not _PERIOD_RE.fullmatch(str(marker.get("opening_period") or "")):
            raise LedgerBlocked("ledger install marker opening period is invalid")
        return marker

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        marker = self._marker()
        try:
            conn = sqlite3.connect(
                f"{self.db_path.as_uri()}?mode=rw",
                uri=True,
                timeout=self.busy_timeout_seconds,
            )
        except sqlite3.Error as exc:
            raise LedgerBlocked("ledger database cannot be opened") from exc
        conn.row_factory = sqlite3.Row
        try:
            self._configure(conn)
            conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_seconds * 1000)}")
            self._verify_metadata(conn, marker)
            yield conn
        except sqlite3.Error as exc:
            raise LedgerBlocked("ledger database operation failed") from exc
        finally:
            conn.close()

    @staticmethod
    def _metadata(conn: sqlite3.Connection) -> dict[str, str]:
        return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM metadata")}

    def _verify_metadata(
        self,
        conn: sqlite3.Connection,
        marker: dict,
        *,
        ledger_schema_version: int | None = None,
    ) -> dict[str, str]:
        try:
            integrity = conn.execute("PRAGMA quick_check(1)").fetchone()
        except sqlite3.Error as exc:
            raise LedgerBlocked("ledger integrity check failed") from exc
        if not integrity or integrity[0] != "ok":
            raise LedgerBlocked("ledger integrity check is not ok")
        metadata = self._metadata(conn)
        if ledger_schema_version is None:
            ledger_schema_version = marker["ledger_schema_version"]
        expected = {
            "schema_version": str(ledger_schema_version),
            "generation": marker["generation"],
            "currency": POLICY_CURRENCY,
        }
        for key, value in expected.items():
            if metadata.get(key) != value:
                raise LedgerBlocked(f"ledger metadata {key} mismatch")
        paired = _CHILD_CAP_SCHEMA_FOR_LEDGER.get(ledger_schema_version)
        child_cap_version = metadata.get("child_cap_schema_version")
        if paired is not None and child_cap_version != str(paired) and not (
            # as before binding: a ledger-schema-2 ledger may lack the child caps
            ledger_schema_version == LEDGER_LEGACY_SCHEMA_VERSION
            and child_cap_version is None
        ):
            raise LedgerBlocked("ledger schema and child cap schema versions do not match")
        # The envelope (trial cutoff / soft-stop / external ceiling) is
        # chosen once at init and stored here - never compared against the
        # current module defaults, which would wrongly reject a deliberately
        # non-default install (e.g. a 40 EUR VM envelope). Its
        # price_policy_hash IS recomputed from the STORED envelope plus the
        # live (non-envelope) pricing constants, so a genuine repricing of
        # rates/margins/token limits still fails closed; only the envelope
        # choice itself is exempt from "must match live code" - a different
        # envelope requires re-init, never a silent match against whatever
        # the CLI's current default happens to be.
        stored_cutoff = self._parse_envelope_int(metadata, "trial_cutoff_micro_eur")
        stored_soft_stop = self._parse_envelope_int(metadata, "soft_stop_micro_eur")
        stored_ceiling = self._parse_envelope_int(metadata, "external_ceiling_micro_eur")
        if not stored_soft_stop < stored_cutoff <= stored_ceiling:
            raise LedgerBlocked("ledger metadata envelope is invalid")
        recomputed_hash = price_policy_hash(
            trial_cutoff_micro_eur=stored_cutoff,
            soft_stop_micro_eur=stored_soft_stop,
            external_ceiling_micro_eur=stored_ceiling,
        )
        if metadata.get("price_policy_hash") != recomputed_hash:
            raise LedgerBlocked("ledger metadata price_policy_hash mismatch")
        if marker.get("price_policy_hash") != recomputed_hash:
            raise LedgerBlocked("ledger install marker price_policy_hash mismatch")
        _parse_utc(metadata.get("initialized_at"))
        _parse_utc(metadata.get("last_accepted_utc"))
        if not _PERIOD_RE.fullmatch(metadata.get("last_accepted_period", "")):
            raise LedgerBlocked("ledger last accepted period is invalid")
        raw_opening = metadata.get("opening_micro_eur")
        try:
            opening_micro_eur = int(raw_opening or "")
        except ValueError as exc:
            raise LedgerBlocked("ledger opening balance is invalid") from exc
        if str(opening_micro_eur) != raw_opening or opening_micro_eur < 0:
            raise LedgerBlocked("ledger opening balance is invalid")
        try:
            opening_evidence = self._opening_evidence(metadata.get("opening_evidence"))
            self._assert_external_envelope(
                opening_micro_eur,
                trial_cutoff_micro_eur=stored_cutoff,
                external_ceiling_micro_eur=stored_ceiling,
            )
        except (ValueError, PolicyBlocked) as exc:
            raise LedgerBlocked("ledger opening balance envelope is invalid") from exc
        observed_at = metadata.get("opening_observed_at")
        observed = _parse_utc(observed_at)
        opening_period = metadata.get("opening_period", "")
        if not _PERIOD_RE.fullmatch(opening_period) or opening_period != _period(observed):
            raise LedgerBlocked("ledger opening period is invalid")
        if (
            marker.get("opening_micro_eur") != opening_micro_eur
            or marker.get("opening_observed_at") != observed_at
            or marker.get("opening_period") != opening_period
            or marker.get("opening_evidence_sha256")
            != hashlib.sha256(opening_evidence.encode("utf-8")).hexdigest()
        ):
            raise LedgerBlocked("ledger opening balance does not match the install marker")
        opening_row = conn.execute(
            "SELECT committed_micro_eur FROM periods WHERE period=?",
            (opening_period,),
        ).fetchone()
        if opening_row is None or int(opening_row[0]) < opening_micro_eur:
            raise LedgerBlocked("ledger opening balance is not represented in its period")
        canary_keys = (
            "canary_attempt_id",
            "canary_checked_at",
            "canary_expected_micro_eur",
            "canary_observed_micro_eur",
            "canary_tolerance_micro_eur",
            "canary_status",
        )
        canary_present = [key in metadata for key in canary_keys]
        if any(canary_present) and not all(canary_present):
            raise LedgerBlocked("dashboard canary metadata is incomplete")
        if all(canary_present):
            attempt_id = metadata["canary_attempt_id"]
            try:
                self._validate_attempt_id(attempt_id)
            except ValueError as exc:
                raise LedgerBlocked("dashboard canary attempt id is invalid") from exc
            _parse_utc(metadata["canary_checked_at"])
            try:
                expected = int(metadata["canary_expected_micro_eur"])
                observed = int(metadata["canary_observed_micro_eur"])
                tolerance = int(metadata["canary_tolerance_micro_eur"])
            except ValueError as exc:
                raise LedgerBlocked("dashboard canary amount is invalid") from exc
            if (
                str(expected) != metadata["canary_expected_micro_eur"]
                or str(observed) != metadata["canary_observed_micro_eur"]
                or str(tolerance) != metadata["canary_tolerance_micro_eur"]
                or expected <= 0
                or observed < 0
                or tolerance <= 0
            ):
                raise LedgerBlocked("dashboard canary amount is invalid")
            status = metadata["canary_status"]
            mathematically_accepted = observed > 0 and abs(observed - expected) <= tolerance
            if status not in {"accepted", "mismatch"} or (
                (status == "accepted") != mathematically_accepted
            ):
                raise LedgerBlocked("dashboard canary status is invalid")
            canary_attempt = conn.execute(
                "SELECT state, actual_micro_eur, model, policy_hash "
                "FROM attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if (
                canary_attempt is None
                or canary_attempt["state"] != "settled"
                or int(canary_attempt["actual_micro_eur"] or 0) != expected
                or canary_attempt["model"] != MODEL_ALIAS
                or canary_attempt["policy_hash"] != recomputed_hash
            ):
                raise LedgerBlocked("dashboard canary attempt binding is invalid")
        return metadata

    @staticmethod
    def _child_cap_table_names(conn: sqlite3.Connection) -> set[str]:
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'child_%'"
            )
        }

    def _child_cap_feature_state(
        self,
        conn: sqlite3.Connection,
        metadata: dict[str, str] | None = None,
    ) -> str:
        metadata = metadata or self._metadata(conn)
        schema_value = metadata.get("child_cap_schema_version")
        policy_value = metadata.get("child_cap_policy_hash")
        issuer_value = metadata.get("child_cap_issuer_sha256")
        tables = self._child_cap_table_names(conn)
        all_child_tables = set(_CHILD_CAP_TABLES_V4)
        if (
            schema_value is None
            and policy_value is None
            and issuer_value is None
            and not (tables & all_child_tables)
            and not self._receipt_pending_exists(conn)
        ):
            return "absent"
        if schema_value == str(CHILD_CAP_SCHEMA_VERSION):
            version = CHILD_CAP_SCHEMA_VERSION
        elif schema_value == str(CHILD_CAP_LEGACY_SCHEMA_VERSION):
            version = CHILD_CAP_LEGACY_SCHEMA_VERSION
        else:
            raise LedgerBlocked("child cap schema version is missing or mismatched")
        # child_turn_max_micro_eur is this ledger's own pinned envelope value
        # (default: equal to its trial cutoff) - never the live module
        # default, for the same reason price_policy_hash is recomputed from
        # the stored envelope in _verify_metadata rather than compared to it.
        child_turn_max_micro_eur = self._parse_envelope_int(
            metadata, "child_turn_max_micro_eur"
        )
        if policy_value != child_cap_policy_hash(
            child_turn_max_micro_eur=child_turn_max_micro_eur,
            schema_version=version,
        ):
            raise LedgerBlocked("child cap policy hash is missing or mismatched")
        if not isinstance(issuer_value, str) or not re.fullmatch(
            r"[a-f0-9]{64}", issuer_value
        ):
            raise LedgerBlocked("child cap issuer authority is missing or invalid")
        if version == CHILD_CAP_LEGACY_SCHEMA_VERSION:
            # Exactly the schema-3 checks; receipt tables on a schema-3 ledger are
            # a half-done migration, never a valid shape.
            if not set(_CHILD_CAP_TABLES) <= tables:
                raise LedgerBlocked("child cap schema is partial")
            if "child_receipts" in tables or self._receipt_pending_exists(conn):
                raise LedgerBlocked("child cap schema is partial")
            self._check_child_cap_v3_rows(conn, child_turn_max_micro_eur)
            return "ready"
        if not all_child_tables <= tables or not self._receipt_pending_exists(conn):
            raise LedgerBlocked("child cap schema is partial")
        self._check_child_cap_v4(conn, metadata, child_turn_max_micro_eur)
        return "ready"

    @staticmethod
    def _receipt_pending_exists(conn: sqlite3.Connection) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='receipt_pending'"
        ).fetchone() is not None

    @staticmethod
    def _child_cap_version(metadata: Mapping[str, str]) -> int:
        """3 or 4, for a ledger whose child-cap feature state is ``ready``."""
        if metadata.get("child_cap_schema_version") == str(CHILD_CAP_SCHEMA_VERSION):
            return CHILD_CAP_SCHEMA_VERSION
        return CHILD_CAP_LEGACY_SCHEMA_VERSION

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
        return tuple(str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})"))

    @staticmethod
    def _binding_required_flag(metadata: Mapping[str, str]) -> bool:
        """The stored binding flag. Anything but "0" or "1" is malformed, never off."""
        value = metadata.get("quota_lease_binding_required")
        if value == "0":
            return False
        if value == "1":
            return True
        raise LedgerBlocked("quota lease binding flag is missing or malformed")

    def _check_child_cap_v4(
        self,
        conn: sqlite3.Connection,
        metadata: Mapping[str, str],
        child_turn_max_micro_eur: int,
    ) -> None:
        """Schema 4: the new columns, the guards, the flag, and per-row caps that
        are never above the ledger ceilings (zero only for a fenced row)."""
        self._binding_required_flag(metadata)
        expected_columns = {
            "child_turns": _CHILD_TURN_COLUMNS_V4,
            "child_capabilities": ("token_sha256", "agent", "message_id", "issued_at"),
            "child_attempts": ("attempt_id", "agent", "message_id", "ordinal"),
            "child_receipts": _RECEIPT_COLUMNS,
            "receipt_pending": _RECEIPT_PENDING_COLUMNS,
        }
        for table, expected in expected_columns.items():
            if self._table_columns(conn, table) != expected:
                raise LedgerBlocked(f"child cap table {table} has an unexpected shape")
        guards = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        indexes = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        if not set(_CHILD_CAP_GUARDS) <= guards or "child_turns_quota_lease_ref" not in indexes:
            raise LedgerBlocked("child cap guards are missing")
        invalid_turn = conn.execute(
            """
            SELECT 1 FROM child_turns
            WHERE state NOT IN ('open', 'capped', 'expired', 'fenced')
               OR (state = 'fenced' AND (max_calls != 0 OR max_micro_eur != 0))
               OR (state != 'fenced' AND (
                    max_calls <= 0 OR max_calls > ?
                    OR max_micro_eur <= 0 OR max_micro_eur > ?
               ))
            LIMIT 1
            """,
            (CHILD_TURN_MAX_CALLS, child_turn_max_micro_eur),
        ).fetchone()
        if invalid_turn is not None:
            raise LedgerBlocked("child cap turn policy binding is invalid")
        for row in conn.execute(
            "SELECT state, opened_at, expires_at, updated_at, terminal_at FROM child_turns"
        ):
            opened = _parse_utc(row["opened_at"])
            expires = _parse_utc(row["expires_at"])
            _parse_utc(row["updated_at"])
            if row["terminal_at"] is not None:
                _parse_utc(row["terminal_at"])
            window = expires - opened
            if row["state"] == "fenced":
                if window != timedelta(0):
                    raise LedgerBlocked("child cap turn expiry binding is invalid")
            elif not timedelta(0) < window <= timedelta(seconds=CHILD_TURN_MAX_SECONDS):
                raise LedgerBlocked("child cap turn expiry binding is invalid")
        for row in conn.execute("SELECT token_sha256, issued_at FROM child_capabilities"):
            if not _SHA256_HEX_RE.fullmatch(str(row["token_sha256"])):
                raise LedgerBlocked("child cap capability hash is invalid")
            _parse_utc(row["issued_at"])

    def _check_child_cap_v3_rows(
        self, conn: sqlite3.Connection, child_turn_max_micro_eur: int
    ) -> None:
        """Schema 3, unchanged: fixed caps and exactly 24 hours per row."""
        expected_columns = {
            "child_turns": _CHILD_TURN_COLUMNS_V3,
            "child_capabilities": (
                "token_sha256",
                "agent",
                "message_id",
                "issued_at",
            ),
            "child_attempts": (
                "attempt_id",
                "agent",
                "message_id",
                "ordinal",
            ),
        }
        for table, expected in expected_columns.items():
            observed = tuple(
                str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
            )
            if observed != expected:
                raise LedgerBlocked(f"child cap table {table} has an unexpected shape")
        invalid_turn = conn.execute(
            """
            SELECT 1 FROM child_turns
            WHERE state NOT IN ('open', 'capped', 'expired')
               OR max_calls != ? OR max_micro_eur != ?
            LIMIT 1
            """,
            (CHILD_TURN_MAX_CALLS, child_turn_max_micro_eur),
        ).fetchone()
        if invalid_turn is not None:
            raise LedgerBlocked("child cap turn policy binding is invalid")
        for row in conn.execute(
            "SELECT opened_at, expires_at, updated_at FROM child_turns"
        ):
            opened = _parse_utc(row["opened_at"])
            expires = _parse_utc(row["expires_at"])
            _parse_utc(row["updated_at"])
            if expires - opened != timedelta(seconds=CHILD_TURN_MAX_SECONDS):
                raise LedgerBlocked("child cap turn expiry binding is invalid")
        for row in conn.execute(
            "SELECT token_sha256, issued_at FROM child_capabilities"
        ):
            if not re.fullmatch(r"[a-f0-9]{64}", str(row["token_sha256"])):
                raise LedgerBlocked("child cap capability hash is invalid")
            _parse_utc(row["issued_at"])

    def install_child_caps(self, *, issuer_token: str | None = None) -> dict:
        """Migrate a v1 ledger to the downgrade-fenced child-cap schema."""
        issuer_hash = self._child_cap_issuer_hash(issuer_token)
        if self.installation_state() != "complete":
            raise LedgerBlocked(
                "child cap install requires a complete existing ledger"
            )
        raw_marker = _strict_json_object(self.marker_path)
        marker_version = raw_marker.get("ledger_schema_version")
        if marker_version in (LEDGER_LEGACY_SCHEMA_VERSION, LEDGER_SCHEMA_VERSION):
            with self._connect() as conn:
                metadata = self._verify_metadata(conn, self._marker())
                if self._child_cap_feature_state(conn, metadata) != "ready":
                    raise LedgerBlocked(
                        "current ledger schema is missing the child cap feature"
                    )
                if not hmac.compare_digest(
                    metadata["child_cap_issuer_sha256"], issuer_hash
                ):
                    raise ChildTurnCapBlocked(
                        "child turn issuer credential is invalid"
                    )
            return {
                "installed": False,
                "schema_version": self._child_cap_version(metadata),
                "policy_hash": metadata["child_cap_policy_hash"],
            }
        if marker_version != 1:
            raise LedgerBlocked("child cap install requires ledger schema v1 or v2")

        # Commit the database version first. During the small marker-update
        # window, old code sees metadata v2 and new code sees marker v1, so both
        # fail closed. A retry recognizes that transitional pair and finishes
        # the durable marker projection.
        marker = self._marker(ledger_schema_version=1)
        try:
            conn = sqlite3.connect(
                f"{self.db_path.as_uri()}?mode=rw",
                uri=True,
                timeout=self.busy_timeout_seconds,
            )
        except sqlite3.Error as exc:
            raise LedgerBlocked("ledger database cannot be opened") from exc
        conn.row_factory = sqlite3.Row
        # The ledger schema the marker is moved to once the database is committed.
        target_version = LEDGER_SCHEMA_VERSION
        try:
            self._configure(conn)
            conn.execute(
                f"PRAGMA busy_timeout={int(self.busy_timeout_seconds * 1000)}"
            )
            self._begin(conn)
            try:
                raw_metadata = self._metadata(conn)
                database_version = raw_metadata.get("schema_version")
                if database_version == "1":
                    metadata = self._verify_metadata(
                        conn, marker, ledger_schema_version=1
                    )
                    if self._unresolved(conn):
                        raise LedgerHold(
                            "child cap install requires all provider attempts reconciled"
                        )
                    state = self._child_cap_feature_state(conn, metadata)
                    if state == "absent":
                        # A genuinely pre-envelope v1 ledger never chose an
                        # envelope at all - it has no child_turn_max_micro_eur
                        # key, and the live module default IS the right
                        # source for it (this migration predates any
                        # custom-envelope choice a v1 ledger could ever have
                        # made). But a ledger that already went through
                        # THIS feature's own initialize() and only had its
                        # child-cap-specific keys stripped (e.g. a test
                        # fixture simulating "v1") already has this key -
                        # reuse it rather than re-deriving and re-inserting,
                        # which would collide with the existing row (the
                        # actual bug behind PR #188's red: an unconditional
                        # INSERT here raised sqlite3.IntegrityError: UNIQUE
                        # constraint failed on metadata.key).
                        existing_child_turn_max = raw_metadata.get(
                            "child_turn_max_micro_eur"
                        )
                        if existing_child_turn_max is None:
                            migrated_child_turn_max_micro_eur = CHILD_TURN_MAX_MICRO_EUR
                        else:
                            migrated_child_turn_max_micro_eur = self._parse_envelope_int(
                                raw_metadata, "child_turn_max_micro_eur"
                            )
                        self._create_child_cap_schema(conn)
                        metadata_rows = [
                            (
                                "child_cap_schema_version",
                                str(CHILD_CAP_SCHEMA_VERSION),
                            ),
                            (
                                "child_cap_policy_hash",
                                child_cap_policy_hash(
                                    child_turn_max_micro_eur=migrated_child_turn_max_micro_eur
                                ),
                            ),
                            ("child_cap_issuer_sha256", issuer_hash),
                            ("quota_lease_binding_required", "0"),
                        ]
                        if existing_child_turn_max is None:
                            metadata_rows.append((
                                "child_turn_max_micro_eur",
                                str(migrated_child_turn_max_micro_eur),
                            ))
                        conn.executemany(
                            "INSERT INTO metadata(key, value) VALUES (?, ?)",
                            metadata_rows,
                        )
                    else:
                        if not hmac.compare_digest(
                            metadata["child_cap_issuer_sha256"], issuer_hash
                        ):
                            raise ChildTurnCapBlocked(
                                "child turn issuer credential is invalid"
                            )
                    conn.execute(
                        "UPDATE metadata SET value=? WHERE key='schema_version'",
                        (str(LEDGER_SCHEMA_VERSION),),
                    )
                elif database_version == str(LEDGER_SCHEMA_VERSION):
                    metadata = self._verify_metadata(
                        conn, marker, ledger_schema_version=LEDGER_SCHEMA_VERSION
                    )
                    if self._child_cap_feature_state(conn, metadata) != "ready":
                        raise LedgerBlocked(
                            "migrated ledger is missing the child cap feature"
                        )
                    if not hmac.compare_digest(
                        metadata["child_cap_issuer_sha256"], issuer_hash
                    ):
                        raise ChildTurnCapBlocked(
                            "child turn issuer credential is invalid"
                        )
                elif database_version == str(LEDGER_LEGACY_SCHEMA_VERSION):
                    # The same pair left by the code before binding: finish its
                    # marker projection exactly as that code would.
                    metadata = self._verify_metadata(
                        conn, marker, ledger_schema_version=LEDGER_LEGACY_SCHEMA_VERSION
                    )
                    if self._child_cap_feature_state(conn, metadata) != "ready":
                        raise LedgerBlocked(
                            "migrated ledger is missing the child cap feature"
                        )
                    if not hmac.compare_digest(
                        metadata["child_cap_issuer_sha256"], issuer_hash
                    ):
                        raise ChildTurnCapBlocked(
                            "child turn issuer credential is invalid"
                        )
                    target_version = LEDGER_LEGACY_SCHEMA_VERSION
                else:
                    raise LedgerBlocked(
                        "ledger schema cannot be migrated to the child cap feature"
                    )
                self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise
        except sqlite3.Error as exc:
            raise LedgerBlocked("ledger database operation failed") from exc
        finally:
            conn.close()

        migrated_marker = dict(marker)
        migrated_marker["ledger_schema_version"] = target_version
        _durable_write_json(self.marker_path, migrated_marker)
        # Re-verify fresh rather than reusing the in-memory `metadata` snapshot
        # above: in the "absent" branch that snapshot predates the very INSERT
        # that added child_cap_policy_hash, so it would not have the key.
        with self._connect() as conn:
            final_metadata = self._verify_metadata(conn, self._marker())
        return {
            "installed": True,
            "schema_version": self._child_cap_version(final_metadata),
            "policy_hash": final_metadata["child_cap_policy_hash"],
        }

    def install_child_cap_binding(
        self,
        *,
        issuer_token: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        """Migrate a child-cap schema-3 ledger to schema 4 (quota lease binding).

        Explicit only: the operator's ``gateway binding-install`` is its one caller.
        Two steps. The database step checks everything before it changes anything
        and runs in one transaction, so a refusal or a fault in it leaves schema 3
        exactly as it was. Then the install marker moves to ledger schema 3. A
        failure between the two leaves the database upgraded and the marker old:
        every reader and writer, older or newer, refuses that pair, and the next
        authenticated run finishes the marker (see
        test_a_failed_marker_move_is_refused_and_finished_by_a_second_run). It
        refuses while any provider attempt is unresolved and after a clock rollback.
        History is kept: every row, cap, reason and time is copied; an open row
        keeps empty endings, and an expired or capped row gets the stated fallback
        ending. The binding flag starts off. A second run changes nothing.
        """
        issuer_hash = self._child_cap_issuer_hash(issuer_token)
        current = (now or self.now()).astimezone(timezone.utc)
        marker = self._marker()
        installed = False
        if marker["ledger_schema_version"] == LEDGER_LEGACY_SCHEMA_VERSION:
            # The database commits first; the install marker follows. In between,
            # code from before binding sees database schema 3 and this code sees
            # marker schema 2, so both refuse; a second run finishes the marker.
            installed = self._install_child_cap_binding_database(marker, issuer_hash, current)
            migrated_marker = dict(marker)
            migrated_marker["ledger_schema_version"] = LEDGER_SCHEMA_VERSION
            _durable_write_json(self.marker_path, migrated_marker)
        with self._connect() as conn:
            final = self._verify_metadata(conn, self._marker())
            self._check_child_cap_issuer(final, issuer_hash)
            if (
                self._child_cap_feature_state(conn, final) != "ready"
                or self._child_cap_version(final) != CHILD_CAP_SCHEMA_VERSION
            ):
                raise LedgerBlocked("binding install did not reach child cap schema 4")
            required = self._binding_required_flag(final)
        return {
            "installed": installed,
            "schema_version": CHILD_CAP_SCHEMA_VERSION,
            "policy_hash": final["child_cap_policy_hash"],
            "quota_lease_binding_required": required,
        }

    def _install_child_cap_binding_database(
        self, marker: dict, issuer_hash: str, current: datetime
    ) -> bool:
        """The database half of install_child_cap_binding, in one transaction.
        Returns True when it migrated, or when it found the database already
        migrated by a run whose marker projection did not finish."""
        try:
            conn = sqlite3.connect(
                f"{self.db_path.as_uri()}?mode=rw",
                uri=True,
                timeout=self.busy_timeout_seconds,
            )
        except sqlite3.Error as exc:
            raise LedgerBlocked("ledger database cannot be opened") from exc
        conn.row_factory = sqlite3.Row
        try:
            self._configure(conn)
            conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_seconds * 1000)}")
            # PRAGMA foreign_keys has no effect inside a transaction: off before BEGIN.
            conn.execute("PRAGMA foreign_keys=OFF")
            self._begin(conn)
            try:
                _binding_migration_checkpoint(conn, "begin")
                database_version = self._metadata(conn).get("schema_version")
                if database_version == str(LEDGER_LEGACY_SCHEMA_VERSION):
                    metadata = self._verify_metadata(conn, marker)
                    if self._child_cap_feature_state(conn, metadata) != "ready":
                        raise LedgerBlocked("child cap feature is not installed")
                    self._check_child_cap_issuer(metadata, issuer_hash)
                    if self._unresolved(conn):
                        raise LedgerHold(
                            "binding install requires all provider attempts resolved"
                        )
                    self._validate_ledger_clock_rollback(metadata, current)
                    self._validate_child_cap_clock(conn, current)
                    self._migrate_child_cap_v3_to_v4(conn, metadata)
                    self._commit(conn)
                elif database_version == str(LEDGER_SCHEMA_VERSION):
                    metadata = self._verify_metadata(
                        conn, marker, ledger_schema_version=LEDGER_SCHEMA_VERSION
                    )
                    if self._child_cap_feature_state(conn, metadata) != "ready":
                        raise LedgerBlocked("child cap feature is not installed")
                    self._check_child_cap_issuer(metadata, issuer_hash)
                    self._rollback(conn)
                else:
                    raise LedgerBlocked("ledger schema cannot be migrated to quota lease binding")
            except Exception:
                self._rollback(conn)
                raise
            conn.execute("PRAGMA foreign_keys=ON")
            _binding_migration_checkpoint(conn, "foreign_keys_on")
        except sqlite3.Error as exc:
            raise LedgerBlocked("ledger database operation failed") from exc
        finally:
            conn.close()
        return True

    @staticmethod
    def _check_child_cap_issuer(metadata: Mapping[str, str], issuer_hash: str) -> None:
        if not hmac.compare_digest(metadata["child_cap_issuer_sha256"], issuer_hash):
            raise ChildTurnCapBlocked("child turn issuer credential is invalid")

    @staticmethod
    def _validate_ledger_clock_rollback(metadata: Mapping[str, str], current: datetime) -> None:
        """Refuse a clock behind the ledger's own accounting clock: its
        initialization and its last accepted admission. The child rows alone are not
        enough, because a ledger can have none."""
        for key in ("initialized_at", "last_accepted_utc"):
            if current < _parse_utc(metadata[key]):
                raise LedgerHold("clock rollback detected; explicit reconciliation is required")

    def _migrate_child_cap_v3_to_v4(
        self, conn: sqlite3.Connection, metadata: Mapping[str, str]
    ) -> None:
        """Rebuild child_turns in SQLite's documented order (new table, copy, drop,
        rename, then indexes and triggers), add the receipt tables, check every
        foreign key, then write the new version and policy hash. Foreign keys are
        off for the whole transaction (set by the caller before BEGIN)."""
        self._create_child_turns_table(conn, "child_turns_v4")
        conn.execute(
            """
            INSERT INTO child_turns_v4(
                agent, message_id, request_id, state, max_calls, max_micro_eur,
                opened_at, expires_at, updated_at, reason, quota_lease_ref_sha256,
                terminal_outcome, terminal_at, terminal_source
            )
            SELECT agent, message_id, request_id, state, max_calls, max_micro_eur,
                   opened_at, expires_at, updated_at, reason, NULL,
                   CASE state WHEN 'expired' THEN 'cancelled'
                              WHEN 'capped' THEN 'failed' END,
                   CASE WHEN state = 'open' THEN NULL ELSE updated_at END,
                   CASE WHEN state = 'open' THEN NULL ELSE 'legacy_fallback' END
            FROM child_turns
            """
        )
        old_count = conn.execute("SELECT COUNT(*) FROM child_turns").fetchone()[0]
        new_count = conn.execute("SELECT COUNT(*) FROM child_turns_v4").fetchone()[0]
        if old_count != new_count:
            raise LedgerBlocked("binding install could not copy every child turn")
        _binding_migration_checkpoint(conn, "copied")
        conn.execute("DROP TABLE child_turns")
        conn.execute("ALTER TABLE child_turns_v4 RENAME TO child_turns")
        _binding_migration_checkpoint(conn, "renamed")
        self._create_child_turn_guards(conn)
        self._create_receipt_tables(conn)
        _binding_migration_checkpoint(conn, "guards")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise LedgerBlocked("binding install found a broken foreign key")
        child_turn_max_micro_eur = self._parse_envelope_int(
            metadata, "child_turn_max_micro_eur"
        )
        conn.execute(
            "UPDATE metadata SET value=? WHERE key='child_cap_schema_version'",
            (str(CHILD_CAP_SCHEMA_VERSION),),
        )
        conn.execute(
            "UPDATE metadata SET value=? WHERE key='child_cap_policy_hash'",
            (child_cap_policy_hash(child_turn_max_micro_eur=child_turn_max_micro_eur),),
        )
        conn.execute(
            "INSERT INTO metadata(key, value) VALUES ('quota_lease_binding_required', '0')"
        )
        # The fence: every writer from before binding requires ledger schema 2.
        conn.execute(
            "UPDATE metadata SET value=? WHERE key='schema_version'",
            (str(LEDGER_SCHEMA_VERSION),),
        )
        _binding_migration_checkpoint(conn, "metadata")

    @staticmethod
    def _begin(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise LedgerBlocked("ledger writer lock could not be acquired") from exc

    def _commit(self, conn: sqlite3.Connection) -> None:
        # PRAGMA synchronous=FULL makes SQLite's successful commit return the
        # sole durability authority for ledger transactions. A second file
        # flush after commit can fail after the terminal row is already visible,
        # falsely reporting failure while leaving the ledger ready.
        try:
            conn.commit()
        except sqlite3.Error as exc:
            raise LedgerBlocked("ledger transaction commit failed") from exc

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        with contextlib.suppress(sqlite3.Error):
            conn.rollback()

    @staticmethod
    def _unresolved(conn: sqlite3.Connection) -> list[sqlite3.Row]:
        return list(
            conn.execute(
                """
                SELECT * FROM attempts
                WHERE state IN (?, ?)
                ORDER BY admitted_at
                """,
                ("reserved", "uncertain"),
            )
        )

    @staticmethod
    def _validate_child_cap_clock(
        conn: sqlite3.Connection, current: datetime
    ) -> None:
        observations = [
            _parse_utc(row[0])
            for row in conn.execute("SELECT updated_at FROM child_turns")
        ]
        if observations and current < max(observations):
            raise LedgerHold(
                "child turn clock rollback detected; explicit reconciliation is required"
            )

    def _observe_child_attempt_clock(
        self,
        conn: sqlite3.Connection,
        *,
        attempt_id: str,
        current: datetime,
        timestamp: str,
    ) -> None:
        self._validate_child_cap_clock(conn, current)
        child = conn.execute(
            """
            SELECT agent, message_id FROM child_attempts WHERE attempt_id=?
            """,
            (attempt_id,),
        ).fetchone()
        if child is not None:
            conn.execute(
                """
                UPDATE child_turns SET updated_at=?
                WHERE agent=? AND message_id=?
                """,
                (timestamp, child["agent"], child["message_id"]),
            )

    # --- schema 4: every child-turn ending goes through one path ---------------------

    @staticmethod
    def _freeze_terminal(
        conn: sqlite3.Connection,
        agent: str,
        message_id: str,
        *,
        state: str,
        reason: str,
        outcome: str,
        at: str,
        updated_at: str,
    ) -> bool:
        """(a) Keep the first ending. An open row gets its terminal state, reason and
        the three ending columns (source ``recorded``); a row that already ended gets
        nothing at all. True when this call wrote the ending."""
        if outcome not in CHILD_OUTCOMES:
            raise ValueError("child turn outcome is not a closed word")
        cursor = conn.execute(
            """
            UPDATE child_turns
            SET state=?, reason=?, updated_at=?, terminal_outcome=?, terminal_at=?,
                terminal_source='recorded'
            WHERE agent=? AND message_id=? AND state='open'
            """,
            (state, reason, updated_at, outcome, at, agent, message_id),
        )
        return cursor.rowcount == 1

    def _ensure_custody(
        self, conn: sqlite3.Connection, agent: str, message_id: str, *, at: str
    ) -> str:
        """(b) Make sure an ended, referenced child turn has its receipt or a pending
        note. Idempotent; runs even when (a) wrote nothing. Returns one word:
        ``unbound`` or ``open`` (nothing to do), ``receipt_exists``, ``pending``
        (an attempt of this child is still unresolved; it keeps its liability), or
        ``receipt_written`` (the receipt and the resolved note, in this transaction)."""
        row = conn.execute(
            """
            SELECT state, reason, quota_lease_ref_sha256, terminal_outcome, terminal_at
            FROM child_turns WHERE agent=? AND message_id=?
            """,
            (agent, message_id),
        ).fetchone()
        if row is None or row["quota_lease_ref_sha256"] is None:
            return "unbound"
        if row["state"] == "open":
            return "open"
        if conn.execute(
            "SELECT 1 FROM child_receipts WHERE agent=? AND message_id=?",
            (agent, message_id),
        ).fetchone() is not None:
            return "receipt_exists"
        if row["terminal_outcome"] is None:
            row = self._repair_missing_ending(conn, agent, message_id)
        totals = self._child_receipt_totals(conn, agent, message_id)
        if totals is None:
            if conn.execute(
                "SELECT 1 FROM receipt_pending WHERE agent=? AND message_id=?",
                (agent, message_id),
            ).fetchone() is None:
                conn.execute(
                    "INSERT INTO receipt_pending(agent, message_id, created_at) VALUES (?, ?, ?)",
                    (agent, message_id, at),
                )
            return "pending"
        calls, input_tokens, output_tokens, actual_micro_eur = totals
        # The next number is taken under the writer lock: no gap, no reuse.
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM child_receipts").fetchone()[0]
        conn.execute(
            """
            INSERT INTO child_receipts(
                seq, agent, message_id, quota_lease_ref_sha256, outcome, calls,
                input_tokens, output_tokens, actual_micro_eur, closed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                seq,
                agent,
                message_id,
                row["quota_lease_ref_sha256"],
                row["terminal_outcome"],
                calls,
                input_tokens,
                output_tokens,
                actual_micro_eur,
                row["terminal_at"],
            ),
        )
        conn.execute(
            """
            UPDATE receipt_pending SET resolved_at=?
            WHERE agent=? AND message_id=? AND resolved_at IS NULL
            """,
            (at, agent, message_id),
        )
        return "receipt_written"

    @staticmethod
    def _repair_missing_ending(
        conn: sqlite3.Connection, agent: str, message_id: str
    ) -> sqlite3.Row:
        """A referenced row that ended without a recorded ending (only a restored
        backup or a hand edit makes one) gets the stated fallback: ``expired`` means
        cancelled, ``capped`` means failed, at the row's own last update, with the
        source ``legacy_fallback``. State and reason stay; a recorded value is never
        changed (the database guard allows only empty to filled here)."""
        conn.execute(
            """
            UPDATE child_turns
            SET terminal_outcome = CASE state WHEN 'capped' THEN 'failed' ELSE 'cancelled' END,
                terminal_at = updated_at,
                terminal_source = 'legacy_fallback'
            WHERE agent=? AND message_id=? AND state != 'open'
              AND terminal_outcome IS NULL AND terminal_at IS NULL AND terminal_source IS NULL
            """,
            (agent, message_id),
        )
        return conn.execute(
            """
            SELECT state, reason, quota_lease_ref_sha256, terminal_outcome, terminal_at
            FROM child_turns WHERE agent=? AND message_id=?
            """,
            (agent, message_id),
        ).fetchone()

    @staticmethod
    def _child_receipt_totals(
        conn: sqlite3.Connection, agent: str, message_id: str
    ) -> tuple[int, int | None, int | None, int] | None:
        """The receipt's totals over this child's attempts, or None while any attempt
        is unresolved. An attempt reconciled ``no-send`` is not a call and adds
        nothing. A token total is None when any counted attempt never recorded its
        usage; the money is always known once every attempt is resolved."""
        rows = list(
            conn.execute(
                """
                SELECT attempt.state, attempt.input_tokens, attempt.output_tokens,
                       attempt.actual_micro_eur,
                       (SELECT outcome FROM reconciliations AS rec
                        WHERE rec.attempt_id = attempt.attempt_id
                        ORDER BY rec.id DESC LIMIT 1) AS reconcile_outcome
                FROM child_attempts AS child
                JOIN attempts AS attempt ON attempt.attempt_id = child.attempt_id
                WHERE child.agent=? AND child.message_id=?
                ORDER BY child.ordinal
                """,
                (agent, message_id),
            )
        )
        if any(row["state"] in _UNRESOLVED_ATTEMPT_STATES for row in rows):
            return None
        calls = 0
        actual_micro_eur = 0
        inputs: list[int | None] = []
        outputs: list[int | None] = []
        for row in rows:
            if row["state"] == "reconciled":
                if row["reconcile_outcome"] not in ("no-send", "charge-reserve"):
                    raise LedgerBlocked("a reconciled child attempt has no reconciliation record")
                if row["reconcile_outcome"] == "no-send":
                    continue
            elif row["state"] != "settled":
                raise LedgerBlocked("a child attempt has an unknown state")
            if row["actual_micro_eur"] is None:
                raise LedgerBlocked("a resolved child attempt has no recorded charge")
            calls += 1
            actual_micro_eur += int(row["actual_micro_eur"])
            inputs.append(row["input_tokens"])
            outputs.append(row["output_tokens"])
        input_tokens = None if any(v is None for v in inputs) else sum(int(v) for v in inputs)
        output_tokens = None if any(v is None for v in outputs) else sum(int(v) for v in outputs)
        return calls, input_tokens, output_tokens, actual_micro_eur

    def _custody_for_attempt(
        self, conn: sqlite3.Connection, attempt_id: str, *, at: str
    ) -> None:
        """The resolving step after settle and reconcile: (b) for the child that owns
        the attempt, on a schema-4 ledger."""
        version = conn.execute(
            "SELECT value FROM metadata WHERE key='child_cap_schema_version'"
        ).fetchone()
        if version is None or version[0] != str(CHILD_CAP_SCHEMA_VERSION):
            return
        child = conn.execute(
            "SELECT agent, message_id FROM child_attempts WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if child is not None:
            self._ensure_custody(conn, child["agent"], child["message_id"], at=at)

    @staticmethod
    def _validate_attempt_id(attempt_id: str) -> None:
        if not isinstance(attempt_id, str) or not _ATTEMPT_ID_RE.fullmatch(attempt_id):
            raise ValueError("attempt_id must be 32 lowercase hexadecimal characters")

    @staticmethod
    def _validate_child_scope(value: object, *, name: str, limit: int) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > limit
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise ValueError(f"{name} must be a non-empty printable string of at most {limit} characters")
        return value

    @staticmethod
    def _capability_hash(capability: object) -> str:
        if not isinstance(capability, str) or not _CHILD_CAPABILITY_RE.fullmatch(capability):
            raise ChildTurnCapBlocked("child turn capability is missing or malformed")
        return hashlib.sha256(capability.encode("ascii")).hexdigest()

    @staticmethod
    def _child_cap_issuer_hash(issuer_token: object) -> str:
        if not isinstance(issuer_token, str) or not _FRONT_TOKEN_RE.fullmatch(
            issuer_token
        ):
            raise ChildTurnCapBlocked(
                "child turn issuer credential is missing or malformed"
            )
        return hashlib.sha256(issuer_token.encode("ascii")).hexdigest()

    def verify_child_cap_issuer(self, issuer_token: str | None) -> None:
        """Fail closed unless the current front token owns child-cap minting."""
        issuer_hash = self._child_cap_issuer_hash(issuer_token)
        with self._connect() as conn:
            metadata = self._verify_metadata(conn, self._marker())
            if self._child_cap_feature_state(conn, metadata) != "ready":
                raise ChildTurnCapBlocked("child turn cap feature is not installed")
            if not hmac.compare_digest(
                metadata["child_cap_issuer_sha256"], issuer_hash
            ):
                raise ChildTurnCapBlocked(
                    "front token does not match the child turn issuer authority"
                )

    @staticmethod
    def _binding_request(
        quota_lease_ref: object,
        max_calls: object,
        max_micro_eur: object,
        ttl_seconds: object,
    ) -> dict | None:
        """The binding keywords of an open, checked for shape (the ceilings need the
        ledger), or None when none was given: then the open is today's call."""
        if (
            quota_lease_ref is None
            and max_calls is None
            and max_micro_eur is None
            and ttl_seconds is None
        ):
            return None
        if quota_lease_ref is None:
            raise ChildTurnCapBlocked("per-row caps need a quota lease reference")
        ref_sha256 = quota_lease_ref_sha256(quota_lease_ref)
        for name, value in (
            ("max_calls", max_calls),
            ("max_micro_eur", max_micro_eur),
            ("ttl_seconds", ttl_seconds),
        ):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value <= 0
            ):
                raise ChildTurnCapBlocked(f"{name} must be a positive integer")
        return {
            "ref_sha256": ref_sha256,
            "max_calls": max_calls,
            "max_micro_eur": max_micro_eur,
            "ttl_seconds": ttl_seconds,
        }

    @staticmethod
    def _effective_binding(binding: dict, child_turn_max_micro_eur: int) -> dict:
        """The caps a binding stores. A null cap means the ledger ceiling and a
        missing ttl 24 hours; a value above a ceiling is refused, never lowered."""
        max_calls = CHILD_TURN_MAX_CALLS if binding["max_calls"] is None else binding["max_calls"]
        max_micro_eur = (
            child_turn_max_micro_eur
            if binding["max_micro_eur"] is None
            else binding["max_micro_eur"]
        )
        ttl_seconds = (
            CHILD_TURN_MAX_SECONDS if binding["ttl_seconds"] is None else binding["ttl_seconds"]
        )
        if max_calls > CHILD_TURN_MAX_CALLS:
            raise ChildTurnCapBlocked("max_calls is above the ledger ceiling")
        if max_micro_eur > child_turn_max_micro_eur:
            raise ChildTurnCapBlocked("max_micro_eur is above the ledger ceiling")
        if ttl_seconds > CHILD_TURN_MAX_SECONDS:
            raise ChildTurnCapBlocked("ttl_seconds is above the ledger ceiling")
        return {
            "ref_sha256": binding["ref_sha256"],
            "max_calls": max_calls,
            "max_micro_eur": max_micro_eur,
            "ttl_seconds": ttl_seconds,
        }

    @staticmethod
    def _check_retry_binding(row: sqlite3.Row, effective: dict | None) -> None:
        """A retry brings exactly the binding its row was opened with: a bound row
        refuses an open without it (never a quiet fall back to the defaults), and an
        unbound row refuses a reference (a row is never rebound)."""
        bound = row["quota_lease_ref_sha256"]
        if bound is None:
            if effective is not None:
                raise ChildTurnCapBlocked(
                    "a quota lease reference cannot bind an already opened unbound child turn"
                )
            return
        if effective is None:
            raise ChildTurnCapBlocked(
                "a bound child turn must present its quota lease binding on every open"
            )
        window = _parse_utc(row["expires_at"]) - _parse_utc(row["opened_at"])
        if not (
            hmac.compare_digest(bound, effective["ref_sha256"])
            and int(row["max_calls"]) == effective["max_calls"]
            and int(row["max_micro_eur"]) == effective["max_micro_eur"]
            and window == timedelta(seconds=effective["ttl_seconds"])
        ):
            raise ChildTurnCapBlocked(
                "child turn quota lease binding changed for an immutable message"
            )

    def open_child_turn(
        self,
        *,
        agent: str,
        message_id: str,
        request_id: str = "",
        issuer_token: str | None = None,
        now: datetime | None = None,
        quota_lease_ref: str | None = None,
        max_calls: int | None = None,
        max_micro_eur: int | None = None,
        ttl_seconds: int | None = None,
    ) -> ChildTurnCredential:
        """Issue an opaque capability bound to one durable wrapper message.

        Without the binding keywords this is exactly the call it has always been: no
        quota lease reference, the ledger's caps and 24 hours. On a child-cap schema-4
        ledger, ``quota_lease_ref`` binds the turn to one turn admission with per-row
        caps (a null cap means the ledger ceiling, a missing ``ttl_seconds`` 24 hours).
        A cap above a ceiling is refused, a retry must bring the same binding, and
        the binding flag, rechecked here under the writer lock, refuses an unbound
        open while it is on."""
        agent = self._validate_child_scope(agent, name="agent", limit=128)
        message_id = self._validate_child_scope(
            message_id, name="message_id", limit=256
        )
        if request_id:
            request_id = self._validate_child_scope(
                request_id, name="request_id", limit=256
            )
        elif not isinstance(request_id, str):
            raise ValueError("request_id must be a string")
        binding = self._binding_request(quota_lease_ref, max_calls, max_micro_eur, ttl_seconds)
        current = (now or self.now()).astimezone(timezone.utc)
        issuer_hash = self._child_cap_issuer_hash(issuer_token)
        timestamp = _iso_utc(current)
        expires_at = _iso_utc(current + timedelta(seconds=CHILD_TURN_MAX_SECONDS))
        token = "atgw-child-" + secrets.token_urlsafe(32)
        token_hash = self._capability_hash(token)
        with self._connect() as conn:
            self._begin(conn)
            try:
                marker = self._marker()
                metadata = self._verify_metadata(conn, marker)
                if self._child_cap_feature_state(conn, metadata) != "ready":
                    raise ChildTurnCapBlocked("child turn cap feature is not installed")
                if not hmac.compare_digest(
                    metadata["child_cap_issuer_sha256"], issuer_hash
                ):
                    raise ChildTurnCapBlocked("child turn issuer credential is invalid")
                version = self._child_cap_version(metadata)
                if binding is not None and version != CHILD_CAP_SCHEMA_VERSION:
                    raise ChildTurnCapBlocked(
                        "quota lease binding is not installed on this ledger"
                    )
                self._validate_child_cap_clock(conn, current)
                self._validate_clock(metadata, current)
                # Refuse to MINT a turn while transport is held (durable accounting hold, or
                # an unresolved prior attempt) - mirrors the reserve-side gate in
                # _reserve_locked. Otherwise a turn opened under a hold starts its wall-time
                # ceiling immediately and burns the whole budget while blocked, so it is
                # already (near-)expired the instant the hold clears (#63: the held-gateway
                # child turn that expired mid-work, surfacing as a misleading config_blocked).
                # A held mint raises LedgerHold (a GatewayError) -> the wrapper spawner treats it
                # as a blocked turn, so NO doomed turn is opened and no wall-time is wasted.
                # (The LEDGER permits a fresh full-window mint once the hold clears. The wrapper
                # currently maps this to config_blocked and parks the head WITHOUT re-driving, so
                # recovery after the hold clears is not yet automatic - #62 makes a transient
                # gateway hold retry-through (INFRA) so the parked head self-heals on clear.)
                if metadata.get("service_hold"):
                    raise LedgerHold("gateway has a durable accounting hold")
                if self._unresolved(conn):
                    raise LedgerHold("an unresolved provider attempt blocks new transport")
                child_turn_max_micro_eur = self._parse_envelope_int(
                    metadata, "child_turn_max_micro_eur"
                )
                effective = (
                    None
                    if binding is None
                    else self._effective_binding(binding, child_turn_max_micro_eur)
                )
                row = conn.execute(
                    "SELECT * FROM child_turns WHERE agent=? AND message_id=?",
                    (agent, message_id),
                ).fetchone()
                if version == CHILD_CAP_SCHEMA_VERSION:
                    binding_required = self._binding_required_flag(metadata)
                    if row is not None:
                        if row["state"] == "fenced":
                            raise ChildTurnCapExceeded("child turn binding is fenced")
                        if row["request_id"] != request_id:
                            raise ChildTurnCapBlocked(
                                "child turn request binding changed for an immutable message"
                            )
                        self._check_retry_binding(row, effective)
                        bound = row["quota_lease_ref_sha256"] is not None
                    else:
                        bound = effective is not None
                    if binding_required and not bound:
                        raise QuotaLeaseBindingRequired(
                            "quota lease binding is required; this child turn has no reference"
                        )
                if row is None and effective is None:
                    conn.execute(
                        """
                        INSERT INTO child_turns(
                            agent, message_id, request_id, state, max_calls,
                            max_micro_eur, opened_at, expires_at, updated_at
                        ) VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?)
                        """,
                        (
                            agent,
                            message_id,
                            request_id,
                            CHILD_TURN_MAX_CALLS,
                            child_turn_max_micro_eur,
                            timestamp,
                            expires_at,
                            timestamp,
                        ),
                    )
                elif row is None:
                    expires_at = _iso_utc(
                        current + timedelta(seconds=effective["ttl_seconds"])
                    )
                    try:
                        conn.execute(
                            """
                            INSERT INTO child_turns(
                                agent, message_id, request_id, state, max_calls,
                                max_micro_eur, opened_at, expires_at, updated_at,
                                quota_lease_ref_sha256
                            ) VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                agent,
                                message_id,
                                request_id,
                                effective["max_calls"],
                                effective["max_micro_eur"],
                                timestamp,
                                expires_at,
                                timestamp,
                                effective["ref_sha256"],
                            ),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise ChildTurnCapBlocked(
                            "quota lease reference already binds another child turn"
                        ) from exc
                else:
                    if row["request_id"] != request_id:
                        raise ChildTurnCapBlocked(
                            "child turn request binding changed for an immutable message"
                        )
                    latest_observation = _parse_utc(row["updated_at"])
                    if current < latest_observation:
                        raise LedgerHold(
                            "child turn clock rollback detected; explicit reconciliation "
                            "is required"
                        )
                    expiry = _parse_utc(row["expires_at"])
                    if row["state"] != "open" or current >= expiry:
                        if version == CHILD_CAP_SCHEMA_VERSION:
                            self._freeze_terminal(
                                conn,
                                agent,
                                message_id,
                                state="expired",
                                reason="wall-time ceiling exceeded",
                                outcome="cancelled",
                                at=row["expires_at"],
                                updated_at=timestamp,
                            )
                            self._ensure_custody(conn, agent, message_id, at=timestamp)
                            self._commit(conn)
                        elif row["state"] == "open":
                            conn.execute(
                                """
                                UPDATE child_turns
                                SET state='expired', reason='wall-time ceiling exceeded',
                                    updated_at=?
                                WHERE agent=? AND message_id=?
                                """,
                                (timestamp, agent, message_id),
                            )
                            self._commit(conn)
                        raise ChildTurnCapExceeded(
                            str(row["reason"] or "child turn wall-time ceiling exceeded")
                        )
                    expires_at = row["expires_at"]
                    conn.execute(
                        """
                        UPDATE child_turns SET updated_at=?
                        WHERE agent=? AND message_id=?
                        """,
                        (timestamp, agent, message_id),
                    )
                try:
                    conn.execute(
                        """
                        INSERT INTO child_capabilities(
                            token_sha256, agent, message_id, issued_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (token_hash, agent, message_id, timestamp),
                    )
                except sqlite3.IntegrityError as exc:
                    raise LedgerBlocked("child turn capability collision") from exc
                self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise
        return ChildTurnCredential(token, agent, message_id, expires_at)

    def close_child_turn(
        self,
        *,
        agent: str,
        message_id: str,
        reason: str,
        issuer_token: str | None = None,
        now: datetime | None = None,
        outcome: str | None = None,
        quota_lease_ref: str | None = None,
    ) -> str | None:
        """Close (expire) an OPEN child turn immediately, instead of leaving it to run
        out its own CHILD_TURN_MAX_SECONDS wall clock. Call this only when the caller
        is DONE driving a durable message and will not retry the SAME (agent,
        message_id) scope again (e.g. a dead-letter dispose) - never on an ordinary
        attempt failure the caller intends to retry, since retries of the SAME message
        deliberately reuse and accumulate against the SAME open turn (its PRIMARY KEY
        is (agent, message_id); there is no way to reopen a fresh one for an existing
        key once closed). State-machine-safe: a no-op unless the row is currently
        'open' - closing an already-capped/expired turn, or one that was never opened,
        does nothing (mirrors the lazy terminal transitions in open_child_turn /
        reserve_for_child; this just makes the SAME transition eager instead of lazy
        rather than inventing a new state outside the schema's CHECK constraint).

        On a child-cap schema-4 ledger the ending is also recorded (outcome and time;
        ``cancelled`` when a legacy call names none). A bound child turn (opened with a
        quota lease reference) closes only with its own reference and an explicit
        outcome, even when it already ended; a different reference is the permanent
        ``reference_mismatch``. A close with a reference for a key that never opened
        writes a fenced row and its zero receipt in this one transaction, so that
        binding can never spend later. A reference given for an unbound row is
        ignored and writes no receipt. A legacy call (no ``outcome``, no reference)
        returns None, as before; a call with either returns one closed word:
        ``closed``, ``already_terminal``, ``fenced``, ``reference_ignored`` or
        ``not_opened``."""
        agent = self._validate_child_scope(agent, name="agent", limit=128)
        message_id = self._validate_child_scope(
            message_id, name="message_id", limit=256
        )
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("a close reason is required")
        if outcome is not None and outcome not in CHILD_OUTCOMES:
            raise ValueError("close outcome is not a closed word")
        ref_sha256 = None if quota_lease_ref is None else quota_lease_ref_sha256(quota_lease_ref)
        legacy = outcome is None and quota_lease_ref is None
        current = (now or self.now()).astimezone(timezone.utc)
        issuer_hash = self._child_cap_issuer_hash(issuer_token)
        timestamp = _iso_utc(current)
        with self._connect() as conn:
            self._begin(conn)
            try:
                marker = self._marker()
                metadata = self._verify_metadata(conn, marker)
                if self._child_cap_feature_state(conn, metadata) != "ready":
                    raise ChildTurnCapBlocked("child turn cap feature is not installed")
                if not hmac.compare_digest(
                    metadata["child_cap_issuer_sha256"], issuer_hash
                ):
                    raise ChildTurnCapBlocked("child turn issuer credential is invalid")
                self._validate_child_cap_clock(conn, current)
                if self._child_cap_version(metadata) != CHILD_CAP_SCHEMA_VERSION:
                    if ref_sha256 is not None:
                        raise ChildTurnCapBlocked(
                            "quota lease binding is not installed on this ledger"
                        )
                    row = conn.execute(
                        "SELECT state FROM child_turns WHERE agent=? AND message_id=?",
                        (agent, message_id),
                    ).fetchone()
                    wrote = False
                    if row is not None and row["state"] == "open":
                        conn.execute(
                            """
                            UPDATE child_turns SET state='expired', reason=?, updated_at=?
                            WHERE agent=? AND message_id=?
                            """,
                            (reason, timestamp, agent, message_id),
                        )
                        wrote = True
                    self._commit(conn)
                    if row is None:
                        result = "not_opened"
                    else:
                        result = "closed" if wrote else "already_terminal"
                else:
                    result = self._close_child_turn_v4(
                        conn,
                        agent,
                        message_id,
                        reason=reason,
                        outcome=outcome,
                        ref_sha256=ref_sha256,
                        timestamp=timestamp,
                    )
                    self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise
        return None if legacy else result

    def _close_child_turn_v4(
        self,
        conn: sqlite3.Connection,
        agent: str,
        message_id: str,
        *,
        reason: str,
        outcome: str | None,
        ref_sha256: str | None,
        timestamp: str,
    ) -> str:
        row = conn.execute(
            "SELECT state, quota_lease_ref_sha256 FROM child_turns WHERE agent=? AND message_id=?",
            (agent, message_id),
        ).fetchone()
        if row is None:
            if ref_sha256 is None:
                return "not_opened"
            if outcome is None:
                raise ChildTurnCapBlocked(
                    "a close with a quota lease reference needs an explicit outcome"
                )
            # The tombstone: a binding that never opened can never spend later.
            try:
                conn.execute(
                    """
                    INSERT INTO child_turns(
                        agent, message_id, request_id, state, max_calls, max_micro_eur,
                        opened_at, expires_at, updated_at, reason, quota_lease_ref_sha256,
                        terminal_outcome, terminal_at, terminal_source
                    ) VALUES (?, ?, '', 'fenced', 0, 0, ?, ?, ?, ?, ?, ?, ?, 'recorded')
                    """,
                    (
                        agent,
                        message_id,
                        timestamp,
                        timestamp,
                        timestamp,
                        reason,
                        ref_sha256,
                        outcome,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ChildTurnCapBlocked(
                    "quota lease reference already binds another child turn"
                ) from exc
            self._ensure_custody(conn, agent, message_id, at=timestamp)
            return "fenced"
        bound = row["quota_lease_ref_sha256"]
        if bound is not None:
            if ref_sha256 is None or outcome is None:
                raise ChildTurnCapBlocked(
                    "a bound child turn closes only with its quota lease reference and an outcome"
                )
            if not hmac.compare_digest(bound, ref_sha256):
                raise QuotaLeaseReferenceMismatch("reference_mismatch")
            wrote = self._freeze_terminal(
                conn,
                agent,
                message_id,
                state="expired",
                reason=reason,
                outcome=outcome,
                at=timestamp,
                updated_at=timestamp,
            )
            self._ensure_custody(conn, agent, message_id, at=timestamp)
            return "closed" if wrote else "already_terminal"
        wrote = self._freeze_terminal(
            conn,
            agent,
            message_id,
            state="expired",
            reason=reason,
            outcome=outcome or "cancelled",
            at=timestamp,
            updated_at=timestamp,
        )
        if ref_sha256 is not None:
            return "reference_ignored"
        return "closed" if wrote else "already_terminal"

    def _advance_period_if_valid(
        self,
        conn: sqlite3.Connection,
        metadata: dict[str, str],
        now: datetime,
    ) -> str:
        admitted_period = self._validate_clock(metadata, now)
        if admitted_period == metadata["last_accepted_period"]:
            return admitted_period
        conn.execute(
            "INSERT OR IGNORE INTO periods(period, committed_micro_eur) VALUES (?, 0)",
            (admitted_period,),
        )
        return admitted_period

    @staticmethod
    def _validate_clock(metadata: dict[str, str], now: datetime) -> str:
        admitted_period = _period(now)
        prior_period = metadata["last_accepted_period"]
        prior_time = _parse_utc(metadata["last_accepted_utc"])
        if now < prior_time:
            raise LedgerHold("clock rollback detected; explicit reconciliation is required")
        if now - prior_time > timedelta(days=40):
            raise LedgerHold("clock or billing period jumped implausibly; explicit advance required")
        if admitted_period == prior_period:
            return admitted_period
        if admitted_period != _next_period(prior_period):
            raise LedgerHold("billing period skipped or moved backward; explicit advance required")
        return admitted_period

    def _reserve_locked(
        self,
        conn: sqlite3.Connection,
        *,
        attempt_id: str,
        model: str,
        current: datetime,
        timestamp: str,
        reserve: int,
        metadata: dict[str, str],
        child_scope: tuple[str, str, int] | None = None,
    ) -> Reservation:
        if metadata.get("service_hold"):
            raise LedgerHold("gateway has a durable accounting hold")
        unresolved = self._unresolved(conn)
        if unresolved:
            raise LedgerHold("an unresolved provider attempt blocks new transport")
        period = self._advance_period_if_valid(conn, metadata, current)
        row = conn.execute(
            "SELECT committed_micro_eur FROM periods WHERE period=?", (period,)
        ).fetchone()
        if row is None:
            raise LedgerBlocked("admission period is missing")
        unresolved_total = sum(int(item["reserved_micro_eur"]) for item in unresolved)
        projected = int(row[0]) + unresolved_total + reserve
        opening_allowance = (
            int(metadata["opening_micro_eur"])
            if period == metadata["opening_period"]
            else 0
        )
        # Both figures are THIS ledger's own pinned envelope (metadata,
        # verified by _verify_metadata), never the live module defaults -
        # a 40 EUR VM install must be held to its own 40 EUR cutoff, not
        # whatever today's code-default cutoff happens to be.
        trial_cutoff_micro_eur = self._parse_envelope_int(
            metadata, "trial_cutoff_micro_eur"
        )
        external_ceiling_micro_eur = self._parse_envelope_int(
            metadata, "external_ceiling_micro_eur"
        )
        if projected > opening_allowance + trial_cutoff_micro_eur:
            raise PolicyBlocked("trial spend cutoff would be exceeded")
        cumulative_row = conn.execute(
            "SELECT COALESCE(SUM(committed_micro_eur), 0) FROM periods"
        ).fetchone()
        cumulative_projected = int(cumulative_row[0]) + unresolved_total + reserve
        if cumulative_projected > external_ceiling_micro_eur:
            raise PolicyBlocked("external ceiling would be exceeded")
        try:
            conn.execute(
                """
                INSERT INTO attempts(
                    attempt_id, period, model, policy_hash, reserved_micro_eur,
                    state, admitted_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'reserved', ?, ?)
                """,
                (
                    attempt_id,
                    period,
                    model,
                    metadata["price_policy_hash"],
                    reserve,
                    timestamp,
                    timestamp,
                ),
            )
            if child_scope is not None:
                child_agent, child_message_id, ordinal = child_scope
                conn.execute(
                    """
                    INSERT INTO child_attempts(attempt_id, agent, message_id, ordinal)
                    VALUES (?, ?, ?, ?)
                    """,
                    (attempt_id, child_agent, child_message_id, ordinal),
                )
        except sqlite3.IntegrityError as exc:
            raise LedgerBlocked("attempt_id or child turn ordinal already exists") from exc
        conn.execute(
            "UPDATE metadata SET value=? WHERE key='last_accepted_utc'", (timestamp,)
        )
        conn.execute(
            "UPDATE metadata SET value=? WHERE key='last_accepted_period'", (period,)
        )
        return Reservation(attempt_id, period, reserve, timestamp)

    def reserve(
        self,
        attempt_id: str,
        *,
        model: str = MODEL_ALIAS,
        now: datetime | None = None,
    ) -> Reservation:
        self._validate_attempt_id(attempt_id)
        if model != MODEL_ALIAS:
            raise PolicyBlocked("model alias is not allowed by the price policy")
        current = (now or self.now()).astimezone(timezone.utc)
        timestamp = _iso_utc(current)
        reserve = reservation_cost_micro_eur()
        with self._connect() as conn:
            self._begin(conn)
            try:
                marker = self._marker()
                metadata = self._verify_metadata(conn, marker)
                reservation = self._reserve_locked(
                    conn,
                    attempt_id=attempt_id,
                    model=model,
                    current=current,
                    timestamp=timestamp,
                    reserve=reserve,
                    metadata=metadata,
                )
                self._commit(conn)
                return reservation
            except Exception:
                self._rollback(conn)
                raise

    def reserve_for_child(
        self,
        attempt_id: str,
        *,
        capability: str,
        model: str = MODEL_ALIAS,
        now: datetime | None = None,
    ) -> Reservation:
        """Atomically consume a child-turn slot and reserve provider exposure."""
        self._validate_attempt_id(attempt_id)
        capability_hash = self._capability_hash(capability)
        if model != MODEL_ALIAS:
            raise PolicyBlocked("model alias is not allowed by the price policy")
        current = (now or self.now()).astimezone(timezone.utc)
        timestamp = _iso_utc(current)
        reserve = reservation_cost_micro_eur()
        denial: str | None = None
        reservation: Reservation | None = None
        with self._connect() as conn:
            self._begin(conn)
            try:
                marker = self._marker()
                metadata = self._verify_metadata(conn, marker)
                if self._child_cap_feature_state(conn, metadata) != "ready":
                    raise ChildTurnCapBlocked("child turn cap feature is not installed")
                self._validate_child_cap_clock(conn, current)
                row = conn.execute(
                    """
                    SELECT turn.*
                    FROM child_capabilities AS capability
                    JOIN child_turns AS turn
                      ON turn.agent=capability.agent
                     AND turn.message_id=capability.message_id
                    WHERE capability.token_sha256=?
                    """,
                    (capability_hash,),
                ).fetchone()
                if row is None:
                    raise ChildTurnCapBlocked("child turn capability is unknown")
                opened = _parse_utc(row["opened_at"])
                latest_observation = _parse_utc(row["updated_at"])
                expiry = _parse_utc(row["expires_at"])
                if current < opened or current < latest_observation:
                    raise LedgerHold(
                        "child turn clock rollback detected; explicit reconciliation is required"
                    )
                if row["state"] != "open":
                    denial = str(row["reason"] or "child turn is no longer open")
                elif current >= expiry:
                    denial = "child turn wall-time ceiling exceeded"
                counts = conn.execute(
                    """
                    SELECT COUNT(*) AS attempt_count,
                           COALESCE(SUM(
                               CASE
                                   WHEN attempt.actual_micro_eur IS NOT NULL
                                   THEN attempt.actual_micro_eur
                                   ELSE attempt.reserved_micro_eur
                               END
                           ), 0) AS exposure_micro_eur
                    FROM child_attempts AS child
                    JOIN attempts AS attempt ON attempt.attempt_id=child.attempt_id
                    WHERE child.agent=? AND child.message_id=?
                    """,
                    (row["agent"], row["message_id"]),
                ).fetchone()
                attempt_count = int(counts["attempt_count"])
                exposure = int(counts["exposure_micro_eur"])
                if denial is None and attempt_count >= int(row["max_calls"]):
                    denial = "child turn call ceiling exceeded"
                if denial is None and exposure + reserve > int(row["max_micro_eur"]):
                    denial = "child turn cost ceiling exceeded"
                if denial is not None:
                    state = "expired" if current >= expiry else "capped"
                    if self._child_cap_version(metadata) == CHILD_CAP_SCHEMA_VERSION:
                        # The first ending stays: an already ended row is not rewritten.
                        self._freeze_terminal(
                            conn,
                            str(row["agent"]),
                            str(row["message_id"]),
                            state=state,
                            reason=denial,
                            outcome="cancelled" if current >= expiry else "failed",
                            at=row["expires_at"] if current >= expiry else timestamp,
                            updated_at=timestamp,
                        )
                        self._ensure_custody(
                            conn, str(row["agent"]), str(row["message_id"]), at=timestamp
                        )
                    else:
                        conn.execute(
                            """
                            UPDATE child_turns
                            SET state=?, reason=?, updated_at=?
                            WHERE agent=? AND message_id=?
                            """,
                            (state, denial, timestamp, row["agent"], row["message_id"]),
                        )
                    self._commit(conn)
                else:
                    conn.execute(
                        """
                        UPDATE child_turns SET updated_at=?
                        WHERE agent=? AND message_id=?
                        """,
                        (timestamp, row["agent"], row["message_id"]),
                    )
                    reservation = self._reserve_locked(
                        conn,
                        attempt_id=attempt_id,
                        model=model,
                        current=current,
                        timestamp=timestamp,
                        reserve=reserve,
                        metadata=metadata,
                        child_scope=(
                            str(row["agent"]),
                            str(row["message_id"]),
                            attempt_count + 1,
                        ),
                    )
                    self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise
        if denial is not None:
            raise ChildTurnCapExceeded(denial)
        if reservation is None:
            raise LedgerBlocked("child turn reservation did not reach a terminal state")
        return reservation

    def mark_uncertain(self, attempt_id: str, *, reason: str, now: datetime | None = None) -> None:
        self._validate_attempt_id(attempt_id)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("an uncertainty reason is required")
        current = (now or self.now()).astimezone(timezone.utc)
        timestamp = _iso_utc(current)
        with self._connect() as conn:
            self._begin(conn)
            try:
                row = conn.execute(
                    "SELECT state FROM attempts WHERE attempt_id=?", (attempt_id,)
                ).fetchone()
                if row is None:
                    raise LedgerBlocked("attempt does not exist")
                if row["state"] in _TERMINAL_ATTEMPT_STATES:
                    return
                self._observe_child_attempt_clock(
                    conn,
                    attempt_id=attempt_id,
                    current=current,
                    timestamp=timestamp,
                )
                conn.execute(
                    """
                    UPDATE attempts SET state='uncertain', reason=?, updated_at=?
                    WHERE attempt_id=?
                    """,
                    (reason.strip()[:512], timestamp, attempt_id),
                )
                self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise

    def settle(
        self,
        attempt_id: str,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        now: datetime | None = None,
    ) -> dict:
        self._validate_attempt_id(attempt_id)
        if model != MODEL_ALIAS:
            self.mark_uncertain(attempt_id, reason="response model mismatch", now=now)
            raise LedgerHold("response model does not match the price policy")
        if (
            not isinstance(input_tokens, int)
            or isinstance(input_tokens, bool)
            or input_tokens <= 0
            or not isinstance(output_tokens, int)
            or isinstance(output_tokens, bool)
            or output_tokens <= 0
        ):
            self.mark_uncertain(attempt_id, reason="invalid or missing usage", now=now)
            raise LedgerHold("response usage is invalid")
        current = (now or self.now()).astimezone(timezone.utc)
        timestamp = _iso_utc(current)
        actual = settlement_cost_micro_eur(input_tokens, output_tokens)
        with self._connect() as conn:
            self._begin(conn)
            try:
                row = conn.execute(
                    "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
                ).fetchone()
                if row is None:
                    raise LedgerBlocked("attempt does not exist")
                if row["state"] != "reserved":
                    raise LedgerHold("only a reserved attempt can settle automatically")
                marker = self._marker()
                metadata = self._verify_metadata(conn, marker)
                self._validate_clock(metadata, current)
                self._observe_child_attempt_clock(
                    conn,
                    attempt_id=attempt_id,
                    current=current,
                    timestamp=timestamp,
                )
                over_limit = (
                    input_tokens > MAX_CONTEXT_TOKENS
                    or output_tokens > MAX_OUTPUT_TOKENS
                    or actual > int(row["reserved_micro_eur"])
                )
                conn.execute(
                    "UPDATE periods SET committed_micro_eur=committed_micro_eur+? WHERE period=?",
                    (actual, row["period"]),
                )
                state = "uncertain" if over_limit else "settled"
                reason = "reported usage exceeded reserved policy" if over_limit else ""
                conn.execute(
                    """
                    UPDATE attempts
                    SET state=?, input_tokens=?, output_tokens=?, actual_micro_eur=?,
                        updated_at=?, reason=?
                    WHERE attempt_id=?
                    """,
                    (state, input_tokens, output_tokens, actual, timestamp, reason, attempt_id),
                )
                if over_limit:
                    conn.execute(
                        "UPDATE metadata SET value=? WHERE key='service_hold'",
                        (f"attempt {attempt_id} exceeded the reserved policy",),
                    )
                self._custody_for_attempt(conn, attempt_id, at=timestamp)
                self._commit(conn)
                return {
                    "attempt_id": attempt_id,
                    "state": state,
                    "period": row["period"],
                    "actual_micro_eur": actual,
                    "held": over_limit,
                }
            except Exception:
                self._rollback(conn)
                raise

    def reconcile(
        self,
        attempt_id: str,
        *,
        outcome: str,
        reason: str,
        now: datetime | None = None,
    ) -> dict:
        self._validate_attempt_id(attempt_id)
        if outcome not in {"no-send", "charge-reserve"}:
            raise ValueError("outcome must be no-send or charge-reserve")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("a reconciliation reason is required")
        current = (now or self.now()).astimezone(timezone.utc)
        timestamp = _iso_utc(current)
        with self._connect() as conn:
            self._begin(conn)
            try:
                row = conn.execute(
                    "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
                ).fetchone()
                if row is None:
                    raise LedgerBlocked("attempt does not exist")
                if row["state"] not in _UNRESOLVED_ATTEMPT_STATES:
                    raise LedgerBlocked("attempt is not unresolved")
                self._observe_child_attempt_clock(
                    conn,
                    attempt_id=attempt_id,
                    current=current,
                    timestamp=timestamp,
                )
                already = int(row["actual_micro_eur"] or 0)
                if outcome == "no-send":
                    if already:
                        raise LedgerBlocked("no-send cannot erase an already recorded charge")
                    desired = 0
                else:
                    desired = max(already, int(row["reserved_micro_eur"]))
                incremental = max(0, desired - already)
                if incremental:
                    conn.execute(
                        "UPDATE periods SET committed_micro_eur=committed_micro_eur+? WHERE period=?",
                        (incremental, row["period"]),
                    )
                conn.execute(
                    """
                    UPDATE attempts SET state='reconciled', actual_micro_eur=?,
                        reason=?, updated_at=? WHERE attempt_id=?
                    """,
                    (desired, reason.strip()[:512], timestamp, attempt_id),
                )
                conn.execute(
                    """
                    INSERT INTO reconciliations(
                        attempt_id, outcome, prior_state, charged_micro_eur,
                        reason, reconciled_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        outcome,
                        row["state"],
                        incremental,
                        reason.strip()[:512],
                        timestamp,
                    ),
                )
                remaining = self._unresolved(conn)
                metadata = self._metadata(conn)
                if (
                    not remaining
                    and metadata.get("service_hold", "").startswith(
                        f"attempt {attempt_id} "
                    )
                ):
                    conn.execute("UPDATE metadata SET value='' WHERE key='service_hold'")
                self._custody_for_attempt(conn, attempt_id, at=timestamp)
                self._commit(conn)
                return {
                    "attempt_id": attempt_id,
                    "state": "reconciled",
                    "outcome": outcome,
                    "charged_micro_eur": incremental,
                    "total_actual_micro_eur": desired,
                }
            except Exception:
                self._rollback(conn)
                raise

    def place_hold(self, *, reason: str, now: datetime | None = None) -> dict:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("a hold reason is required")
        current = (now or self.now()).astimezone(timezone.utc)
        timestamp = _iso_utc(current)
        value = "manual: " + reason.strip()[:500]
        with self._connect() as conn:
            self._begin(conn)
            try:
                conn.execute("UPDATE metadata SET value=? WHERE key='service_hold'", (value,))
                conn.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES ('hold_set_at', ?)",
                    (timestamp,),
                )
                self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise
        return {"held": True, "reason": value, "held_at": timestamp}

    def clear_hold(self, *, reason: str, now: datetime | None = None) -> dict:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("a clear-hold reason is required")
        current = (now or self.now()).astimezone(timezone.utc)
        timestamp = _iso_utc(current)
        with self._connect() as conn:
            self._begin(conn)
            try:
                if self._unresolved(conn):
                    raise LedgerHold("unresolved attempts must be reconciled before clearing hold")
                metadata = self._metadata(conn)
                prior = metadata.get("service_hold") or ""
                conn.execute("UPDATE metadata SET value='' WHERE key='service_hold'")
                conn.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES ('hold_cleared_at', ?)",
                    (timestamp,),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES ('hold_clear_reason', ?)",
                    (reason.strip()[:512],),
                )
                self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise
        return {"held": False, "prior_hold": prior or None, "cleared_at": timestamp}

    def verify_dashboard_canary(
        self,
        attempt_id: str,
        *,
        observed_delta_micro_eur: int,
        now: datetime | None = None,
    ) -> dict:
        """Persist a fail-closed live dashboard comparison for one settled call."""
        self._validate_attempt_id(attempt_id)
        if (
            not isinstance(observed_delta_micro_eur, int)
            or isinstance(observed_delta_micro_eur, bool)
            or observed_delta_micro_eur < 0
        ):
            raise ValueError("observed dashboard delta must be non-negative micro-EUR")
        current = (now or self.now()).astimezone(timezone.utc)
        timestamp = _iso_utc(current)
        with self._connect() as conn:
            self._begin(conn)
            try:
                metadata = self._verify_metadata(conn, self._marker())
                row = conn.execute(
                    "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
                ).fetchone()
                if row is None:
                    raise LedgerBlocked("attempt does not exist")
                expected = int(row["actual_micro_eur"] or 0)
                if (
                    row["state"] != "settled"
                    or row["model"] != MODEL_ALIAS
                    or row["policy_hash"] != metadata["price_policy_hash"]
                    or expected <= 0
                ):
                    raise LedgerBlocked(
                        "dashboard canary requires a positive policy-matched settled attempt"
                    )
                self._observe_child_attempt_clock(
                    conn,
                    attempt_id=attempt_id,
                    current=current,
                    timestamp=timestamp,
                )
                tolerance = max(
                    1,
                    (
                        expected * CANARY_TOLERANCE_BPS
                        + 10_000
                        - 1
                    )
                    // 10_000,
                )
                accepted = (
                    observed_delta_micro_eur > 0
                    and abs(observed_delta_micro_eur - expected) <= tolerance
                )
                values = {
                    "canary_attempt_id": attempt_id,
                    "canary_checked_at": timestamp,
                    "canary_expected_micro_eur": str(expected),
                    "canary_observed_micro_eur": str(observed_delta_micro_eur),
                    "canary_tolerance_micro_eur": str(tolerance),
                    "canary_status": "accepted" if accepted else "mismatch",
                }
                conn.executemany(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
                    values.items(),
                )
                if not accepted:
                    conn.execute(
                        "UPDATE metadata SET value='dashboard_canary_mismatch' "
                        "WHERE key='service_hold'"
                    )
                self._commit(conn)
                return {
                    "attempt_id": attempt_id,
                    "accepted": accepted,
                    "expected_micro_eur": expected,
                    "observed_delta_micro_eur": observed_delta_micro_eur,
                    "tolerance_micro_eur": tolerance,
                    "held": not accepted,
                    "checked_at": timestamp,
                }
            except Exception:
                self._rollback(conn)
                raise

    def policy_hashes(self) -> dict:
        """The ledger's own verified policy hashes (what ``gateway init`` returns).

        ``child_cap_policy_hash`` is None until the child caps are installed.
        """
        with self._connect() as conn:
            metadata = self._metadata(conn)
            ready = self._child_cap_feature_state(conn, metadata) == "ready"
            return {
                "price_policy_hash": metadata["price_policy_hash"],
                "child_cap_policy_hash": (
                    metadata["child_cap_policy_hash"] if ready else None
                ),
            }

    def _verified_child_cap(
        self, conn: sqlite3.Connection, issuer_hash: str
    ) -> dict[str, str]:
        metadata = self._verify_metadata(conn, self._marker())
        if self._child_cap_feature_state(conn, metadata) != "ready":
            raise ChildTurnCapBlocked("child turn cap feature is not installed")
        if not hmac.compare_digest(metadata["child_cap_issuer_sha256"], issuer_hash):
            raise ChildTurnCapBlocked("child turn issuer credential is invalid")
        return metadata

    def quota_lease_binding_state(self, *, issuer_token: str | None = None) -> dict:
        """Whether quota lease binding is installed and whether the flag is on.

        An authenticated read that writes nothing. A malformed flag raises; it is
        never read as off. On a schema-3 ledger binding is not installed, so no open
        can carry a reference and the flag cannot be on."""
        issuer_hash = self._child_cap_issuer_hash(issuer_token)
        with self._connect() as conn:
            metadata = self._verified_child_cap(conn, issuer_hash)
            installed = self._child_cap_version(metadata) == CHILD_CAP_SCHEMA_VERSION
            return {
                "binding_installed": installed,
                "quota_lease_binding_required": (
                    self._binding_required_flag(metadata) if installed else False
                ),
            }

    def set_quota_lease_binding_required(
        self, *, required: bool, issuer_token: str | None = None
    ) -> dict:
        """The operator's explicit flag switch (``gateway binding-required``). Nothing
        else changes the flag. It decides only whether an unbound open is allowed: it
        never removes a binding and never turns the caps off."""
        if not isinstance(required, bool):
            raise ValueError("required must be a boolean")
        issuer_hash = self._child_cap_issuer_hash(issuer_token)
        with self._connect() as conn:
            self._begin(conn)
            try:
                metadata = self._verified_child_cap(conn, issuer_hash)
                if self._child_cap_version(metadata) != CHILD_CAP_SCHEMA_VERSION:
                    raise ChildTurnCapBlocked(
                        "quota lease binding is not installed on this ledger"
                    )
                before = self._binding_required_flag(metadata)
                conn.execute(
                    "UPDATE metadata SET value=? WHERE key='quota_lease_binding_required'",
                    ("1" if required else "0",),
                )
                self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise
        return {"quota_lease_binding_required": required, "changed": before != required}

    # The operator commands call these: the ledger reads the operator's credential (the
    # front token, from its usual file) itself, so the command never holds it.

    def install_child_cap_binding_as_operator(self, *, now: datetime | None = None) -> dict:
        return self.install_child_cap_binding(issuer_token=_operator_credential(), now=now)

    def set_quota_lease_binding_required_as_operator(self, *, required: bool) -> dict:
        return self.set_quota_lease_binding_required(
            required=required, issuer_token=_operator_credential()
        )

    def child_receipts_page_as_operator(self, *, after_seq: int = 0, limit: int = 100) -> dict:
        return self.child_receipts_page(
            after_seq=after_seq, limit=limit, issuer_token=_operator_credential()
        )

    def sweep_child_receipts(
        self, *, issuer_token: str | None = None, now: datetime | None = None
    ) -> dict:
        """The start-up sweep, through the one ending path: end every open referenced
        child turn past its expiry (``cancelled`` at its expiry time), then make sure
        every ended referenced child turn has its receipt or a pending note, which
        completes notes whose attempts are now resolved and repairs a missing
        ending with the stated fallback. Counts only; no reference leaves it."""
        issuer_hash = self._child_cap_issuer_hash(issuer_token)
        current = (now or self.now()).astimezone(timezone.utc)
        timestamp = _iso_utc(current)
        with self._connect() as conn:
            self._begin(conn)
            try:
                metadata = self._verified_child_cap(conn, issuer_hash)
                if self._child_cap_version(metadata) != CHILD_CAP_SCHEMA_VERSION:
                    self._rollback(conn)
                    return {
                        "binding_installed": False,
                        "ended": 0,
                        "receipts_written": 0,
                        "pending": 0,
                        "fallback": 0,
                    }
                self._validate_child_cap_clock(conn, current)
                ended = 0
                for row in conn.execute(
                    """
                    SELECT agent, message_id, expires_at FROM child_turns
                    WHERE state='open' AND quota_lease_ref_sha256 IS NOT NULL
                    ORDER BY expires_at, agent, message_id
                    """
                ).fetchall():
                    if current >= _parse_utc(row["expires_at"]) and self._freeze_terminal(
                        conn,
                        row["agent"],
                        row["message_id"],
                        state="expired",
                        reason="wall-time ceiling exceeded",
                        outcome="cancelled",
                        at=row["expires_at"],
                        updated_at=timestamp,
                    ):
                        ended += 1
                written = 0
                for row in conn.execute(
                    """
                    SELECT agent, message_id FROM child_turns
                    WHERE state != 'open' AND quota_lease_ref_sha256 IS NOT NULL
                    ORDER BY terminal_at, agent, message_id
                    """
                ).fetchall():
                    result = self._ensure_custody(
                        conn, row["agent"], row["message_id"], at=timestamp
                    )
                    written += result == "receipt_written"
                report = self._receipt_report(conn)
                self._commit(conn)
            except Exception:
                self._rollback(conn)
                raise
        return {
            "binding_installed": True,
            "ended": ended,
            "receipts_written": written,
            "pending": report["child_receipts_pending"],
            "fallback": report["child_receipts_fallback"],
        }

    def child_receipts_page(
        self,
        *,
        after_seq: int = 0,
        limit: int = 100,
        issuer_token: str | None = None,
    ) -> dict:
        """One page of receipts, oldest first, as closed JSON-safe data. A read only:
        it writes nothing and never sweeps. The page names the ledger generation even
        when it is empty. A gap or a value outside the page's bounds refuses the page
        (``ReceiptPageRefused``); nothing is rounded or dropped to make it fit."""
        if (
            not isinstance(after_seq, int)
            or isinstance(after_seq, bool)
            or not 0 <= after_seq <= RECEIPT_MAX_SEQ
        ):
            raise ValueError("after_seq is out of range")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= RECEIPT_PAGE_MAX_LIMIT
        ):
            raise ValueError("limit is out of range")
        issuer_hash = self._child_cap_issuer_hash(issuer_token)
        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                metadata = self._verified_child_cap(conn, issuer_hash)
                if self._child_cap_version(metadata) != CHILD_CAP_SCHEMA_VERSION:
                    raise ChildTurnCapBlocked(
                        "quota lease binding is not installed on this ledger"
                    )
                count, highest = conn.execute(
                    "SELECT COUNT(*), COALESCE(MAX(seq), 0) FROM child_receipts"
                ).fetchone()
                if count != highest:
                    raise ReceiptPageRefused("receipt sequence has a gap")
                rows = conn.execute(
                    """
                    SELECT seq, agent, message_id, quota_lease_ref_sha256, outcome, calls,
                           input_tokens, output_tokens, actual_micro_eur, closed_at
                    FROM child_receipts WHERE seq > ? ORDER BY seq LIMIT ?
                    """,
                    (after_seq, limit + 1),
                ).fetchall()
                # derived in the same read as the rows; nothing is stored
                periods = [
                    self._charge_period(conn, row["agent"], row["message_id"])
                    for row in rows[:limit]
                ]
                generation = metadata["generation"]
            finally:
                self._rollback(conn)
        has_more = len(rows) > limit
        rows = rows[:limit]
        receipts = []
        for index, row in enumerate(rows):
            if row["seq"] != after_seq + 1 + index:
                raise ReceiptPageRefused("receipt sequence has a gap")
            receipts.append(
                {
                    "actual_micro_eur": row["actual_micro_eur"],
                    "calls": row["calls"],
                    "charge_period": periods[index],
                    "closed_at": row["closed_at"],
                    "input_tokens": row["input_tokens"],
                    "outcome": row["outcome"],
                    "output_tokens": row["output_tokens"],
                    "quota_lease_ref_sha256": row["quota_lease_ref_sha256"],
                    "seq": row["seq"],
                }
            )
        page = {
            "after_seq": after_seq,
            "envelope_version": RECEIPT_ENVELOPE_VERSION,
            "generation": generation,
            "has_more": has_more,
            "next_seq": receipts[-1]["seq"] if receipts else after_seq,
            "receipts": receipts,
        }
        check_receipt_page(page, after_seq=after_seq, limit=limit)
        return page

    def status(self) -> dict:
        with self._connect() as conn:
            # One snapshot: the metadata, the generation, the period totals and the
            # receipt coverage below all come from this one read transaction, so a
            # report can never claim a receipt whose cost is missing from the totals.
            conn.execute("BEGIN")
            try:
                return self._status_snapshot(conn)
            finally:
                self._rollback(conn)

    def report(self) -> dict:
        """The ledger-only report, version ``GATEWAY_REPORT_VERSION``: one read
        snapshot of the ledger's money figures as closed JSON-safe data. It needs no
        credential and checks nothing outside the ledger. It writes nothing, never
        sweeps and never ends a turn, and it names no agent, message, attempt or
        reference. It has no readiness figure: a readable report is never
        permission to spend. Every figure is defined in docs/QWEN-OVH-TRIAL.md."""
        with self._connect() as conn:
            conn.execute("BEGIN")
            try:
                report = self._report_snapshot(conn)
            finally:
                self._rollback(conn)
        check_gateway_report(report)
        return report

    def _report_snapshot(self, conn: sqlite3.Connection) -> dict:
        marker = self._marker()
        metadata = self._verify_metadata(conn, marker)
        current = self.now().astimezone(timezone.utc)
        self._validate_clock(metadata, current)
        child_cap_ready = self._child_cap_feature_state(conn, metadata) == "ready"
        if child_cap_ready:
            self._validate_child_cap_clock(conn, current)
        bound = (
            child_cap_ready
            and self._child_cap_version(metadata) == CHILD_CAP_SCHEMA_VERSION
        )
        open_turns = expired = earliest = None
        if child_cap_ready:
            expiries = sorted(
                _parse_utc(row[0])
                for row in conn.execute("SELECT expires_at FROM child_turns WHERE state='open'")
            )
            open_turns = len(expiries)
            expired = sum(current >= expiry for expiry in expiries)  # the sweep's own test
            earliest = _iso_utc(expiries[0]) if expiries else None
        receipts = self._receipt_report(conn) if bound else None
        hold = metadata.get("service_hold") or ""
        return {
            "gateway_report_version": GATEWAY_REPORT_VERSION,
            "observed_at": _iso_utc(current),
            "generation": metadata["generation"],
            "policy_hash": metadata["price_policy_hash"],
            "child_cap_policy_hash": (
                metadata["child_cap_policy_hash"] if child_cap_ready else None
            ),
            "child_cap_ready": child_cap_ready,
            "periods": [
                {"period": row["period"], "committed_micro_eur": int(row["committed_micro_eur"])}
                for row in conn.execute(
                    "SELECT period, committed_micro_eur FROM periods ORDER BY period"
                )
            ],
            "unresolved": [
                {
                    "state": row["state"],
                    "reserved_micro_eur": int(row["reserved_micro_eur"]),
                    "actual_micro_eur": (
                        None if row["actual_micro_eur"] is None else int(row["actual_micro_eur"])
                    ),
                    "period": row["period"],
                }
                for row in self._unresolved(conn)
            ],
            "open_child_turns": open_turns,
            "open_child_turns_expired": expired,
            "earliest_open_expiry": earliest,
            "child_receipt_report_version": (
                receipts["child_receipt_report_version"] if receipts else None
            ),
            "child_receipts_through_seq": (
                receipts["child_receipts_through_seq"] if receipts else None
            ),
            "child_receipts_pending": receipts["child_receipts_pending"] if receipts else None,
            "service_hold": bool(hold),
            "service_hold_reason": _hold_word(hold),
            # without binding no turn can carry a reference, so there is none
            "unreceipted_bound_actual": self._unreceipted_bound_actual(conn) if bound else [],
        }

    @staticmethod
    def _unreceipted_bound_actual(conn: sqlite3.Connection) -> list[dict]:
        """Per period, the recorded actuals of charged attempts whose child turn has
        a reference and no receipt yet: the money a receipt will carry later."""
        totals: dict[str, int] = {}
        for row in conn.execute(
            """
            SELECT attempt.period, attempt.state, attempt.actual_micro_eur,
                   (SELECT rec.outcome FROM reconciliations AS rec
                    WHERE rec.attempt_id = attempt.attempt_id
                    ORDER BY rec.id DESC LIMIT 1) AS reconcile_outcome
            FROM child_turns AS turn
            JOIN child_attempts AS child
              ON child.agent = turn.agent AND child.message_id = turn.message_id
            JOIN attempts AS attempt ON attempt.attempt_id = child.attempt_id
            WHERE turn.quota_lease_ref_sha256 IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM child_receipts AS receipt
                  WHERE receipt.agent = turn.agent AND receipt.message_id = turn.message_id
              )
            """
        ):
            if not _charged(row["state"], row["actual_micro_eur"], row["reconcile_outcome"]):
                continue
            if row["actual_micro_eur"] is None:
                raise LedgerBlocked("a charged attempt has no recorded charge")
            period = row["period"]
            totals[period] = totals.get(period, 0) + int(row["actual_micro_eur"])
        return [{"period": period, "micro_eur": totals[period]} for period in sorted(totals)]

    @staticmethod
    def _charge_period(conn: sqlite3.Connection, agent: str, message_id: str) -> str | None:
        """The one period every charged attempt of this child turn shares; None when
        their periods differ or the turn has no charged attempt."""
        periods = {
            row["period"]
            for row in conn.execute(
                """
                SELECT attempt.period, attempt.state, attempt.actual_micro_eur,
                       (SELECT rec.outcome FROM reconciliations AS rec
                        WHERE rec.attempt_id = attempt.attempt_id
                        ORDER BY rec.id DESC LIMIT 1) AS reconcile_outcome
                FROM child_attempts AS child
                JOIN attempts AS attempt ON attempt.attempt_id = child.attempt_id
                WHERE child.agent=? AND child.message_id=?
                """,
                (agent, message_id),
            )
            if _charged(row["state"], row["actual_micro_eur"], row["reconcile_outcome"])
        }
        return periods.pop() if len(periods) == 1 else None

    @staticmethod
    def _receipt_report(conn: sqlite3.Connection) -> dict:
        """The five schema-4 status keys, read inside the status snapshot."""
        count, through_seq = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(seq), 0) FROM child_receipts"
        ).fetchone()
        pending = conn.execute(
            "SELECT COUNT(*) FROM receipt_pending WHERE resolved_at IS NULL"
        ).fetchone()[0]
        fallback = conn.execute(
            """
            SELECT COUNT(*) FROM child_turns
            WHERE terminal_source='legacy_fallback' AND quota_lease_ref_sha256 IS NOT NULL
            """
        ).fetchone()[0]
        return {
            "child_receipt_report_version": CHILD_RECEIPT_REPORT_VERSION,
            "child_receipts": int(count),
            "child_receipts_pending": int(pending),
            "child_receipts_fallback": int(fallback),
            "child_receipts_through_seq": int(through_seq),
        }

    def _status_snapshot(self, conn: sqlite3.Connection) -> dict:
        marker = self._marker()
        metadata = self._verify_metadata(conn, marker)
        current = self.now().astimezone(timezone.utc)
        self._validate_clock(metadata, current)
        child_cap_state = self._child_cap_feature_state(conn, metadata)
        if child_cap_state == "ready":
            self._validate_child_cap_clock(conn, current)
        periods = [dict(row) for row in conn.execute("SELECT * FROM periods ORDER BY period")]
        unresolved = [dict(row) for row in self._unresolved(conn)]
        current_period = metadata["last_accepted_period"]
        current = next(
            (row for row in periods if row["period"] == current_period),
            {"committed_micro_eur": 0},
        )
        opening_micro_eur = int(metadata["opening_micro_eur"])
        current_opening = (
            opening_micro_eur if current_period == metadata["opening_period"] else 0
        )
        dashboard_canary = (
            {
                "attempt_id": metadata["canary_attempt_id"],
                "checked_at": metadata["canary_checked_at"],
                "expected_micro_eur": int(metadata["canary_expected_micro_eur"]),
                "observed_delta_micro_eur": int(
                    metadata["canary_observed_micro_eur"]
                ),
                "tolerance_micro_eur": int(
                    metadata["canary_tolerance_micro_eur"]
                ),
                "status": metadata["canary_status"],
            }
            if metadata.get("canary_attempt_id")
            else None
        )
        accounting_ready = not unresolved and not metadata.get("service_hold")
        worker_spend_errors: list[str] = []
        if not accounting_ready:
            worker_spend_errors.append("ledger_not_ready")
        if dashboard_canary is None:
            worker_spend_errors.append("dashboard_canary_absent")
        elif dashboard_canary["status"] != "accepted":
            worker_spend_errors.append("dashboard_canary_mismatch")
        if child_cap_state != "ready":
            worker_spend_errors.append("child_cap_unavailable")
        active_child_turns = []
        if child_cap_state == "ready":
            active_child_turns = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT turn.agent, turn.message_id, turn.request_id,
                           turn.state, turn.opened_at, turn.expires_at,
                           turn.reason,
                           COUNT(child.attempt_id) AS attempt_count,
                           COALESCE(SUM(
                               CASE
                                   WHEN attempt.actual_micro_eur IS NOT NULL
                                   THEN attempt.actual_micro_eur
                                   ELSE attempt.reserved_micro_eur
                               END
                           ), 0) AS exposure_micro_eur
                    FROM child_turns AS turn
                    LEFT JOIN child_attempts AS child
                      ON child.agent=turn.agent
                     AND child.message_id=turn.message_id
                    LEFT JOIN attempts AS attempt
                      ON attempt.attempt_id=child.attempt_id
                    WHERE turn.state != 'fenced'
                    GROUP BY turn.agent, turn.message_id
                    ORDER BY turn.opened_at, turn.agent, turn.message_id
                    """
                )
            ]
        child_cap_version = (
            self._child_cap_version(metadata) if child_cap_state == "ready" else None
        )
        receipt_report = (
            self._receipt_report(conn)
            if child_cap_version == CHILD_CAP_SCHEMA_VERSION
            else {}
        )
        return {
            "schema_version": int(metadata["schema_version"]),
            "generation": metadata["generation"],
            "policy_hash": metadata["price_policy_hash"],
            "currency": metadata["currency"],
            "trial_cutoff_micro_eur": self._parse_envelope_int(
                metadata, "trial_cutoff_micro_eur"
            ),
            "soft_stop_micro_eur": self._parse_envelope_int(
                metadata, "soft_stop_micro_eur"
            ),
            "external_ceiling_micro_eur": self._parse_envelope_int(
                metadata, "external_ceiling_micro_eur"
            ),
            "opening_micro_eur": opening_micro_eur,
            "opening_evidence": metadata["opening_evidence"],
            "opening_observed_at": metadata["opening_observed_at"],
            "opening_period": metadata["opening_period"],
            "current_period": current_period,
            "current_committed_micro_eur": int(current["committed_micro_eur"]),
            "current_trial_committed_micro_eur": max(
                0,
                int(current["committed_micro_eur"]) - current_opening,
            ),
            "periods": periods,
            "unresolved": unresolved,
            "service_hold": metadata.get("service_hold") or None,
            "dashboard_canary": dashboard_canary,
            "child_cap_ready": child_cap_state == "ready",
            "child_cap_schema_version": child_cap_version,
            "child_cap_policy_hash": (
                metadata["child_cap_policy_hash"] if child_cap_state == "ready" else None
            ),
            "child_turn_max_calls": CHILD_TURN_MAX_CALLS,
            "child_turn_max_micro_eur": (
                self._parse_envelope_int(metadata, "child_turn_max_micro_eur")
                if child_cap_state == "ready"
                else None
            ),
            "child_turn_max_seconds": CHILD_TURN_MAX_SECONDS,
            "active_child_turns": active_child_turns,
            "ready": accounting_ready,
            "worker_spend_ready": not worker_spend_errors,
            "worker_spend_errors": worker_spend_errors,
            **receipt_report,
        }


def generate_token() -> str:
    return "atgw-" + secrets.token_urlsafe(32)


def write_secret_file(path: Path, value: str) -> None:
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise ValueError("secret value must be a single non-empty line")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise GatewayConfigError(f"refusing to replace existing secret file {path}")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, (value + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    _flush_parent(path)


def _operator_credential() -> str:
    """The operator's child-cap issuer credential: the front token, from its usual file."""
    return read_secret_file(default_front_token_path())


def read_secret_file(path: Path) -> str:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise GatewayConfigError(f"required secret file is unavailable: {Path(path).name}") from exc
    value = raw.rstrip("\r\n")
    if not value or "\n" in value or "\r" in value:
        raise GatewayConfigError(f"required secret file is malformed: {Path(path).name}")
    return value


def token_matches(header: str | None, expected: str) -> bool:
    if not isinstance(header, str) or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:], expected)


def child_capability_from_header(header: str | None) -> str | None:
    if not isinstance(header, str) or not header.startswith("Bearer "):
        return None
    token = header[7:]
    return token if _CHILD_CAPABILITY_RE.fullmatch(token) else None


def render_litellm_config(
    *,
    api_base: str,
    reasoning_params: Mapping[str, ReasoningValue] | None = None,
) -> str:
    """Render the single-model, callback-free LiteLLM trial configuration.

    Reasoning is deliberately NOT merged into the reply text
    (``merge_reasoning_content_in_choices``): merged reasoning is ordinary
    assistant text to the Claude CLI, which re-sends it as input on every later
    call of the tool loop. LiteLLM instead emits typed ``thinking`` content
    blocks, and the front removes those before the CLI sees them
    (``ovh_gateway_reasoning.SseReasoningStripper``), so reasoning is never
    carried forward and cannot trip the CLI's thinking-block state machine.

    ``reasoning_params`` are fixed request parameters for the route (for
    example ``reasoning_effort=low``), validated by
    ``ovh_gateway_reasoning`` and rendered under the deployment's
    ``extra_body``. ``extra_body`` is used because LiteLLM passes it through
    verbatim, whereas a top-level ``litellm_params`` key is silently dropped
    (``drop_params: true``) for a model LiteLLM does not know supports it.
    With no parameters the ``extra_body`` block is exactly ``store: false``.
    """
    if not isinstance(api_base, str) or not api_base.startswith(("https://", "http://")):
        raise ValueError("api_base must be an explicit HTTP(S) URL")
    extra_body = render_extra_body_lines(reasoning_params, indent="        ")
    return (
        "model_list:\n"
        f"  - model_name: {MODEL_ALIAS}\n"
        "    litellm_params:\n"
        f"      model: openai/{MODEL_ALIAS}\n"
        f"      api_base: {api_base}\n"
        "      api_key: os.environ/OVH_KEY\n"
        "      store: false\n"
        "      extra_body:\n"
        "        store: false\n"
        f"{extra_body}"
        "      max_retries: 0\n"
        "litellm_settings:\n"
        "  drop_params: true\n"
        "  num_retries: 0\n"
        "  telemetry: false\n"
        "  use_chat_completions_url_for_anthropic_messages: true\n"
        "router_settings:\n"
        "  num_retries: 0\n"
        "general_settings:\n"
        "  master_key: os.environ/LITELLM_MASTER_KEY\n"
    )


def policy_summary() -> dict:
    return {
        **price_policy(),
        "price_policy_hash": price_policy_hash(),
    }


_RECEIPT_PAGE_KEYS = frozenset(
    {"after_seq", "envelope_version", "generation", "has_more", "next_seq", "receipts"}
)
_RECEIPT_KEYS = frozenset(
    {
        "actual_micro_eur",
        "calls",
        "charge_period",
        "closed_at",
        "input_tokens",
        "outcome",
        "output_tokens",
        "quota_lease_ref_sha256",
        "seq",
    }
)


def _month(value: object) -> bool:
    """A UTC ``YYYY-MM`` period, in ASCII digits."""
    return isinstance(value, str) and value.isascii() and bool(_PERIOD_RE.fullmatch(value))


def _whole_number(value: object, low: int, high: int) -> bool:
    """An integer in [low, high]; a boolean or a float is never an integer here."""
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _ledger_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not _LEDGER_TIME_RE.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def check_receipt_page(page: object, *, after_seq: int, limit: int) -> None:
    """Refuse (``ReceiptPageRefused``) any receipt page that breaks the version-1
    rules: the closed keys at both levels, the integer types and bounds, the
    generation and hash formats, real timestamps, the charge period (a month or
    null), the zero-call rule, no gap and no repeat, the requested ``after_seq``, and
    no more rows than the limit."""
    if not isinstance(page, dict) or set(page) != _RECEIPT_PAGE_KEYS:
        raise ReceiptPageRefused("receipt page keys are not the closed set")
    if not _whole_number(page["envelope_version"], 1, 1):
        raise ReceiptPageRefused("receipt page version is not 1")
    if not _whole_number(page["after_seq"], 0, RECEIPT_MAX_SEQ) or page["after_seq"] != after_seq:
        raise ReceiptPageRefused("receipt page after_seq is not the one asked for")
    generation = page["generation"]
    if not isinstance(generation, str) or not _ATTEMPT_ID_RE.fullmatch(generation):
        raise ReceiptPageRefused("receipt page generation is malformed")
    receipts = page["receipts"]
    if not isinstance(receipts, list) or len(receipts) > min(limit, RECEIPT_PAGE_MAX_LIMIT):
        raise ReceiptPageRefused("receipt page has more rows than its limit")
    if not isinstance(page["has_more"], bool):
        raise ReceiptPageRefused("receipt page has_more is not a boolean")
    if not receipts and page["has_more"]:
        raise ReceiptPageRefused("an empty receipt page cannot have more")
    for index, receipt in enumerate(receipts):
        if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_KEYS:
            raise ReceiptPageRefused("receipt keys are not the closed set")
        if not _whole_number(receipt["seq"], 1, RECEIPT_MAX_SEQ) or (
            receipt["seq"] != after_seq + 1 + index
        ):
            raise ReceiptPageRefused("receipt sequence has a gap or a repeat")
        if not _whole_number(receipt["calls"], 0, RECEIPT_MAX_CALLS):
            raise ReceiptPageRefused("receipt calls are out of bounds")
        if not _whole_number(receipt["actual_micro_eur"], 0, RECEIPT_MAX_AMOUNT):
            raise ReceiptPageRefused("receipt money is out of bounds")
        for key in ("input_tokens", "output_tokens"):
            value = receipt[key]
            if value is not None and not _whole_number(value, 0, RECEIPT_MAX_AMOUNT):
                raise ReceiptPageRefused("receipt token count is out of bounds")
        if receipt["charge_period"] is not None and not _month(receipt["charge_period"]):
            raise ReceiptPageRefused("receipt charge period is not a month")
        if receipt["calls"] == 0 and not (
            receipt["input_tokens"] == 0
            and receipt["output_tokens"] == 0
            and receipt["actual_micro_eur"] == 0
            and receipt["charge_period"] is None
        ):
            raise ReceiptPageRefused("a receipt with no calls must be all zero")
        if receipt["outcome"] not in CHILD_OUTCOMES:
            raise ReceiptPageRefused("receipt outcome is not a closed word")
        if not isinstance(receipt["quota_lease_ref_sha256"], str) or not _SHA256_HEX_RE.fullmatch(
            receipt["quota_lease_ref_sha256"]
        ):
            raise ReceiptPageRefused("receipt reference hash is malformed")
        if not _ledger_timestamp(receipt["closed_at"]):
            raise ReceiptPageRefused("receipt close time is not a ledger timestamp")
    expected_next = receipts[-1]["seq"] if receipts else after_seq
    if not _whole_number(page["next_seq"], 0, RECEIPT_MAX_SEQ) or page["next_seq"] != expected_next:
        raise ReceiptPageRefused("receipt page next_seq is wrong")


def _refuse_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    keys = [key for key, _value in pairs]
    if len(keys) != len(set(keys)):
        raise ReceiptPageRefused("receipt page has a duplicate key")
    return dict(pairs)


def _refuse_constant(_name: str) -> object:
    raise ReceiptPageRefused("receipt page has a non-finite number")


def parse_receipt_page(text: str, *, after_seq: int, limit: int) -> dict:
    """Read a receipt page's JSON text as a reader must: duplicate keys and
    non-finite numbers are refused, then every rule of ``check_receipt_page``."""
    try:
        page = json.loads(
            text,
            object_pairs_hook=_refuse_duplicate_keys,
            parse_constant=_refuse_constant,
        )
    except ValueError as exc:
        raise ReceiptPageRefused("receipt page is not JSON") from exc
    check_receipt_page(page, after_seq=after_seq, limit=limit)
    return page


_REPORT_KEYS = frozenset(
    {
        "child_cap_policy_hash",
        "child_cap_ready",
        "child_receipt_report_version",
        "child_receipts_pending",
        "child_receipts_through_seq",
        "earliest_open_expiry",
        "gateway_report_version",
        "generation",
        "observed_at",
        "open_child_turns",
        "open_child_turns_expired",
        "periods",
        "policy_hash",
        "service_hold",
        "service_hold_reason",
        "unreceipted_bound_actual",
        "unresolved",
    }
)


def _months_in_order(rows: object, keys: frozenset, amount: str) -> bool:
    """Rows with exactly ``keys``, one per month, in month order, each with a
    bounded whole-number ``amount``."""
    if not isinstance(rows, list):
        return False
    months = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != keys or not _month(row["period"]):
            return False
        if not _whole_number(row[amount], 0, RECEIPT_MAX_AMOUNT):
            return False
        months.append(row["period"])
    return months == sorted(set(months))


def check_gateway_report(report: object) -> None:
    """Refuse (``GatewayReportRefused``) any report that breaks the version-1 rules:
    the closed keys at every level, the types and bounds, which figures are null
    together, and the closed hold words. What passes holds no free text."""

    def refuse(reason: str) -> None:
        raise GatewayReportRefused(reason)

    if not isinstance(report, dict) or set(report) != _REPORT_KEYS:
        refuse("report keys are not the closed set")
    if not _whole_number(report["gateway_report_version"], 1, 1):
        refuse("report version is not 1")
    if not _ledger_timestamp(report["observed_at"]):
        refuse("report time is not a ledger timestamp")
    if not isinstance(report["generation"], str) or not _ATTEMPT_ID_RE.fullmatch(report["generation"]):
        refuse("report generation is malformed")
    if not isinstance(report["policy_hash"], str) or not _SHA256_HEX_RE.fullmatch(report["policy_hash"]):
        refuse("report policy hash is malformed")
    ready = report["child_cap_ready"]
    if not isinstance(ready, bool):
        refuse("report child_cap_ready is not a boolean")
    child_hash = report["child_cap_policy_hash"]
    if (child_hash is None) == ready or (
        child_hash is not None
        and (not isinstance(child_hash, str) or not _SHA256_HEX_RE.fullmatch(child_hash))
    ):
        refuse("report child-cap policy hash does not match child_cap_ready")
    if not _months_in_order(report["periods"], frozenset({"period", "committed_micro_eur"}),
                            "committed_micro_eur"):
        refuse("report periods are malformed")
    unresolved = report["unresolved"]
    if not isinstance(unresolved, list):
        refuse("report unresolved attempts are not a list")
    for row in unresolved:
        if (
            not isinstance(row, dict)
            or set(row) != {"state", "reserved_micro_eur", "actual_micro_eur", "period"}
            or row["state"] not in _UNRESOLVED_ATTEMPT_STATES
            or not _whole_number(row["reserved_micro_eur"], 0, RECEIPT_MAX_AMOUNT)
            or not (
                row["actual_micro_eur"] is None
                or _whole_number(row["actual_micro_eur"], 0, RECEIPT_MAX_AMOUNT)
            )
            or not _month(row["period"])
        ):
            refuse("report unresolved attempt is malformed")
    turns = (report["open_child_turns"], report["open_child_turns_expired"])
    earliest = report["earliest_open_expiry"]
    if not ready:
        if turns != (None, None) or earliest is not None:
            refuse("report child-turn figures need child_cap_ready")
    elif (
        not _whole_number(turns[0], 0, RECEIPT_MAX_SEQ)
        or not _whole_number(turns[1], 0, turns[0])
        or (earliest is None) != (turns[0] == 0)
        or (earliest is not None and not _ledger_timestamp(earliest))
    ):
        refuse("report child-turn figures are malformed")
    receipts = (
        report["child_receipt_report_version"],
        report["child_receipts_through_seq"],
        report["child_receipts_pending"],
    )
    if receipts != (None, None, None) and not (
        ready
        and _whole_number(receipts[0], CHILD_RECEIPT_REPORT_VERSION, CHILD_RECEIPT_REPORT_VERSION)
        and _whole_number(receipts[1], 0, RECEIPT_MAX_SEQ)
        and _whole_number(receipts[2], 0, RECEIPT_MAX_SEQ)
    ):
        refuse("report receipt figures are malformed")
    hold, word = report["service_hold"], report["service_hold_reason"]
    if not isinstance(hold, bool) or (word is None) == hold or (
        word is not None and word not in HOLD_REASON_WORDS
    ):
        refuse("report hold is not a flag with its closed word")
    unreceipted = report["unreceipted_bound_actual"]
    if not _months_in_order(unreceipted, frozenset({"period", "micro_eur"}), "micro_eur") or (
        unreceipted and receipts == (None, None, None)
    ):
        refuse("report unreceipted money is malformed")
