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
from typing import Callable, Iterator


MODEL_ALIAS = "Qwen3.5-397B-A17B"
POLICY_SOURCE = "https://www.ovhcloud.com/en/public-cloud/ai-endpoints/catalog/"
POLICY_OBSERVED_DATE = "2026-07-16"
POLICY_CURRENCY = "EUR"
TOKENS_PER_RATE_UNIT = 1_000_000
INPUT_RATE_MICRO_EUR = 600_000
OUTPUT_RATE_MICRO_EUR = 3_600_000
RESERVE_INPUT_RATE_MICRO_EUR = 720_000
RESERVE_OUTPUT_RATE_MICRO_EUR = 4_320_000
MAX_CONTEXT_TOKENS = 262_144
MAX_OUTPUT_TOKENS = 4_096
TRIAL_CUTOFF_MICRO_EUR = 25_000_000
SOFT_STOP_MICRO_EUR = 20_000_000
EXTERNAL_CEILING_MICRO_EUR = 100_000_000
CANARY_TOLERANCE_BPS = 1_000
LEDGER_SCHEMA_VERSION = 2
INSTALL_MARKER_SCHEMA_VERSION = 1
CHILD_CAP_SCHEMA_VERSION = 1
CHILD_TURN_MAX_CALLS = 8
CHILD_TURN_MAX_MICRO_EUR = 500_000
CHILD_TURN_MAX_SECONDS = 300
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
_FRONT_TOKEN_RE = re.compile(r"^atgw-[A-Za-z0-9_-]{43}$")
_CHILD_CAPABILITY_RE = re.compile(r"^atgw-child-[A-Za-z0-9_-]{43}$")
_CHILD_CAP_TABLES = (
    "child_turns",
    "child_capabilities",
    "child_attempts",
)


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


def price_policy() -> dict:
    """Canonical non-secret policy object whose digest binds persisted state."""
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
        "trial_cutoff_micro_eur": TRIAL_CUTOFF_MICRO_EUR,
        "soft_stop_micro_eur": SOFT_STOP_MICRO_EUR,
        "external_ceiling_micro_eur": EXTERNAL_CEILING_MICRO_EUR,
        "dashboard_canary": {
            "requires_nonzero_delta": True,
            "tolerance_basis_points": CANARY_TOLERANCE_BPS,
        },
    }


