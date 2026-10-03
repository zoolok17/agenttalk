"""What `agenttalk status` and `doctor` say about the turn journal.

The label is computed from live facts (the process, its start token, how recent
the record is, and the wrapper's own health and runtime records), never from the
newest file alone. With the journal never used, `status` and `doctor` read
exactly as they did before.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import pytest

from agenttalk import cli, doctor
from agenttalk import turn_events as te
from agenttalk import wrapper_runtime as wr
from agenttalk.store import Store
from agenttalk.wrapper.health import WrapperHealthWriter

ME = os.getpid()
TOKENS = {ME: "tok-me", 4242: "tok-other"}


def _tok(pid: int) -> str | None:
    return TOKENS.get(pid)


@pytest.fixture(autouse=True)
def _journal_root_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv(te.ENV_TURN_EVENTS_DIR, str(tmp_path / "journal-root"))


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "project")
    store.init(["alpha", "beta"])
    return store


def _runtime(pid: int = ME, start: str | None = "tok-me") -> dict:
    return {"wrapper_pid": pid, "wrapper_start": start}


def _real_runtime() -> dict:
    """This process as the real wrapper runtime record describes it."""
    return {"wrapper_pid": ME, "wrapper_start": wr.process_start_token(ME)}


def _label(store, mode, runtime=None, *, now=None, tokens=None):
    """`tokens=None` uses the real process start token; a function fakes other processes."""
    return te.status_label(
        store.root,
        "beta",
        health_mode=mode,
        runtime_record=runtime,
        now_epoch=now,
        start_token=tokens,
    )


def _live_sink(store, **kw) -> te.TurnEventSink:
    sink = te.TurnEventSink(te.default_turn_events_root(store.root), "beta", **kw)
    assert sink.start() is True
    _wait_for_status(sink)
    return sink


def _wait_for_status(sink, seconds: float = 5.0) -> None:
    """The first status record is written just after `start` returns."""
    deadline = time.monotonic() + seconds
    while not os.path.exists(sink.status_path) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert os.path.exists(sink.status_path)


def _write_status(store, name="status-g-old.json", **over):
    directory = te.default_turn_events_root(store.root) / "beta"
    directory.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": 1,
        "agent": "beta",
        "stream": "beta.g-old",
        "mode": "loop",
        "unmanaged": [],
        "pid": 4242,
        "process_start_token": "tok-other",
        "started_at": "2026-10-03T00:00:00.000Z",
        "updated_at": te.format_time(int(time.time() * 1000)),
        "state": "on",
        "off_reason": None,
        "last_fault": None,
        "counts": {},
        "faults": {},
    }
    data.update(over)
    (directory / name).write_text(json.dumps(data), encoding="ascii")


# --- the labels ---------------------------------------------------------------------


def test_a_live_loop_writer_is_on(tmp_path):
    store = _store(tmp_path)
    sink = _live_sink(store)
    try:
        assert _label(store, "wrapper-loop", _real_runtime()) == "on (loop)"
    finally:
        sink.close()


def test_a_lead_loop_says_its_cadence_turns_are_unmanaged(tmp_path):
    store = _store(tmp_path)
    sink = _live_sink(store, unmanaged=("cadence",))
    try:
        assert _label(store, "lead-loop", _real_runtime()) == "on (loop); cadence turns unmanaged"
        assert _label(store, "wrapper-loop", _real_runtime()) == "on (loop); cadence turns unmanaged"
    finally:
        sink.close()


def test_a_closed_stream_is_ended_not_on(tmp_path):
    store = _store(tmp_path)
    sink = _live_sink(store)
    sink.close()
    assert _label(store, "wrapper-loop", _real_runtime()) == "ended"


def test_a_live_process_whose_record_stopped_updating_is_not_responding(tmp_path):
    store = _store(tmp_path)
    sink = _live_sink(store)
    try:
        later = time.time() + te.TURN_EVENTS_STATUS_STALE_SECONDS + 5
        assert _label(store, "wrapper-loop", _real_runtime(), now=later) == "writer not responding"
        soon = time.time() + 1
        assert _label(store, "wrapper-loop", _real_runtime(), now=soon) == "on (loop)"
    finally:
        sink.close()


def test_a_run_whose_start_failed_shows_off_with_its_reason(tmp_path):
    store = _store(tmp_path)

    class Failing(te.JournalFiles):
        def append_line(self, path, data):
            raise OSError("no")

    sink = te.TurnEventSink(te.default_turn_events_root(store.root), "beta", files=Failing())
    assert sink.start() is False
    assert _label(store, "wrapper-loop", _real_runtime()) == "off (start_failed)"
    sink.close()


def test_a_start_that_timed_out_shows_its_own_reason(tmp_path):
    import threading

    store = _store(tmp_path)
    unblock = threading.Event()

    class Slow(te.JournalFiles):
        def append_line(self, path, data):
            unblock.wait(10)
            super().append_line(path, data)

    sink = te.TurnEventSink(te.default_turn_events_root(store.root), "beta", files=Slow(), start_seconds=0.2)
    assert sink.start() is False
    unblock.set()
    sink._thread.join(5)  # noqa: SLF001
    assert _label(store, "wrapper-loop", _real_runtime()) == "off (start_timeout)"
    sink.close()


def test_no_record_at_all_is_off(tmp_path):
    store = _store(tmp_path)
    assert _label(store, "wrapper-loop", _runtime()) == "off"


def test_an_old_record_from_an_earlier_run_never_says_on(tmp_path):
    store = _store(tmp_path)
    _write_status(store, pid=4242, process_start_token="tok-other", state="on")
    # this wrapper (a different, live process) is running without a journal
    assert _label(store, "wrapper-loop", _runtime(pid=ME, start="tok-me"), tokens=_tok) == "off"
    # no wrapper is known to be running: the old stream's process is not there either
    # a token that cannot be read is no evidence of a running writer
    assert _label(store, "wrapper-loop", None, tokens=lambda pid: None) == "ended"
    gone = _label(store, "wrapper-loop", None, now=time.time() + 600, tokens=lambda pid: None)
    assert gone == "ended"


def test_a_pid_reused_by_another_process_is_not_the_old_writer(tmp_path):
    store = _store(tmp_path)
    _write_status(store, pid=4242, process_start_token="tok-old-run", state="on")
    assert _label(store, "wrapper-loop", None, tokens=_tok) == "ended"  # pid 4242 now has another start token


def test_an_old_off_record_keeps_its_reason_visible_but_never_becomes_on(tmp_path):
    store = _store(tmp_path)
    _write_status(store, state="off", off_reason="close_timeout")
    assert _label(store, "wrapper-loop", None, tokens=_tok) == "off (close_timeout)"
    _write_status(store, state="off", off_reason="free text from a bad file")
    assert _label(store, "wrapper-loop", None, tokens=_tok) == "off"


def test_garbage_in_a_status_file_is_ignored(tmp_path):
    store = _store(tmp_path)
    directory = te.default_turn_events_root(store.root) / "beta"
    directory.mkdir(parents=True)
    (directory / "status-x.json").write_text("{not json", encoding="ascii")
    (directory / "status-y.json").write_text(json.dumps({"schema_version": 9}), encoding="ascii")
    assert _label(store, "wrapper-loop", _runtime()) == "off"


def test_a_one_shot_loop_wrapper_and_a_plain_wrapper_are_unmanaged_and_say_so(tmp_path):
    store = _store(tmp_path)
    assert _label(store, "wrapper-one-shot", _runtime(), tokens=_tok) == "unmanaged (one_shot)"
    assert _label(store, "wrapper-one-shot", None, tokens=_tok) == "unmanaged (plain)"
    # a runtime record of a process that is gone does not make a plain wrapper a one-shot
    assert _label(store, "wrapper-one-shot", _runtime(pid=4242, start="tok-old"), tokens=_tok) == "unmanaged (plain)"


def test_an_unknown_or_missing_mode_is_off(tmp_path):
    store = _store(tmp_path)
    sink = _live_sink(store)
    try:
        assert _label(store, None, _real_runtime()) == "off"
        assert _label(store, "something-else", _real_runtime()) == "off"
    finally:
        sink.close()


# --- status and doctor ------------------------------------------------------------------


def _wrapper_state(store, mode="wrapper-loop") -> None:
    WrapperHealthWriter(store, "beta", "claude", mode=mode, min_interval=0.0).idle()
    wr.WrapperRuntimeWriter(store.state_dir, "beta", "gen-1").idle()


def _row(store) -> dict:
    return next(a for a in cli._gather_status(store)["agents"] if a["name"] == "beta")


def test_status_reads_exactly_as_before_when_the_journal_was_never_used(tmp_path):
    store = _store(tmp_path)
    _wrapper_state(store)
    assert all("turn_events" not in a for a in cli._gather_status(store)["agents"])
    assert doctor._check_turn_journal(store) is None


def test_status_json_and_text_show_the_label_once_the_journal_exists(tmp_path, capsys):
    store = _store(tmp_path)
    _wrapper_state(store)
    sink = _live_sink(store)
    try:
        assert _row(store)["turn_events"] == "on (loop)"
        assert cli.cmd_status(argparse.Namespace(root=str(store.root), json=False)) == 0
        out = capsys.readouterr().out
        beta = next(line for line in out.splitlines() if line.strip().startswith("beta"))
        assert "turn_events=on (loop)" in beta
        assert next(line for line in out.splitlines() if line.strip().startswith("alpha")).count("turn_events") == 0
        assert cli.cmd_status(argparse.Namespace(root=str(store.root), json=True)) == 0
        payload = json.loads(capsys.readouterr().out)
        assert next(a for a in payload["agents"] if a["name"] == "beta")["turn_events"] == "on (loop)"
    finally:
        sink.close()


def test_status_shows_a_stopped_journal_as_ended_and_a_plain_wrapper_as_unmanaged(tmp_path):
    store = _store(tmp_path)
    _wrapper_state(store)
    sink = _live_sink(store)
    sink.close()
    assert _row(store)["turn_events"] == "ended"
    _wrapper_state(store, mode="wrapper-one-shot")
    assert _row(store)["turn_events"] == "unmanaged (one_shot)"


def test_doctor_says_ok_for_a_live_writer_and_warns_for_one_that_stopped_responding(tmp_path):
    store = _store(tmp_path)
    _wrapper_state(store)
    sink = _live_sink(store)
    try:
        check = doctor._check_turn_journal(store)
        assert check.status == "ok" and "beta: on (loop)" in check.details
        stale = te.default_turn_events_root(store.root) / "beta" / os.path.basename(sink.status_path)
        data = json.loads(stale.read_text(encoding="ascii"))
        data["updated_at"] = te.format_time(int((time.time() - 600) * 1000))
        stale.write_text(json.dumps(data), encoding="ascii")
        check = doctor._check_turn_journal(store)
        assert check.status == "warn" and "writer not responding" in check.details
        assert check.data == {"agents": {"beta": "writer not responding"}}
    finally:
        sink.close()


def test_doctor_warns_when_a_start_failed(tmp_path):
    store = _store(tmp_path)
    _wrapper_state(store)

    class Failing(te.JournalFiles):
        def append_line(self, path, data):
            raise OSError("no")

    sink = te.TurnEventSink(te.default_turn_events_root(store.root), "beta", files=Failing())
    sink.start()
    check = doctor._check_turn_journal(store)
    assert check.status == "warn" and "off (start_failed)" in check.details and check.fix
    sink.close()


# --- fix round 1: liveness is proven, the owner comes first, a failed start is visible -----


def _exited_child_pid() -> int:
    import subprocess
    import sys

    return int(subprocess.check_output([sys.executable, "-c", "import os; print(os.getpid())"]))


def test_a_writer_whose_process_already_exited_is_ended_even_with_a_fresh_record(tmp_path):
    store = _store(tmp_path)
    pid = _exited_child_pid()
    _write_status(store, pid=pid, process_start_token="old-token")  # updated_at is "now"
    assert _label(store, "wrapper-loop", None) == "ended"
    # the same through a process table that cannot name the pid's start token
    assert _label(store, "wrapper-loop", None, tokens=lambda p: None) == "ended"


def test_an_unreadable_start_token_never_proves_a_running_writer(tmp_path):
    store = _store(tmp_path)
    sink = _live_sink(store)
    try:
        assert _label(store, "wrapper-loop", _real_runtime(), tokens=lambda p: None) == "ended"
    finally:
        sink.close()


def test_an_unreadable_token_does_not_make_a_runtime_record_prove_a_live_one_shot_loop(tmp_path):
    store = _store(tmp_path)
    assert _label(store, "wrapper-one-shot", _runtime(), tokens=lambda p: None) == "unmanaged (plain)"
    dead = {"wrapper_pid": _exited_child_pid(), "wrapper_start": "gone"}
    assert _label(store, "wrapper-one-shot", dead) == "unmanaged (plain)"


def test_an_old_generation_stamped_ahead_does_not_hide_the_current_writer(tmp_path):
    store = _store(tmp_path)
    sink = _live_sink(store)
    try:
        ahead = te.format_time(int((time.time() + 3600) * 1000))
        _write_status(store, name="status-g-old.json", pid=4242, process_start_token="tok-other", updated_at=ahead)
        assert _label(store, "wrapper-loop", _real_runtime()) == "on (loop)"
        # and with no runtime record at all: the provably running writer is preferred
        assert _label(store, "wrapper-loop", None,
                      tokens=lambda p: {ME: wr.process_start_token(ME)}.get(p)) == "on (loop)"
    finally:
        sink.close()


def test_a_record_of_another_run_with_the_same_pid_but_another_token_is_not_the_owners(tmp_path):
    store = _store(tmp_path)
    _write_status(store, pid=ME, process_start_token="some-earlier-process")
    assert _label(store, "wrapper-loop", _real_runtime()) == "off"


def test_a_start_failure_reaches_status_through_the_health_record_only(tmp_path):
    store = _store(tmp_path)
    te.default_turn_events_root(store.root).mkdir(parents=True)  # the journal folder exists
    runtime = _real_runtime()
    token = runtime["wrapper_start"]
    for reason, label in (("start_failed", "off (start_failed)"), ("start_timeout", "off (start_timeout)")):
        assert _label_w(store, runtime, [te.start_failure_warning(reason, ME, token)]) == label
    # unrelated or malformed warnings change nothing; with no live owner they are not believed
    mine = te.start_failure_warning("start_failed", ME, token)
    assert _label_w(store, runtime, ["lock_contention_admission", 5, None]) == "off"
    assert _label_w(store, None, [mine]) == "off"
    # a word without an owner (or with another owner) is not this wrapper's
    assert _label_w(store, runtime, ["turn_journal_start_failed"]) == "off"
    assert _label_w(store, runtime, [te.start_failure_warning("start_failed", ME + 1, token)]) == "off"
    assert _label_w(store, runtime, [te.start_failure_warning("start_failed", ME, "another-token")]) == "off"


def _label_w(store, runtime, warnings):
    return te.status_label(
        store.root, "beta", health_mode="wrapper-loop", runtime_record=runtime, health_warnings=warnings
    )


def test_the_health_writer_carries_the_start_failure_in_the_write_it_already_makes(tmp_path):
    store = _store(tmp_path)
    writer = WrapperHealthWriter(store, "beta", "claude", mode="wrapper-loop", min_interval=0.0)
    writer.idle()
    assert store.read_health_raw("beta")["warnings"] == []
    word = te.start_failure_warning("start_failed", ME, "tok-me")
    writer.standing_warnings = (word,)
    writer.idle()
    assert store.read_health_raw("beta")["warnings"] == [word]
    assert word.startswith("turn_journal_start_failed:%d:" % ME) and "tok-me" not in word
    assert te.start_failure_warning("writer_error", ME, "tok-me") is None


def test_a_thread_that_cannot_start_shows_off_start_failed_without_any_status_write(tmp_path, monkeypatch):
    import threading

    store = _store(tmp_path)
    _wrapper_state(store)
    te.default_turn_events_root(store.root).mkdir(parents=True)
    sink = te.TurnEventSink(te.default_turn_events_root(store.root), "beta")

    def refuse(self):
        raise RuntimeError("cannot start a thread")

    # Only the thread fault is scoped: a plain undo() would also drop the autouse journal-folder setting.
    with monkeypatch.context() as scoped:
        scoped.setattr(threading.Thread, "start", refuse)
        assert sink.start() is False and sink.off_reason == "start_failed"
    assert os.environ[te.ENV_TURN_EVENTS_DIR].startswith(str(tmp_path))
    runtime = _real_runtime()
    writer = WrapperHealthWriter(store, "beta", "claude", mode="wrapper-loop", min_interval=0.0)
    writer.standing_warnings = (
        te.start_failure_warning(sink.off_reason, runtime["wrapper_pid"], runtime["wrapper_start"]),)
    writer.idle()
    assert not os.path.exists(sink.status_path)
    assert _row(store)["turn_events"] == "off (start_failed)"
    check = doctor._check_turn_journal(store)
    assert check.status == "warn" and "off (start_failed)" in check.details


# --- fix round 2: proof of life, and a start-failure warning that belongs to its wrapper -----


def _exited_child_holding_its_handle():
    """A real child that has exited while this parent still holds its process handle."""
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE)
    token = wr.process_start_token(child.pid)
    assert token
    child.communicate(timeout=10)
    assert child.returncode == 0
    return child, token


def _close(child):
    if os.name == "nt":
        child._handle.Close()  # noqa: SLF001


def test_f3_an_exited_child_whose_parent_holds_its_handle_is_ended(tmp_path):
    store = _store(tmp_path)
    child, token = _exited_child_holding_its_handle()
    try:
        # the identity still reads back (that is the trap) ...
        assert wr.process_start_token(child.pid) == token or os.name != "nt"
        _write_status(store, pid=child.pid, process_start_token=token)
        # ... but it is not running
        assert te.status_label(store.root, "beta", health_mode="wrapper-loop") == "ended"
    finally:
        _close(child)


def test_f3_the_same_case_for_the_one_shot_classification(tmp_path):
    store = _store(tmp_path)
    child, token = _exited_child_holding_its_handle()
    try:
        record = {"wrapper_pid": child.pid, "wrapper_start": token}
        label = te.status_label(store.root, "beta", health_mode="wrapper-one-shot", runtime_record=record)
        assert label == "unmanaged (plain)"
    finally:
        _close(child)


def test_f3_a_running_process_with_a_matching_token_is_still_alive(tmp_path):
    store = _store(tmp_path)
    sink = _live_sink(store)
    try:
        assert te.status_label(store.root, "beta", health_mode="wrapper-loop",
                               runtime_record=_real_runtime()) == "on (loop)"
    finally:
        sink.close()


def test_f3_an_unknown_running_answer_is_not_alive(tmp_path):
    store = _store(tmp_path)
    sink = _live_sink(store)
    try:
        real = wr.process_start_token

        def label(running):
            return te.status_label(
                store.root, "beta", health_mode="wrapper-loop", runtime_record=_real_runtime(),
                start_token=real, process_running=running,
            )

        assert label(lambda pid: True) == "on (loop)"
        for unknown in (lambda pid: False, lambda pid: None, lambda pid: "yes"):
            assert label(unknown) == "ended"

        def boom(pid):
            raise OSError("no")

        assert label(boom) == "ended"
    finally:
        sink.close()


def _health_written_by_another_process(store, warning_owner_pid=None):
    """A separate process writes a start-failure health record, then exits."""
    import subprocess
    import sys

    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from agenttalk import turn_events as te, wrapper_runtime as wr; "
        "from agenttalk.store import Store; "
        "from agenttalk.wrapper.health import WrapperHealthWriter; "
        "import os; "
        "w=WrapperHealthWriter(Store(sys.argv[2]), 'beta', 'claude', mode='wrapper-loop', min_interval=0); "
        "w.standing_warnings=(te.start_failure_warning('start_failed', os.getpid(), "
        "wr.process_start_token(os.getpid())),); "
        "w.idle()"
    )
    subprocess.run([sys.executable, "-c", code, str(Path(te.__file__).parents[1]), str(store.root)], check=True)


def test_f5_a_previous_wrappers_start_failure_is_not_shown_for_its_replacement(tmp_path):
    from agenttalk import cli

    store = _store(tmp_path)
    te.default_turn_events_root(store.root).mkdir(parents=True)
    _health_written_by_another_process(store)
    assert store.read_health_raw("beta")["warnings"]  # the old bytes are really there
    # a replacement wrapper has published its runtime; its own health write has not happened yet
    wr.WrapperRuntimeWriter(store.state_dir, "beta", "new-generation").idle()
    assert cli._turn_journal_label(store, "beta", None) == "off"  # noqa: SLF001
    check = doctor._check_turn_journal(store)
    assert check is None or "start_failed" not in check.details


def test_f5_the_control_the_wrappers_own_warning_is_still_shown(tmp_path):
    from agenttalk import cli

    store = _store(tmp_path)
    te.default_turn_events_root(store.root).mkdir(parents=True)
    _wrapper_state(store)
    runtime = _real_runtime()
    writer = WrapperHealthWriter(store, "beta", "claude", mode="wrapper-loop", min_interval=0.0)
    writer.standing_warnings = (
        te.start_failure_warning("start_timeout", runtime["wrapper_pid"], runtime["wrapper_start"]),)
    writer.idle()
    assert cli._turn_journal_label(store, "beta", None) == "off (start_timeout)"  # noqa: SLF001
    check = doctor._check_turn_journal(store)
    assert check.status == "warn" and "off (start_timeout)" in check.details


def test_f5_a_current_journal_overrides_an_old_warning(tmp_path):
    store = _store(tmp_path)
    _wrapper_state(store)
    _write_status(store, pid=ME, process_start_token=wr.process_start_token(ME))
    runtime = _real_runtime()
    word = te.start_failure_warning("start_failed", ME, runtime["wrapper_start"])
    assert _label_w(store, runtime, [word]) == "on (loop)"
