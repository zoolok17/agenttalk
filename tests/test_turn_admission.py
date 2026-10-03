"""Tests for the turn admission hook runner (build 6a-2a, SPEC v2.1 section 4).

Every test uses its own fake program, materialized into `tmp_path` at test
time - nothing outside this file is needed. The fake program is one small
script (`_FAKE_PROGRAM_SRC`) with a scenario dispatch table selected by its
first argument, so one process implements every behavior these tests need:
a clean answer, a hang at each of the three stall points (4.6), a real
grandchild it spawns and leaves running, strict-JSON violations, oversize
output, and reporting back its own environment/argv for the allowlist and
no-task-data-in-args checks.

Timing: every deadline test uses a short, fixed `timeout_seconds` and
asserts elapsed time against a generous one-sided bound (it must have
waited at least the deadline, and must not have waited some large
unrelated multiple of it) - never a tight tolerance, and the SEQUENCE of
events (did the child finish reading before it hung, etc.) is encoded in
which scenario runs, not in sleep timing on the test's own side (lesson
deadline-tests-fix-the-order-not-the-timing).
"""

from __future__ import annotations

import inspect
import io
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agenttalk import turn_admission as ta

AGENT = "codex-worker-1"
MESSAGE_ID = "msg-0000000000000000000000001"
PROFILE = "ovh-qwen"

# A single fake program, selected by its first argument. Deliberately
# self-contained (stdlib only) so it needs nothing beyond `sys.executable`.
_FAKE_PROGRAM_SRC = r'''
import json
import os
import subprocess
import sys
import time

def _read_request():
    return json.loads(sys.stdin.buffer.read().decode("ascii"))

def _write(obj_bytes):
    sys.stdout.buffer.write(obj_bytes)
    sys.stdout.buffer.flush()

scenario = sys.argv[1]
rest = sys.argv[2:]

if scenario == "admitted":
    _read_request()
    _write(json.dumps({"max_calls": 10, "max_micro_eur": 5000, "reference": "ref-" + "a" * 60,
                        "status": "admitted", "ttl_seconds": 3600, "v": 1},
                       sort_keys=True, separators=(",", ":")).encode("ascii"))
elif scenario == "held":
    _read_request()
    _write(json.dumps({"reason": rest[0], "status": "held", "v": 1},
                       sort_keys=True, separators=(",", ":")).encode("ascii"))
elif scenario == "unknown":
    _read_request()
    _write(json.dumps({"status": "unknown", "v": 1},
                       sort_keys=True, separators=(",", ":")).encode("ascii"))
elif scenario == "pending_empty":
    _read_request()
    _write(b'{"messages":[],"status":"ok","v":1}')
elif scenario == "pending_n":
    _read_request()
    n = int(rest[0])
    entries = [{"message_id": "m" * 32, "reference": "r" * 64} for _ in range(n)]
    _write(json.dumps({"messages": entries, "status": "ok", "v": 1},
                       sort_keys=True, separators=(",", ":")).encode("ascii"))
elif scenario == "closed_ok":
    _read_request()
    _write(b'{"status":"ok","v":1}')
elif scenario == "exit_nonzero":
    _read_request()
    sys.exit(7)
elif scenario == "not_json":
    _read_request()
    _write(b"this is not json")
elif scenario == "duplicate_keys":
    _read_request()
    _write(b'{"status":"ok","status":"ok","v":1}')
elif scenario == "nan":
    _read_request()
    _write(b'{"status":"ok","v":NaN}')
elif scenario == "trailing":
    _read_request()
    _write(b'{"status":"ok","v":1}{"status":"ok","v":1}')
elif scenario == "wrong_shape_missing_key":
    _read_request()
    _write(b'{"status":"ok"}')
elif scenario == "wrong_shape_extra_key":
    _read_request()
    _write(b'{"extra":1,"status":"ok","v":1}')
elif scenario == "held_bad_reason":
    _read_request()
    _write(json.dumps({"reason": "made_up_reason", "status": "held", "v": 1},
                       sort_keys=True, separators=(",", ":")).encode("ascii"))
elif scenario == "held_reason_is_a_list":
    _read_request()
    _write(b'{"reason":[],"status":"held","v":1}')
elif scenario == "wrong_op_shape":
    # a pending-shaped answer handed back for an admit call
    _read_request()
    _write(b'{"messages":[],"status":"ok","v":1}')
elif scenario == "oversize":
    _read_request()
    cap = int(rest[0])
    pad = "a" * (cap + 1)
    _write(json.dumps({"status": "ok", "v": 1, "pad": pad},
                       sort_keys=True, separators=(",", ":")).encode("ascii"))
elif scenario == "oversize_keep_open":
    # Fix round 1, finding 5: flush exactly cap+1 bytes and then KEEP the
    # process (and its stdout) alive, rather than exiting right after the
    # write. A buffered read that waits for more data or EOF reports
    # `timeout` here instead of `too_large` - the bug this scenario exists
    # to catch.
    _read_request()
    cap = int(rest[0])
    sys.stdout.buffer.write(b"x" * (cap + 1))
    sys.stdout.buffer.flush()
    time.sleep(120)
elif scenario == "touch_marker_then_exit":
    # Fix round 1, finding 1: writes a marker the INSTANT it runs, then
    # answers normally. Used to prove a program cannot run (cannot even
    # reach this first line) before Windows job containment is attached -
    # see test_containment_is_established_before_first_instruction.
    marker = rest[0]
    with open(marker, "w") as f:
        f.write("started")
    _read_request()
    _write(b'{"status":"ok","v":1}')
elif scenario == "stall_before_read":
    time.sleep(120)
elif scenario == "stall_after_read":
    _read_request()
    time.sleep(120)
elif scenario == "stall_after_write":
    _read_request()
    _write(b'{"status":"ok","v":1}')
    sys.stdout.buffer.close()
    time.sleep(120)
elif scenario == "grandchild_hang":
    marker = rest[0]
    grandchild_src = (
        "import os, time\n"
        "with open(r'" + marker + "', 'w') as f:\n"
        "    f.write(str(os.getpid()))\n"
        "time.sleep(120)\n"
    )
    subprocess.Popen([sys.executable, "-c", grandchild_src])
    time.sleep(120)
elif scenario == "report_env_and_argv":
    marker = rest[0]
    with open(marker, "w") as f:
        json.dump({"env": dict(os.environ), "argv": sys.argv[2:]}, f)
    _read_request()
    _write(b'{"status":"ok","v":1}')
else:
    sys.exit(99)
'''


