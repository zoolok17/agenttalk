"""#659: a running `wrap --lane-id` session keeps its lane "in use" (the occupancy token), and every cleanup path
checks that first. These tests start REAL wrappers (the real CLI, a stand-in model program) and run the cleanup
commands against them. Each refusal asserts the whole of obligation 10: files, branch, status, reservation and
acknowledgement are unchanged."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_lanes import _assign_worktree, _branch, _deliverable_worktree_lane, _git_rc, _repo, _run_raw

from agenttalk import lane_occupancy, lanes
from agenttalk.store import Store

SRC = Path(__file__).resolve().parents[1] / "src"
# The real wrappers below start `python -m agenttalk --version` as a probe with a 5 second limit: run this file's
# tests one after the other on one worker, so they do not load the machine against each other.
pytestmark = pytest.mark.xdist_group("lane-occupancy-wrappers")
STAND_IN = """
import json, os, sys, time
print(json.dumps({"type": "system", "subtype": "init", "session_id": "stand-in"}), flush=True)
os.chdir(os.path.dirname(sys.argv[1]))              # out of the worktree: only the token may keep it, never the system
open(sys.argv[1], "w").close()                      # ready: the "model" is mid-turn
deadline = time.time() + 120
while not os.path.exists(sys.argv[2]) and time.time() < deadline:
    time.sleep(0.05)
"""


class Wrapper:
    """A real `agenttalk wrap --lane-id` process whose stand-in model keeps running until released."""

    def __init__(self, root: Path, tmp: Path, lane_id: str, cli: str = "claude") -> None:
        self.ready, self.release = tmp / f"{lane_id}.ready", tmp / f"{lane_id}.release"
        worktree = lanes.load_lanes(Store(root))["lanes"][lane_id]["worktree_path"]
        stand_in = tmp / "stand_in.py"
        stand_in.write_text(STAND_IN, encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTTALK_")}
        env.update(PYTHONPATH=str(SRC), AGENTTALK_PY=sys.executable, PYTHONDONTWRITEBYTECODE="1")
        self.proc = subprocess.Popen(  # noqa: S603 - test-only, fixed argv
            [
                sys.executable,
                "-m",
                "agenttalk",
                "--root",
                str(root),
                "wrap",
                "--for",
                "dev",
                "--cli",
                cli,
                "--lane-id",
                lane_id,
                "--",
                # Not a virtual environment's launcher: on Windows that process stays in the worktree after the model
                # moved out, and the system would refuse the removal in the token's place.
                getattr(sys, "_base_executable", sys.executable),
                str(stand_in),
                str(self.ready),
                str(self.release),
                *(["--add-dir", str(worktree)] if cli == "codex" else []),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(root),
        )

    def wait_ready(self, timeout: float = 90.0) -> None:
        deadline = time.time() + timeout
        while not self.ready.exists():
            if self.proc.poll() is not None:
                out, err = self.proc.communicate()
                raise AssertionError(f"the wrapper ended early: rc={self.proc.returncode}\n{out}\n{err}")
            if time.time() > deadline:
                raise AssertionError("the wrapper never reached its model")
            time.sleep(0.05)

    def finish(self, timeout: float = 60.0) -> int:
        self.release.write_text("go", encoding="utf-8")
        try:
            self.proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.communicate()
        return self.proc.returncode


@pytest.fixture
def running(tmp_path):
    started: list[Wrapper] = []

    def start(root: Path, lane_id: str, cli: str = "claude") -> Wrapper:
        for attempt in range(8):
            wrapper = Wrapper(root, tmp_path, lane_id, cli)
            started.append(wrapper)
            try:
                wrapper.wait_ready()
            except AssertionError as error:
                # The wrapper's own start-up probe of `python -m agenttalk --version` has a 5 second limit, which a
                # busy machine can exceed. That is the machine, not the lane: start again.
                if "TimeoutExpired" not in str(error) or attempt == 7:
                    raise
                continue
            return wrapper
        raise AssertionError("unreachable")

    yield start
    for wrapper in started:
        if wrapper.proc.poll() is None:
            wrapper.finish()


def _snapshot(root: Path, lane_id: str) -> dict:
    """Everything obligation 10 says a refusal must leave alone."""
    lane = lanes.load_lanes(Store(root))["lanes"][lane_id]
    worktree = Path(lane["worktree_path"]) if lane.get("worktree_path") else None
    return {
        "lane": json.dumps(lane, sort_keys=True),
        "worktree": worktree is not None and worktree.exists(),
        "branch": _git_rc(root, "rev-parse", "--verify", f"refs/heads/lane/{lane_id}^{{commit}}").returncode == 0,
    }


def _restore_lane(root: Path, lane_id: str, record: dict) -> None:
    store = Store(root)
    data = lanes.load_lanes(store)
    data["lanes"][lane_id] = dict(record)
    lanes.save_lanes(store, data)


def _lane_on_its_own_path(root: Path, base: str, lane_id: str) -> None:
    """A second lane in the same store needs a path of its own (reservations may not overlap)."""
    assert (
        _run_raw(
            [
                "lane",
                "assign",
                "--id",
                lane_id,
                "--from",
                "lead",
                "--assignee",
                "dev",
                "--domain",
                "core",
                "--base",
                base,
                "--target",
                _branch(root),
                "--path",
                f"src/core/{lane_id}",
                "--worktrees-root",
                str(root / ".worktrees"),
            ],
            root,
        )
        == 0
    )


def _set_lane(root: Path, lane_id: str, **changes) -> None:
    store = Store(root)
    data = lanes.load_lanes(store)
    data["lanes"][lane_id].update(changes)
    lanes.save_lanes(store, data)


def test_plain_abandon_refuses_while_a_wrapper_runs_on_a_clean_lane_and_works_after_it_ends(tmp_path, running, capsys):
    """Condition 1 (red on the base, where `lane abandon` answers 0 and abandons the lane): the wrapper leaves no
    record the old check can read, so the lane looks idle. The token says otherwise: exit 3 and nothing changes."""
    root, base = _repo(tmp_path)
    _assign_worktree(root, base, "occupied")
    wrapper = running(root, "occupied")
    before = _snapshot(root, "occupied")

    capsys.readouterr()
    assert _run_raw(["lane", "abandon", "--id", "occupied"], root) == 3
    assert "occupied" in capsys.readouterr().err
    assert _snapshot(root, "occupied") == before

    wrapper.finish()
    assert _run_raw(["lane", "abandon", "--id", "occupied"], root) == 0  # free once the session ended
    assert lanes.load_lanes(Store(root))["lanes"]["occupied"]["status"] == lanes.STATUS_ABANDONED


def test_gc_delete_keeps_the_worktree_and_the_branch_of_a_lane_a_wrapper_still_runs_on(tmp_path, running, capsys):
    """Condition 1, `gc --delete`: the lane is eligible by its record (abandoned), but a wrapper still runs on it.
    Master removes a clean worktree and then deletes the branch; the token keeps both and says why."""
    root, base = _repo(tmp_path)
    _assign_worktree(root, base, "gcbusy")
    wrapper = running(root, "gcbusy")
    _set_lane(root, "gcbusy", status=lanes.STATUS_ABANDONED, worktree_state=lanes.STATUS_CLEANUP_PENDING)
    before = _snapshot(root, "gcbusy")

    capsys.readouterr()
    assert _run_raw(["lane", "gc", "--delete", "--json"], root) == 0
    item = next(i for i in json.loads(capsys.readouterr().out)["items"] if i["lane_id"] == "gcbusy")

    assert item.get("worktree_removed") is not True and item.get("branch_deleted") is not True
    assert item["branch_delete_safe"] is False and item["worktree_remove_safe"] is False
    assert _snapshot(root, "gcbusy") == before

    wrapper.finish()
    capsys.readouterr()
    assert _run_raw(["lane", "gc", "--delete", "--json"], root) == 0
    item = next(i for i in json.loads(capsys.readouterr().out)["items"] if i["lane_id"] == "gcbusy")
    assert item["branch_deleted"] is True  # free: today's behaviour


def test_deliver_leaves_the_cleanup_pending_while_a_wrapper_runs_on_the_lane(tmp_path, running, capsys):
    """Condition 1, the end of `lane deliver`: the delivery stands, the worktree stays."""
    root, base = _repo(tmp_path)
    head = _deliverable_worktree_lane(root, base, "deliverbusy")
    wrapper = running(root, "deliverbusy")
    before = _snapshot(root, "deliverbusy")
    tracked = Path(json.loads(before["lane"])["worktree_path"]) / "src" / "core" / "deliverbusy" / "x.py"
    contents = tracked.read_bytes()

    capsys.readouterr()
    assert _run_raw(["lane", "deliver", "--id", "deliverbusy", "--from", "dev", "--head", head], root) == 0

    captured = capsys.readouterr()
    assert "delivered lane deliverbusy" in captured.out and "cleanup pending" in captured.out
    assert lane_occupancy.refusal("deliverbusy", lane_occupancy.IN_USE) in captured.err  # the token, nothing else
    assert tracked.read_bytes() == contents
    after = _snapshot(root, "deliverbusy")
    assert after["worktree"] is True and after["branch"] is True
    assert json.loads(after["lane"])["status"] == lanes.STATUS_DELIVERED
    assert json.loads(before["lane"])["status"] == "active"
    wrapper.finish()


def test_acknowledged_abandon_refuses_while_a_wrapper_runs_on_the_lane(tmp_path, running, capsys):
    """Condition 1, the acknowledged abandon of #654: it records a person's word that the worktree is gone, so it must
    not run while a session is on the lane. The stored folder is made to point at nothing, as the option requires."""
    root, base = _repo(tmp_path)
    _assign_worktree(root, base, "ackbusy")
    wrapper = running(root, "ackbusy")
    _set_lane(root, "ackbusy", worktree_path=str(tmp_path / "no-such-folder"))
    before = _snapshot(root, "ackbusy")

    capsys.readouterr()
    assert _run_raw(["lane", "abandon", "--id", "ackbusy", "--acknowledge-missing-worktree"], root) == 3

    assert "ackbusy" in capsys.readouterr().err
    assert _snapshot(root, "ackbusy") == before
    assert "worktree_cleanup_acknowledged_at" not in json.loads(before["lane"])
    wrapper.finish()


# -- the token module, on its own ------------------------------------------------------------------------------------


def test_the_two_sides_exclude_each_other_and_release_by_closing(tmp_path):
    root, _base = _repo(tmp_path)
    store = Store(root)
    with lane_occupancy.cleanup_claim(store, "one") as busy:
        assert busy is None  # a missing token is created and is free
        with lane_occupancy.cleanup_claim(store, "one") as second:
            assert second == lane_occupancy.IN_USE  # nobody waits: the second answer is immediate
        assert lane_occupancy.peek(store, "one") == lane_occupancy.IN_USE
    assert lane_occupancy.peek(store, "one") is None  # released by closing; the file stays
    assert (store.dir / "lane-occupancy" / "lane-one.token").is_file()
    assert lane_occupancy.peek(store, "never-seen") is None
    assert not (store.dir / "lane-occupancy" / "lane-never-seen.token").exists()  # a look creates nothing


def _in_process_hold(store, lane_id, monkeypatch):
    monkeypatch.setattr(lane_occupancy, "_HELD", {})
    lane_occupancy.hold(store, lane_id)
    return lane_occupancy._HELD[lane_id]


def test_a_held_token_refuses_cleanup_and_a_claimed_one_refuses_the_wrapper(tmp_path, monkeypatch):
    root, _base = _repo(tmp_path)
    store = Store(root)
    handle = _in_process_hold(store, "two", monkeypatch)
    with lane_occupancy.cleanup_claim(store, "two") as busy:
        assert busy == lane_occupancy.IN_USE
    lane_occupancy._close(handle)
    lane_occupancy._HELD.clear()
    with lane_occupancy.cleanup_claim(store, "two") as busy:
        assert busy is None
        with pytest.raises(lane_occupancy.LaneOccupied, match="being cleaned up"):
            lane_occupancy.hold(store, "two")  # cleanup first: the wrapper is refused, nothing started
    assert lane_occupancy._HELD == {}


@pytest.mark.parametrize("shape", ["folder", "link", "pipe", "denied", "folder-vanished"])
def test_a_token_that_is_not_a_plain_file_is_never_free(tmp_path, monkeypatch, shape):
    """Condition 5: a folder, link, pipe, denied access or vanished folder at the token's place."""
    root, _base = _repo(tmp_path)
    store = Store(root)
    place = store.dir / "lane-occupancy"
    place.mkdir()
    token = place / "lane-odd.token"
    if shape == "folder":
        token.mkdir()
    elif shape == "link":
        target = tmp_path / "elsewhere.token"
        target.write_text("", encoding="utf-8")
        try:
            token.symlink_to(target)
        except OSError:
            pytest.skip("no symbolic links here")
    elif shape == "pipe":
        if not hasattr(os, "mkfifo"):
            pytest.skip("no named pipes here")
        os.mkfifo(token)
    elif shape == "denied":
        real_open = lane_occupancy._open
        monkeypatch.setattr(lane_occupancy, "_open", lambda path, shared: (None, f"{lane_occupancy.UNKNOWN}: denied"))
        assert real_open is not None
    else:
        shutil.rmtree(place)
        (store.dir / "lane-occupancy").write_text("a file where the folder should be", encoding="utf-8")

    with lane_occupancy.cleanup_claim(store, "odd") as busy:
        assert busy is not None and busy != lane_occupancy.IN_USE and lane_occupancy.UNKNOWN in busy
    with pytest.raises(lane_occupancy.LaneOccupied):
        lane_occupancy.hold(store, "odd")


