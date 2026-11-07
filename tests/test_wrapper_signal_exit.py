"""#677: a model child ended by a signal is reported truthfully, and no reader turns that into the wrong thing.

The native program always reports a signal; the npm launcher (codex.js) passes it on from codex 0.166.0. A signal is
a failed exit like any other nonzero code: a delivered reply still commits once, a failed turn's draft is never
published, no watchdog or session budget is charged, and a stop the wrapper made itself keeps its own cause. Plain
`wrap` exits 128+N for signal N. The tests marked POSIX run real stand-in children that die of the signal."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agenttalk import cli, reply_transport, wrapper_logs
from agenttalk.store import Store
from agenttalk.turn_events import EXITS
from agenttalk.wrapper import loop, run, session
from agenttalk.wrapper.obligations import DetectionCommitGate

POSIX = pytest.mark.skipif(os.name == "nt", reason="POSIX signals: a Windows child never exits with a negative code")
SIGNALS = ["SIGHUP", "SIGINT", "SIGTERM", "SIGPIPE", "SIGUSR1"]
# This platform's own numbers (SIGUSR1 is 10 on Linux, 30 on macOS), then the 128+N form the launcher gives
# on Node 22 and older.
CODES = [-getattr(signal, name) for name in SIGNALS if hasattr(signal, name)] + [141, 138]
EVENTS = [{"type": "thread.started", "thread_id": "t-1"}, {"type": "turn.started"},
          {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}, {"type": "turn.completed"}]
SHOWN = {"completed": 4, "partial": 3, "empty": 0}
CHILD = textwrap.dedent("""
    import json, os, signal, sys, time
    if sys.argv[3:] == ["stdin"]:  # only a _ProcStream child gets (and closes) a prompt pipe
        sys.stdin.read()
    for event in json.loads(sys.argv[1]):
        print(json.dumps(event), flush=True)
    if sys.argv[2] == "hold":
        time.sleep(60)
    number = getattr(signal, sys.argv[2])
    signal.signal(number, signal.SIG_DFL)
    os.kill(os.getpid(), number)
    time.sleep(5)