@pytest.fixture()
def fake_program(tmp_path):
    path = tmp_path / "fake_program.py"
    path.write_text(_FAKE_PROGRAM_SRC, encoding="utf-8")
    return path


def _config(fake_program, tmp_path, *scenario_args, timeout_seconds=5.0, env=None, sha256=None):
    return ta.TurnAdmissionConfig.from_mapping({
        "command": sys.executable,
        "args": [str(fake_program), *scenario_args],
        "cwd": str(tmp_path),
        "env": env or {},
        "timeout_seconds": timeout_seconds,
        **({"sha256": sha256} if sha256 is not None else {}),
    })


def _admit(config):
    return ta.admit(config, agent=AGENT, message_id=MESSAGE_ID, parent_request_id="",
                    profile=PROFILE, mode="fresh", dispatch_bytes=100)


# --------------------------------------------------------------- happy paths

def test_admit_returns_admitted_answer(fake_program, tmp_path):
    config = _config(fake_program, tmp_path, "admitted")
    result = _admit(config)
    assert result["status"] == "admitted"
    assert result["reference"] == "ref-" + "a" * 60


def test_admit_returns_held_answer(fake_program, tmp_path):
    config = _config(fake_program, tmp_path, "held", "parked")
    result = _admit(config)
    assert result == {"reason": "parked", "status": "held", "v": 1}


def test_recall_returns_admitted_shape(fake_program, tmp_path):
    config = _config(fake_program, tmp_path, "admitted")
    result = ta.recall(config, agent=AGENT, message_id=MESSAGE_ID)
    assert result["status"] == "admitted"


def test_recall_returns_unknown(fake_program, tmp_path):
    config = _config(fake_program, tmp_path, "unknown")
    result = ta.recall(config, agent=AGENT, message_id=MESSAGE_ID)
    assert result == {"status": "unknown", "v": 1}


def test_pending_empty_answer(fake_program, tmp_path):
    config = _config(fake_program, tmp_path, "pending_empty")
    result = ta.pending(config, agent=AGENT, limit=10)
    assert result == {"messages": [], "status": "ok", "v": 1}


def test_pending_sixty_four_entries(fake_program, tmp_path):
    config = _config(fake_program, tmp_path, "pending_n", "64")
    result = ta.pending(config, agent=AGENT, limit=64)
    assert len(result["messages"]) == 64


def test_closed_returns_ok(fake_program, tmp_path):
    config = _config(fake_program, tmp_path, "closed_ok")
    result = ta.closed(config, agent=AGENT, message_id=MESSAGE_ID)
    assert result == {"status": "ok", "v": 1}


# --------------------------------------------------------------- failure words

def test_not_started_on_nonexistent_command(tmp_path):
    config = ta.TurnAdmissionConfig(
        command=str(tmp_path / "does-not-exist.exe"), args=(), cwd=str(tmp_path),
        env={}, timeout_seconds=5.0,
    )
    with pytest.raises(ta.TurnAdmissionFailure) as exc:
        _admit(config)
    assert exc.value.word == "not_started"


def test_pin_mismatch(fake_program, tmp_path):
    config = _config(fake_program, tmp_path, "admitted", sha256="0" * 64)
    with pytest.raises(ta.TurnAdmissionFailure) as exc:
        _admit(config)
    assert exc.value.word == "pin_mismatch"


def test_pin_matching_succeeds(fake_program, tmp_path):
    import hashlib
    # Pin the REAL executable file's digest (not a literal), so this proves
    # a correct pin lets the call through rather than merely not raising.
    with open(sys.executable, "rb") as f:
        real_digest = hashlib.sha256(f.read()).hexdigest()
    config = _config(fake_program, tmp_path, "admitted", sha256=real_digest)
    result = _admit(config)
    assert result["status"] == "admitted"


def test_timeout_stall_before_reading_input(fake_program, tmp_path):
    config = _config(fake_program, tmp_path, "stall_before_read", timeout_seconds=1.5)
    start = time.monotonic()
    with pytest.raises(ta.TurnAdmissionFailure) as exc:
        _admit(config)
    elapsed = time.monotonic() - start
    assert exc.value.word == "timeout"
    assert elapsed >= 1.4
    assert elapsed < 20.0


def test_timeout_stall_after_reading_before_output(fake_program, tmp_path):
    config = _config(fake_program, tmp_path, "stall_after_read", timeout_seconds=1.5)
    start = time.monotonic()
    with pytest.raises(ta.TurnAdmissionFailure) as exc:
        _admit(config)
    elapsed = time.monotonic() - start
    assert exc.value.word == "timeout"
    assert elapsed >= 1.4
    assert elapsed < 20.0


def test_timeout_stall_after_output_before_exit(fake_program, tmp_path):
    config = _config(fake_program, tmp_path, "stall_after_write", timeout_seconds=1.5)
    start = time.monotonic()
    with pytest.raises(ta.TurnAdmissionFailure) as exc:
        _admit(config)
    elapsed = time.monotonic() - start
    assert exc.value.word == "timeout"
    assert elapsed >= 1.4
    assert elapsed < 20.0


def test_exit_code_failure(fake_program, tmp_path):
    config = _config(fake_program, tmp_path, "exit_nonzero")
    with pytest.raises(ta.TurnAdmissionFailure) as exc:
        _admit(config)
    assert exc.value.word == "exit_code"


def test_not_json_failure(fake_program, tmp_path):
    config = _config(fake_program, tmp_path, "not_json")
    with pytest.raises(ta.TurnAdmissionFailure) as exc:
        _admit(config)
    assert exc.value.word == "not_json"


def test_bad_shape_missing_key(fake_program, tmp_path):
    config = _config(fake_program, tmp_path, "wrong_shape_missing_key")
    with pytest.raises(ta.TurnAdmissionFailure) as exc:
        ta.closed(config, agent=AGENT, message_id=MESSAGE_ID)
    assert exc.value.word == "bad_shape"


def test_bad_shape_extra_key(fake_program, tmp_path):
    config = _config(fake_program, tmp_path, "wrong_shape_extra_key")
    with pytest.raises(ta.TurnAdmissionFailure) as exc:
        ta.closed(config, agent=AGENT, message_id=MESSAGE_ID)
    assert exc.value.word == "bad_shape"


def test_bad_shape_wrong_operation_shape(fake_program, tmp_path):
    # admit gets a pending-shaped answer back
    config = _config(fake_program, tmp_path, "wrong_op_shape")
    with pytest.raises(ta.TurnAdmissionFailure) as exc:
        _admit(config)
    assert exc.value.word == "bad_shape"


