"""Writer-to-model regression for the console v2 stuck rule.

The health files the console reads are written by the real ``WrapperHealthWriter`` and read back
through the real ``Store.read_health`` with a live heartbeat, exactly as ``/api/state`` does. The
rows are then run through the node model (``agentView``). No hand-written health JSON is involved.

Two real-writer facts drive these tests:

* ``WrapperHealthWriter`` keeps ``last_progress_at`` across ``idle()`` and ``turn_start()``, so a new
  turn must not be judged by an earlier turn's progress;
* nothing refreshes the health file while a turn is silent (and idle health is written once, when the
  turn ends), while the heartbeat keeps moving. The reader therefore turns the snapshot into ``unknown``
  (older than the TTL, or older than the heartbeat by more than the skew) and, since M2c, adds
  ``last_known_*`` so the console can still tell a wedged silent turn from a healthy one.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agenttalk import health as health_model
from agenttalk.store import Store
from agenttalk.wrapper.events import Event, EventType
from agenttalk.wrapper.health import WrapperHealthWriter

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECK = REPO_ROOT / "tests" / "console2_writer_check.mjs"
AGENT = "codex-probe-developer-1"
NOON = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


class Harness:
    """A real store and writer under a controllable clock."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.store = Store(tmp_path)
        self.store.init([AGENT, "claude-probe-lead"])
        self.now = NOON - timedelta(hours=1)
        monkeypatch.setattr(health_model, "now_iso", lambda: self._iso(self.now))
        self.writer = WrapperHealthWriter(self.store, AGENT, "codex", mode="wrapper", min_interval=0)
        self.scenarios: list[dict] = []

    @staticmethod
    def _iso(moment: datetime) -> str:
        return moment.isoformat().replace("+00:00", "Z")

    def at(self, hh: int, mm: int, ss: int = 0) -> None:
        self.now = NOON.replace(hour=hh, minute=mm, second=ss)

    def progress(self) -> None:
        event = Event(EventType.TOOL_STARTED, tool="bash")
        assert event.is_progress
        self.writer.event(event)

    def note_progress(self) -> None:
        """What the ``agenttalk progress`` CLI does: annotate a working snapshot in place."""
        raw = self.store.read_health_raw(AGENT)
        stamped = health_model.stamp_progress(raw, now=self._iso(self.now))
        assert stamped is not None
        self.store.write_health(AGENT, stamped)

    def evaluate(self, name: str, hh: int, mm: int, ss: int = 0, recent: list | None = None,
                 heartbeat_age: float | None = 20, ttl: float | None = None) -> None:
        """Read the health the way /api/state does (with the agent's heartbeat) at the evaluation time.

        ``heartbeat_age`` seconds before the evaluation time (None = no heartbeat at all).
        """
        eval_time = NOON.replace(hour=hh, minute=mm, second=ss)
        heartbeat = None if heartbeat_age is None else eval_time - timedelta(seconds=heartbeat_age)
        kwargs = {} if ttl is None else {"ttl_seconds": ttl}
        health = self.store.read_health(AGENT, now_epoch=eval_time.timestamp(), heartbeat=heartbeat, **kwargs)
        row = {"name": AGENT, "health": health}
        if heartbeat is not None:
            row["last_seen"] = self._iso(heartbeat)
            row["last_seen_age_seconds"] = heartbeat_age
        self.scenarios.append({"name": name, "evalMs": int(eval_time.timestamp() * 1000), "agent": row,
                               "recent": recent or [], "reason": health.get("reason_code")})

    def run(self, tmp_path: Path) -> dict:
        path = tmp_path / "scenarios.json"
        path.write_text(json.dumps({"scenarios": self.scenarios}), encoding="utf-8")
        result = subprocess.run(
            ["node", str(CHECK), str(path)],
            capture_output=True, text=True, encoding="utf-8", timeout=60, cwd=REPO_ROOT,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)