""")


def _signal_name(name: str) -> int:
    if not hasattr(signal, name):
        pytest.skip(f"{name} does not exist on this platform")
    return getattr(signal, name)


def _lines(stream: str) -> list[str]:
    return [json.dumps(event) for event in EVENTS[:SHOWN[stream]]]


def _child_argv(stream: str, ending: str, *extra: str) -> list[str]:
    return [sys.executable, "-c", CHILD, json.dumps(EVENTS[:SHOWN[stream]]), ending, *extra]


class _Stream:
    def __init__(self, lines, returncode):
        self._lines, self.returncode = lines, returncode

    def __iter__(self):
        return iter(self._lines)


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path)
    store.init(["alpha", "beta"])
    return store


def _record(store: Store) -> dict:
    return {"from": "alpha", "kind": "message", "body": "hi", "correlation_id": None, "request_id": None,
            "broadcast_id": None, "id": "m-1"}


# ------------------------------------------------------------------ T1: plain wrap's two exit contracts

@POSIX
@pytest.mark.parametrize("stream", ["completed", "partial", "empty"])
@pytest.mark.parametrize("name", SIGNALS)
def test_plain_wrap_returns_the_signal_and_health_keeps_the_empty_stream_branch(tmp_path, stream, name) -> None:
    number = _signal_name(name)
    store = _store(tmp_path)
    rc = run.run_wrapper(cli="codex", agent="beta", argv=_child_argv(stream, name), store=store, render=False,
                         min_interval=0.0, restart_fn=lambda _signal: None)
    assert rc == -number  # the Python contract: the child's code as Popen reports it
    reason = store.read_health("beta", ttl_seconds=999999)["reason_code"]
    assert reason == ("wrapper_no_turn_driven" if stream == "empty" else "wrapper_child_nonzero_exit")


@pytest.mark.parametrize("child_rc, exit_status", [
    (-1, 129), (-2, 130), (-15, 143), (-13, 141), (-10, 138),
    (0, 0), (7, 7), (141, 141), (138, 138), (4294967281, 4294967281),  # positive codes, Windows' too, pass through
])
def test_the_wrap_command_and_its_log_report_128_plus_n(tmp_path, monkeypatch, child_rc, exit_status) -> None:
    _store(tmp_path)
    nonce = "6" * 32
    monkeypatch.setenv(wrapper_logs.ENV_STDOUT_PATH, str(tmp_path / "stdout.log"))
    monkeypatch.setenv(wrapper_logs.ENV_STDERR_PATH, str(tmp_path / "stderr.log"))
    monkeypatch.setenv(wrapper_logs.ENV_LAUNCH_NONCE, nonce)
    monkeypatch.setattr("sys.stdout", io.StringIO())
    monkeypatch.setattr("sys.stderr", io.StringIO())
    monkeypatch.setattr(run, "run_wrapper", lambda **_kwargs: child_rc)
    logged, real_exited = [], wrapper_logs.WrapperLifecycleLog.wrapper_exited
    monkeypatch.setattr(wrapper_logs.WrapperLifecycleLog, "wrapper_exited",
                        lambda log, code, **kw: logged.append(code) or real_exited(log, code, **kw))
    argv = ["--root", str(tmp_path), "--supervisor-launch-nonce", nonce, "wrap", "--for", "beta", "--cli", "codex",
            "--", sys.executable]
    assert cli.main(argv) == exit_status
    assert logged == [exit_status]  # the lifecycle log records what the caller gets


@POSIX
@pytest.mark.parametrize("name", SIGNALS)
def test_the_wrap_process_exits_128_plus_n_instead_of_dying_or_wrapping(tmp_path, name) -> None:
    number = _signal_name(name)
    _store(tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTTALK_")}
    done = subprocess.run([sys.executable, "-m", "agenttalk", "--root", str(tmp_path), "wrap", "--for", "beta",
                           "--cli", "codex", "--", *_child_argv("completed", name)],
                          env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 128 + number, done.stderr  # not 256-N, and the wrapper itself was not signalled


# ------------------------------------------------------------------ T2: the drive's verdict and its budgets

@pytest.mark.parametrize("name", SIGNALS)
def test_a_negative_code_names_its_signal_and_a_positive_one_stays_a_number(name) -> None:
    code = -_signal_name(name)
    sig = {"started": True, "completed": True, "rc": code}
    assert run._classify_drive_failure(sig) == (
        loop.CLASS_AMBIGUOUS, f"child ended by signal {name} (rc={code}) after start")
    assert run._classify_drive_failure({**sig, "rc": 128 - code}) == (
        loop.CLASS_AMBIGUOUS, f"nonzero child exit (rc={128 - code}) after start")
    summary = run._classify_drive_failure(sig)[1]
    assert session.resume_failure_is_session_attributable(loop.CLASS_AMBIGUOUS, summary) is False


@pytest.mark.parametrize("rc", [-999, None, 7])
def test_a_code_that_proves_no_known_signal_keeps_the_numeric_wording(rc) -> None:
    assert run._classify_drive_failure({"started": True, "completed": True, "rc": rc}) == (
        loop.CLASS_AMBIGUOUS, f"nonzero child exit (rc={rc}) after start")


def test_partial_and_never_started_wording_and_stronger_causes_are_unchanged() -> None:
    base = {"started": True, "completed": True, "rc": -15}
    assert run._classify_drive_failure({**base, "completed": False})[1].startswith("partial stream: started")
    assert run._classify_drive_failure({**base, "started": False})[1].startswith(loop.NEVER_STARTED_SUMMARY_PREFIX)
    stronger = [
        ({"watchdog": {"summary": "watchdog killed a tool"}}, (loop.CLASS_AMBIGUOUS, "watchdog killed a tool")),
        ({"config_blocked": True, "config_blocked_text": "denied"}, (loop.CLASS_CONFIG_BLOCKED, "denied")),
        ({"bus_failure": {"summary": "bus write failed"}}, (loop.CLASS_AMBIGUOUS, "bus write failed")),
        ({"setup_failure": {"summary": "setup failed"}}, (loop.CLASS_CONFIG_BLOCKED, "setup failed")),
        ({"terminal": True, "terminal_text": "boom"},
         (loop.CLASS_AMBIGUOUS, "terminal failure, unrecognized cause: boom")),
    ]
    for evidence, expected in stronger:
        assert run._classify_drive_failure({**base, **evidence}) == expected, evidence


@pytest.mark.parametrize("seed", [0, 3])
@pytest.mark.parametrize("stream", ["completed", "partial", "empty"])
@pytest.mark.parametrize("code", CODES)
def test_a_signal_ended_turn_fails_without_charging_a_budget(tmp_path, code, stream, seed) -> None:
    store = _store(tmp_path)
    state = session.SessionState(cli="codex", codex_thread_id="t-1", resume_failure_count=seed,
                                 resume_failure_key="earlier-attempt" if seed else "")
    drive = run.make_drive(store, "beta", "codex", state, ["codex"], clock=lambda: 0.0, render=False,
                           spawn=lambda _argv, _stdin: _Stream(_lines(stream), code))
    out = drive(_record(store))
    assert (out.ok, out.failure_class, out.interrupted, out.interruption_kind) == (
        False, loop.CLASS_AMBIGUOUS, False, None)
    assert store.read_heartbeat("beta") is None
    assert store.read_health("beta", ttl_seconds=999999)["reason_code"] == "child_nonzero_exit"
    # A failure the session is not to blame for resets the resume count and keeps the session (session.py:441-455).
    assert (state.resume_failure_count, state.resume_failure_key) == (0, "")
    assert (state.codex_thread_id, state.resume_available) == ("t-1", True)


# ------------------------------------------------------------------ T3: the loops, continuous and scoped

def _question(tmp_path: Path, monkeypatch) -> tuple[Store, DetectionCommitGate, object]:
    store = _store(tmp_path)
    monkeypatch.delenv("AGENTTALK_COMMIT_GATE_POLICY", raising=False)
    gate = DetectionCommitGate.from_environment(store, "beta", fence="wrapper-1")
    inbound = store.send(sender="alpha", recipient="beta", kind="question", body="Answer it.",
                         meta={"request_id": "q-677"})
    return store, gate, inbound


def _replies(store: Store) -> list:
    return [m for m in store.messages_for("alpha") if m.sender == "beta"]


def _run(store, gate, drive, scoped: bool, polls: int = 1) -> None:
    loop.run_loop(store, "beta", drive, commit_gate=gate, only_request_id="q-677" if scoped else None,
                  clock=lambda: 0.0, sleep=lambda _delay: None, max_polls=polls)


def _drive(store, spawn):
    return run.make_drive(store, "beta", "codex", session.SessionState(cli="codex"), ["codex"],
                          clock=lambda: 0.0, render=False, spawn=spawn)


@pytest.mark.parametrize("scoped", [False, True], ids=["continuous", "scoped"])
@pytest.mark.parametrize("code", CODES)
def test_a_delivered_reply_commits_once_after_the_child_was_signalled(tmp_path, monkeypatch, code, scoped) -> None:
    store, gate, inbound = _question(tmp_path, monkeypatch)
    runs = []

    def spawn(_argv, _stdin):
        runs.append(code)
        store.send(sender="beta", recipient="alpha", kind="message", body="the answer",
                   meta={"request_id": "q-677", "in_reply_to": inbound.id})
        return _Stream(_lines("completed"), code)

    _run(store, gate, _drive(store, spawn), scoped, polls=2)  # a second poll would recover a missed commit
    assert len(runs) == 1 and len(_replies(store)) == 1
    if scoped:
        assert store.thread_seen("beta", "q-677") == inbound.id
    else:
        assert store.cursor("beta") == inbound.id
    # The failed turn's landed reply is recorded as overriding the drive's verdict. Without the after-drive commit
    # the second poll's pre-drive check still commits it once, so this record is what tells the two apart (F1).
    ledger = json.loads(gate.path.read_text(encoding="utf-8"))
    overrides = [e for e in ledger["transitions"] if e["transition"] == "LANDED_WORK_OVERRIDES_DRIVE_FAILURE"]
    assert [e["data"]["heuristic_class"] for e in overrides] == [loop.CLASS_AMBIGUOUS]


@pytest.mark.parametrize("scoped", [False, True], ids=["continuous", "scoped"])
@pytest.mark.parametrize("code", CODES)
def test_a_failed_turns_draft_is_never_published(tmp_path, monkeypatch, code, scoped) -> None:
    store, gate, inbound = _question(tmp_path, monkeypatch)
    draft = reply_transport.reply_draft_path(store, "beta", inbound.id)
    runs = []

    def spawn(_argv, _stdin):
        runs.append(1)
        if len(runs) == 1:  # the signalled turn writes a draft; the clean re-run writes none
            draft.write_text("the signalled turn's draft\n", encoding="utf-8")
            return _Stream(_lines("completed"), code)
        return _Stream(_lines("completed"), 0)

    drive = _drive(store, spawn)
    _run(store, gate, drive, scoped)
    assert len(runs) == 1 and _replies(store) == []
    if not scoped:
        attempt = store.attempt_record("beta", inbound.id)
        assert attempt["last_failure_class"] == loop.CLASS_AMBIGUOUS
        assert f"(rc={code})" in attempt["last_failure_summary"]
        assert int(attempt.get("ambiguous_failures") or 0) == 1
        assert int(attempt.get("poison_eligible_failures") or 0) == 0
        assert int(attempt.get("interrupted_watchdog_consecutive") or 0) == 0
    _run(store, gate, drive, scoped)  # the next attempt re-drives; the old draft must not become its answer
    assert len(runs) == 2 and _replies(store) == []


# ------------------------------------------------------------------ T4: a stop the wrapper made itself

def _stopping_after_completed_output(real: run._ProcStream):
    """The wrapper's own stop (KeyboardInterrupt) right after the completed stream, while the child still runs."""

    class Stream:
        returncode = property(lambda self: real.returncode)

        def __iter__(self):
            lines = iter(real)
            try:
                for line in lines:
                    yield line
                    if "turn.completed" in line:
                        raise KeyboardInterrupt
            finally:
                lines.close()  # what the unwinding does to the real stream: terminate, wait, report

    return Stream()