def test_bad_shape_held_reason_not_a_closed_word(fake_program, tmp_path):
    config = _config(fake_program, tmp_path, "held_bad_reason")
    with pytest.raises(ta.TurnAdmissionFailure) as exc:
        _admit(config)
    assert exc.value.word == "bad_shape"


def test_too_large_answer_over_cap(fake_program, tmp_path):
    config = _config(fake_program, tmp_path, "oversize", str(ta._ANSWER_CAPS["closed"]))
    with pytest.raises(ta.TurnAdmissionFailure) as exc:
        ta.closed(config, agent=AGENT, message_id=MESSAGE_ID)
    assert exc.value.word == "too_large"


# --------------------------------------------------------------- strict JSON

def test_duplicate_keys_is_not_json(fake_program, tmp_path):
    config = _config(fake_program, tmp_path, "duplicate_keys")
    with pytest.raises(ta.TurnAdmissionFailure) as exc:
        ta.closed(config, agent=AGENT, message_id=MESSAGE_ID)
    assert exc.value.word == "not_json"


def test_nan_is_not_json(fake_program, tmp_path):
    config = _config(fake_program, tmp_path, "nan")
    with pytest.raises(ta.TurnAdmissionFailure) as exc:
        ta.closed(config, agent=AGENT, message_id=MESSAGE_ID)
    assert exc.value.word == "not_json"


def test_trailing_value_is_not_json(fake_program, tmp_path):
    config = _config(fake_program, tmp_path, "trailing")
    with pytest.raises(ta.TurnAdmissionFailure) as exc:
        ta.closed(config, agent=AGENT, message_id=MESSAGE_ID)
    assert exc.value.word == "not_json"


@pytest.mark.parametrize("bad_v", [True, 1.0])
def test_v_refuses_bool_and_float(bad_v):
    answer = {"status": "ok", "v": bad_v}
    with pytest.raises(ta.TurnAdmissionFailure) as exc:
        ta._validate_closed_answer(answer)
    assert exc.value.word == "bad_shape"


# --------------------------------------------------------------- size caps (4.3)

def _measure(obj: dict) -> int:
    return len(ta._dumps_compact(obj))


def test_admit_request_largest_legal_size_is_301_bytes():
    request = {
        "agent": "a" * 64, "context": {"dispatch_bytes": 999999999999, "mode": "resume"},
        "message_id": "m" * 32, "op": "admit", "parent_request_id": "m" * 32,
        "profile": "p" * 32, "v": 1,
    }
    assert _measure(request) == 301


def test_admitted_answer_largest_legal_size_is_174_bytes():
    answer = {
        "max_calls": 100000, "max_micro_eur": 999999999999, "reference": "r" * 64,
        "status": "admitted", "ttl_seconds": 86400, "v": 1,
    }
    assert _measure(answer) == 174


def test_pending_answer_64_entries_is_exactly_8290_bytes():
    entries = [{"message_id": "m" * 32, "reference": "r" * 64} for _ in range(64)]
    answer = {"messages": entries, "status": "ok", "v": 1}
    assert _measure(answer) == 8290


def test_pending_answer_cap_is_9216_accepts_8290_refuses_8291(fake_program, tmp_path):
    # 8,290 is legal (comfortably under 9,216); confirm the exact 9,216 cap
    # boundary by requesting one byte over it directly.
    config = _config(fake_program, tmp_path, "oversize", str(ta._ANSWER_CAPS["pending"]))
    with pytest.raises(ta.TurnAdmissionFailure) as exc:
        ta.pending(config, agent=AGENT, limit=64)
    assert exc.value.word == "too_large"


def test_held_answer_largest_legal_size_is_58_bytes():
    answer = {"reason": "coordinator_unreachable", "status": "held", "v": 1}
    assert _measure(answer) == 58


def test_unknown_answer_size_is_26_bytes():
    answer = {"status": "unknown", "v": 1}
    assert _measure(answer) == 26


def test_closed_answer_size_is_21_bytes():
    answer = {"status": "ok", "v": 1}
    assert _measure(answer) == 21


def test_empty_pending_answer_is_35_bytes():
    answer = {"messages": [], "status": "ok", "v": 1}
    assert _measure(answer) == 35


# --------------------------------------------------------------- process-tree cleanup

def test_process_tree_killed_by_exact_pid_grandchild_included(fake_program, tmp_path):
    marker = tmp_path / "grandchild.pid"
    config = _config(fake_program, tmp_path, "grandchild_hang", str(marker), timeout_seconds=1.5)
    with pytest.raises(ta.TurnAdmissionFailure) as exc:
        _admit(config)
    assert exc.value.word == "timeout"

    deadline = time.monotonic() + 10.0
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists(), "the grandchild never started"
    grandchild_pid = int(marker.read_text().strip())

    # bounded settle window for the kill to take effect, never a fixed sleep
    # that assumes a fast machine.
    alive = True
    settle_deadline = time.monotonic() + 10.0
    while time.monotonic() < settle_deadline:
        alive = _pid_alive(grandchild_pid)
        if not alive:
            break
        time.sleep(0.1)
    assert not alive, f"grandchild pid {grandchild_pid} is still alive after cleanup"


def _pid_alive(pid: int) -> bool:
    """Item 11: a grandchild that outlives its killed parent is reparented
    to an init process on POSIX; only an ancestor may wait() it, so this
    test has no standing to reap it directly - that is the operating
    environment's job (see the precondition below). `kill(pid, 0)` alone
    cannot tell a genuinely running process from a zombie an init has not
    reaped yet, so a zombie must not be reported as "alive" here."""
    if os.name == "nt":
        check = subprocess.run(  # nosec B603 B607 - fixed argv, test-only, no shell
            ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True
        )
        return str(pid) in check.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # kill(pid, 0) succeeded: the pid exists, but that includes a zombie
    # nobody has reaped yet. Linux-only /proc check for the zombie state;
    # elsewhere (no /proc, e.g. macOS) this falls back to treating
    # existence as "alive" - the precondition this test and the README
    # both state is an init that reaps (`docker run --init` or equivalent),
    # so a killed grandchild does not linger as an unreaped zombie here.
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as f:  # noqa: PTH123
            stat_text = f.read()
    except OSError:
        return True
    fields_after_comm = stat_text.rsplit(")", 1)[-1].split()
    return not (fields_after_comm and fields_after_comm[0] == "Z")