def price_policy_hash() -> str:
    encoded = json.dumps(
        price_policy(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def child_cap_policy() -> dict:
    """Canonical separately-versioned child-turn admission policy."""
    return {
        "schema_version": CHILD_CAP_SCHEMA_VERSION,
        "max_calls": CHILD_TURN_MAX_CALLS,
        "max_micro_eur": CHILD_TURN_MAX_MICRO_EUR,
        "max_seconds": CHILD_TURN_MAX_SECONDS,
        "reservation_micro_eur": reservation_cost_micro_eur(),
    }


def child_cap_policy_hash() -> str:
    encoded = json.dumps(
        child_cap_policy(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    def _assert_external_envelope(opening_micro_eur: int) -> None:
        projected = (
            opening_micro_eur
            + TRIAL_CUTOFF_MICRO_EUR
            + reservation_cost_micro_eur()
        )
        if projected > EXTERNAL_CEILING_MICRO_EUR:
            raise PolicyBlocked(
                "opening balance plus trial cutoff and one reservation exceeds "
                "the external account ceiling"
            )

    def initialize(
        self,
        *,
        opening_micro_eur: int,
        opening_evidence: str,
        generation: str | None = None,
        child_cap_issuer_token: str | None = None,
    ) -> dict:
        if self.installation_state() != "absent":
            raise LedgerBlocked(
                "ledger initialization requires both database and install marker to be absent"
            )
        opening_micro_eur = self._opening_amount(opening_micro_eur)
        opening_evidence = self._opening_evidence(opening_evidence)
        issuer_hash = self._child_cap_issuer_hash(child_cap_issuer_token)
        self._assert_external_envelope(opening_micro_eur)
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
                    "price_policy_hash": price_policy_hash(),
                    "currency": POLICY_CURRENCY,
                    "trial_cutoff_micro_eur": str(TRIAL_CUTOFF_MICRO_EUR),
                    "soft_stop_micro_eur": str(SOFT_STOP_MICRO_EUR),
                    "external_ceiling_micro_eur": str(EXTERNAL_CEILING_MICRO_EUR),
                    "opening_micro_eur": str(opening_micro_eur),
                    "opening_evidence": opening_evidence,
                    "opening_observed_at": observed_at,
                    "opening_period": opening_period,
                    "initialized_at": observed_at,
                    "last_accepted_utc": observed_at,
                    "last_accepted_period": opening_period,
                    "service_hold": "",
                    "child_cap_schema_version": str(CHILD_CAP_SCHEMA_VERSION),
                    "child_cap_policy_hash": child_cap_policy_hash(),
                    "child_cap_issuer_sha256": issuer_hash,
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
                "price_policy_hash": price_policy_hash(),
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
    def _create_child_cap_schema(conn: sqlite3.Connection) -> None:
        statements = (
            """CREATE TABLE child_turns (
                agent TEXT NOT NULL,
                message_id TEXT NOT NULL,
                request_id TEXT NOT NULL,
                state TEXT NOT NULL CHECK (
                    state IN ('open', 'capped', 'expired')
                ),
                max_calls INTEGER NOT NULL CHECK (max_calls > 0),
                max_micro_eur INTEGER NOT NULL CHECK (max_micro_eur > 0),
                opened_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(agent, message_id)
            )""",
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

    def _marker(self, *, ledger_schema_version: int | None = None) -> dict:
        state = self.installation_state()
        if state == "absent":
            raise LedgerBlocked("ledger is not initialized; run explicit ledger init")
        if state == "partial":
            raise LedgerBlocked("ledger installation is partial; explicit recovery is required")
        marker = _strict_json_object(self.marker_path)
        expected = {
            "schema_version": INSTALL_MARKER_SCHEMA_VERSION,
            "ledger_schema_version": (
                LEDGER_SCHEMA_VERSION
                if ledger_schema_version is None
                else ledger_schema_version
            ),
            "price_policy_hash": price_policy_hash(),
        }
        for key, value in expected.items():
            if marker.get(key) != value:
                raise LedgerBlocked(f"ledger install marker {key} mismatch")
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
        expected = {
            "schema_version": str(
                LEDGER_SCHEMA_VERSION
                if ledger_schema_version is None
                else ledger_schema_version
            ),
            "generation": marker["generation"],
            "price_policy_hash": price_policy_hash(),
            "currency": POLICY_CURRENCY,
            "trial_cutoff_micro_eur": str(TRIAL_CUTOFF_MICRO_EUR),
            "soft_stop_micro_eur": str(SOFT_STOP_MICRO_EUR),
            "external_ceiling_micro_eur": str(EXTERNAL_CEILING_MICRO_EUR),
        }
        for key, value in expected.items():
            if metadata.get(key) != value:
                raise LedgerBlocked(f"ledger metadata {key} mismatch")
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
            self._assert_external_envelope(opening_micro_eur)
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
                or canary_attempt["policy_hash"] != price_policy_hash()
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
        expected_tables = set(_CHILD_CAP_TABLES)
        if (
            schema_value is None
            and policy_value is None
            and issuer_value is None
            and not (tables & expected_tables)
        ):
            return "absent"
        if schema_value != str(CHILD_CAP_SCHEMA_VERSION):
            raise LedgerBlocked("child cap schema version is missing or mismatched")
        if policy_value != child_cap_policy_hash():
            raise LedgerBlocked("child cap policy hash is missing or mismatched")
        if not isinstance(issuer_value, str) or not re.fullmatch(
            r"[a-f0-9]{64}", issuer_value
        ):
            raise LedgerBlocked("child cap issuer authority is missing or invalid")
        if not expected_tables <= tables:
            raise LedgerBlocked("child cap schema is partial")
        expected_columns = {
            "child_turns": (
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
            ),
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
            (CHILD_TURN_MAX_CALLS, CHILD_TURN_MAX_MICRO_EUR),
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
        return "ready"

    def install_child_caps(self, *, issuer_token: str | None = None) -> dict:
        """Migrate a v1 ledger to the downgrade-fenced child-cap schema."""
        issuer_hash = self._child_cap_issuer_hash(issuer_token)
        if self.installation_state() != "complete":
            raise LedgerBlocked(
                "child cap install requires a complete existing ledger"
            )
        raw_marker = _strict_json_object(self.marker_path)
        marker_version = raw_marker.get("ledger_schema_version")
        if marker_version == LEDGER_SCHEMA_VERSION:
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
                "schema_version": CHILD_CAP_SCHEMA_VERSION,
                "policy_hash": child_cap_policy_hash(),
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
                        self._create_child_cap_schema(conn)
                        conn.executemany(
                            "INSERT INTO metadata(key, value) VALUES (?, ?)",
                            (
                                (
                                    "child_cap_schema_version",
                                    str(CHILD_CAP_SCHEMA_VERSION),
                                ),
                                ("child_cap_policy_hash", child_cap_policy_hash()),
                                ("child_cap_issuer_sha256", issuer_hash),
                            ),
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
        migrated_marker["ledger_schema_version"] = LEDGER_SCHEMA_VERSION
        _durable_write_json(self.marker_path, migrated_marker)
        return {
            "installed": True,
            "schema_version": CHILD_CAP_SCHEMA_VERSION,
            "policy_hash": child_cap_policy_hash(),
        }

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

    def open_child_turn(
        self,
        *,
        agent: str,
        message_id: str,
        request_id: str = "",
        issuer_token: str | None = None,
        now: datetime | None = None,
    ) -> ChildTurnCredential:
        """Issue an opaque capability bound to one durable wrapper message."""
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
                self._validate_child_cap_clock(conn, current)
                self._validate_clock(metadata, current)
                # Refuse to MINT a turn while transport is held (durable accounting hold, or
                # an unresolved prior attempt) - mirrors the reserve-side gate in
                # _reserve_locked. Otherwise a turn opened under a hold starts its wall-time
                # ceiling immediately and burns the whole budget while blocked, so it is
                # already (near-)expired the instant the hold clears (#63: the held-gateway
                # child turn that expired mid-work, surfacing as a misleading config_blocked).
                # A held mint raises LedgerHold (a GatewayError) which the wrapper spawner
                # treats as a transient park, and the next drive after the hold clears mints a
                # fresh turn with a full wall-time window.
                if metadata.get("service_hold"):
                    raise LedgerHold("gateway has a durable accounting hold")
                if self._unresolved(conn):
                    raise LedgerHold("an unresolved provider attempt blocks new transport")
                row = conn.execute(
                    "SELECT * FROM child_turns WHERE agent=? AND message_id=?",
                    (agent, message_id),
                ).fetchone()
                if row is None:
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
                            CHILD_TURN_MAX_MICRO_EUR,
                            timestamp,
                            expires_at,
                            timestamp,
                        ),
                    )
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
                        if row["state"] == "open":
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
        if projected > opening_allowance + TRIAL_CUTOFF_MICRO_EUR:
            raise PolicyBlocked("trial spend cutoff would be exceeded")
        cumulative_row = conn.execute(
            "SELECT COALESCE(SUM(committed_micro_eur), 0) FROM periods"
        ).fetchone()
        cumulative_projected = int(cumulative_row[0]) + unresolved_total + reserve
        if cumulative_projected > EXTERNAL_CEILING_MICRO_EUR:
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
                    price_policy_hash(),
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
                row = conn.execute(
                    "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
                ).fetchone()
                if row is None:
                    raise LedgerBlocked("attempt does not exist")
                expected = int(row["actual_micro_eur"] or 0)
                if (
                    row["state"] != "settled"
                    or row["model"] != MODEL_ALIAS
                    or row["policy_hash"] != price_policy_hash()
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

    def status(self) -> dict:
        with self._connect() as conn:
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
                        GROUP BY turn.agent, turn.message_id
                        ORDER BY turn.opened_at, turn.agent, turn.message_id
                        """
                    )
                ]
            return {
                "schema_version": LEDGER_SCHEMA_VERSION,
                "generation": metadata["generation"],
                "policy_hash": metadata["price_policy_hash"],
                "currency": metadata["currency"],
                "trial_cutoff_micro_eur": TRIAL_CUTOFF_MICRO_EUR,
                "soft_stop_micro_eur": SOFT_STOP_MICRO_EUR,
                "external_ceiling_micro_eur": EXTERNAL_CEILING_MICRO_EUR,
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
                "child_cap_schema_version": (
                    CHILD_CAP_SCHEMA_VERSION if child_cap_state == "ready" else None
                ),
                "child_cap_policy_hash": (
                    child_cap_policy_hash() if child_cap_state == "ready" else None
                ),
                "child_turn_max_calls": CHILD_TURN_MAX_CALLS,
                "child_turn_max_micro_eur": CHILD_TURN_MAX_MICRO_EUR,
                "child_turn_max_seconds": CHILD_TURN_MAX_SECONDS,
                "active_child_turns": active_child_turns,
                "ready": accounting_ready,
                "worker_spend_ready": not worker_spend_errors,
                "worker_spend_errors": worker_spend_errors,
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


def render_litellm_config(*, api_base: str) -> str:
    """Render the single-model, callback-free LiteLLM trial configuration."""
    if not isinstance(api_base, str) or not api_base.startswith(("https://", "http://")):
        raise ValueError("api_base must be an explicit HTTP(S) URL")
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