@POSIX
@pytest.mark.parametrize("reply", ["sent", "draft"])
def test_a_stop_the_wrapper_made_keeps_its_cause_and_a_restart_never_reruns_a_delivered_reply(
        tmp_path, monkeypatch, reply) -> None:
    store, gate, inbound = _question(tmp_path, monkeypatch)
    draft = reply_transport.reply_draft_path(store, "beta", inbound.id)
    codes, runs = [], []

    def spawn(_argv, stdin):
        runs.append(1)
        if len(runs) > 1:
            return _Stream(_lines("completed"), 0)
        if reply == "sent":
            store.send(sender="beta", recipient="alpha", kind="message", body="the answer",
                       meta={"request_id": "q-677", "in_reply_to": inbound.id})
        else:
            draft.write_text("the stopped turn's draft\n", encoding="utf-8")
        real = run._ProcStream(_child_argv("completed", "hold", "stdin"), stdin,
                               on_exit=lambda _pid, _start, rc: codes.append(rc))
        return _stopping_after_completed_output(real)

    drive = _drive(store, spawn)
    with pytest.raises(KeyboardInterrupt):
        _run(store, gate, drive, scoped=False)
    assert codes == [-signal.SIGTERM]  # the wrapper ended the child; its code is kept as evidence only
    attempt = store.attempt_record("beta", inbound.id)
    assert attempt["in_progress"] and not attempt.get("last_failure_class")  # no failure recorded from that code
    assert len(_replies(store)) == (1 if reply == "sent" else 0)
    _run(store, gate, drive, scoped=False)  # the restart
    assert len(runs) == (1 if reply == "sent" else 2)  # a delivered reply is never run again
    assert len(_replies(store)) == (1 if reply == "sent" else 0)  # and the stopped turn's draft is never published