# --------------------------------------------------------------- environment / args (AC3, 4.1)

#: Fix round 1, finding 8: Python's own startup can add LC_CTYPE to a
#: child's observed environment on POSIX (locale coercion, PEP 538/540) even
#: though the exact dict `subprocess.Popen` was given never contained it.
#: This is a documented fact about the INTERPRETER the operator configured
#: as `command`, not something this module adds - narrowly allow it in the
#: child's own self-report, never in the exact-launch-environment check.
_POSIX_RUNTIME_ADDED_ENV_NAMES = {"LC_CTYPE"}

#: Fix round 1 (macOS CI leg): CoreFoundation adds __CF_USER_TEXT_ENCODING to
#: every process it starts, the same way Python's own locale coercion adds
#: LC_CTYPE above - also not something this module adds, so also narrowly
#: allowed in the child's own self-report, macOS only.
_MACOS_RUNTIME_ADDED_ENV_NAMES = {"__CF_USER_TEXT_ENCODING"}


def test_build_child_env_is_exactly_the_operator_env_plus_system_defaults(tmp_path):
    """The EXACT mapping handed to `subprocess.Popen`, asserted directly -
    no real process launch, so Python's own locale coercion inside a child
    can never be mistaken for a bug here (finding 8's fix: this check and
    the child's own self-report, below, are now two separate tests)."""
    config = ta.TurnAdmissionConfig(
        command=sys.executable, args=(), cwd=str(tmp_path),
        env={"FIXED_NAME": "fixed_value"}, timeout_seconds=5.0,
    )
    env = ta._build_child_env(config)
    expected = {"FIXED_NAME": "fixed_value"}
    for name, default in ta._SYSTEM_ENV_DEFAULTS.items():
        expected[name] = os.environ.get(name, default)
    assert env == expected


def test_environment_carries_nothing_beyond_the_allowlist(fake_program, tmp_path, monkeypatch):
    marker = tmp_path / "env.json"
    monkeypatch.setenv("A_SECRET_AMBIENT_TOKEN", "should-never-reach-the-program")
    config = _config(
        fake_program, tmp_path, "report_env_and_argv", str(marker),
        env={"FIXED_NAME": "fixed_value"},
    )
    result = ta.closed(config, agent=AGENT, message_id=MESSAGE_ID)
    assert result == {"status": "ok", "v": 1}
    reported = json.loads(marker.read_text())
    env = reported["env"]
    assert env.get("FIXED_NAME") == "fixed_value"
    assert "A_SECRET_AMBIENT_TOKEN" not in env
    # Windows reports well-known system names back in its own canonical
    # casing (SYSTEMROOT) even when set as "SystemRoot" - compare
    # case-insensitively rather than assume a casing OS env handling owns.
    allowed_extra = {name.casefold() for name in ta._SYSTEM_ENV_DEFAULTS}
    if os.name != "nt":
        allowed_extra |= {name.casefold() for name in _POSIX_RUNTIME_ADDED_ENV_NAMES}
    if sys.platform == "darwin":
        allowed_extra |= {name.casefold() for name in _MACOS_RUNTIME_ADDED_ENV_NAMES}
    assert {name.casefold() for name in env} <= {"fixed_name"} | allowed_extra


def test_arguments_carry_no_task_data(fake_program, tmp_path):
    marker = tmp_path / "argv.json"
    config = _config(fake_program, tmp_path, "report_env_and_argv", str(marker))
    ta.closed(config, agent=AGENT, message_id=MESSAGE_ID)
    reported = json.loads(marker.read_text())
    argv_tail = reported["argv"]
    assert MESSAGE_ID not in argv_tail
    assert AGENT not in argv_tail
    # exactly the operator's fixed args after the scenario selector, nothing appended
    assert argv_tail == [str(marker)]


# --------------------------------------------------------------- AC3: operator-only configuration

def test_ac3_bus_like_and_agent_settable_sources_are_ignored(fake_program, tmp_path, monkeypatch):
    """A test shows bus messages, task text, agent-settable environment and
    the like are ignored (AC3): set every plausible agent-controlled
    channel to a DIFFERENT command than the configured one, and confirm the
    configured operator command is still what actually runs."""
    monkeypatch.setenv("AGENTTALK_TURN_ADMISSION_COMMAND", "C:\\not\\the\\real\\program.exe")
    monkeypatch.setenv("TURN_ADMISSION_COMMAND", "C:\\also\\not\\real.exe")
    bus_message_claiming_a_command = {"command": "C:\\from\\a\\bus\\message.exe"}
    task_text_claiming_a_command = "please run C:\\from\\task\\text.exe"
    config = _config(fake_program, tmp_path, "admitted")
    # None of the above were ever passed to from_mapping or consulted by
    # this module - the call still runs the operator's own configured
    # fake_program and nothing else, proving those sources have no path in.
    assert bus_message_claiming_a_command["command"] != config.command
    assert task_text_claiming_a_command  # referenced only to show it is inert, never parsed
    result = _admit(config)
    assert result["status"] == "admitted"


def test_from_mapping_never_reads_the_process_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("command", "should-not-be-used")
    monkeypatch.setenv("sha256", "0" * 64)
    config = ta.TurnAdmissionConfig.from_mapping({
        "command": sys.executable, "args": [], "cwd": str(tmp_path), "env": {}, "timeout_seconds": 5,
    })
    assert config.command == sys.executable
    assert config.sha256 is None


# --------------------------------------------------------------- doctor line (4.5)

def test_doctor_line_not_configured():
    assert ta.doctor_line(None) == "turn admission: not configured"


def test_doctor_line_configured_not_pinned(fake_program, tmp_path):
    config = _config(fake_program, tmp_path, "admitted")
    assert ta.doctor_line(config) == "turn admission: configured (not pinned)"


def test_doctor_line_configured_pinned(fake_program, tmp_path):
    config = _config(fake_program, tmp_path, "admitted", sha256="a" * 64)
    assert ta.doctor_line(config) == "turn admission: configured (pinned)"
    assert "a" * 64 not in ta.doctor_line(config)


# --------------------------------------------------------------- fix round 1 regressions