def test_an_error_after_the_token_is_open_does_not_leave_the_lane_claimed(tmp_path, monkeypatch):
    """R1-4: the claim is open (a real kernel handle) when the next step fails. Windows looks at the file's attributes
    after opening it, POSIX locks it; either failure must close the file, or the next, real claim says "in use"."""
    root, _base = _repo(tmp_path)
    store = Store(root)
    token = lane_occupancy.token_path(store, "leaky")
    if os.name == "nt":
        real = os.lstat

        def lstat(path, *args, **kwargs):
            if Path(path) == token:
                raise PermissionError("injected: the attributes cannot be read")
            return real(path, *args, **kwargs)

        monkeypatch.setattr(os, "lstat", lstat)
    else:
        import fcntl

        def flock(fd, how):
            raise OSError(5, "injected: the lock cannot be taken")

        monkeypatch.setattr(fcntl, "flock", flock)
    with lane_occupancy.cleanup_claim(store, "leaky") as busy:
        assert busy is not None and busy != lane_occupancy.IN_USE and lane_occupancy.UNKNOWN in busy
    monkeypatch.undo()

    with lane_occupancy.cleanup_claim(store, "leaky") as retry:
        assert retry is None  # nobody holds the lane: the failed attempt let go of its file


# -- a lane-bound Codex must be proved to be the real program ----------------------------------------------------------


def _codex_package(tmp_path, name="pkg", *, mutate=None, npm_version=None):
    """A fake native Codex package in the real layout: <package>/vendor/<triple>/bin/codex[.exe] with its record two
    folders above the program. `mutate(record)` damages the record; `npm_version` adds the launcher package above."""
    triples = sorted(lane_occupancy._machine_triples())
    if not triples:
        pytest.skip("no Codex target for this machine")
    base = tmp_path / name
    if npm_version is not None:
        base = base / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai" / "codex-x"
    folder = base / "vendor" / triples[0]
    program = folder / "bin" / ("codex.exe" if os.name == "nt" else "codex")
    program.parent.mkdir(parents=True)
    program.write_bytes(b"not a real program")
    record = {"layoutVersion": 1, "version": "0.163.0", "target": triples[0], "entrypoint": f"bin/{program.name}"}
    if mutate:
        mutate(record)
    (folder / "codex-package.json").write_text(json.dumps(record), encoding="utf-8")
    if npm_version is not None:
        launcher = program.parents[6]
        (launcher / "package.json").write_text(json.dumps({"name": "@openai/codex", "version": npm_version}), "utf-8")
    return program


