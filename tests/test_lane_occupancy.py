"""#659: a running `wrap --lane-id` session keeps its lane "in use" (the occupancy token), and every cleanup path
checks that first. These tests start REAL wrappers (the real CLI, a stand-in model program) and run the cleanup
commands against them. Each refusal asserts the whole of obligation 10: files, branch, status, reservation and
acknowledgement are unchanged."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_lanes import _assign_worktree, _git_rc, _repo, _run_raw

from agenttalk import lanes
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
