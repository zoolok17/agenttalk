"""Writer-to-model regression for the console v2 stuck rule.

The health files the console reads are written by the real ``WrapperHealthWriter`` and read back
through the real ``Store.read_health``. ``WrapperHealthWriter`` keeps ``last_progress_at`` across
``idle()`` and ``turn_start()``, so progress from an EARLIER turn is still in the file when a new
turn begins. These tests drive that writer under a fake clock and feed the resulting rows to the
node model (``agentView``): a turn that has just started must not be judged by the previous turn's
progress. No hand-written health JSON is involved.
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
                 ttl: float | None = None) -> None:
        """Read the health the way /api/state does, at the evaluation time, with a fresh heartbeat.

        ``ttl`` overrides the reader's 300 s freshness window. The default reader turns a snapshot
        older than 300 s into ``unknown`` (see test_health_older_than_the_ttl_reads_unknown...), so
        cases that need a snapshot older than five minutes to still carry its state pass a longer
        window and say so in their name.
        """
        eval_time = NOON.replace(hour=hh, minute=mm, second=ss)
        kwargs = {} if ttl is None else {"ttl_seconds": ttl}
        health = self.store.read_health(AGENT, now_epoch=eval_time.timestamp(), **kwargs)
        row = {
            "name": AGENT,
            "last_seen": self._iso(eval_time - timedelta(seconds=20)),
            "last_seen_age_seconds": 20,
            "health": health,
        }
        self.scenarios.append({"name": name, "evalMs": int(eval_time.timestamp() * 1000), "agent": row,
                               "recent": recent or []})

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
    h.evaluate("still_quiet_9m", 12, 9, 0, ttl=3600)      # (longer reader window: see the TTL test)
    h.evaluate("quiet_10m", 12, 10, 5, ttl=3600)           # 10 min 10 s since the turn began, nothing said
    out = h.run(tmp_path)
    assert out["just_started"]["state"] == "busy"
    assert out["just_started"]["stuck"] is None
    assert out["just_started"]["line"] == "Quiet · no progress since the turn began 5s ago"
    assert out["still_quiet_9m"]["state"] == "busy"
    assert out["quiet_10m"]["state"] == "stuck"
    assert out["quiet_10m"]["stuck"].startswith("No progress since the turn began 10m ago")


def test_progress_noted_inside_the_current_turn_counts(h: Harness, tmp_path: Path) -> None:
    h.at(11, 40)
    h.writer.turn_start({"id": "m1", "request_id": "r1"})
    h.at(11, 45)
    h.note_progress()                  # `agenttalk progress`: state stays working_silent
    assert h.store.read_health_raw(AGENT)["state"] == "working_silent"
    h.evaluate("after_note_5m", 11, 50)
    h.evaluate("after_note_13m", 11, 58, ttl=3600)
    out = h.run(tmp_path)
    assert out["after_note_5m"]["state"] == "busy"
    assert out["after_note_5m"]["line"].startswith("Quiet · last progress 5m ago")
    assert out["after_note_13m"]["state"] == "stuck"
    assert out["after_note_13m"]["stuck"].startswith("Last progress 13m ago")


def test_a_progress_event_moves_the_turn_to_working_and_never_stuck(h: Harness, tmp_path: Path) -> None:
    h.at(11, 40)
    h.writer.turn_start({"id": "m1", "request_id": "r1"})
    h.at(11, 41)
    h.progress()
    h.evaluate("long_turn", 12, 0, ttl=3600)
    out = h.run(tmp_path)
    assert out["long_turn"]["state"] == "working"
    assert out["long_turn"]["stuck"] is None


def test_a_watchdog_flag_counts_from_when_it_fired(h: Harness, tmp_path: Path) -> None:
    h.at(11, 20)
    h.progress()                       # earlier progress
    h.at(11, 30)
    h.writer.turn_start({"id": "m2", "request_id": "r2"})
    h.at(11, 45)
    h.writer.failure({"watchdog": True}, None)    # the wrapper's own stall flag
    assert h.store.read_health_raw(AGENT)["state"] == "stuck_suspected"
    h.evaluate("just_flagged", 11, 50)
    h.evaluate("flagged_15m", 12, 0, ttl=3600)
    out = h.run(tmp_path)
    assert out["just_flagged"]["state"] == "busy"        # a candidate, not yet a card
    assert out["just_flagged"]["candidate"] is True
    assert out["flagged_15m"]["state"] == "stuck"
    assert out["flagged_15m"]["stuck"].startswith("Wrapper flagged a stall 15m ago")


def test_health_older_than_the_ttl_reads_unknown_so_a_silent_turn_shows_no_card_by_default(
    h: Harness, tmp_path: Path,
) -> None:
    """KNOWN LIMITATION, pinned so it is not forgotten (docs/STEP-CONSOLE-V2-PITCH.md section 13).

    Nothing refreshes the health file while a turn is silent (the writer writes on state changes
    and adapter events only), and the reader turns a snapshot older than 300 s into ``unknown``
    with its state, ``since`` and ``last_progress_at`` dropped. A wedged turn therefore reads
    "No fresh health" after five minutes, before the ten-minute stuck rule can be evaluated.
    """
    h.at(11, 45)
    h.writer.turn_start({"id": "m1", "request_id": "r1"})
    h.evaluate("silent_15m_default_ttl", 12, 0)
    h.evaluate("silent_15m_long_ttl", 12, 0, ttl=3600)
    out = h.run(tmp_path)
    assert out["silent_15m_default_ttl"]["state"] == "unknown"
    assert out["silent_15m_default_ttl"]["line"].startswith("No fresh health")
    assert out["silent_15m_long_ttl"]["state"] == "stuck"