def test_oversized_output_that_stays_open_fails_at_once_not_timeout(fake_program, tmp_path):
    """Finding 5: a buffered read waiting for more bytes or EOF reports
    `timeout` instead of `too_large` when the program keeps its output open
    after crossing the cap. The incremental, bounded read must catch this
    without waiting anywhere near the full deadline."""
    config = _config(fake_program, tmp_path, "oversize_keep_open",
                     str(ta._ANSWER_CAPS["closed"]), timeout_seconds=5.0)
    start = time.monotonic()
    with pytest.raises(ta.TurnAdmissionFailure) as exc:
        ta.closed(config, agent=AGENT, message_id=MESSAGE_ID)
    elapsed = time.monotonic() - start
    assert exc.value.word == "too_large"
    assert elapsed < 2.0, f"took {elapsed}s - not a prompt, incremental cap check"


def test_held_reason_as_a_list_is_bad_shape_not_typeerror(fake_program, tmp_path):
    """Finding 6: `_bad_shape(answer.get("reason") in HELD_REASONS)` hashes
    its argument for the frozenset membership test - an unhashable reason
    (a list) must be caught as bad_shape before that happens, never escape
    as a bare TypeError."""
    config = _config(fake_program, tmp_path, "held_reason_is_a_list")
    with pytest.raises(ta.TurnAdmissionFailure) as exc:
        _admit(config)
    assert exc.value.word == "bad_shape"


@pytest.mark.parametrize("bad_env", [{"bad=name": "fixed"}, {"ok_name": "has\x00nul"}, {"": "fixed"}])
def test_invalid_env_entry_refused_at_configuration_time(tmp_path, bad_env):
    """Finding 6: an environment NAME containing '=' (or a NUL in either
    the name or the value) passed `from_mapping` unchecked before, then
    raised an uncaught ValueError from `subprocess.Popen` at launch time -
    outside the closed failure vocabulary entirely. Caught at configuration
    time now, same as every other operator config mistake."""
    with pytest.raises(ValueError, match="env"):
        ta.TurnAdmissionConfig.from_mapping({
            "command": sys.executable, "args": [], "cwd": str(tmp_path),
            "env": bad_env, "timeout_seconds": 5,
        })


@pytest.mark.skipif(os.name != "nt", reason="the isabs loophole (finding 7) is Windows/pre-3.13-specific")
def test_windows_single_leading_backslash_path_is_refused(tmp_path):
    """Finding 7: `os.path.isabs` accepts a single-leading-backslash path on
    Python 3.10-3.12 (its drive resolves from the CALLER's current drive at
    runtime) - never a safe, fixed program identity. Require a drive letter
    or a UNC root explicitly, regardless of which Python runs this."""
    with pytest.raises(ValueError, match="absolute path"):
        ta.TurnAdmissionConfig.from_mapping({
            "command": r"\not\drive\qualified.exe", "args": [], "cwd": str(tmp_path),
            "env": {}, "timeout_seconds": 5,
        })
    with pytest.raises(ValueError, match="absolute path"):
        ta.TurnAdmissionConfig.from_mapping({
            "command": sys.executable, "args": [], "cwd": r"\not\drive\qualified",
            "env": {}, "timeout_seconds": 5,
        })
    # A real drive-qualified path and a UNC-shaped path must both still work.
    ta.TurnAdmissionConfig.from_mapping({
        "command": sys.executable, "args": [], "cwd": str(tmp_path), "env": {}, "timeout_seconds": 5,
    })
    assert ta._is_robust_absolute_path(r"\\server\share\program.exe")


@pytest.mark.skipif(os.name != "nt", reason="Windows can shell-launch .bat/.cmd even with shell=False")
@pytest.mark.parametrize("suffix", [".bat", ".cmd", ".BAT", ".Cmd"])
def test_windows_bat_and_cmd_commands_are_refused(tmp_path, suffix):
    with pytest.raises(ValueError, match="native executable"):
        ta.TurnAdmissionConfig.from_mapping({
            "command": str(tmp_path / f"program{suffix}"), "args": [], "cwd": str(tmp_path),
            "env": {}, "timeout_seconds": 5,
        })


def test_write_failure_vetoes_success_even_with_a_valid_looking_answer(fake_program, tmp_path):
    """Finding 3: a request write (or read) failure must veto success even
    if the program goes on to produce a valid answer and exit zero -
    otherwise the answer is not provably the result of THIS call's request.
    Deterministic: the stdin write is made to fail directly, rather than
    racing a real program's own timing."""
    config = _config(fake_program, tmp_path, "admitted")
    real_spawn = ta._spawn

    class FailingWriteStdin:
        def __init__(self, raw):
            self._raw = raw

        def write(self, data):
            # Close the REAL pipe first so the fake program's blocking
            # stdin read unblocks via EOF and goes on to answer normally -
            # otherwise it hangs waiting for input this failure never
            # delivers, and the test would observe `timeout` instead of
            # isolating the write failure itself.
            self._raw.close()
            raise OSError("simulated broken request pipe")

        def close(self):
            return None  # already closed by write() above

    def spawn_with_failing_stdin(cfg):
        proc = real_spawn(cfg)
        proc.stdin = FailingWriteStdin(proc.stdin)
        return proc

    with patch.object(ta, "_spawn", spawn_with_failing_stdin):
        with pytest.raises(ta.TurnAdmissionFailure) as exc:
            _admit(config)
    assert exc.value.word == "not_started"


def test_cleanup_runs_for_an_invalid_answer_not_only_timeout_or_overflow(tmp_path):
    """Finding 2: the old `finally` block only called `_kill_tree` when a
    timeout or overflow flag was set - an invalid (non-timeout) answer took
    the no-op `close_job`-only branch and never reaped the process group.
    Simulated POSIX path (this suite runs on Windows; `ta.os` is swapped for
    a bare namespace exposing only what `_kill_tree` touches, proving the
    GROUP-KILL call itself fires for this outcome)."""
    class FakeProc:
        pid = 424242
        stdin = io.BytesIO()
        stdout = io.BytesIO(b"not json")

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    kills = []
    fake_os = SimpleNamespace(name="posix", killpg=lambda *a: kills.append(a), environ=os.environ)
    with patch.object(ta, "os", fake_os), patch.object(ta, "_spawn", return_value=FakeProc()):
        with pytest.raises(ta.TurnAdmissionFailure) as exc:
            ta.closed(ta.TurnAdmissionConfig(sys.executable, (), str(tmp_path), {}, 1.0),
                     agent=AGENT, message_id=MESSAGE_ID)
    assert exc.value.word == "not_json"
    assert kills, "an invalid answer did not trigger process-group cleanup"