# ------------------------------------------------------------------ T5 and T6: cadence, logs and journal

@pytest.mark.parametrize("code", [*CODES, 0])
def test_a_signal_ended_cadence_turn_fails(tmp_path, code) -> None:
    store = _store(tmp_path)
    cadence = run.make_cadence_drive(store, "beta", "codex", session.SessionState(cli="codex"), ["codex"],
                                     clock=lambda: 0.0, render=False,
                                     spawn=lambda _argv, _stdin: _Stream(_lines("completed"), code))
    assert cadence({"agent": "beta"}, [{"type": "dead_letter", "key": "dl:1"}]) is (code == 0)


def test_the_journal_records_the_failure_without_a_new_exit_value() -> None:
    trace = run._DispatchTrace()
    trace.mark_launched()
    sig = {"ok": False, "started": True, "completed": True, "rc": -15}
    for got, exit_value in ((run._dispatch_end_fields(trace, sig, backend_profile=None, duration_ms=1), "normal"),
                            (run._dispatch_end_fields(trace, None, backend_profile=None, duration_ms=1), "exception")):
        assert (got["outcome"], got["exit"]) == ("failed", exit_value) and got["exit"] in EXITS


@POSIX
@pytest.mark.parametrize("name", SIGNALS)
def test_the_process_stream_passes_the_signal_on_and_reports_it(name) -> None:
    number = _signal_name(name)
    codes = []
    stream = run._ProcStream(_child_argv("completed", name, "stdin"), None,
                             on_exit=lambda _pid, _start, rc: codes.append(rc))
    assert len(list(stream)) == 4
    assert stream.returncode == -number and codes == [-number]
