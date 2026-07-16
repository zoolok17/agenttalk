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
POLICY_OBSERVED_DATE = "2026-07-15"
POLICY_CURRENCY = "EUR"
TOKENS_PER_RATE_UNIT = 1_000_000
INPUT_RATE_MICRO_EUR = 710_000
OUTPUT_RATE_MICRO_EUR = 4_250_000
RESERVE_INPUT_RATE_MICRO_EUR = 852_000
RESERVE_OUTPUT_RATE_MICRO_EUR = 5_100_000
MAX_CONTEXT_TOKENS = 262_144
MAX_OUTPUT_TOKENS = 4_096
TRIAL_CUTOFF_MICRO_EUR = 25_000_000
SOFT_STOP_MICRO_EUR = 20_000_000
LEDGER_SCHEMA_VERSION = 1
INSTALL_MARKER_SCHEMA_VERSION = 1
BACKEND_PROFILE = "ovh-qwen"
EXTERNAL_WORKER = "external-worker"
PUBLIC_HOST = "127.0.0.1"
PUBLIC_PORT = 4000
INTERNAL_HOST = "127.0.0.1"
INTERNAL_PORT = 4001
MAX_REQUEST_BYTES = 128 * 1024
DEFAULT_LOCAL_DIRNAME = "agenttalk-ovh"
DEFAULT_SPEND_DIRNAME = "agenttalk-ovh-spend"

_ATTEMPT_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_PERIOD_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")
_TERMINAL_ATTEMPT_STATES = frozenset({"settled", "reconciled"})
_UNRESOLVED_ATTEMPT_STATES = frozenset({"reserved", "uncertain"})


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
    }