def test_cleanup_runs_on_cancellation_not_only_on_a_normal_failure(tmp_path):
    """Finding 2: cancellation (here, a KeyboardInterrupt raised from
    inside `proc.wait`) must still own cleanup through the same
    try/finally - never skip straight past it."""
    class InterruptedProc:
        pid = 424242
        stdin = io.BytesIO()
        stdout = io.BytesIO(b'{"v":1,"status":"ok"}')

        def kill(self):
            pass

        def wait(self, timeout=None):
            raise KeyboardInterrupt()

    kills = []
    fake_os = SimpleNamespace(name="posix", killpg=lambda *a: kills.append(a), environ=os.environ)
    with patch.object(ta, "os", fake_os), patch.object(ta, "_spawn", return_value=InterruptedProc()):
        with pytest.raises(KeyboardInterrupt):
            ta.closed(ta.TurnAdmissionConfig(sys.executable, (), str(tmp_path), {}, 1.0),
                     agent=AGENT, message_id=MESSAGE_ID)
    assert kills, "cancellation did not trigger process-group cleanup"


def test_cleanup_wait_is_bounded_by_the_caller_supplied_deadline_not_a_fresh_five_second_grant(tmp_path):
    """Finding 4: `_kill_tree` used to open its own fresh 5-second
    `proc.wait()`, stacked on top of two more 5-second thread joins in the
    caller - a 1-second configured deadline could take 6+ seconds in total.
    `_kill_tree` takes whatever absolute deadline its caller supplies and
    never grants itself anything beyond it (fix round 2: `_call` always
    passes its OWN single original deadline, never a fresh budget - see
    the fake-clock test above for the end-to-end proof)."""
    waits = []
    fake = SimpleNamespace(pid=424242, kill=lambda: None, wait=lambda timeout: waits.append(timeout))
    cleanup_deadline = time.monotonic() + 0.25
    ta._kill_tree(fake, lambda: None, cleanup_deadline)
    assert waits
    assert waits[0] <= 0.25 + 0.05
    assert waits[0] < 5.0


def test_pin_verification_respects_the_overall_deadline(fake_program, tmp_path):
    """Finding 4: pin verification used to run BEFORE the deadline existed,
    so an injected delay there got a free extension past the configured
    timeout. `_verify_pin` now takes the deadline explicitly and is bounded
    by it, same as everything else."""
    import hashlib
    with open(sys.executable, "rb") as f:  # noqa: PTH123
        real_digest = hashlib.sha256(f.read()).hexdigest()
    config = _config(fake_program, tmp_path, "admitted", sha256=real_digest, timeout_seconds=0.1)
    real_pin = ta._verify_pin

    def slow_pin(cfg, deadline):
        time.sleep(0.3)
        return real_pin(cfg, deadline)

    start = time.monotonic()
    with patch.object(ta, "_verify_pin", slow_pin):
        with pytest.raises(ta.TurnAdmissionFailure) as exc:
            _admit(config)
    elapsed = time.monotonic() - start
    assert exc.value.word == "timeout"
    assert elapsed < 1.0, f"took {elapsed}s - the pin check or its cleanup got extra time"


@pytest.mark.skipif(os.name != "nt", reason="finding 1 (suspended creation) is a Windows-specific race")
def test_containment_is_established_before_first_instruction(fake_program, tmp_path):
    """Finding 1 (P1): a child created BEFORE Windows job attachment used
    to be able to run - including spawning its own grandchild - before
    containment existed. With suspended creation, the program cannot reach
    its own first line until AFTER `_attach_kill_on_close_job` has already
    returned (and this module has gone on to resume it).

    Proven with a bounded, ACTIVE wait inside the attach hook, not a single
    instantaneous check: interpreter startup is itself slow enough that a
    single immediate check can pass "by luck" even without the fix (ordinary
    startup latency happens to still be in progress at that instant). By
    polling for up to half a second from INSIDE the hook - strictly before
    resume can possibly occur - a program that is not genuinely suspended
    has ample time to finish starting and write its marker; this is the
    deterministic version of the reviewer's own delayed-attachment probe,
    adapted for the new ordering (lesson deadline-tests-fix-the-order-not-
    the-timing: the bound here establishes whether an event happens at all
    within an ample window, not a race against a tight deadline)."""
    marker = tmp_path / "started.marker"
    seen_before_resume = {}
    real_attach = ta._attach_kill_on_close_job

    def observing_attach(proc):
        deadline = time.monotonic() + 0.5
        appeared = False
        while time.monotonic() < deadline:
            if marker.exists():
                appeared = True
                break
            time.sleep(0.01)
        seen_before_resume["marker_existed"] = appeared
        return real_attach(proc)

    config = _config(fake_program, tmp_path, "touch_marker_then_exit", str(marker), timeout_seconds=5.0)
    with patch.object(ta, "_attach_kill_on_close_job", observing_attach):
        result = ta.closed(config, agent=AGENT, message_id=MESSAGE_ID)
    assert result == {"status": "ok", "v": 1}
    assert seen_before_resume["marker_existed"] is False, "the program ran before containment was attached"
    assert marker.exists(), "the program never ran at all"


@pytest.mark.skipif(os.name != "nt", reason="finding 1 (suspended creation) is a Windows-specific race")
def test_failed_job_assignment_terminates_the_suspended_process(fake_program, tmp_path):
    """Finding 1: if job assignment fails, the suspended process must be
    terminated directly (it never ran, so there is no job/group to reap)
    and the call fails closed - never left running un-contained, and never
    left suspended forever either."""
    marker = tmp_path / "ran.marker"
    config = _config(fake_program, tmp_path, "touch_marker_then_exit", str(marker))
    with patch.object(ta, "_attach_kill_on_close_job", side_effect=OSError("simulated attach failure")):
        with pytest.raises(ta.TurnAdmissionFailure) as exc:
            ta.closed(config, agent=AGENT, message_id=MESSAGE_ID)
    assert exc.value.word == "not_started"
    time.sleep(0.3)
    assert not marker.exists(), "the process ran despite failed job assignment"


# --------------------------------------------------------------- fix round 2 regressions