def _damage(**changes):
    def apply(record):
        for key, value in changes.items():
            if value is None:
                record.pop(key, None)
            else:
                record[key] = value

    return apply


def test_a_package_with_a_matching_record_is_proved(tmp_path):
    assert lane_occupancy.codex_proof(_codex_package(tmp_path)) is None
    assert lane_occupancy.codex_proof(_codex_package(tmp_path, "npm", npm_version="0.163.0")) is None


@pytest.mark.parametrize(
    ("damage", "fragment"),
    [
        (_damage(layoutVersion=True), "layoutVersion"),
        (_damage(layoutVersion=2), "layoutVersion"),
        (_damage(layoutVersion=None), "layoutVersion"),
        (_damage(target="riscv64-unknown-elf"), "another machine"),
        (_damage(entrypoint="bin/other"), "no readable package record"),
        (_damage(entrypoint="../escape/bin/codex"), "inside the package"),
        (_damage(entrypoint=None), "inside the package"),
        (_damage(version="latest"), "plain release"),
        (_damage(version="0.163"), "plain release"),
        (_damage(version="0.162.0"), "plain release"),
        (_damage(version=163), "plain release"),
    ],
)
def test_a_damaged_record_does_not_prove_the_program(tmp_path, damage, fragment):
    why = lane_occupancy.codex_proof(_codex_package(tmp_path, mutate=damage))
    assert why is not None and fragment in why


def test_other_programs_are_not_proved(tmp_path):
    proved = _codex_package(tmp_path)
    copy = tmp_path / "copy" / "bin" / proved.name
    copy.parent.mkdir(parents=True)
    shutil.copyfile(proved, copy)  # a compiled launcher named codex, as a copy
    linked = tmp_path / "linked" / "bin" / proved.name
    linked.parent.mkdir(parents=True)
    try:
        os.link(proved, linked)  # ... and as a hard link
    except OSError:
        pytest.skip("no hard links here")
    script = proved.parent / "codex.js"  # the npm launcher script
    script.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    for other in (copy, linked, script, tmp_path / "missing" / "codex"):
        assert lane_occupancy.codex_proof(other) is not None, other


def test_the_launcher_package_must_be_the_same_release_on_the_npm_route(tmp_path):
    why = lane_occupancy.codex_proof(_codex_package(tmp_path, npm_version="0.164.0"))
    assert why is not None and "launcher package" in why


def _lane_bound_codex(monkeypatch, proof=True):
    monkeypatch.setattr(lane_occupancy, "_HELD", {"lane": 1})
    monkeypatch.setattr(lane_occupancy, "_PROOF_CLI", "codex" if proof else None)


def test_preflight_blocks_an_unproved_lane_bound_codex_and_names_what_to_give(tmp_path, monkeypatch):
    from agenttalk.wrapper import run as wrapper_run

    _lane_bound_codex(monkeypatch)
    unproved = tmp_path / "bin" / "codex"
    unproved.parent.mkdir()
    unproved.write_bytes(b"x")
    result = wrapper_run.preflight_launch_runtime(
        [str(unproved), "exec"], "codex", tmp_path, dict(os.environ), runner=lambda *a: (0, "")
    )
    assert result.blocked is not None
    for text in ("codex-package.json two folders above it", "AGENTTALK_CODEX", "CODEX_MANAGED_*", "update action"):
        assert text in result.blocked
    assert "PATH is not changed" in result.blocked

    proved = _codex_package(tmp_path)
    ok = wrapper_run.preflight_launch_runtime(
        [str(proved), "exec"], "codex", tmp_path, dict(os.environ), runner=lambda *a: (0, "")
    )
    assert ok.blocked is None and ok.argv[0] == str(proved.resolve())
    _lane_bound_codex(monkeypatch, proof=False)  # no lane, or an ephemeral launch with evidence
    free = wrapper_run.preflight_launch_runtime(
        [str(unproved), "exec"], "codex", tmp_path, dict(os.environ), runner=lambda *a: (0, "")
    )
    assert free.blocked is None


def test_a_real_lane_bound_codex_that_is_not_proved_starts_nothing(tmp_path):
    """The real wrapper: the launch.blocked route, exit 1, and the model never ran."""
    root, base = _repo(tmp_path)
    _assign_worktree(root, base, "unproved")
    wrapper = Wrapper(root, tmp_path, "unproved", cli="codex")
    out, err = wrapper.proc.communicate(timeout=120)
    assert wrapper.proc.returncode == 1, (out, err)
    assert "launch config-blocked" in err and "codex-package.json" in err
    assert not wrapper.ready.exists()  # no child ran
    assert lanes.load_lanes(Store(root))["lanes"]["unproved"]["status"] == "active"


def test_the_proof_runs_again_before_every_spawn_on_both_paths(tmp_path, monkeypatch):
    """Obligation 8 / test 8: at both `Popen` sites the token reaches the child, and a program that stopped being the
    proved one between two spawns is refused the second time (startup is not the only check)."""
    from agenttalk.wrapper import run as wrapper_run

    root, _base = _repo(tmp_path)
    handle = _in_process_hold(Store(root), "spawn", monkeypatch)
    monkeypatch.setattr(lane_occupancy, "_PROOF_CLI", "codex")
    proved = _codex_package(tmp_path)
    seen: list[dict] = []

    class FakeProc:
        pid = 1
        stdout = iter(())
        stdin = None

        def poll(self):
            return 0

        def wait(self, *args, **kwargs):
            return 0

    def fake_popen(argv, **kwargs):
        seen.append(kwargs)
        return FakeProc()

    monkeypatch.setattr(wrapper_run.subprocess, "Popen", fake_popen)
    wrapper_run._ProcStream([str(proved), "exec"], "hello")
    assert len(seen) == 1
    if os.name == "nt":
        assert seen[0]["startupinfo"].lpAttributeList["handle_list"] == [handle]
    else:
        assert seen[0]["pass_fds"] == (handle,)
    proved.write_bytes(b"replaced")  # still named like the proved program ...
    (proved.parent.parent / "codex-package.json").write_text("{}", encoding="utf-8")  # ... but no longer its record
    with pytest.raises(PermissionError, match="cannot be proved"):
        wrapper_run._ProcStream([str(proved), "exec"], "hello")
    with pytest.raises(PermissionError, match="cannot be proved"):
        wrapper_run.run_wrapper(cli="codex", agent="dev", argv=[str(proved), "exec"])
    assert len(seen) == 1  # the refused spawns never reached Popen


