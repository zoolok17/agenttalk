"""#659: a running `wrap --lane-id` session keeps its lane "in use" (the occupancy token), and every cleanup path
checks that first. These tests start REAL wrappers (the real CLI, a stand-in model program) and run the cleanup
commands against them. Each refusal asserts the whole of obligation 10: files, branch, status, reservation and
acknowledgement are unchanged."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_lanes import _assign_worktree, _git_rc, _repo, _run_raw

from agenttalk import lane_occupancy, lanes
from agenttalk.store import Store

SRC = Path(__file__).resolve().parents[1] / "src"
STAND_IN = """
import json, os, sys, time
print(json.dumps({"type": "system", "subtype": "init", "session_id": "stand-in"}), flush=True)
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
                sys.executable,
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
        for attempt in range(5):
            wrapper = Wrapper(root, tmp_path, lane_id, cli)
            started.append(wrapper)
            try:
                wrapper.wait_ready()
            except AssertionError as error:
                # The wrapper's own start-up probe of `python -m agenttalk --version` has a 5 second limit, which a
                # busy machine can exceed. That is the machine, not the lane: start again.
                if "TimeoutExpired" not in str(error) or attempt == 4:
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
    from test_lanes import _deliverable_worktree_lane

    head = _deliverable_worktree_lane(root, base, "deliverbusy")
    wrapper = running(root, "deliverbusy")
    before = _snapshot(root, "deliverbusy")

    capsys.readouterr()
    assert _run_raw(["lane", "deliver", "--id", "deliverbusy", "--from", "dev", "--head", head], root) == 0

    out = capsys.readouterr().out
    assert "delivered lane deliverbusy" in out and "cleanup pending" in out
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