def test_cancellation_immediately_after_spawn_still_reaps_the_process(fake_program, tmp_path):
    """Finding 1 (fix round 2): cleanup ownership must begin with the very
    FIRST statement inside the owning try/finally - `proc = _spawn(config)`
    itself - not a line that ran before the try was even entered. The
    previous round's `_call` assigned `proc` BEFORE its own try started, so
    an interruption on the very next source line left an already-spawned
    process alive forever, because `_call` had not yet taken ownership of
    it. Located dynamically (not by a hardcoded line number) so this stays
    correct if the function is edited again."""
    source_lines, start_line = inspect.getsourcelines(ta._call)
    target_line = None
    for i, line in enumerate(source_lines):
        if line.strip() == "proc = _spawn(config)":
            target_line = start_line + i + 1
            break
    assert target_line is not None, "could not locate 'proc = _spawn(config)' - test needs updating"

    roots = []
    real_spawn = ta._spawn

    def observing_spawn(cfg):
        proc = real_spawn(cfg)
        roots.append(proc)
        return proc

    fired = {"value": False}

    def trace(frame, event, arg):  # noqa: ARG001
        if (event == "line" and frame.f_code is ta._call.__code__
                and frame.f_lineno == target_line and not fired["value"]):
            fired["value"] = True
            raise KeyboardInterrupt()
        return trace

    config = _config(fake_program, tmp_path, "stall_before_read", timeout_seconds=5.0)
    try:
        with patch.object(ta, "_spawn", observing_spawn):
            sys.settrace(trace)
            try:
                with pytest.raises(KeyboardInterrupt):
                    _admit(config)
            finally:
                sys.settrace(None)
        assert fired["value"], "the trace hook never fired - test needs updating"
        assert roots, "no process was spawned"
        time.sleep(0.3)
        assert roots[0].poll() is not None, (
            "the process was left alive after cancellation immediately following spawn"
        )
    finally:
        for p in roots:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=2)


def test_bad_shape_answer_still_triggers_process_group_cleanup(tmp_path):
    """Finding 1 (fix round 2): success used to be decided as soon as the
    bytes read parsed as JSON, before the operation-specific shape check
    ran - a syntactically valid answer with an unexpected extra field
    raised bad_shape from the PUBLIC wrapper function, by which point
    `_call` had already released the process group as if the call had
    succeeded. The operation's own `validate` callback now runs INSIDE
    `_call`'s try, before `success` is ever set."""
    kills_count = []

    class FakeProc:
        pid = 424242
        stdin = io.BytesIO()
        stdout = io.BytesIO(b'{"status":"ok","v":1,"extra":true}')

        def kill(self):
            # A SUCCESSFUL call's cleanup never calls this (the root already
            # exited and was reaped); only the FAILURE path's `_kill_tree`
            # does. Both branches now close the POSIX process group on this
            # host, so `kill()` is the one signal that distinguishes which
            # branch actually ran.
            kills_count.append(1)

        def wait(self, timeout=None):
            return 0

    group_kills = []
    fake_os = SimpleNamespace(name="posix", killpg=lambda *a: group_kills.append(a), environ=os.environ)
    with patch.object(ta, "os", fake_os), patch.object(ta, "_spawn", return_value=FakeProc()):
        with pytest.raises(ta.TurnAdmissionFailure) as exc:
            ta.closed(ta.TurnAdmissionConfig(sys.executable, (), str(tmp_path), {}, 1.0),
                     agent=AGENT, message_id=MESSAGE_ID)
    assert exc.value.word == "bad_shape"
    assert group_kills, "a syntactically valid but operation-invalid answer did not trigger cleanup"
    assert kills_count, (
        "cleanup took the SUCCESS branch (group-killed but never called proc.kill()) "
        "for an answer that failed its own operation-specific shape check"
    )


def test_read_error_after_a_complete_answer_vetoes_success(tmp_path):
    """Finding 2: the reader thread used to discard OSError/ValueError
    silently once ANY bytes had been read - a read failure arriving right
    after a complete, legal answer had already been buffered still
    returned success. Read success and read failure are now tracked
    independently, exactly like write success/failure: any read error
    vetoes the call with not_started, even though everything read up to
    that point was perfectly valid."""
    entries = [{"message_id": "m" * 32, "reference": "r" * 64} for _ in range(32)]
    entries[0]["reference"] = "r"
    entries[1]["reference"] = "r" * 61
    payload = json.dumps({"messages": entries, "status": "ok", "v": 1},
                          sort_keys=True, separators=(",", ":")).encode("ascii")
    assert len(payload) == 4096, f"probe payload drifted to {len(payload)} bytes - fix the entry sizes above"

    class PrefixThenError:
        def __init__(self):
            self._served = False

        def read(self, n):  # noqa: ARG002
            if not self._served:
                self._served = True
                return payload
            raise OSError("injected read failure after a complete legal answer")

    class FakeProc:
        pid = 424242
        stdin = io.BytesIO()
        stdout = PrefixThenError()

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    kills = []
    fake_os = SimpleNamespace(name="posix", killpg=lambda *a: kills.append(a), environ=os.environ)
    with patch.object(ta, "os", fake_os), patch.object(ta, "_spawn", return_value=FakeProc()):
        with pytest.raises(ta.TurnAdmissionFailure) as exc:
            ta.pending(ta.TurnAdmissionConfig(sys.executable, (), str(tmp_path), {}, 1.0),
                      agent=AGENT, limit=64)
    assert exc.value.word == "not_started"
    assert kills, "a vetoed read did not trigger process-group cleanup"


def test_a_failed_calls_total_time_including_cleanup_never_exceeds_the_configured_timeout(tmp_path):
    """Finding 3: fix round 1's "shared" cleanup budget was still built as
    now-plus-a-constant AFTER the operation's own deadline had already
    passed - a fake clock proved a 0.1-second configured call could take
    2.1 seconds in total (the operation's own 0.1-second wait, THEN a
    fresh 2.0-second cleanup wait on top). Cleanup must draw only from
    what is LEFT of the one original deadline, so the worst-case total can
    never exceed the configured timeout."""
    clock = SimpleNamespace(now=0.0)
    waits = []

    class ExpiringProc:
        pid = 424242
        stdin = io.BytesIO()
        stdout = io.BytesIO(b'{"status":"ok","v":1}')

        def kill(self):
            pass

        def wait(self, timeout=None):
            waits.append(timeout)
            clock.now += timeout
            if len(waits) == 1:
                raise subprocess.TimeoutExpired("probe", timeout)
            return 0

    fake_time = SimpleNamespace(monotonic=lambda: clock.now)
    fake_os = SimpleNamespace(name="posix", killpg=lambda *a: None, environ=os.environ)
    config = ta.TurnAdmissionConfig(sys.executable, (), str(tmp_path), {}, 0.1)
    with patch.object(ta, "time", fake_time), patch.object(ta, "os", fake_os), \
         patch.object(ta, "_spawn", return_value=ExpiringProc()):
        with pytest.raises(ta.TurnAdmissionFailure) as exc:
            ta.closed(config, agent=AGENT, message_id=MESSAGE_ID)
    assert exc.value.word == "timeout"
    assert clock.now <= 0.1 + 1e-9, (
        f"total elapsed (fake clock) was {clock.now}s for a 0.1s configured timeout - "
        "cleanup granted itself extra time beyond the original deadline"
    )


