"""The dashboard's opt-in, machine-wide budget feed.

The ledger uses rollback journaling: even read-only connections briefly hold
reader locks. A disposable process bounds that lifetime, including Python work
between SQL queries. The gateway's connection/configuration path is never used.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3

# Used only to supervise the fixed local reader, with an argument list.
import subprocess  # nosec B404
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from . import ovh_gateway as gateway

QUERY_SECONDS = 0.15
# Cold interpreter imports on a loaded machine must not consume the lock budget.
# Ten seconds allows a slow start while still bounding a broken installation.
START_SECONDS = 10
CACHE_SECONDS = 10
_READY = b"ready\n"

# Bypass Windows' virtualenv redirector: terminating that launcher alone leaves
# its interpreter alive. Reuse the server's import paths with the base interpreter,
# without running startup hooks or writing bytecode. The worker spawns no children.
_READER_BOOTSTRAP = (
    "import json, runpy, sys; "
    "sys.path = json.loads(sys.argv.pop(1)); "
    "runpy.run_module('agenttalk.budget', run_name='__main__')"
)

COVERAGE = "These are this machine's ledger figures, not the provider's bill. Other machines are not included."


def _answer(status: str, now: datetime, **fields) -> dict:
    return {
        "schema_version": 1,
        "status": status,
        "coverage": COVERAGE,
        "observed_at": gateway._iso_utc(now),
        **fields,
    }


def _busy(now: datetime) -> dict:
    return _answer("busy", now, message="Busy, try again.")


def _read_snapshot(path: Path, marker: Path, now: datetime) -> dict:
    connection = None
    deadline = time.monotonic() + QUERY_SECONDS
    expired = False

    def interrupt() -> bool:
        nonlocal expired
        expired = time.monotonic() >= deadline
        return expired

    try:
        # A read-only WAL connection may create shared-memory sidecars. The
        # gateway uses PERSIST, so refuse WAL instead of opening it or converting it.
        with path.open("rb") as source:
            header = source.read(20)
        if header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
            return _answer("unavailable", now, message="Budget figures are unavailable.")
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.set_progress_handler(interrupt, 100)
        connection.execute("BEGIN")
        ledger = gateway.SpendLedger(path, marker, now=lambda: now)
        snapshot = ledger._status_snapshot(connection)
        if interrupt():
            return _busy(now)
        # The status reader exposes all stored periods. Select the UTC calendar
        # month without advancing the ledger's admission clock or counting an old
        # opening balance a second time. Unresolved reservations stay separate,
        # exactly as they do in gateway status/report.
        month = now.strftime("%Y-%m")
        committed = next(
            (row["committed_micro_eur"] for row in snapshot["periods"] if row["period"] == month),
            0,
        )
        return _answer(
            "ok",
            now,
            month=month,
            committed_micro_eur=gateway._stored_money(committed),
            opening_micro_eur=snapshot["opening_micro_eur"],
            opening_period=snapshot["opening_period"],
            soft_stop_micro_eur=snapshot["soft_stop_micro_eur"],
            trial_cutoff_micro_eur=snapshot["trial_cutoff_micro_eur"],
            external_ceiling_micro_eur=snapshot["external_ceiling_micro_eur"],
            service_hold=bool(snapshot["service_hold"]),
            unresolved_attempts=len(snapshot["unresolved"]),
        )
    except FileNotFoundError:
        status = "not_set_up" if not path.exists() and not marker.exists() else "unavailable"
        message = "Not set up." if status == "not_set_up" else "Budget figures are unavailable."
        return _answer(status, now, message=message)
    except (OSError, sqlite3.Error, gateway.GatewayError, ValueError, TypeError, KeyError) as exc:
        # Snapshot validation wraps SQLite failures; retain busy/timeout semantics
        # without sending exception text (which may contain private values).
        cause: BaseException | None = exc
        while cause is not None:
            if isinstance(cause, sqlite3.OperationalError) and str(cause) in {
                "database is locked",
                "database table is locked",
                "interrupted",
            }:
                return _busy(now)
            cause = cause.__cause__
        if expired:
            return _busy(now)
        return _answer("unavailable", now, message="Budget figures are unavailable.")
    finally:
        if connection is not None:
            connection.set_progress_handler(None, 0)
            try:
                connection.rollback()
            finally:
                connection.close()


def _start_reader(path: Path, marker: Path, now: datetime) -> subprocess.Popen:
    # The interpreter and bootstrap are fixed; no HTTP arguments enter this call.
    return subprocess.Popen(  # nosec B603
        [
            sys._base_executable,
            "-I",
            "-S",
            "-B",
            "-c",
            _READER_BOOTSTRAP,
            json.dumps(sys.path),
            str(path),
            str(marker),
            now.isoformat(),
            str(QUERY_SECONDS),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def read_budget(*, now: datetime | None = None) -> dict:
    """Allow startup first, then bound the read separately; no cached result here."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    process = None
    ready_reader = None
    try:
        path = gateway.default_ledger_path().resolve()
        marker = gateway.default_install_marker_path().resolve()
        process = _start_reader(path, marker, now)
        ready = []

        def read_ready() -> None:
            try:
                ready.append(process.stdout.readline(len(_READY)))
            except (OSError, ValueError):
                pass  # A broken startup pipe is unavailable, with no private detail.

        ready_reader = threading.Thread(target=read_ready, daemon=True)
        ready_reader.start()
        ready_reader.join(START_SECONDS)
        if ready_reader.is_alive() or ready != [_READY]:
            return _answer("unavailable", now, message="Budget figures are unavailable.")
        # The child cannot open the ledger until this permission arrives. The
        # parent's deadline covers Python work as well as SQLite progress checks.
        output, _ = process.communicate(input=b"read\n", timeout=QUERY_SECONDS)
        if process.returncode != 0:
            return _answer("unavailable", now, message="Budget figures are unavailable.")
        result = json.loads(output)
        if not isinstance(result, dict) or result.get("status") not in {"ok", "busy", "not_set_up", "unavailable"}:
            raise ValueError("invalid budget answer")
        return result
    except subprocess.TimeoutExpired:
        return _busy(now)
    except (OSError, ValueError, RuntimeError):
        return _answer("unavailable", now, message="Budget figures are unavailable.")
    finally:
        if process is not None:
            # Query timeouts normally unwind the child's finally. For a stalled
            # interpreter or file read, killing and reaping releases OS handles
            # and reader locks before returning; no reader is left in a thread.
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            if ready_reader is not None and ready_reader.ident is not None:
                # Killing the sole writer releases a blocked startup read. Join
                # before communicate so two readers never share the output pipe.
                ready_reader.join()
            process.communicate()