def _runnable_native_codex(tmp_path):
    """A proved package whose program really runs and exits with the code it is given: a copy of the system command
    interpreter on Windows (`/c exit N`), a two-line script on POSIX. Returns the program and a function making its
    arguments for an exit code."""
    program = _codex_package(tmp_path)
    if os.name == "nt":
        shutil.copyfile(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe", program)
        return program, lambda code: ["/c", f"exit {code}"]
    program.write_text('#!/bin/sh\nexit "$1"\n', encoding="utf-8")
    program.chmod(0o755)
    return program, lambda code: [str(code)]


def _codex_wrap_exit(root, tmp_path, lane_id, program, args, worktree):
    """The exit code of a real `wrap --cli codex` run on this input; started again after a slow start-up probe."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTTALK_")}
    env.update(PYTHONPATH=str(SRC), AGENTTALK_PY=sys.executable, PYTHONDONTWRITEBYTECODE="1")
    argv = [sys.executable, "-m", "agenttalk", "--root", str(root), "wrap", "--for", "dev", "--cli", "codex"]
    argv += ["--lane-id", lane_id] if lane_id else []
    argv += ["--", str(program), *args, "--add-dir", str(worktree)]
    for _attempt in range(8):
        proc = subprocess.run(  # noqa: S603 - test-only, fixed argv
            argv, env=env, capture_output=True, text=True, cwd=str(root), timeout=180
        )
        if "TimeoutExpired" not in proc.stderr:
            return proc.returncode, proc.stderr
    raise AssertionError("the wrapper never got past its start-up probe")


@pytest.mark.parametrize("code", [0, 3, 7])
def test_an_explicit_proved_native_codex_exits_as_the_same_input_does_without_a_lane(tmp_path, code):
    """R1-5: the token and the proof must not change what the wrapper answers. The same program with the same
    arguments, once on a lane (proved, token handed over) and once with no lane at all (what the wrapper did before the
    token): the same exit code, and the program's own code at that."""
    root, base = _repo(tmp_path)
    worktree = Path(_assign_worktree(root, base, "parity")["worktree_path"])
    program, make_args = _runnable_native_codex(tmp_path)

    lane_code, lane_err = _codex_wrap_exit(root, tmp_path, "parity", program, make_args(code), worktree)
    plain_code, plain_err = _codex_wrap_exit(root, tmp_path, None, program, make_args(code), worktree)

    assert lane_code == plain_code, (lane_err, plain_err)
    assert code == 0 or lane_code == code  # a failing program's own code comes through (0 is 1: no turn was driven)


# -- an ephemeral launch is waived from the proof only on positive evidence of the CURRENT request ----------


def _ephemeral_launch(root, base, *, lane="eph", launched=False):
    """A real prepared ephemeral launch request on a real lane: the supervisor's own `prepare_launch_request` writes the
    marker, the review request, the roster entry and the active record (with its launch binding), and the isolated
    Codex home it needs exists. Returns the store and the temporary agent's name."""
    from test_ephemeral_reviewers import _cfg, _marker

    from agenttalk import ephemeral, supervisor

    store = Store(root)
    shim = store.dir / "bin" / "agenttalk.cmd"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_bytes(supervisor.agenttalk_shim(sys.executable).encode("utf-8"))
    config = _cfg(default_timeout_seconds=600)
    (store.dir / "supervisor.json").write_text(json.dumps(config), encoding="utf-8")
    _lane_on_its_own_path(root, base, lane)
    store.write_launch_request(_marker("req-1", lane_id=lane))
    state: dict = {}
    now = time.time()
    spec = supervisor.prepare_launch_request(store, state, config, "req-1", now_epoch=now)
    codex_home = Path(spec["recovery_environment"]["CODEX_HOME"])
    (codex_home / "skills" / "agenttalk-listen").mkdir(parents=True, exist_ok=True)
    (codex_home / "auth.json").write_text("{}", encoding="utf-8")
    (codex_home / "config.toml").write_text("", encoding="utf-8")
    if launched:
        ephemeral.record_launched(state, request_id="req-1", pid=None, pid_start=None, now_epoch=now)
        store.update_launch_request("req-1", {"state": "launched"})
    supervisor.save_supervisor_state(store.dir / "supervisor-state.json", state)
    return store, spec["agent"]


def _edit_json(path: Path, change) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def _active_record(data: dict) -> dict:
    return data["ephemeral_reviewers"]["active"]["req-1"]


@pytest.fixture
def eph_root(tmp_path):
    return _repo(tmp_path)


def test_a_valid_current_prepared_request_before_its_first_poll_is_waived(eph_root):
    store, agent = _ephemeral_launch(*eph_root)
    assert lane_occupancy.current_ephemeral_launch(store, "req-1", agent, "eph") is True


def test_the_restart_route_keeps_the_waiver_for_a_launched_request_and_not_for_a_marker_merely_on_disk(eph_root):
    store, agent = _ephemeral_launch(*eph_root, launched=True)
    assert lane_occupancy.current_ephemeral_launch(store, "req-1", agent, "eph") is True
    (store.dir / "supervisor-state.json").write_text(
        "{}", encoding="utf-8"
    )  # the marker is on disk, nothing records it
    assert lane_occupancy.current_ephemeral_launch(store, "req-1", agent, "eph") is False


def _changed_launch_hash(state):
    _active_record(state)["effective_launch_binding"]["launch_sha256"] = "0" * 64


def _changed_review_hash(state):
    _active_record(state)["effective_launch_binding"]["review_request_sha256"] = "0" * 64


def _changed_saved_launch(state):
    _active_record(state)["launch"] = {"windows_file": "a different program"}


def _edit_marker(store, change):
    _edit_json(store.launch_requests_dir / "req-1.json", change)


def _edit_state(store, change):
    _edit_json(store.dir / "supervisor-state.json", change)


@pytest.mark.parametrize(
    "case",
    [
        "no-request",
        "another-agent",
        "another-lane",
        "lane-and-scope-disagree",
        "scope-only-other-lane",
        "failed-marker",
        "completed-marker",
        "damaged-marker",
        "no-marker",
        "stale-binding",
        "binding-missing",
        "past-deadline",
        "unfinished-phase",
        "unreadable-state",
        "changed-launch-hash",
        "changed-review-hash",
        "changed-saved-launch",
        "changed-profile-launch",
    ],
)
def test_anything_short_of_positive_current_evidence_is_not_waived(eph_root, case):
    """The maintainers' binding P2 (test 10) and round 3 of the review (R1-3): a stale, terminal, damaged or unreadable
    record, or a launch binding that is no longer the one the supervisor itself would accept for recovery (a changed
    launch hash, review-request hash or saved launch projection), must not waive the proof."""
    store, agent = _ephemeral_launch(*eph_root)
    request, lane = "req-1", "eph"
    marker_file = store.launch_requests_dir / "req-1.json"
    edits = {
        "failed-marker": lambda: _edit_marker(store, lambda m: m.update(state="failed")),
        "completed-marker": lambda: _edit_marker(store, lambda m: m.update(state="completed")),
        "lane-and-scope-disagree": lambda: _edit_marker(store, lambda m: m["scope"].update(lane_id="other")),
        "scope-only-other-lane": lambda: _edit_marker(
            store, lambda m: m.update(lane_id=None) or m["scope"].update(lane_id="other")
        ),
        "damaged-marker": lambda: marker_file.write_text("{not json", encoding="utf-8"),
        "no-marker": lambda: marker_file.unlink(),
        "stale-binding": lambda: _edit_marker(store, lambda m: m.update(prompt="a different request")),
        "binding-missing": lambda: _edit_state(
            store, lambda d: _active_record(d).update(effective_launch_binding=None)
        ),
        "past-deadline": lambda: _edit_state(store, lambda d: _active_record(d).update(deadline_epoch=time.time() - 5)),
        "unfinished-phase": lambda: _edit_state(store, lambda d: _active_record(d).update(phase="completed")),
        "unreadable-state": lambda: (store.dir / "supervisor-state.json").write_text("{not json", encoding="utf-8"),
        "changed-launch-hash": lambda: _edit_state(store, _changed_launch_hash),
        "changed-review-hash": lambda: _edit_state(store, _changed_review_hash),
        "changed-saved-launch": lambda: _edit_state(store, _changed_saved_launch),
        "changed-profile-launch": lambda: _edit_json(
            store.dir / "supervisor.json",
            lambda c: c["ephemeral_reviewers"]["allowed_profiles"]["codex-evidence-reviewer"]["launch"][
                "windows_args"
            ].append("--other"),
        ),
    }
    edits.get(case, lambda: None)()
    if case == "no-request":
        request = None
    elif case == "another-agent":
        agent = "someone-else"
    elif case == "another-lane":
        lane = "another"

    assert lane_occupancy.current_ephemeral_launch(store, request, agent, lane) is False


# -- the lane stays in use while anything holding the token lives (test 6) ----------------------------------

SURVIVOR_POSIX = """
import json, os, subprocess, sys, time
print(json.dumps({"type": "system", "subtype": "init", "session_id": "survivor"}), flush=True)
waiter = "import os, sys, time\\nd = time.time() + 120\\n"
waiter += "while not os.path.exists(sys.argv[1]) and time.time() < d: time.sleep(0.05)"
sys.stdout.flush()
for fd in (0, 1, 2):    # let go of the wrapper's pipes, or it would wait for the survivor too
    os.close(fd)
null = subprocess.DEVNULL    # no pipe of the wrapper's is shared, or it would wait for this process too
subprocess.Popen([sys.executable, "-c", waiter, sys.argv[2]], stdin=null, stdout=null, stderr=null, close_fds=False)
open(sys.argv[1], "w").close()                                                   # ready: and now this child ends
"""


# Windows: the real wrapper also waits for every holder of its output pipe, so the survivor may inherit nothing but the
# token. The model finds the token among its open handles (it is the only file handle named like the lane's token) and
# starts the survivor with that one handle in the inherit list and no pipes.
SURVIVOR_WINDOWS = r"""
import ctypes, json, os, subprocess, sys
from ctypes import wintypes
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.GetFileType.argtypes = [wintypes.HANDLE]
k32.GetFileType.restype = wintypes.DWORD
k32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
k32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
tokens = []
for handle in range(4, 65536, 4):
    if k32.GetFileType(handle) == 1:
        name = ctypes.create_unicode_buffer(32768)
        if k32.GetFinalPathNameByHandleW(handle, name, len(name), 0) and name.value.endswith("lane-survivor.token"):
            tokens.append(handle)
assert len(tokens) == 1, tokens
os.set_handle_inheritable(tokens[0], True)
waiter = "import os, sys, time\nd = time.time() + 120\n"
waiter += "while not os.path.exists(sys.argv[1]) and time.time() < d: time.sleep(0.05)"
null = subprocess.DEVNULL
subprocess.Popen(
    [sys.executable, "-c", waiter, sys.argv[2]], stdin=null, stdout=null, stderr=null, close_fds=True,
    startupinfo=subprocess.STARTUPINFO(lpAttributeList={"handle_list": tokens}),
)
print(json.dumps({"type": "system", "subtype": "init", "session_id": "survivor"}), flush=True)
open(sys.argv[1], "w").close()                                                   # ready: and now this child ends
"""
SURVIVOR = SURVIVOR_WINDOWS if os.name == "nt" else SURVIVOR_POSIX


def _wait_until(condition, timeout=60.0):
    deadline = time.time() + timeout
    while not condition():
        assert time.time() < deadline, "timed out"
        time.sleep(0.1)


def test_a_hard_killed_wrapper_leaves_the_lane_in_use_until_its_child_exits(tmp_path, running, capsys):
    """Test 6: the system releases the token when the last holder exits, whatever became of the wrapper."""
    root, base = _repo(tmp_path)
    _assign_worktree(root, base, "killed")
    wrapper = running(root, "killed")
    before = _snapshot(root, "killed")
    wrapper.proc.kill()
    wrapper.proc.wait(timeout=30)

    assert _run_raw(["lane", "abandon", "--id", "killed"], root) == 3  # the stand-in model still lives
    assert _snapshot(root, "killed") == before

    wrapper.release.write_text("go", encoding="utf-8")  # the model child exits
    _wait_until(lambda: lane_occupancy.peek(Store(root), "killed") is None)
    assert _run_raw(["lane", "abandon", "--id", "killed"], root) == 0


def test_a_normal_teardown_with_a_surviving_child_keeps_the_lane_in_use_until_it_exits(tmp_path, capsys):
    """Test 6, the other half: the wrapper and its model child end normally, but a process the model started and left
    running still holds the token. A server left behind keeps the lane in use (the stated limit); stopping it frees
    it."""
    root, base = _repo(tmp_path)
    _assign_worktree(root, base, "survivor")
    stand_in = tmp_path / "survivor.py"
    stand_in.write_text(SURVIVOR, encoding="utf-8")
    ready, release = tmp_path / "survivor.ready", tmp_path / "survivor.release"
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTTALK_")}
    env.update(PYTHONPATH=str(SRC), AGENTTALK_PY=sys.executable, PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run(  # noqa: S603 - test-only, fixed argv
        [
            sys.executable,
            "-m",
            "agenttalk",
            "--root",
            str(root),
            "wrap",
            "--for",
            "dev",
            "--cli",
            "claude",
            "--lane-id",
            "survivor",
            "--",
            sys.executable,
            str(stand_in),
            str(ready),
            str(release),
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(root),
        timeout=180,
    )
    assert ready.exists(), done.stderr
    before = _snapshot(root, "survivor")

    assert _run_raw(["lane", "abandon", "--id", "survivor"], root) == 3  # the wrapper is gone; its survivor is not
    assert _snapshot(root, "survivor") == before

    release.write_text("go", encoding="utf-8")
    _wait_until(lambda: lane_occupancy.peek(Store(root), "survivor") is None)
    assert _run_raw(["lane", "abandon", "--id", "survivor"], root) == 0


# -- the race: both orders, each path, and the whole interval from eligibility to deletion (test 3) --------


def _refused_wrapper(root, tmp_path, lane_id):
    """Starts a real wrapper on the lane and returns (exit code, stderr, whether its model ever ran). A start-up probe
    that timed out on a busy machine is tried again."""
    for _attempt in range(8):
        wrapper = Wrapper(root, tmp_path, lane_id)
        _out, err = wrapper.proc.communicate(timeout=180)
        if "TimeoutExpired" not in err:
            return wrapper.proc.returncode, err, wrapper.ready.exists()
    raise AssertionError("the wrapper never got past its start-up probe")


def _gate_after_claim(monkeypatch, gate):
    """Runs `gate(lane_id)` once, right after a cleanup has taken a FREE claim and before it does anything else."""
    real = lane_occupancy.cleanup_claim
    fired: list[str] = []

    @contextlib.contextmanager
    def gated(store, lane_id):
        with real(store, lane_id) as busy:
            if busy is None and not fired:
                fired.append(lane_id)
                gate(lane_id)
            yield busy

    monkeypatch.setattr(lane_occupancy, "cleanup_claim", gated)
    return fired


@pytest.mark.parametrize("path", ["abandon", "acknowledged", "gc", "deliver", "release"])
def test_a_wrapper_that_arrives_while_cleanup_holds_the_lane_is_refused_before_any_child(tmp_path, monkeypatch, path):
    """Cleanup first: the wrapper is refused with no child, and the cleanup goes through. A wrapper that arrives
    afterwards finds the lane no longer active and is refused as well."""
    root, base = _repo(tmp_path)
    if path in ("deliver", "release"):
        head = _deliverable_worktree_lane(root, base, "arrive")
    else:
        _assign_worktree(root, base, "arrive")
    wt = Path(lanes.load_lanes(Store(root))["lanes"]["arrive"]["worktree_path"])
    if path == "acknowledged":
        shutil.rmtree(wt)
        _git_rc(root, "worktree", "prune")
    if path == "gc":
        _set_lane(root, "arrive", status=lanes.STATUS_ABANDONED, worktree_state=lanes.STATUS_CLEANUP_PENDING)
    if path == "release":
        _merged_on_origin(root, head)
        _set_lane(root, "arrive", status=lanes.STATUS_DELIVERED, worktree_state=lanes.STATUS_CLEANUP_PENDING)
    outcome: list = []
    fired = _gate_after_claim(monkeypatch, lambda lane_id: outcome.append(_refused_wrapper(root, tmp_path, lane_id)))

    command = {
        "abandon": ["lane", "abandon", "--id", "arrive"],
        "acknowledged": ["lane", "abandon", "--id", "arrive", "--acknowledge-missing-worktree"],
        "gc": ["lane", "gc", "--delete"],
        "deliver": ["lane", "deliver", "--id", "arrive", "--from", "dev", "--head", head if path == "deliver" else ""],
        "release": ["janitor", "--release", str(wt)],
    }[path]
    assert _run_raw(command, root) == 0

    assert fired == ["arrive"]
    code, err, model_ran = outcome[0]
    assert code == 2 and "being cleaned up" in err and not model_ran
    if path != "gc":
        assert lanes.load_lanes(Store(root))["lanes"]["arrive"]["status"] != lanes.STATUS_ACTIVE
    monkeypatch.undo()
    later_code, later_err, later_ran = _refused_wrapper(root, tmp_path, "arrive")
    assert later_code == 2 and not later_ran
    assert "not active for launch" in later_err or "no active provisioned worktree" in later_err


def _merged_on_origin(root: Path, head: str) -> None:
    """What the release janitor asks for: the lane's commit on the default branch of a (local, bare) origin, with its
    scratch folders beside the repository (the default temporary folder is outside the test's store fence)."""
    config, folders = Store(root).config_path, [root.parent / f"{root.name}-{name}" for name in ("scratch", "temp")]
    for folder in folders:
        folder.mkdir()
    settings = json.loads(config.read_text(encoding="utf-8"))
    settings["scratch"] = {"root": str(folders[0]), "tmp_root": str(folders[1])}
    config.write_text(json.dumps(settings), encoding="utf-8")
    remote = root.parent / f"{root.name}-origin.git"
    assert _git_rc(root, "init", "--bare", "-b", _branch(root), str(remote)).returncode == 0
    assert _git_rc(root, "remote", "add", "origin", str(remote)).returncode == 0
    assert _git_rc(root, "push", "origin", f"{head}:refs/heads/{_branch(root)}").returncode == 0


@pytest.mark.parametrize("status", [lanes.STATUS_DELIVERED, lanes.STATUS_ABANDONED])
def test_the_release_janitor_keeps_a_checkout_while_a_surviving_child_holds_its_lane(tmp_path, running, capsys, status):
    """The fifth cleanup path. The lane is finished on paper and its commit is merged, so every record says the checkout
    can go, but the wrapper's model child lives on (outside the checkout, so no file of it is open). Only the token
    tells. Master answers 0 and removes the checkout; once the child exits it can go."""
    root, base = _repo(tmp_path)
    head = _deliverable_worktree_lane(root, base, "stillbusy")
    wrapper = running(root, "stillbusy")
    if status == lanes.STATUS_DELIVERED:
        assert _run_raw(["lane", "deliver", "--id", "stillbusy", "--from", "dev", "--head", head], root) == 0
    else:
        _set_lane(root, "stillbusy", status=status, worktree_state=lanes.STATUS_CLEANUP_PENDING)
    wrapper.proc.kill()
    wrapper.proc.wait(timeout=30)
    _merged_on_origin(root, head)
    checkout = Path(lanes.load_lanes(Store(root))["lanes"]["stillbusy"]["worktree_path"])
    tracked = checkout / "src" / "core" / "stillbusy" / "x.py"
    contents, before = tracked.read_bytes(), _snapshot(root, "stillbusy")

    capsys.readouterr()
    assert _run_raw(["janitor", "--release", str(checkout)], root) == 1

    assert lane_occupancy.refusal("stillbusy", lane_occupancy.IN_USE) in capsys.readouterr().out
    assert _snapshot(root, "stillbusy") == before and tracked.read_bytes() == contents
    assert lane_occupancy.peek(Store(root), "stillbusy") == lane_occupancy.IN_USE

    wrapper.release.write_text("go", encoding="utf-8")  # the model child exits
    _wait_until(lambda: lane_occupancy.peek(Store(root), "stillbusy") is None)
    assert _run_raw(["janitor", "--release", str(checkout)], root) == 0
    assert not checkout.exists()
    assert _git_rc(root, "rev-parse", "--verify", "refs/heads/lane/stillbusy^{commit}").returncode == 0  # branch kept


def _force_assign(root: Path, base: str, lane_id: str) -> int:
    """The real public reassignment: a new generation of the lane. It does not take the occupancy token."""
    return _run_raw(
        [
            "lane",
            "assign",
            "--id",
            lane_id,
            "--force",
            "--from",
            "lead",
            "--assignee",
            "dev2",
            "--advisory",
            "--domain",
            "core",
            "--base",
            base,
            "--target",
            _branch(root),
            "--path",
            f"src/core/{lane_id}",
            "--no-worktree",
            "--worktree-waiver-reason",
            "second generation",
        ],
        root,
    )


def test_a_force_assign_at_abandons_last_decision_waits_until_the_branch_is_deleted(tmp_path, monkeypatch, capsys):
    """Obligation 6 for abandon. The reassignment starts the moment abandon has decided the branch may go. Master let
    go of the config lock before that decision, so the new generation was saved and abandon then deleted the branch of
    an active lane. The record abandon judged must still be the current one when the branch goes."""
    import threading

    from agenttalk import cli

    root, base = _repo(tmp_path)
    _assign_worktree(root, base, "again")
    store, real_safe, real_write = Store(root), cli._lane_branch_delete_safe, cli._git_write
    codes: list[int] = []
    current: list[str] = []
    thread = threading.Thread(target=lambda: codes.append(_force_assign(root, base, "again")))

    def safe(*args, **kwargs):
        answer = real_safe(*args, **kwargs)
        thread.start()
        thread.join(timeout=4.0)  # long enough for a writer that is not held back to finish
        return answer

    def write(root_arg, argv, **kwargs):
        if argv[:2] == ["branch", "-D"]:  # how abandon deletes the branch since #686
            current.append(lanes.load_lanes(store)["lanes"]["again"]["status"])
        return real_write(root_arg, argv, **kwargs)

    monkeypatch.setattr(cli, "_lane_branch_delete_safe", safe)
    monkeypatch.setattr(cli, "_git_write", write)

    capsys.readouterr()
    assert _run_raw(["lane", "abandon", "--id", "again", "--delete-branch", "--target", _branch(root)], root) == 0
    thread.join(timeout=30)

    assert "branch deleted" in capsys.readouterr().out
    assert current == [lanes.STATUS_ABANDONED]
    assert codes == [0]  # it was only held back: the reassignment itself goes through afterwards
    assert lanes.load_lanes(store)["lanes"]["again"]["status"] == lanes.STATUS_ACTIVE


def test_a_force_assign_before_deliverys_removal_keeps_the_old_worktree(tmp_path, monkeypatch, capsys):
    """Obligation 6 for delivery. The reassignment lands after delivery has read the lane for its cleanup. Master
    removed the worktree and only noticed at the checkpoint. The look inside the lock refuses first: it stays."""
    from agenttalk import cli

    root, base = _repo(tmp_path)
    head = _deliverable_worktree_lane(root, base, "reused")
    checkout = Path(lanes.load_lanes(Store(root))["lanes"]["reused"]["worktree_path"])
    real_cleanup, fired = cli._lane_deliver_cleanup, []

    def cleanup(*args, **kwargs):
        assert _force_assign(root, base, "reused") == 0
        fired.append(True)
        return real_cleanup(*args, **kwargs)

    monkeypatch.setattr(cli, "_lane_deliver_cleanup", cleanup)

    capsys.readouterr()
    code = _run_raw(["lane", "deliver", "--id", "reused", "--from", "dev", "--head", head], root)

    assert fired and code == 2
    assert "changed" in capsys.readouterr().err
    assert checkout.exists()
    assert _git_rc(root, "rev-parse", "--verify", "refs/heads/lane/reused^{commit}").returncode == 0


def test_a_lane_writer_that_arrives_while_delivery_removes_the_worktree_waits_until_it_is_gone(
    tmp_path, monkeypatch, capsys
):
    """The same fence as gc's: a writer that starts as `git worktree remove` is about to run is held back until the
    removal is over: removed first, saved second."""
    import threading

    from agenttalk import cli

    root, base = _repo(tmp_path)
    head = _deliverable_worktree_lane(root, base, "fenced")
    store, real_write, events = Store(root), cli._git_write, []

    def writer():
        with store._config_lock():
            events.append("saved")

    thread = threading.Thread(target=writer)

    def write(root_arg, argv, **kwargs):
        if argv[:2] == ["worktree", "remove"]:
            thread.start()
            thread.join(timeout=2.0)  # long enough for a writer that is not held back to get its save in
        result = real_write(root_arg, argv, **kwargs)
        if argv[:2] == ["worktree", "remove"]:
            events.append("removed")
        return result

    monkeypatch.setattr(cli, "_git_write", write)
    capsys.readouterr()
    assert _run_raw(["lane", "deliver", "--id", "fenced", "--from", "dev", "--head", head], root) == 0
    thread.join(timeout=30)

    assert events == ["removed", "saved"]


def test_abandon_never_changes_a_lane_that_was_assigned_while_it_was_looking(tmp_path, monkeypatch, capsys):
    """R1-1 of the review: the ordinary `lane assign` writer runs after abandon's first look at the lane list (the
    moment it is about to take the config lock), and a wrapper already holds the new lane. Abandon must not abandon it
    or remove its checkout without the exclusive claim, whatever it saw first."""
    root, base = _repo(tmp_path)
    store, real_lock, fired = Store(root), Store._config_lock, []

    def lock(self, *args, **kwargs):
        if not fired:
            fired.append(True)
            _lane_on_its_own_path(root, base, "late")  # the ordinary writer
            lane_occupancy.hold(store, "late")  # and a regular wrapper holds its token
        return real_lock(self, *args, **kwargs)

    monkeypatch.setattr(Store, "_config_lock", lock)
    try:
        capsys.readouterr()
        code = _run_raw(["lane", "abandon", "--id", "late"], root)
    finally:
        monkeypatch.undo()
        lane_occupancy._close(lane_occupancy._HELD.pop("late"))

    lane = lanes.load_lanes(store)["lanes"]["late"]
    assert fired and code == 3
    assert lane["status"] == lanes.STATUS_ACTIVE and Path(lane["worktree_path"]).exists()
    assert _git_rc(root, "rev-parse", "--verify", "refs/heads/lane/late^{commit}").returncode == 0


def test_a_lane_that_changes_between_the_claim_and_the_judging_is_judged_as_it_is_then(tmp_path, monkeypatch, capsys):
    """gc works from cached records, so under the claim it reads the lane again: a lane that became active again after
    gc started is not eligible any more, and nothing of it is touched."""
    root, base = _repo(tmp_path)
    original = dict(_assign_worktree(root, base, "flips"))
    _set_lane(root, "flips", status=lanes.STATUS_ABANDONED, worktree_state=lanes.STATUS_CLEANUP_PENDING)
    before = _snapshot(root, "flips")
    _gate_after_claim(monkeypatch, lambda lane_id: _restore_lane(root, lane_id, original))  # recovered: as it was

    capsys.readouterr()
    assert _run_raw(["lane", "gc", "--delete", "--json"], root) == 0
    item = next(i for i in json.loads(capsys.readouterr().out)["items"] if i["lane_id"] == "flips")

    assert item.get("worktree_removed") is not True and item.get("branch_deleted") is not True
    after = _snapshot(root, "flips")
    assert after["worktree"] and after["branch"] and json.loads(after["lane"])["status"] == lanes.STATUS_ACTIVE
    assert before["worktree"] and before["branch"]


@pytest.mark.parametrize("moment", ["after-the-judging", "before-the-look", "removal-fails"])
def test_a_conflicting_change_anywhere_in_the_interval_from_eligibility_to_deletion_refuses_the_deletion(
    tmp_path, monkeypatch, capsys, moment
):
    """Obligation 6, an acceptance test: a change that gets in before gc looks at the lane again (right after the
    judging, or right before the look) refuses the deletion, and BOTH the checkout and the branch survive; a failed
    removal keeps the branch too (#657)."""
    from agenttalk import cli

    root, base = _repo(tmp_path)
    checkout = Path(_assign_worktree(root, base, "interval")["worktree_path"])
    _set_lane(root, "interval", status=lanes.STATUS_ABANDONED, worktree_state=lanes.STATUS_CLEANUP_PENDING)
    flip = lambda: _set_lane(root, "interval", status=lanes.STATUS_ACTIVE)  # noqa: E731 - recovery re-activates it
    real_judge, real_write, real_lock = cli._lane_worktree_remove_safe, cli._git_write, Store._config_lock
    judged: list[bool] = []

    def judge_then_change(*args, **kwargs):
        answer = real_judge(*args, **kwargs)
        judged.append(True)
        if moment == "after-the-judging":
            flip()
        return answer

    def lock(self, *args, **kwargs):
        if moment == "before-the-look" and judged and len(judged) == 1:
            judged.append(True)
            flip()
        return real_lock(self, *args, **kwargs)

    def write(root_arg, argv, **kwargs):
        if argv[:2] == ["worktree", "remove"] and moment == "removal-fails":
            return 1, "", "injected: the removal failed"
        return real_write(root_arg, argv, **kwargs)

    monkeypatch.setattr(cli, "_lane_worktree_remove_safe", judge_then_change)
    monkeypatch.setattr(Store, "_config_lock", lock)
    monkeypatch.setattr(cli, "_git_write", write)

    capsys.readouterr()
    assert _run_raw(["lane", "gc", "--delete", "--json"], root) == 0
    item = next(i for i in json.loads(capsys.readouterr().out)["items"] if i["lane_id"] == "interval")

    assert item.get("branch_deleted") is not True and item["branch_delete_safe"] is False
    assert item.get("worktree_removed") is not True
    assert checkout.exists()  # the checkout, not only the branch (the lesson of the first review)
    assert _git_rc(root, "rev-parse", "--verify", "refs/heads/lane/interval^{commit}").returncode == 0


def test_a_lane_writer_that_arrives_while_gc_removes_the_checkout_waits_until_gc_is_done(tmp_path, monkeypatch, capsys):
    """R1-2: no change gets in between gc's last look and its removal. A lane writer (it takes the config lock, as every
    one does) that starts at the moment `git worktree remove` is about to run is held back until gc has finished: the
    order is removed first, saved second - never saved in the middle of the removal."""
    import threading

    from agenttalk import cli

    root, base = _repo(tmp_path)
    _assign_worktree(root, base, "serial")
    _set_lane(root, "serial", status=lanes.STATUS_ABANDONED, worktree_state=lanes.STATUS_CLEANUP_PENDING)
    store, real_write, events = Store(root), cli._git_write, []

    def writer():
        with store._config_lock():  # a recovery or reassignment, saving the lane under the real lock
            events.append("saved")

    thread = threading.Thread(target=writer)

    def write(root_arg, argv, **kwargs):
        if argv[:2] == ["worktree", "remove"] and not thread.is_alive() and not events:
            thread.start()
            time.sleep(1.0)  # long enough for a writer that is not held back to get its save in
        result = real_write(root_arg, argv, **kwargs)
        if argv[:2] == ["worktree", "remove"]:
            events.append("removed")
        return result

    monkeypatch.setattr(cli, "_git_write", write)
    capsys.readouterr()
    assert _run_raw(["lane", "gc", "--delete", "--json"], root) == 0
    thread.join(timeout=30)

    assert events == ["removed", "saved"]


def test_a_gc_that_fails_lets_go_of_its_claim_and_the_config_lock_even_if_its_exception_is_kept(tmp_path, monkeypatch):
    """R3-1: a caller (a logger, a test harness) can keep a failed command's exception, and with it every local of the
    frames it passed through. gc's claim and config lock must not live in such a local: they are let go of when the
    command ends, however it ends."""
    import traceback

    from agenttalk import cli

    root, base = _repo(tmp_path)
    _assign_worktree(root, base, "kept")
    _set_lane(root, "kept", status=lanes.STATUS_ABANDONED, worktree_state=lanes.STATUS_CLEANUP_PENDING)
    real_write = cli._git_write

    def write(root_arg, argv, **kwargs):
        if argv[:2] == ["branch", "-D"]:
            raise cli.GitWriteError("injected: git could not be started for the branch deletion")
        return real_write(root_arg, argv, **kwargs)

    monkeypatch.setattr(cli, "_git_write", write)
    store, caught = Store(root), None
    try:
        _run_raw(["lane", "gc", "--delete", "--json"], root)
    except cli.GitWriteError as error:
        caught = error  # kept, with its traceback, as a caller that logs it would
    try:
        assert caught is not None
        assert lane_occupancy.peek(store, "kept") is None  # no claim is still held
        with store._config_lock(timeout=0, poll=0):  # and the lane records can be locked again at once
            pass
    finally:  # let go of anything a failed run kept, so a red run cannot hold the lock for the tests after it
        for frame, _line in traceback.walk_tb(caught.__traceback__ if caught else None):
            if frame.f_code.co_name == "_cmd_lane" and frame.f_locals.get("claims") is not None:
                frame.f_locals["claims"].close()
        caught = None


# -- plain and looped wrappers, no lane, never-wrapped lanes, and the listing (tests 2 and 5) --------------------------


def _start_looped(root: Path, tmp_path: Path, lane_id: str | None):
    """A real `wrap --loop` (with a lane when given): no inbound message, so it sits quiet between turns."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("AGENTTALK_")}
    env.update(PYTHONPATH=str(SRC), AGENTTALK_PY=sys.executable, PYTHONDONTWRITEBYTECODE="1")
    stand_in = tmp_path / "idle.py"
    stand_in.write_text("import time\ntime.sleep(300)\n", encoding="utf-8")
    argv = [sys.executable, "-m", "agenttalk", "--root", str(root), "wrap", "--for", "dev", "--cli", "claude", "--loop"]
    argv += ["--lane-id", lane_id] if lane_id else []
    argv += ["--", sys.executable, str(stand_in)]
    return subprocess.Popen(  # noqa: S603 - test-only, fixed argv
        argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(root)
    )


def _looped_and_idle(root: Path, tmp_path: Path, lane_id: str | None):
    """A looped wrapper that has reached its first idle wait; started again after a slow start-up probe."""
    runtime = Store(root).state_dir / "dev.wrapper-runtime.json"
    for attempt in range(8):
        proc = _start_looped(root, tmp_path, lane_id)
        _wait_until(lambda proc=proc: runtime.exists() or proc.poll() is not None, 120)
        if proc.poll() is None:
            time.sleep(1.0)  # past its first idle wait
            return proc
        _out, err = proc.communicate()
        assert "TimeoutExpired" in err and attempt < 7, err
    raise AssertionError("unreachable")


def test_a_looped_wrapper_that_is_quiet_between_turns_still_holds_the_lane(tmp_path):
    """Test 2: no child exists between turns, yet the lane is in use for as long as the session is."""
    root, base = _repo(tmp_path)
    _assign_worktree(root, base, "quiet")
    proc = _looped_and_idle(root, tmp_path, "quiet")
    try:
        before = _snapshot(root, "quiet")
        assert _run_raw(["lane", "abandon", "--id", "quiet"], root) == 3
        assert _snapshot(root, "quiet") == before
    finally:
        proc.kill()
        proc.communicate()
    assert _run_raw(["lane", "abandon", "--id", "quiet"], root) == 0  # the session is gone: the system released it


def test_a_wrapper_without_a_lane_makes_no_token(tmp_path):
    root, _base = _repo(tmp_path)
    proc = _looped_and_idle(root, tmp_path, None)
    proc.kill()
    proc.communicate()
    assert not (Store(root).dir / "lane-occupancy").exists()


def test_lanes_that_were_never_wrapped_clean_up_as_before_and_cleanup_creates_a_missing_token(tmp_path, capsys):
    """Test 5: a lane no session ever held behaves as it always did; the cleanup creates the missing token (so a missing
    token means nobody holds it), and the stop-first sentence stays in the help."""
    root, base = _repo(tmp_path)
    lane = _assign_worktree(root, base, "never")
    token = Store(root).dir / "lane-occupancy" / "lane-never.token"
    assert not token.exists()

    assert _run_raw(["lane", "abandon", "--id", "never"], root) == 0
    assert token.is_file() and not Path(lane["worktree_path"]).exists()  # abandoned, worktree removed, token made

    capsys.readouterr()
    with pytest.raises(SystemExit):
        _run_raw(["lane", "abandon", "--help"], root)
    assert "Running `wrap --lane-id` sessions are not detected; stop them first." in " ".join(
        capsys.readouterr().out.split()
    )


def test_the_listing_agrees_with_gc_delete_and_creates_nothing(tmp_path, running, capsys):
    """`lane gc` without --delete only looks: it reports a lane in use as `manual`, and makes no token for a lane that
    never had one."""
    root, base = _repo(tmp_path)
    _lane_on_its_own_path(root, base, "listed")
    _lane_on_its_own_path(root, base, "unlisted")
    wrapper = running(root, "listed")  # started while the lane is active, then both are made eligible by their records
    for lane_id in ("listed", "unlisted"):
        _set_lane(root, lane_id, status=lanes.STATUS_ABANDONED, worktree_state=lanes.STATUS_CLEANUP_PENDING)
    tokens = Store(root).dir / "lane-occupancy"
    capsys.readouterr()

    assert _run_raw(["lane", "gc", "--json"], root) == 0
    items = {i["lane_id"]: i for i in json.loads(capsys.readouterr().out)["items"]}

    assert items["listed"]["worktree_remove_safe"] is False and items["listed"]["branch_delete_safe"] is False
    assert "in use" in items["listed"]["reason"]
    assert items["unlisted"]["branch_delete_safe"] is True  # the same lane without a session
    assert not (tokens / "lane-unlisted.token").exists()  # looking made no token
    wrapper.finish()


def test_a_process_the_model_leaves_behind_keeps_the_token_after_the_model_and_the_wrapper_are_gone(tmp_path):
    """Test 6 on every platform, without the wrapper's pipes: the token is handed to the model child the way the wrapper
    does (`child_kwargs`), the model starts a process that inherits it and ends, the wrapper side lets go, and the
    survivor alone keeps the lane in use until it exits."""
    root, _base = _repo(tmp_path)
    store = Store(root)
    lane_occupancy.hold(store, "left-behind")
    try:
        stand_in = tmp_path / "survivor.py"
        stand_in.write_text(
            SURVIVOR_POSIX.replace("sys.stdout.flush()", "pass").replace("os.close(fd)", "pass"), "utf-8"
        )
        ready, release = tmp_path / "left.ready", tmp_path / "left.release"
        child = subprocess.Popen(  # noqa: S603 - test-only, fixed argv
            [sys.executable, str(stand_in), str(ready), str(release)],
            stdout=subprocess.DEVNULL,
            **lane_occupancy.child_kwargs(),
        )
        assert child.wait(timeout=60) == 0  # the model child is gone ...
    finally:
        lane_occupancy._close(lane_occupancy._HELD.pop("left-behind"))  # ... and so is the wrapper's own handle
    assert lane_occupancy.peek(store, "left-behind") == lane_occupancy.IN_USE  # the survivor still holds it
    release.write_text("go", encoding="utf-8")
    _wait_until(lambda: lane_occupancy.peek(store, "left-behind") is None)