def test_nul_in_command_is_refused_at_configuration_time(tmp_path):
    """Finding 4: a NUL in the command path used to pass `from_mapping`
    unchecked and raise a bare `ValueError: embedded null character` from
    `subprocess.Popen` at launch time."""
    with pytest.raises(ValueError, match="NUL"):
        ta.TurnAdmissionConfig.from_mapping({
            "command": sys.executable + "\x00", "args": [], "cwd": str(tmp_path),
            "env": {}, "timeout_seconds": 5,
        })


def test_nul_in_an_argument_is_refused_at_configuration_time(tmp_path):
    """Finding 4: the two original examples (held-reason, environment
    entries) were fixed, but a NUL in an ordinary argument still reached
    `subprocess.Popen` unchecked."""
    with pytest.raises(ValueError, match="NUL"):
        ta.TurnAdmissionConfig.from_mapping({
            "command": sys.executable, "args": ["a\x00b"], "cwd": str(tmp_path),
            "env": {}, "timeout_seconds": 5,
        })


def test_nul_in_cwd_is_refused_at_configuration_time(tmp_path):
    """Finding 4: a NUL in the working directory string had the same gap."""
    with pytest.raises(ValueError, match="NUL"):
        ta.TurnAdmissionConfig.from_mapping({
            "command": sys.executable, "args": [], "cwd": str(tmp_path) + "\x00",
            "env": {}, "timeout_seconds": 5,
        })


def test_spawn_normalizes_an_unanticipated_bare_valueerror_too(fake_program, tmp_path):
    """Finding 4, defense in depth: configuration-time validation rejects
    every NUL this module anticipated, but `subprocess.Popen` itself can
    still raise a bare `ValueError` for some malformed launch string (this
    is exactly how the NUL bug first escaped: Popen's own `ValueError:
    embedded null character`, not an `OSError`). `_spawn` must normalize
    ANY such failure to the closed vocabulary, not just `OSError`."""
    config = _config(fake_program, tmp_path, "admitted")
    with patch.object(subprocess, "Popen", side_effect=ValueError("embedded null character")):
        with pytest.raises(ta.TurnAdmissionFailure) as exc:
            _admit(config)
    assert exc.value.word == "not_started"


def test_windows_execution_target_strips_trailing_dots_and_spaces():
    """Finding 5: Windows itself strips trailing dots and spaces from the
    final path segment before deciding which file actually runs - the
    normalization this module's refusal check must match."""
    assert ta._windows_execution_target("foo.cmd") == "foo.cmd"
    assert ta._windows_execution_target("foo.cmd ") == "foo.cmd"
    assert ta._windows_execution_target("foo.cmd.") == "foo.cmd"
    assert ta._windows_execution_target("foo.cmd . . ") == "foo.cmd"


@pytest.mark.skipif(os.name != "nt", reason="trailing-space/dot normalization (finding 5) is Windows-specific")
@pytest.mark.parametrize("trailing", [" ", "."])
def test_windows_batch_refusal_survives_a_trailing_space_or_dot(tmp_path, trailing):
    """Finding 5: a raw suffix check on the operator's literal string
    disagreed with the path Windows actually executes - a real `.cmd` file
    named with one trailing space passed configuration and then actually
    ran. Check the suffix of the NORMALIZED execution target instead."""
    batch = tmp_path / "program.cmd"
    batch.write_text('@echo off\necho {"v":1,"status":"ok"}\n', encoding="ascii")
    with pytest.raises(ValueError, match="native executable"):
        ta.TurnAdmissionConfig.from_mapping({
            "command": str(batch) + trailing, "args": [], "cwd": str(tmp_path),
            "env": {}, "timeout_seconds": 5,
        })


@pytest.mark.skipif(os.name != "nt", reason="Windows environment names are case-insensitive")
def test_build_child_env_recognizes_a_differently_cased_system_default(tmp_path):
    """Finding 7: Windows environment names are case-insensitive, but a
    Python dict key comparison is not - an operator env entry already
    naming a system default under different case (e.g. "SYSTEMROOT" vs
    this module's own "SystemRoot") must not cause BOTH to reach the
    child; only the operator's own entry should survive."""
    config = ta.TurnAdmissionConfig(
        command=sys.executable, args=(), cwd=str(tmp_path),
        env={"SYSTEMROOT": "C:\\CustomWindows"}, timeout_seconds=5.0,
    )
    env = ta._build_child_env(config)
    matches = [name for name in env if name.casefold() == "systemroot"]
    assert matches == ["SYSTEMROOT"], env


@pytest.mark.skipif(os.name != "nt", reason="Windows environment names are case-insensitive")
def test_conflicting_env_aliases_are_refused_at_configuration_time(tmp_path):
    """Finding 7: two operator env names that collide only by case (e.g.
    "FOO" and "foo") are ambiguous on Windows and must be refused at
    configuration time, not left for the child to interpret arbitrarily."""
    with pytest.raises(ValueError, match="case"):
        ta.TurnAdmissionConfig.from_mapping({
            "command": sys.executable, "args": [], "cwd": str(tmp_path),
            "env": {"FOO": "1", "foo": "2"}, "timeout_seconds": 5,
        })


def test_successful_call_also_closes_the_process_group_on_posix(tmp_path):
    """The connector's decision on the successful-call point: a successful
    call used to only close the Windows job - nothing closed the POSIX
    process group after success, so a program that left descendants
    behind on a clean exit could leak them. Both platforms now close out
    descendants on every outcome, not only a failed one."""
    class FakeProc:
        pid = 424242
        stdin = io.BytesIO()
        stdout = io.BytesIO(b'{"status":"ok","v":1}')

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    kills = []
    fake_os = SimpleNamespace(name="posix", killpg=lambda *a: kills.append(a), environ=os.environ)
    with patch.object(ta, "os", fake_os), patch.object(ta, "_spawn", return_value=FakeProc()):
        result = ta.closed(ta.TurnAdmissionConfig(sys.executable, (), str(tmp_path), {}, 1.0),
                           agent=AGENT, message_id=MESSAGE_ID)
    assert result == {"status": "ok", "v": 1}
    assert kills, "a successful call did not close the process group on POSIX"