@pytest.fixture
def h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(tmp_path, monkeypatch)


def _reason(h: Harness, name: str) -> str | None:
    return next(s["reason"] for s in h.scenarios if s["name"] == name)


def test_the_writer_really_keeps_progress_across_idle_and_a_new_turn(h: Harness) -> None:
    """The premise of the regression, checked against the real writer (not assumed)."""
    h.at(11, 30)
    h.progress()
    h.at(11, 31)
    h.writer.idle()
    h.at(11, 59, 55)
    h.writer.turn_start({"id": "m1", "request_id": "r1"})
    raw = h.store.read_health_raw(AGENT)
    assert raw["state"] == "working_silent"
    assert raw["last_progress_at"].startswith("2026-09-26T11:30:00")
    assert raw["since"].startswith("2026-09-26T11:59:55")
    assert "turn_started_at" not in raw   # the file carries `since`, not a separate turn start


def test_a_new_turn_is_not_stuck_because_of_the_previous_turns_progress(h: Harness, tmp_path: Path) -> None:
    h.at(11, 30)
    h.progress()                       # progress in the previous turn
    h.at(11, 31)
    h.writer.idle()                    # turn ends
    h.at(11, 59, 55)
    h.writer.turn_start({"id": "m1", "request_id": "r1"})   # a new turn, five seconds ago
    h.evaluate("just_started", 12, 0, 0)
    h.evaluate("quiet_9m", 12, 9, 0)
    h.evaluate("quiet_10m", 12, 10, 5)     # 10 min 10 s since the turn began, nothing said
    out = h.run(tmp_path)
    assert _reason(h, "just_started") == "turn_spawned"                           # the writer's own reason: fresh
    assert out["just_started"]["state"] == "busy"
    assert out["just_started"]["stuck"] is None
    assert out["just_started"]["line"] == "Quiet · no progress since the turn began 5s ago"
    assert _reason(h, "quiet_9m") == "health_stale_ttl"                          # nothing wrote since
    assert out["quiet_9m"]["state"] == "busy"
    assert out["quiet_9m"]["line"] == "Health stale 9m (last known: silent turn) · no reply sent"
    assert out["quiet_10m"]["state"] == "stuck"
    assert out["quiet_10m"]["stuck"] == (
        "Health stale 10m (last known: silent turn) · no reply sent · heartbeat still fresh")


def test_a_wedged_silent_turn_is_judged_through_either_stale_branch(h: Harness, tmp_path: Path) -> None:
    """Younger than the TTL but older than the heartbeat, and older than the TTL: both keep last_known_*."""
    h.at(11, 45)
    h.writer.turn_start({"id": "m1", "request_id": "r1"})
    h.evaluate("by_heartbeat", 11, 47, 0)                         # 2 min: only the heartbeat says it is old
    h.evaluate("by_ttl", 12, 0, 0)                                # 15 min: past the 300 s TTL
    h.evaluate("no_heartbeat_row", 12, 0, 0, heartbeat_age=None)  # the same file, no heartbeat evidence
    out = h.run(tmp_path)
    assert _reason(h, "by_heartbeat") == "health_older_than_heartbeat"
    assert _reason(h, "by_ttl") == "health_stale_ttl"
    assert out["by_heartbeat"]["state"] == "busy"                 # stale, but only 2 minutes
    assert out["by_heartbeat"]["line"] == "Health stale 2m (last known: silent turn) · no reply sent"
    assert out["by_ttl"]["state"] == "stuck"
    assert out["by_ttl"]["stuck"].startswith("Health stale 15m (last known: silent turn)")
    assert out["no_heartbeat_row"]["state"] == "unknown"          # without a fresh heartbeat: not judged
    assert out["no_heartbeat_row"]["stuck"] is None


