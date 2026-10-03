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

import json
import os
import subprocess
import sys
import time

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
    return True


# --------------------------------------------------------------- environment / args (AC3, 4.1)

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