class BudgetFeed:
    """One result per server for ten seconds, including unavailable/busy results."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._read = read_budget
        self._lock = threading.Lock()
        self._result: dict | None = None
        self._observed = 0.0

    def get(self) -> dict:
        if not self._lock.acquire(blocking=False):
            return {
                **_answer(
                    "unavailable", datetime.now(timezone.utc), message="Budget figures are being read. Try again."
                ),
                "age_seconds": 0,
                "cache_seconds": CACHE_SECONDS,
            }
        try:
            if self._result is None or self._clock() - self._observed >= CACHE_SECONDS:
                self._result = self._read()
                self._observed = self._clock()
            return {
                **self._result,
                "age_seconds": max(0, int(self._clock() - self._observed)),
                "cache_seconds": CACHE_SECONDS,
            }
        finally:
            self._lock.release()


def _reader_main() -> None:
    # Internal child entry point. Arguments come from the server, never an HTTP
    # path or query. No ledger access is allowed before the parent's permission.
    global QUERY_SECONDS
    if len(sys.argv) != 5:
        sys.exit(2)
    QUERY_SECONDS = float(sys.argv[4])
    sys.stdout.buffer.write(_READY)
    sys.stdout.buffer.flush()
    if sys.stdin.readline() != "read\n":
        sys.exit(2)
    print(json.dumps(_read_snapshot(Path(sys.argv[1]), Path(sys.argv[2]), datetime.fromisoformat(sys.argv[3]))))


if __name__ == "__main__":
    _reader_main()