def test_progress_noted_inside_the_current_turn_counts(h: Harness, tmp_path: Path) -> None:
    h.at(11, 40)
    h.writer.turn_start({"id": "m1", "request_id": "r1"})
    h.at(11, 45)
    h.note_progress()                  # `agenttalk progress`: state stays working_silent
    assert h.store.read_health_raw(AGENT)["state"] == "working_silent"
    h.evaluate("after_note_5m", 11, 50)
    h.evaluate("after_note_13m", 11, 58)
    out = h.run(tmp_path)
    assert out["after_note_5m"]["state"] == "busy"
    assert out["after_note_5m"]["line"] == "Health stale 5m (last known: silent turn) · no reply sent"
    assert out["after_note_13m"]["state"] == "stuck"
    assert out["after_note_13m"]["stuck"].startswith("Health stale 13m (last known: silent turn)")


def test_a_working_turn_that_keeps_writing_is_working_and_one_that_went_quiet_is_flagged(
    h: Harness, tmp_path: Path,
) -> None:
    h.at(11, 40)
    h.writer.turn_start({"id": "m1", "request_id": "r1"})
    h.at(11, 41)
    h.progress()
    h.at(11, 59, 50)
    h.progress()                       # events kept arriving: health is fresh
    h.evaluate("events_flowing", 12, 0, 0)
    h.evaluate("quiet_19m", 12, 19, 0)  # ...and then no event for 19 minutes
    out = h.run(tmp_path)
    assert out["events_flowing"]["state"] == "working"
    assert out["events_flowing"]["stuck"] is None
    assert out["quiet_19m"]["state"] == "stuck"
    assert out["quiet_19m"]["stuck"].startswith("Health stale 19m (last known: working)")


def test_a_watchdog_flag_counts_from_when_it_fired(h: Harness, tmp_path: Path) -> None:
    h.at(11, 20)
    h.progress()                       # earlier progress
    h.at(11, 30)
    h.writer.turn_start({"id": "m2", "request_id": "r2"})
    h.at(11, 45)
    h.writer.failure({"watchdog": True}, None)    # the wrapper's own stall flag
    assert h.store.read_health_raw(AGENT)["state"] == "stuck_suspected"
    h.evaluate("just_flagged", 11, 50)
    h.evaluate("flagged_15m", 12, 0)
    out = h.run(tmp_path)
    assert out["just_flagged"]["state"] == "busy"        # stale, but not yet ten minutes
    assert out["flagged_15m"]["state"] == "stuck"
    assert out["flagged_15m"]["stuck"] == (
        "Health stale 15m (last known: wrapper flagged a stall) · no reply sent · heartbeat still fresh")


def test_idle_health_written_at_turn_end_still_reads_idle_while_the_heartbeat_lives(
    h: Harness, tmp_path: Path,
) -> None:
    """Idle health is written once (turn end); the heartbeat keeps moving, so the reader calls it stale."""
    h.at(11, 20)
    h.writer.turn_start({"id": "m1", "request_id": "r1"})
    h.at(11, 21)
    h.writer.idle(reason_code="turn_completed")
    h.evaluate("idle_40m", 12, 0, 0)
    h.evaluate("idle_no_heartbeat", 12, 0, 0, heartbeat_age=900)
    out = h.run(tmp_path)
    assert _reason(h, "idle_40m") == "health_stale_ttl"
    assert out["idle_40m"]["state"] == "idle"
    assert out["idle_40m"]["line"] == "Idle · 39m"
    assert out["idle_no_heartbeat"]["state"] == "unknown"


def test_no_heartbeat_evidence_is_never_stuck(h: Harness, tmp_path: Path) -> None:
    h.at(11, 30)
    h.writer.turn_start({"id": "m1", "request_id": "r1"})
    h.evaluate("silent_no_heartbeat", 12, 0, 0, heartbeat_age=None)
    out = h.run(tmp_path)
    assert out["silent_no_heartbeat"]["state"] == "unknown"
    assert out["silent_no_heartbeat"]["stuck"] is None