def price_policy_hash() -> str:
    encoded = json.dumps(
        price_policy(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
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

    def initialize(self, *, generation: str | None = None) -> dict:
        if self.installation_state() != "absent":
            raise LedgerBlocked(
                "ledger initialization requires both database and install marker to be absent"
            )
        now = self.now().astimezone(timezone.utc)
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
                    "initialized_at": _iso_utc(now),
                    "last_accepted_utc": _iso_utc(now),
                    "last_accepted_period": _period(now),
                    "service_hold": "",
                }
                conn.execute("BEGIN IMMEDIATE")
                conn.executemany(
                    "INSERT INTO metadata(key, value) VALUES (?, ?)", values.items()
                )
                conn.execute(
                    "INSERT INTO periods(period, committed_micro_eur) VALUES (?, 0)",
                    (_period(now),),
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
                "initialized_at": _iso_utc(now),
            }
            _durable_write_json(self.marker_path, marker)
            return marker
        finally:
            with contextlib.suppress(FileNotFoundError):
                temp_db.unlink()

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

    def _marker(self) -> dict:
        state = self.installation_state()
        if state == "absent":
            raise LedgerBlocked("ledger is not initialized; run explicit ledger init")
        if state == "partial":
            raise LedgerBlocked("ledger installation is partial; explicit recovery is required")
        marker = _strict_json_object(self.marker_path)
        expected = {
            "schema_version": INSTALL_MARKER_SCHEMA_VERSION,
            "ledger_schema_version": LEDGER_SCHEMA_VERSION,
            "price_policy_hash": price_policy_hash(),
        }
        for key, value in expected.items():
            if marker.get(key) != value:
                raise LedgerBlocked(f"ledger install marker {key} mismatch")
        generation = marker.get("generation")
        if not isinstance(generation, str) or not _ATTEMPT_ID_RE.fullmatch(generation):
            raise LedgerBlocked("ledger install marker generation is invalid")
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

    def _verify_metadata(self, conn: sqlite3.Connection, marker: dict) -> dict[str, str]:
        try:
            integrity = conn.execute("PRAGMA quick_check(1)").fetchone()
        except sqlite3.Error as exc:
            raise LedgerBlocked("ledger integrity check failed") from exc
        if not integrity or integrity[0] != "ok":
            raise LedgerBlocked("ledger integrity check is not ok")
        metadata = self._metadata(conn)
        expected = {
            "schema_version": str(LEDGER_SCHEMA_VERSION),
            "generation": marker["generation"],
            "price_policy_hash": price_policy_hash(),
            "currency": POLICY_CURRENCY,
            "trial_cutoff_micro_eur": str(TRIAL_CUTOFF_MICRO_EUR),
            "soft_stop_micro_eur": str(SOFT_STOP_MICRO_EUR),
        }
        for key, value in expected.items():
            if metadata.get(key) != value:
                raise LedgerBlocked(f"ledger metadata {key} mismatch")
        _parse_utc(metadata.get("initialized_at"))
        _parse_utc(metadata.get("last_accepted_utc"))
        if not _PERIOD_RE.fullmatch(metadata.get("last_accepted_period", "")):
            raise LedgerBlocked("ledger last accepted period is invalid")
        return metadata

    @staticmethod
    def _begin(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise LedgerBlocked("ledger writer lock could not be acquired") from exc

    def _commit(self, conn: sqlite3.Connection) -> None:
        try:
            conn.commit()
            self._durability_barrier(self.db_path)
        except (OSError, sqlite3.Error) as exc:
            raise LedgerBlocked("ledger durability barrier failed") from exc

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
    def _validate_attempt_id(attempt_id: str) -> None:
        if not isinstance(attempt_id, str) or not _ATTEMPT_ID_RE.fullmatch(attempt_id):
            raise ValueError("attempt_id must be 32 lowercase hexadecimal characters")

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
                if projected > TRIAL_CUTOFF_MICRO_EUR:
                    raise PolicyBlocked("trial spend cutoff would be exceeded")
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
                except sqlite3.IntegrityError as exc:
                    raise LedgerBlocked("attempt_id already exists") from exc
                conn.execute(
                    "UPDATE metadata SET value=? WHERE key='last_accepted_utc'", (timestamp,)
                )
                conn.execute(
                    "UPDATE metadata SET value=? WHERE key='last_accepted_period'", (period,)
                )
                self._commit(conn)
                return Reservation(attempt_id, period, reserve, timestamp)
            except Exception:
                self._rollback(conn)
                raise

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
        actual_micro_eur: int | None = None,
        now: datetime | None = None,
    ) -> dict:
        self._validate_attempt_id(attempt_id)
        if outcome not in {"no-send", "charge-actual", "charge-reserve"}:
            raise ValueError("outcome must be no-send, charge-actual, or charge-reserve")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("a reconciliation reason is required")
        if outcome == "charge-actual" and (
            not isinstance(actual_micro_eur, int)
            or isinstance(actual_micro_eur, bool)
            or actual_micro_eur < 0
        ):
            raise ValueError("charge-actual requires non-negative actual_micro_eur")
        timestamp = _iso_utc((now or self.now()).astimezone(timezone.utc))
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
                already = int(row["actual_micro_eur"] or 0)
                if outcome == "no-send":
                    if already:
                        raise LedgerBlocked("no-send cannot erase an already recorded charge")
                    desired = 0
                elif outcome == "charge-reserve":
                    desired = max(already, int(row["reserved_micro_eur"]))
                else:
                    desired = max(already, int(actual_micro_eur or 0))
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
        timestamp = _iso_utc((now or self.now()).astimezone(timezone.utc))
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
        timestamp = _iso_utc((now or self.now()).astimezone(timezone.utc))
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

    def status(self) -> dict:
        with self._connect() as conn:
            marker = self._marker()
            metadata = self._verify_metadata(conn, marker)
            self._validate_clock(metadata, self.now().astimezone(timezone.utc))
            periods = [dict(row) for row in conn.execute("SELECT * FROM periods ORDER BY period")]
            unresolved = [dict(row) for row in self._unresolved(conn)]
            current_period = metadata["last_accepted_period"]
            current = next(
                (row for row in periods if row["period"] == current_period),
                {"committed_micro_eur": 0},
            )
            return {
                "schema_version": LEDGER_SCHEMA_VERSION,
                "generation": metadata["generation"],
                "policy_hash": metadata["price_policy_hash"],
                "currency": metadata["currency"],
                "trial_cutoff_micro_eur": TRIAL_CUTOFF_MICRO_EUR,
                "soft_stop_micro_eur": SOFT_STOP_MICRO_EUR,
                "current_period": current_period,
                "current_committed_micro_eur": int(current["committed_micro_eur"]),
                "periods": periods,
                "unresolved": unresolved,
                "service_hold": metadata.get("service_hold") or None,
                "ready": not unresolved and not metadata.get("service_hold"),
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
