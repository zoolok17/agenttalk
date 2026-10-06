"""The opt-in machine budget feed reads only synthetic, temporary ledgers."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agenttalk import budget, cli, ovh_gateway as gateway, web


NOW = datetime(2026, 10, 6, 10, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(gateway, "default_ledger_path", lambda: tmp_path / "ledger.sqlite3")
    monkeypatch.setattr(gateway, "default_install_marker_path", lambda: tmp_path / "install.json")


@pytest.fixture(autouse=True)
def generous_test_budgets(monkeypatch):
    # Figure/format tests are not speed tests. Deadline tests override these.
    production_budgets = {"start": budget.START_SECONDS, "query": budget.QUERY_SECONDS}
    monkeypatch.setattr(budget, "START_SECONDS", 30, raising=False)
    monkeypatch.setattr(budget, "QUERY_SECONDS", 30)
    return production_budgets


def test_production_start_allowance_is_generous_and_matches_reference(generous_test_budgets):
    start_seconds = generous_test_budgets["start"]
    assert start_seconds >= 5
    reference = (Path(__file__).resolve().parents[1] / "docs" / "BUDGET-FEED.md").read_text(encoding="utf-8")
    assert f"The helper process first gets up to {start_seconds:g} seconds" in reference


@pytest.fixture
def ledger(tmp_path):
    result = gateway.SpendLedger(
        tmp_path / "ledger.sqlite3",
        tmp_path / "install.json",
        now=lambda: NOW,
    )
    result.initialize(
        opening_micro_eur=2_000_000,
        opening_evidence="synthetic opening balance",
        child_cap_issuer_token="atgw-" + "i" * 43,
    )
    return result


@pytest.fixture(params=[False, True], ids=["normal-start", "slow-start"])
def helper_start(request, monkeypatch):
    if request.param:
        # Deliberately exceed the old 500 ms whole-process limit before imports.
        monkeypatch.setattr(budget, "_READER_BOOTSTRAP", "import time; time.sleep(0.75); " + budget._READER_BOOTSTRAP)


@contextlib.contextmanager
def serving(store, **kwargs):
    server, thread, url = web.serve_in_thread(store, port=0, **kwargs)
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
        return json.load(response)


@pytest.mark.parametrize("command", ["serve", "dashboard"])
def test_cli_budget_flag_is_explicit_and_off_by_default(command):
    assert cli.build_parser().parse_args([command]).enable_budget is False
    assert cli.build_parser().parse_args([command, "--enable-budget"]).enable_budget is True


@pytest.mark.parametrize("kwargs", [{}, {"enable_budget": False}])
def test_off_has_no_budget_route_and_never_reads_ledger(store, monkeypatch, kwargs):
    def forbidden():
        pytest.fail("disabled budget route attempted a ledger read")

    monkeypatch.setattr(budget, "read_budget", forbidden)
    with serving(store, **kwargs) as url:
        with pytest.raises(urllib.error.HTTPError) as error:
            get(url + "/api/budget")
        assert error.value.code == 404
        assert "budget" not in get(url + "/api/state")
        with pytest.raises(urllib.error.HTTPError) as session_error:
            get(url + "/api/session")
        assert session_error.value.code == 404  # Actions remain off.


def test_opt_in_route_reads_temporary_ledger(store, ledger, monkeypatch):
    read = budget.read_budget
    monkeypatch.setattr(budget, "read_budget", lambda: read(now=NOW))
    with serving(store, enable_budget=True) as url:
        result = get(url + "/api/budget")
    assert result == {
        "schema_version": 1,
        "status": "ok",
        "coverage": budget.COVERAGE,
        "observed_at": "2026-10-06T10:00:00.000000Z",
        "age_seconds": 0,
        "cache_seconds": 10,
        "month": "2026-10",
        "committed_micro_eur": 2_000_000,
        "opening_micro_eur": 2_000_000,
        "opening_period": "2026-10",
        "soft_stop_micro_eur": gateway.SOFT_STOP_MICRO_EUR,
        "trial_cutoff_micro_eur": gateway.TRIAL_CUTOFF_MICRO_EUR,
        "external_ceiling_micro_eur": gateway.EXTERNAL_CEILING_MICRO_EUR,
        "service_hold": False,
        "unresolved_attempts": 0,
    }
    assert "this machine" in result["coverage"].lower()
    assert "provider" in result["coverage"].lower()
    assert "other machines" in result["coverage"].lower()


def test_absent_ledger_is_not_set_up_and_creates_nothing(tmp_path):
    before = set(tmp_path.rglob("*"))
    assert budget.read_budget(now=NOW)["status"] == "not_set_up"
    assert set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("missing", ["db_path", "marker_path"])
def test_partial_install_is_unavailable_and_creates_nothing(ledger, missing):
    getattr(ledger, missing).unlink()
    parent = ledger.db_path.parent
    before = {p.name: p.read_bytes() for p in parent.iterdir() if p.is_file()}
    result = budget.read_budget(now=NOW)
    assert result["status"] == "unavailable"
    assert "committed_micro_eur" not in result
    assert {p.name: p.read_bytes() for p in parent.iterdir() if p.is_file()} == before


def test_write_locked_ledger_returns_busy_promptly_and_keeps_bytes(ledger):
    # Closing another handle while SQLite holds a POSIX lock releases that lock.
    before = ledger.db_path.read_bytes()
    connection = sqlite3.connect(ledger.db_path, timeout=0)
    try:
        connection.execute("BEGIN EXCLUSIVE")
        result = budget.read_budget(now=NOW)
        assert result["status"] == "busy"
        assert result["message"] == "Busy, try again."
    finally:
        connection.rollback()
        connection.close()
    assert ledger.db_path.read_bytes() == before


def test_reads_leave_database_marker_and_sidecars_byte_identical(ledger):
    parent = ledger.db_path.parent
    before = {p.name: p.read_bytes() for p in parent.iterdir() if p.is_file()}
    for _ in range(3):
        assert budget.read_budget(now=NOW)["status"] == "ok"
    after = {p.name: p.read_bytes() for p in parent.iterdir() if p.is_file()}
    assert after == before


def test_new_month_does_not_recount_reset_opening_balance(ledger):
    result = budget.read_budget(now=datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert result["month"] == "2026-11"
    assert result["committed_micro_eur"] == 0
    assert (result["opening_micro_eur"], result["opening_period"]) == (2_000_000, "2026-10")


def test_committed_and_unresolved_reservations_and_hold_are_separate(ledger, helper_start):
    ledger.reserve("a" * 32)
    with contextlib.closing(sqlite3.connect(ledger.db_path)) as connection:
        connection.execute("UPDATE metadata SET value='private hold detail' WHERE key='service_hold'")
        connection.commit()
    result = budget.read_budget(now=NOW)
    assert result["committed_micro_eur"] == 2_000_000
    assert result["unresolved_attempts"] == 1
    assert result["service_hold"] is True
    assert "private hold detail" not in json.dumps(result)


def test_corrupt_ledger_does_not_return_zero_or_expose_a_path(tmp_path, helper_start):
    (tmp_path / "ledger.sqlite3").write_bytes(b"not a database")
    result = budget.read_budget(now=NOW)
    assert result["status"] == "unavailable"
    assert "committed_micro_eur" not in result
    assert str(tmp_path) not in json.dumps(result)


def test_start_timeout_is_unavailable_and_reaps_the_real_helper(tmp_path, monkeypatch):
    monkeypatch.setattr(budget, "START_SECONDS", 0.05)
    monkeypatch.setattr(budget, "_READER_BOOTSTRAP", "import time; time.sleep(60)")
    start = budget._start_reader
    children = []

    def capture(*args):
        child = start(*args)
        children.append(child)
        return child

    monkeypatch.setattr(budget, "_start_reader", capture)
    result = budget.read_budget(now=NOW)
    assert result["status"] == "unavailable"
    assert "committed_micro_eur" not in result
    assert not (tmp_path / "ledger.sqlite3").exists()
    assert len(children) == 1 and children[0].poll() is not None
    assert children[0].stdout.closed and children[0].stdin.closed


@pytest.mark.parametrize("permission", ["read\n", ""])
def test_helper_opens_no_ledger_before_read_permission(tmp_path, monkeypatch, permission):
    output = io.BytesIO()
    stdout = io.TextIOWrapper(output, write_through=True)
    events = []

    class Input:
        def readline(self):
            assert output.getvalue() == b"ready\n"
            assert events == []
            events.append("permission")
            return permission

    def snapshot(*args):
        assert permission == "read\n" and events == ["permission"]
        events.append("read")
        return {"status": "not_set_up"}

    monkeypatch.setattr(budget, "_read_snapshot", snapshot)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stdin", Input())
    monkeypatch.setattr(
        sys, "argv", ["reader", str(tmp_path / "ledger"), str(tmp_path / "marker"), NOW.isoformat(), "0.15"]
    )
    if permission:
        budget._reader_main()
        assert events == ["permission", "read"]
    else:
        with pytest.raises(SystemExit) as error:
            budget._reader_main()
        assert error.value.code == 2 and events == ["permission"]


@pytest.mark.parametrize("failure", ["deadline", "crash", "interrupted", "bad-ready", "thread-start"])
def test_read_phase_failure_reaps_helper_without_figures(monkeypatch, failure, generous_test_budgets):
    class Helper:
        stdout = io.BytesIO(b"bad\n" if failure == "bad-ready" else b"ready\n")
        returncode = None
        killed = False
        calls = []

        def communicate(self, input=None, timeout=None):
            self.calls.append((input, timeout))
            if timeout is not None:
                assert input == b"read\n" and 0 < timeout <= 0.15
                if failure == "deadline":
                    raise subprocess.TimeoutExpired("reader", timeout)
                if failure == "interrupted":
                    raise KeyboardInterrupt
                self.returncode = 7
            return b"", None

        def poll(self):
            return self.returncode

        def kill(self):
            self.killed = True
            self.returncode = -1

    child = Helper()
    if failure == "thread-start":
        class CannotStart:
            ident = None

            def __init__(self, **kwargs):
                pass

            def start(self):
                raise RuntimeError("cannot start helper reader thread")

        monkeypatch.setattr(budget.threading, "Thread", CannotStart)
    monkeypatch.setattr(budget, "QUERY_SECONDS", generous_test_budgets["query"])
    monkeypatch.setattr(budget, "_start_reader", lambda *args: child)
    if failure == "interrupted":
        with pytest.raises(KeyboardInterrupt):
            budget.read_budget(now=NOW)
    else:
        result = budget.read_budget(now=NOW)
        assert result["status"] == ("busy" if failure == "deadline" else "unavailable")
        assert "committed_micro_eur" not in result
    assert child.poll() is not None
    assert child.killed == (failure != "crash")
    assert child.calls[-1] == (None, None)  # The helper is always reaped.


@pytest.mark.parametrize("enabled", [False, True])
def test_budget_flag_does_not_wrap_or_extend_existing_answers(store, monkeypatch, enabled):
    # Frozen builders let us compare exact response shapes without clocks or scans.
    routes = {
        "/api/status": "status_payload",
        "/api/state": "build_state",
        "/api/messages": "messages_payload",
        "/api/intents": "build_intents",
        "/api/preflight": "build_preflight",
        "/api/attention": "build_attention",
        "/api/gates": "build_gates",
        "/api/risk-register": "build_risk_register",
        "/api/ownership": "build_ownership",
        "/api/learning": "build_learning",
        "/api/onboarding": "build_onboarding",
        "/api/lead-chat": "build_lead_chat",
        "/api/threads": "build_threads_index",
    }
    expected = {"existing": [1, None, {"unchanged": True}]}
    for builder in routes.values():
        monkeypatch.setattr(web, builder, lambda *args, **kwargs: expected)
    monkeypatch.setattr(web._snapshots.SnapshotService, "board", lambda self: expected)
    with serving(store, enable_budget=enabled) as url:
        for path in [*routes, "/api/work-board"]:
            assert get(url + path) == expected, path


@pytest.mark.parametrize("command", ["serve", "dashboard"])
def test_cli_passes_opt_in_to_server(store, monkeypatch, command):
    flags = []

    class Server:
        server_address = ("127.0.0.1", 12345)

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    def make_server(*args, **kwargs):
        flags.append(kwargs.get("enable_budget", False))
        return Server()

    monkeypatch.setattr(web, "make_server", make_server)
    monkeypatch.setattr(cli, "_get_store", lambda args: store)
    assert cli.main([command, "--enable-budget", "--port", "0"]) == 0
    assert flags == [True]


def test_cache_reuses_success_and_failure_for_ten_seconds(monkeypatch):
    clock = [0.0]
    calls = []

    def read():
        calls.append(True)
        return {"status": "ok" if len(calls) == 1 else "busy"}

    monkeypatch.setattr(budget, "read_budget", read)
    feed = budget.BudgetFeed(clock=lambda: clock[0])
    assert feed.get()["status"] == "ok"
    clock[0] = 9.99
    assert feed.get()["status"] == "ok"
    assert len(calls) == 1
    clock[0] = 10.0
    assert feed.get()["status"] == "busy"
    clock[0] = 19.99
    assert feed.get()["status"] == "busy"
    assert len(calls) == 2


def test_concurrent_polls_start_only_one_reader(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def read():
        calls.append(True)
        entered.set()
        assert release.wait(5)
        return {"status": "ok"}

    monkeypatch.setattr(budget, "read_budget", read)
    feed = budget.BudgetFeed()
    first = threading.Thread(target=feed.get)
    first.start()
    try:
        assert entered.wait(5)
        assert all(feed.get()["status"] == "unavailable" for _ in range(100))
        assert len(calls) == 1
    finally:
        release.set()
        first.join(5)
        assert not first.is_alive()


def test_slow_query_times_out_and_releases_connection(ledger, monkeypatch):
    connections = []

    def slow_snapshot(self, connection):
        connections.append(connection)
        connection.execute("SELECT * FROM metadata").fetchall()
        connection.execute(
            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n"
        ).fetchone()

    monkeypatch.setattr(gateway.SpendLedger, "_status_snapshot", slow_snapshot)
    clock = iter([0.0, 31.0])
    monkeypatch.setattr(budget.time, "monotonic", lambda: next(clock, 31.0))
    result = budget._read_snapshot(ledger.db_path, ledger.marker_path, NOW)
    assert result["status"] == "busy"
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    with contextlib.closing(sqlite3.connect(ledger.db_path, timeout=0)) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        writer.rollback()


def test_hard_deadline_stops_worker_holding_read_lock(ledger, tmp_path, monkeypatch):
    ready = tmp_path / "reader-ready"
    monkeypatch.setattr(
        budget,
        "_READER_BOOTSTRAP",
        "import sqlite3, sys, time\n"
        "from pathlib import Path\n"
        "c=sqlite3.connect(Path(sys.argv[2]).as_uri()+'?mode=ro',uri=True,timeout=0)\n"
        "c.execute('BEGIN')\nc.execute('SELECT * FROM metadata').fetchall()\n"
        "Path(sys.argv[3]).write_text('ready')\n"
        "sys.stdout.buffer.write(b'ready\\n');sys.stdout.buffer.flush()\ntime.sleep(60)\n",
    )
    process = budget._start_reader(ledger.db_path, ready, NOW)
    writer_started = threading.Event()
    writes, errors = [], []

    def write_during_read():
        start = time.monotonic()
        writer_started.set()
        try:
            ledger.reserve("b" * 32)
            writes.append(time.monotonic() - start)
        except Exception as exc:
            errors.append(exc)

    writer_thread = None
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            threading.Event().wait(0.005)
        assert ready.exists()
        writer_thread = threading.Thread(target=write_during_read)
        writer_thread.start()
        assert writer_started.wait(5)
        # Reuse a real already-started child to make the held-lock case deterministic.
        monkeypatch.setattr(budget, "_start_reader", lambda *args: process)
        communicate = process.communicate

        def deadline_expired(input=None, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired(process.args, timeout)
            return communicate(input=input, timeout=timeout)

        monkeypatch.setattr(process, "communicate", deadline_expired)
        assert budget.read_budget(now=NOW)["status"] == "busy"
        assert process.poll() is not None
        writer_thread.join(5)
        assert not writer_thread.is_alive()
        assert errors == []
        assert len(writes) == 1
        with contextlib.closing(sqlite3.connect(ledger.db_path, timeout=0)) as writer:
            writer.execute("BEGIN EXCLUSIVE")
            writer.execute("UPDATE metadata SET value='' WHERE key='service_hold'")
            writer.commit()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        if writer_thread is not None:
            writer_thread.join(5)


def test_writer_commits_while_feed_is_polled(ledger):
    feed = budget.BudgetFeed()
    # Use the real reader and a fixed ledger clock; cache still follows monotonic time.
    feed._read = lambda: budget.read_budget(now=NOW)
    barrier = threading.Barrier(2)
    results = []

    def poll():
        barrier.wait(timeout=5)
        for _ in range(100):
            results.append(feed.get()["status"])

    reader = threading.Thread(target=poll)
    reader.start()
    try:
        barrier.wait(timeout=5)
        for attempt in range(20):
            attempt_id = f"{attempt:032x}"
            ledger.reserve(attempt_id)
            ledger.settle(attempt_id, model=gateway.MODEL_ALIAS, input_tokens=1000, output_tokens=100)
    finally:
        reader.join(40)
    assert not reader.is_alive()
    assert len(results) == 100
    assert set(results) <= {"ok", "busy"}


def test_settled_money_matches_gateway_report(ledger):
    ledger.reserve("a" * 32)
    ledger.settle("a" * 32, model=gateway.MODEL_ALIAS, input_tokens=1000, output_tokens=100)
    result = budget.read_budget(now=NOW)
    report = ledger.report()
    assert result["committed_micro_eur"] == report["periods"][0]["committed_micro_eur"]
    assert result["committed_micro_eur"] > 2_000_000
    assert result["unresolved_attempts"] == 0


def test_ledger_owns_thresholds_not_module_defaults(tmp_path):
    ledger = gateway.SpendLedger(tmp_path / "ledger.sqlite3", tmp_path / "install.json", now=lambda: NOW)
    ledger.initialize(
        opening_micro_eur=500_000,
        opening_evidence="synthetic reset",
        child_cap_issuer_token="atgw-" + "i" * 43,
        soft_stop_micro_eur=35_000_000,
        trial_cutoff_micro_eur=38_000_000,
        external_ceiling_micro_eur=50_000_000,
    )
    result = budget.read_budget(now=NOW)
    assert result["soft_stop_micro_eur"] == 35_000_000
    assert result["trial_cutoff_micro_eur"] == 38_000_000
    assert result["external_ceiling_micro_eur"] == 50_000_000


@pytest.mark.parametrize("force_writable_open", [False, True])
def test_accidental_snapshot_write_is_refused_and_file_is_unchanged(ledger, monkeypatch, force_writable_open):
    if force_writable_open:
        # Fault-inject a writable open to prove query_only independently of mode=ro.
        connect = sqlite3.connect

        def writable_connect(database, **kwargs):
            return connect(database.replace("?mode=ro", "?mode=rw"), **kwargs)

        monkeypatch.setattr(budget.sqlite3, "connect", writable_connect)

    refused = []

    def writes(self, connection):
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("UPDATE metadata SET value='changed' WHERE key='service_hold'")
        refused.append(True)
        raise gateway.GatewayError("synthetic snapshot stopped after checking write refusal")

    before = ledger.db_path.read_bytes()
    monkeypatch.setattr(gateway.SpendLedger, "_status_snapshot", writes)
    assert budget._read_snapshot(ledger.db_path, ledger.marker_path, NOW)["status"] == "unavailable"
    assert refused == [True]
    assert ledger.db_path.read_bytes() == before


def test_hot_journal_is_unavailable_without_recovering_or_changing_files(ledger):
    parent = ledger.db_path.parent
    original = ledger.db_path.read_bytes()
    # Spill uncommitted pages, then exit without SQLite cleanup to leave a real
    # hot journal. Use the interpreter directly so a timeout owns the whole child.
    subprocess.run(
        [
            sys._base_executable,
            "-I",
            "-S",
            "-B",
            "-c",
            "import os, sqlite3, sys\n"
            "c = sqlite3.connect(sys.argv[1], timeout=0)\n"
            "c.execute('PRAGMA journal_mode=PERSIST')\n"
            "c.execute('PRAGMA cache_size=1')\n"
            "c.execute('BEGIN')\n"
            "c.execute('CREATE TABLE hot_journal_probe(value)')\n"
            "for _ in range(3000):\n"
            "    c.execute('INSERT INTO hot_journal_probe VALUES (?)', ('y'*500,))\n"
            "os._exit(0)\n",
            str(ledger.db_path),
        ],
        check=True,
        timeout=15,
    )
    journal = ledger.db_path.with_name(ledger.db_path.name + "-journal")
    assert journal.read_bytes()[:8] == bytes.fromhex("d9d505f920a163d7")
    assert ledger.db_path.read_bytes() != original
    before = {p.name: p.read_bytes() for p in parent.iterdir() if p.is_file()}
    result = budget.read_budget(now=NOW)
    assert result["status"] == "unavailable"
    assert "committed_micro_eur" not in result
    assert {p.name: p.read_bytes() for p in parent.iterdir() if p.is_file()} == before


@pytest.mark.parametrize("stored", [2_000_000.5, "broken"])
def test_invalid_stored_money_is_not_coerced_or_shown_as_zero(ledger, stored):
    with contextlib.closing(sqlite3.connect(ledger.db_path)) as connection:
        connection.execute("UPDATE periods SET committed_micro_eur=?", (stored,))
        connection.commit()
    result = budget.read_budget(now=NOW)
    assert result["status"] == "unavailable"
    assert "committed_micro_eur" not in result


def test_wal_ledger_is_refused_without_sidecar_changes(ledger):
    with contextlib.closing(sqlite3.connect(ledger.db_path)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute("UPDATE metadata SET value='' WHERE key='service_hold'")
        connection.commit()
        parent = ledger.db_path.parent
        before = {p.name: p.read_bytes() for p in parent.iterdir() if p.is_file()}
        assert budget.read_budget(now=NOW)["status"] == "unavailable"
        assert {p.name: p.read_bytes() for p in parent.iterdir() if p.is_file()} == before
