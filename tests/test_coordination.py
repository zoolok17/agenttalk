"""Tests for the 0.12.0 coordination-recovery features: per-agent
threadstate, scoped (non-consuming) wait, explicit ack --to-request
closure, and the `sync` rejoin digest."""

from __future__ import annotations

import hashlib
import json
import threading
import time as _time
from pathlib import Path

import pytest

from agenttalk import cli
from agenttalk.store import Store


def _run(argv: list[str], root: Path) -> int:
    return cli.main(["--root", str(root), *argv])


def _run_expect_exit(argv: list[str], root: Path, code: int) -> None:
    try:
        rc = cli.main(["--root", str(root), *argv])
    except SystemExit as e:
        rc = 0 if e.code is None else int(e.code)
    assert int(rc) == code


def _approval_meta_args() -> list[str]:
    return [
        "--meta", "status=approved",
        "--meta", "risk_class=none",
        "--meta", "release_blocker=no",
        "--meta", "tests_referenced=n/a",
        "--meta", "tests_executed=n/a",
        "--meta", "evidence=n/a",
        "--meta", "residual_risk=n/a",
        "--meta", "na_reason=lightweight review",
    ]


# --------------------------------------------------------- threadstate (store)

def test_threadstate_seen_is_monotonic(store: Store) -> None:
    store.mark_thread_seen("alpha", "r1", "002")
    assert store.thread_seen("alpha", "r1") == "002"
    store.mark_thread_seen("alpha", "r1", "001")  # older — ignored
    assert store.thread_seen("alpha", "r1") == "002"
    assert store.thread_closed("alpha", "r1") is False


def test_close_thread_sets_closed_and_seen(store: Store) -> None:
    store.close_thread("alpha", "r1", seen_msg_id="005", reason="manual")
    assert store.thread_closed("alpha", "r1") is True
    assert store.thread_seen("alpha", "r1") == "005"
    entry = store.read_threadstate("alpha")["r1"]
    assert entry["closed_reason"] == "manual" and "closed_at" in entry


def test_closed_rids_requires_strict_boolean(store: Store) -> None:
    """A malformed non-boolean `closed` must NOT count as closed (hardening)."""
    from agenttalk.cli import _closed_rids
    store._write_threadstate("alpha", {
        "r1": {"seen_msg_id": "x", "closed": "true"},  # string — not closed
        "r2": {"closed": True},                          # real closure
    })
    assert _closed_rids(store, "alpha") == {"r2"}


def test_read_threadstate_missing_or_corrupt_is_empty(store: Store, store_root: Path) -> None:
    assert store.read_threadstate("alpha") == {}
    p = store_root / ".agenttalk" / "state" / "alpha.threadstate.json"
    p.write_text("{ not json", encoding="utf-8")
    assert store.read_threadstate("alpha") == {}  # never raises


# ------------------------------------------------------ scoped wait (CLI)

def test_scoped_wait_is_non_consuming(store: Store, store_root: Path) -> None:
    store.send(sender="beta", recipient="alpha", kind="review-request",
               body="review", meta={"request_id": "r1"})
    store.send(sender="beta", recipient="alpha", kind="note", body="unrelated")
    assert store.cursor("alpha") == ""
    # match already exists → returns 0 immediately
    rc = _run(["wait", "--for", "alpha", "--to-request", "r1", "--timeout", "5", "--quiet"], store_root)
    assert rc == 0
    # global cursor UNMOVED; both messages still unread
    assert store.cursor("alpha") == ""
    assert len(store.unread_for("alpha")) == 2
    # only the per-thread pointer advanced
    r1_id = [m.id for m in store.messages_for("alpha") if m.meta.get("request_id") == "r1"][0]
    assert store.thread_seen("alpha", "r1") == r1_id
    assert store.thread_closed("alpha", "r1") is False  # seen != handled


def test_scoped_wait_kind_filter(store: Store, store_root: Path) -> None:
    store.send(sender="beta", recipient="alpha", kind="note",
               body="note on r1", meta={"request_id": "r1"})
    # no review-result on r1 yet → scoped wait restricted to that kind times out
    rc = _run(["wait", "--for", "alpha", "--to-request", "r1", "--kind", "review-result",
               "--timeout", "0.3", "--grace", "0", "--quiet"], store_root)
    assert rc == 1


def test_scoped_wait_kind_requires_to_request(store_root: Path) -> None:
    _run_expect_exit(["wait", "--for", "alpha", "--kind", "note", "--timeout", "1"], store_root, 2)


def test_scoped_wait_ignores_stale_composing(store: Store, store_root: Path) -> None:
    """Regression (Codex review): a STALE composing ping (present before the
    wait, and never consumed because scoped wait is non-consuming) must NOT
    extend a later scoped wait. With the bug it would extend by
    --composing-extend each time → the wait runs far past its timeout."""
    import time as _t
    store.send(sender="beta", recipient="alpha", kind="composing", body="drafting")
    t0 = _t.monotonic()
    rc = _run(["wait", "--for", "alpha", "--to-request", "r1", "--timeout", "0.1",
               "--grace", "0", "--interval", "0.05", "--composing-extend", "5",
               "--quiet"], store_root)
    elapsed = _t.monotonic() - t0
    assert rc == 1
    assert elapsed < 2.0, f"stale composing extended the scoped wait ({elapsed:.1f}s)"


def test_scoped_wait_does_not_redeliver_globally_consumed(store: Store, store_root: Path) -> None:
    """Regression (final review): a message already consumed via the GLOBAL
    cursor (drain/plain wait) must NOT be re-delivered by a scoped wait —
    else after draining+answering a needs-info, `wait --to-request` would
    re-show the old needs-info instead of awaiting the next reply."""
    import time as _t
    store.send(sender="beta", recipient="alpha", kind="review-result",
               body="needs-info", meta={"request_id": "r1", "status": "needs-info"})
    _run(["drain", "--for", "alpha", "--quiet"], store_root)  # global cursor advances past it
    assert store.cursor("alpha") != ""
    t0 = _t.monotonic()
    rc = _run(["wait", "--for", "alpha", "--to-request", "r1", "--timeout", "0.2",
               "--grace", "0", "--interval", "0.05", "--quiet"], store_root)
    elapsed = _t.monotonic() - t0
    assert rc == 1, "scoped wait re-delivered an already-drained message"
    assert elapsed < 2.0


# ------------------- perf fix #1: scoped-wait scan bound (floor vs baseline)

def test_scoped_wait_composing_extends_mid_wait(tmp_path: Path) -> None:
    """Baseline behavior under the perf fix: a composing that arrives DURING
    a scoped wait (id > baseline) still extends the deadline. The wait would
    have timed out at its 0.5s base deadline; the +10s extension lets the
    real reply (sent at ~0.8s) arrive and return 0."""
    root = _init_team(tmp_path, "lead,exec")
    _run(["send", "--from", "lead", "--to", "exec", "--kind", "question",
          "--meta", "request_id=q-fire", "-m", "fire", "--quiet"], root)
    assert _run(["drain", "--for", "exec", "--quiet"], root) == 0  # cursor=baseline
    result: list[int] = []

    def _waiter() -> None:
        result.append(cli.main([
            "--root", str(root), "wait", "--for", "exec", "--to-request", "q-fire",
            "--timeout", "0.5", "--grace", "0", "--interval", "0.05",
            "--composing-extend", "10", "--heartbeat-interval", "0", "--quiet",
        ]))

    t = threading.Thread(target=_waiter)
    t.start()
    _time.sleep(0.2)  # waiter arms; baseline captured
    _run(["send", "--from", "lead", "--to", "exec", "--kind", "composing",
          "-m", "drafting", "--quiet"], root)
    _time.sleep(0.6)  # past the 0.5s base deadline — only the extension keeps it alive
    _run(["send", "--from", "lead", "--to", "exec", "--kind", "review-result",
          "--meta", "request_id=q-fire", *_approval_meta_args(),
          "-m", "lgtm", "--quiet"], root)
    t.join(timeout=15)
    assert not t.is_alive(), "waiter never returned"
    assert result == [0], "mid-wait composing failed to extend the deadline"


def test_scoped_wait_composing_extends_when_cursor_exceeds_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Perf-fix edge: a concurrent same-agent consumer advances the GLOBAL
    cursor above the wait's baseline, so floor = max(thread_seen, cursor) >
    baseline. Scanning only from floor would skip a composing in
    (baseline, floor]; scanning from min(floor, baseline) still sees it. The
    composing here (id < the advanced cursor) must still extend the deadline.

    The clock/sleep hooks make the original deadline pass before the reply is
    sent, so this fails deterministically if the composing is skipped."""
    root = _init_team(tmp_path, "lead,exec")
    _run(["send", "--from", "lead", "--to", "exec", "--kind", "question",
          "--meta", "request_id=q-fire", "-m", "fire", "--quiet"], root)
    assert _run(["drain", "--for", "exec", "--quiet"], root) == 0
    s = Store(root)
    baseline = s.cursor("exec")
    now = 1_000.0
    sleep_calls = 0
    sent_reply = False

    def fake_sleep(_duration: float) -> None:
        nonlocal now, sent_reply, sleep_calls
        sleep_calls += 1
        if sleep_calls == 1:
            # The waiter has captured `baseline`. Now place a composing in
            # (baseline, floor] and advance time beyond the original deadline.
            _run(["send", "--from", "lead", "--to", "exec", "--kind", "composing",
                  "-m", "drafting", "--quiet"], root)
            r1 = s.send(sender="lead", recipient="exec", kind="note", body="other")
            s.advance_cursor("exec", r1.id)
            assert baseline < r1.id
            assert s.cursor("exec") == r1.id
            now = 1_001.0
            return
        if sleep_calls == 2:
            _run(["send", "--from", "lead", "--to", "exec", "--kind", "review-result",
                  "--meta", "request_id=q-fire", *_approval_meta_args(),
                  "-m", "lgtm", "--quiet"], root)
            sent_reply = True
            return
        raise AssertionError("wait loop kept sleeping after the deterministic reply")

    class FakeTime:
        def time(self) -> float:
            return now

        def sleep(self, duration: float) -> None:
            fake_sleep(duration)

    monkeypatch.setattr(cli, "time", FakeTime())

    rc = cli.main([
        "--root", str(root), "wait", "--for", "exec", "--to-request", "q-fire",
        "--timeout", "0.5", "--grace", "0", "--interval", "0.05",
        "--composing-extend", "10", "--heartbeat-interval", "0", "--quiet",
    ])

    assert rc == 0, (
        "composing in (baseline, floor] was skipped — scan bound regressed "
        "from min(floor, baseline) to floor"
    )
    assert sent_reply


def test_scoped_wait_rescind_wakes_when_cursor_exceeds_baseline(
    tmp_path: Path, capsys
) -> None:
    """Perf-fix edge: a rescind on the waited thread whose id falls in
    (baseline, floor] must still wake the scoped wait (exit 3) even after a
    concurrent consumer advanced the global cursor above it. Scanning only
    from floor would never surface the rescind; min(floor, baseline) does."""
    root = _init_team(tmp_path, "lead,exec")
    _run(["send", "--from", "lead", "--to", "exec", "--kind", "question",
          "--meta", "request_id=q-fire", "-m", "fire", "--quiet"], root)
    assert _run(["drain", "--for", "exec", "--quiet"], root) == 0
    s = Store(root)
    result: list[int] = []

    def _waiter() -> None:
        result.append(cli.main([
            "--root", str(root), "wait", "--for", "exec", "--to-request", "q-fire",
            "--timeout", "2", "--grace", "0", "--interval", "0.05",
            "--heartbeat-interval", "0",
        ]))

    t = threading.Thread(target=_waiter)
    t.start()
    _time.sleep(0.3)  # waiter arms; baseline = the q-fire id
    # rescind lands first (id in (baseline, floor])...
    assert _run(["rescind", "--from", "lead", "--to-request", "q-fire",
                 "-m", "HOLD", "--quiet"], root) == 0
    # ...then a later unrelated message a concurrent consumer drains, pulling
    # the global cursor ABOVE the rescind id.
    r1 = s.send(sender="lead", recipient="exec", kind="note", body="other")
    s.advance_cursor("exec", r1.id)
    assert s.cursor("exec") == r1.id  # floor now exceeds the rescind id
    t.join(timeout=15)
    assert not t.is_alive(), "waiter failed to wake on the rescind"
    assert result == [3], "rescind in (baseline, floor] was skipped by the scan bound"
    assert "RESCINDED" in capsys.readouterr().out


# ------------------------- WP-A: poll backoff (#3) + waiter reaping (#4)

def test_next_backoff_growth_and_reset() -> None:
    base, cap = 0.3, 2.0
    seq, cur = [], base
    for _ in range(5):
        seq.append(round(cur, 3))
        cur = cli._next_backoff(cur, base, cap, activity=False)
    assert seq == [0.3, 0.6, 1.2, 2.0, 2.0]          # grows x2, clamps at cap
    assert cli._next_backoff(2.0, base, cap, activity=True) == base  # reset on activity


def test_next_backoff_disabled_when_cap_le_base() -> None:
    # cap <= base => fixed-interval polling, byte-identical to pre-backoff.
    assert cli._next_backoff(0.3, 0.3, 0.3, activity=False) == 0.3
    assert cli._next_backoff(0.3, 0.3, 0.1, activity=False) == 0.3
    assert cli._next_backoff(0.3, 0.3, 0.3, activity=True) == 0.3


def test_clamp_sleep_bounds() -> None:
    # No deadline, no heartbeat -> the desired sleep is unchanged.
    assert cli._clamp_sleep(2.0, 100.0, None, 0.0, 0.0) == 2.0
    # Clamp to time-left-to-deadline.
    assert cli._clamp_sleep(2.0, 100.0, 100.5, 0.0, 0.0) == pytest.approx(0.5)
    # Clamp to next-heartbeat-due (last_heartbeat + interval - now).
    assert cli._clamp_sleep(2.0, 100.0, None, 99.0, 1.5) == pytest.approx(0.5)
    # Min of both boundaries.
    assert cli._clamp_sleep(2.0, 100.0, 100.8, 99.7, 1.0) == pytest.approx(0.7)
    # Floor at 0 when the deadline already passed.
    assert cli._clamp_sleep(2.0, 200.0, 100.0, 0.0, 0.0) == 0.0


def test_wait_idle_times_out_with_backoff(store_root: Path) -> None:
    """Backoff (default cap 2.0) must NOT delay the timeout: the sleep is
    clamped to the deadline, so a short idle wait still exits 1 promptly.
    One-sided bound, no exact wall-clock assertion."""
    import time as _t
    t0 = _t.monotonic()
    rc = _run(["wait", "--for", "alpha", "--timeout", "0.3", "--grace", "0",
               "--quiet"], store_root)
    elapsed = _t.monotonic() - t0
    assert rc == 1
    assert elapsed < 3.0, f"backoff overshot the deadline ({elapsed:.1f}s)"


def test_wait_backoff_sleep_sequence(
    store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the real wait loop with an idle bus and intercept time.sleep:
    enabled backoff grows 0.3->0.6->1.2->2.0; disabled (cap <= interval) stays
    a flat fixed interval — byte-identical to pre-backoff. --timeout 0 +
    --heartbeat-interval 0 removes the deadline/heartbeat clamps so the raw
    sequence is observable. Fully deterministic — no real sleeping."""
    recorded: list[float] = []

    class _Stop(Exception):
        pass

    def fake_sleep(d: float) -> None:
        recorded.append(round(d, 3))
        if len(recorded) >= 4:
            raise _Stop()

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    base_argv = ["--root", str(store_root), "wait", "--for", "alpha",
                 "--timeout", "0", "--heartbeat-interval", "0",
                 "--interval", "0.3", "--quiet"]
    try:
        cli.main([*base_argv, "--max-poll-interval", "2.0"])
    except _Stop:
        pass
    assert recorded == [0.3, 0.6, 1.2, 2.0]

    recorded.clear()
    try:
        cli.main([*base_argv, "--max-poll-interval", "0"])  # disabled
    except _Stop:
        pass
    assert recorded == [0.3, 0.3, 0.3, 0.3]


def test_wait_refuse_stacked_exits_6(
    store: Store, store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fix #4a opt-in: a LIVE duplicate waiter + --refuse-stacked-wait -> exit 6."""
    monkeypatch.setattr("agenttalk.store._process_alive", lambda pid: True)
    store.write_waiting("alpha", {"agent": "alpha", "pid": 999999,
                                  "deadline_epoch": None})
    rc = _run(["wait", "--for", "alpha", "--refuse-stacked-wait",
               "--timeout", "5", "--quiet"], store_root)
    assert rc == 6


def test_wait_warns_but_proceeds_without_refuse(
    store: Store, store_root: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Default stays WARN: a live duplicate warns but the wait still arms
    (so re-arm loops in sk-loop/listen are unaffected) and times out."""
    monkeypatch.setattr("agenttalk.store._process_alive", lambda pid: True)
    store.write_waiting("alpha", {"agent": "alpha", "pid": 999999,
                                  "deadline_epoch": None})
    rc = _run(["wait", "--for", "alpha", "--timeout", "0.2", "--grace", "0",
               "--quiet"], store_root)
    assert rc == 1
    assert "another live process" in capsys.readouterr().err


def test_wait_dead_marker_does_not_block(
    store: Store, store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fix #4b: a CONFIRMED-DEAD waiter marker neither blocks arming nor (with
    the strict flag) triggers a refuse — it is reaped, and the wait proceeds."""
    monkeypatch.setattr("agenttalk.store._process_alive", lambda pid: False)
    store.write_waiting("alpha", {"agent": "alpha", "pid": 999999,
                                  "deadline_epoch": None})
    rc = _run(["wait", "--for", "alpha", "--refuse-stacked-wait",
               "--timeout", "0.2", "--grace", "0", "--quiet"], store_root)
    assert rc == 1


def test_wait_softcap_warns(
    store: Store, store_root: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """fix #4c: arming as the (WAITER_SOFTCAP+1)-th waiter warns. Seeds EXACTLY
    WAITER_SOFTCAP existing markers — this wait is the one that tips over the
    cap, so it must warn even though it hasn't written its own marker yet
    (regression guard for the count+1 off-by-one)."""
    monkeypatch.setattr("agenttalk.store._process_alive", lambda pid: True)
    for i in range(cli.WAITER_SOFTCAP):
        store.write_waiting(f"w{i}", {"agent": f"w{i}", "pid": 1000 + i,
                                      "deadline_epoch": None})
    rc = _run(["wait", "--for", "alpha", "--timeout", "0.05", "--grace", "0",
               "--quiet"], store_root)
    assert rc == 1
    assert "live agenttalk waiters" in capsys.readouterr().err


def _drive_wait_to_cap_then_inject(store_root: Path, monkeypatch, inject, *,
                                   scoped: bool) -> list[float]:
    """Run a real wait loop with an idle bus until backoff reaches the 2.0s
    cap, then call `inject()` (which writes a fresh message) so the NEXT poll
    sees activity. Returns the recorded sleep durations. The sleep right after
    injection must be base (0.3), not the capped 2.0 — that is the
    immediate-reset invariant. Deterministic: time.sleep is intercepted."""
    recorded: list[float] = []

    class _Stop(Exception):
        pass

    def fake_sleep(d: float) -> None:
        recorded.append(round(d, 3))
        if len(recorded) == 4:      # just slept the first capped 2.0
            inject()
        if len(recorded) >= 5:      # captured the post-injection sleep
            raise _Stop()

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    argv = ["--root", str(store_root), "wait", "--for", "alpha",
            "--timeout", "0", "--heartbeat-interval", "0", "--interval", "0.3",
            "--max-poll-interval", "2.0", "--quiet"]
    if scoped:
        argv += ["--to-request", "r1"]
    try:
        cli.main(argv)
    except _Stop:
        pass
    return recorded


def test_wait_backoff_resets_immediately_on_activity(
    store: Store, store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (codex MAJOR): after backoff reaches the cap, a fresh
    composing must drop the NEXT plain-wait sleep back to base — not leave a
    real reply waiting out a full ~2s capped interval."""
    def inject() -> None:
        store.send(sender="beta", recipient="alpha", kind="composing",
                   body="drafting")
    recorded = _drive_wait_to_cap_then_inject(store_root, monkeypatch, inject,
                                              scoped=False)
    assert recorded == [0.3, 0.6, 1.2, 2.0, 0.3]


def test_scoped_wait_backoff_resets_immediately_on_activity(
    store: Store, store_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (codex MAJOR), scoped path: fresh thread traffic after the
    cap resets the next scoped-wait sleep to base."""
    def inject() -> None:
        store.send(sender="beta", recipient="alpha", kind="composing",
                   body="drafting", meta={"request_id": "r1"})
    recorded = _drive_wait_to_cap_then_inject(store_root, monkeypatch, inject,
                                              scoped=True)
    assert recorded == [0.3, 0.6, 1.2, 2.0, 0.3]


# ------------------------------------------------ ack --to-request (closure)

def test_ack_to_request_closes_thread(store: Store, store_root: Path) -> None:
    store.send(sender="beta", recipient="alpha", kind="review-request",
               body="review", meta={"request_id": "r1"})
    # alpha owes a review-result → owed-inbound until explicitly closed
    _run(["ack", "--for", "alpha", "--to-request", "r1"], store_root)
    assert store.thread_closed("alpha", "r1") is True
    out_rc = _run(["threads", "--for", "alpha", "--json"], store_root)
    assert out_rc == 0


def test_ack_to_request_makes_threads_report_closed(
    store: Store, store_root: Path, capsys: pytest.CaptureFixture,
) -> None:
    store.send(sender="beta", recipient="alpha", kind="review-request",
               body="review", meta={"request_id": "r1"})
    store.close_thread("alpha", "r1", seen_msg_id=None, reason="manual")
    capsys.readouterr()
    _run(["threads", "--for", "alpha", "--all", "--json"], store_root)
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["closed"] == 1
    assert payload["counts"]["owed-inbound"] == 0


# ----------------------------------------------------------- sync digest

def test_sync_separates_owed_work_from_fyi(
    store: Store, store_root: Path, capsys: pytest.CaptureFixture,
) -> None:
    store.send(sender="beta", recipient="alpha", kind="review-request",
               body="please review", subject="WP1", meta={"request_id": "r1"})
    store.send(sender="beta", recipient="alpha", kind="note", body="just an fyi")
    capsys.readouterr()
    _run(["sync", "--for", "alpha", "--json"], store_root)
    payload = json.loads(capsys.readouterr().out)
    assert payload["agent"] == "alpha"
    assert payload["counts"]["owed-inbound"] == 1
    # the owed thread is actionable, with owe=you and a reply hint
    t = payload["threads"][0]
    assert t["request_id"] == "r1" and t["owe"] == "you"
    assert "reply --to-request r1" in t["hint"]
    # the note is FYI, kept OUT of owed work
    fyi_kinds = [f["kind"] for f in payload["unread_fyi"]]
    assert "note" in fyi_kinds
    assert all(f.get("kind") != "review-request" for f in payload["unread_fyi"])


def test_sync_reply_waiting_hints_read_not_reply(
    store: Store, store_root: Path, capsys: pytest.CaptureFixture,
) -> None:
    """A reply-waiting thread (a reply landed, unread) should tell the agent
    to READ it (drain), not fire off another message."""
    store.send(sender="alpha", recipient="beta", kind="review-request",
               body="review", meta={"request_id": "r1"})
    store.send(sender="beta", recipient="alpha", kind="review-result",
               body="approved", meta={"request_id": "r1", "status": "approved"})
    # alpha has NOT consumed the verdict → reply-waiting
    capsys.readouterr()
    _run(["sync", "--for", "alpha", "--json"], store_root)
    payload = json.loads(capsys.readouterr().out)
    t = payload["threads"][0]
    assert t["state"] == "reply-waiting"
    assert t["owe"] == "read"
    assert "drain" in t["hint"] and "reply --to-request" not in t["hint"]


def test_sync_caught_up_has_no_actionable(store: Store, store_root: Path,
                                          capsys: pytest.CaptureFixture) -> None:
    capsys.readouterr()
    _run(["sync", "--for", "alpha", "--json"], store_root)
    payload = json.loads(capsys.readouterr().out)
    assert payload["threads"] == []
    assert payload["counts"]["owed-inbound"] == 0


# ======================================================================
# 0.14.0 end-to-end gates (WP04): the two production incidents, dead
# ======================================================================

def _init_team(tmp_path: Path, agents: str) -> Path:
    rc = cli.main(["init", "--path", str(tmp_path), "--agents", agents])
    assert rc == 0
    return tmp_path


def _json_of(argv: list[str], root: Path, capsys) -> dict:
    capsys.readouterr()
    assert _run(argv, root) == 0
    return json.loads(capsys.readouterr().out)


# ----------------------------- T019: the rescind race (Success Criterion 1)

def test_rescind_race_wake_path_mid_wait(tmp_path: Path, capsys) -> None:
    """The HOLD/fire crossing, wake flavor: the executor is BLOCKED in a
    scoped wait when the rescind lands - it must wake with exit 3, not
    act, and not consume."""
    root = _init_team(tmp_path, "lead,exec")
    _run(["send", "--from", "lead", "--to", "exec", "--kind", "question",
          "--meta", "request_id=q-fire", "-m", "fire the launch", "--quiet"], root)
    s = Store(root)
    # exec drains the request (reads it) and arms a scoped wait for
    # follow-ups; the rescind arrives DURING the wait.
    assert _run(["drain", "--for", "exec", "--quiet"], root) == 0
    result: list[int] = []

    def _waiter() -> None:
        result.append(cli.main([
            "--root", str(root), "wait", "--for", "exec",
            "--to-request", "q-fire", "--timeout", "30",
            "--heartbeat-interval", "0",
        ]))

    t = threading.Thread(target=_waiter)
    t.start()
    _time.sleep(0.8)  # let the waiter arm (poll interval 0.3s)
    assert _run(["rescind", "--from", "lead", "--to-request", "q-fire",
                 "-m", "HOLD - new data", "--quiet"], root) == 0
    t.join(timeout=15)
    assert not t.is_alive(), "waiter failed to wake on the rescind"
    assert result == [3]
    # the waiter's user-visible wake: banner + the rescind reason
    out = capsys.readouterr().out
    assert "RESCINDED" in out
    assert "HOLD - new data" in out
    # non-consuming: the rescind is still unread for a later drain
    assert any(m.kind == "rescind" for m in s.unread_for("exec"))
    # and the executor's thread view is terminal-superseded
    data = _json_of(["threads", "--for", "exec", "--all", "--json"], root, capsys)
    row = next(r for r in data["threads"] if r["request_id"] == "q-fire")
    assert row["state"] == "closed-superseded"


def test_rescind_race_check_gate_path(tmp_path: Path, capsys) -> None:
    """The already-drained race no inbox primitive can close: exec read
    the request minutes ago; the contract gate (`check`) catches the
    rescind that landed after."""
    root = _init_team(tmp_path, "lead,exec")
    _run(["send", "--from", "lead", "--to", "exec", "--kind", "question",
          "--meta", "request_id=q-fire", "-m", "fire", "--quiet"], root)
    assert _run(["drain", "--for", "exec", "--quiet"], root) == 0  # consumed
    assert _run(["rescind", "--from", "lead", "--to-request", "q-fire",
                 "--quiet"], root) == 0
    # the gate, immediately before the irreversible action:
    _run_expect_exit(["check", "--for", "exec", "--to-request", "q-fire"], root, 3)
    # exec declines instead of firing; thread is terminal for both
    assert _run(["send", "--from", "exec", "--to", "lead", "--kind", "message",
                 "--meta", "request_id=q-fire",
                 "-m", "aborting: request was rescinded", "--quiet"], root) == 0
    for agent in ("lead", "exec"):
        data = _json_of(["threads", "--for", agent, "--all", "--json"], root, capsys)
        row = next(t for t in data["threads"] if t["request_id"] == "q-fire")
        assert row["state"] == "closed-superseded"


def test_rescind_race_negative_control(tmp_path: Path) -> None:
    """Success Criterion 1's '100% abort' is only meaningful if live
    requests PASS the gate - no false positives."""
    root = _init_team(tmp_path, "lead,exec")
    _run(["send", "--from", "lead", "--to", "exec", "--kind", "question",
          "--meta", "request_id=q-go", "-m", "proceed", "--quiet"], root)
    assert _run(["drain", "--for", "exec", "--quiet"], root) == 0
    _run_expect_exit(["check", "--for", "exec", "--to-request", "q-go"], root, 0)


def test_rescind_race_crossing_variant(tmp_path: Path) -> None:
    """The rescind is written WHILE exec is mid-drain (between send and
    drain): supersession is decided by message-id order, not by what a
    drain happened to consume."""
    root = _init_team(tmp_path, "lead,exec")
    _run(["send", "--from", "lead", "--to", "exec", "--kind", "question",
          "--meta", "request_id=q-fire", "-m", "fire", "--quiet"], root)
    # the crossing: rescind lands before exec's drain
    assert _run(["rescind", "--from", "lead", "--to-request", "q-fire",
                 "--quiet"], root) == 0
    assert _run(["drain", "--for", "exec", "--quiet"], root) == 0  # consumes BOTH
    _run_expect_exit(["check", "--for", "exec", "--to-request", "q-fire"], root, 3)


# --------------------------- T020: the liaison flow (Success Criterion 3)

def test_liaison_flow_happy_loop(tmp_path: Path, capsys) -> None:
    root = _init_team(tmp_path, "lead,w1,w2")
    assert _run(["roster", "set-operator-facing", "lead"], root) == 0
    capsys.readouterr()
    assert _run(["escalate", "--from", "w1",
                 "-m", "Operator: deploy window today or tomorrow?"], root) == 0
    out = capsys.readouterr().out
    rid = next(ln for ln in out.splitlines()
               if ln.startswith("request_id=")).split("=", 1)[1]
    assert rid.startswith("esc-")
    # liaison's bucket has exactly this one entry
    digest = _json_of(["sync", "--for", "lead", "--json"], root, capsys)
    assert [e["request_id"] for e in digest["escalations"]] == [rid]
    # scoped visibility: w2 never sees it
    w2 = _json_of(["sync", "--for", "w2", "--json"], root, capsys)
    assert "escalations" not in w2
    assert all(t["request_id"] != rid for t in w2["threads"])
    # liaison answers on the same thread
    assert _run(["reply", "--from", "lead", "--to-request", rid,
                 "--meta", "operator_answer=true",
                 "-m", "Operator says: tomorrow.", "--quiet"], root) == 0
    digest = _json_of(["sync", "--for", "lead", "--json"], root, capsys)
    assert digest.get("escalations", []) == [] or "escalations" not in digest
    # the worker sees it answered
    rows = _json_of(["threads", "--for", "w1", "--all", "--json"], root, capsys)
    row = next(t for t in rows["threads"] if t["request_id"] == rid)
    assert row["operator_state"] == "answered"


def test_liaison_flow_refusals_e2e(tmp_path: Path, capsys) -> None:
    root = _init_team(tmp_path, "lead,w1,w2")
    # no liaison: refuse loudly WITH the remediation hint (FR-013/NFR-004)
    capsys.readouterr()
    _run_expect_exit(["escalate", "--from", "w1", "-m", "ping"], root, 2)
    err = capsys.readouterr().err
    assert "set-operator-facing" in err
    assert "--to" in err
    # explicit --to override still works
    assert _run(["escalate", "--from", "w1", "--to", "lead", "-m", "ping",
                 "--quiet"], root) == 0
    # lead self-escalation routes to the reserved operator principal so
    # lead-chat pending decisions use the same escalation channel.
    assert _run(["roster", "set-operator-facing", "lead"], root) == 0
    capsys.readouterr()
    assert _run(["escalate", "--from", "lead", "-m", "self",
                 "--quiet"], root) == 0
    lead_escalations = [
        msg for msg in Store(root).valid_messages()
        if msg.sender == "lead" and msg.recipient == "operator"
    ]
    assert len(lead_escalations) == 1
    # liaison cleared mid-flight: the pending escalation survives and the
    # (former) liaison's answer still closes it
    capsys.readouterr()
    assert _run(["escalate", "--from", "w2", "-m", "decision needed"], root) == 0
    rid = next(ln for ln in capsys.readouterr().out.splitlines()
               if ln.startswith("request_id=")).split("=", 1)[1]
    assert _run(["roster", "set-operator-facing", "--clear"], root) == 0
    assert _run(["reply", "--from", "lead", "--to-request", rid,
                 "-m", "answer", "--quiet"], root) == 0
    # consume the answer first: until drained it is (correctly) reply-waiting
    assert _run(["drain", "--for", "w2", "--quiet"], root) == 0
    rows = _json_of(["threads", "--for", "w2", "--all", "--json"], root, capsys)
    row = next(t for t in rows["threads"] if t["request_id"] == rid)
    assert row["state"] == "closed"
    assert row["operator_state"] == "answered"


def test_liaison_single_channel_invariant(tmp_path: Path, capsys) -> None:
    root = _init_team(tmp_path, "lead,w1,w2")
    assert _run(["roster", "set-operator-facing", "lead"], root) == 0
    for w in ("w1", "w2"):
        assert _run(["escalate", "--from", w, "-m", f"question from {w}",
                     "--quiet"], root) == 0
    digest = _json_of(["sync", "--for", "lead", "--json"], root, capsys)
    assert len(digest["escalations"]) == 2
    # zero escalation traffic addressed to anyone but the liaison
    s = Store(root)
    esc = [m for m in s.valid_messages()
           if (m.meta or {}).get("needs_operator") == "true"]
    assert len(esc) == 2
    assert all(m.recipient == "lead" for m in esc)


# ------------------------ T021: backward compatibility (NFR-001 / SC 5)

_PRE_014_THREAD_KEYS = {"request_id", "opener_kind", "subject", "peer", "role",
                        "state", "age_seconds", "last_msg_id", "unread"}

# #19 Phase A: next_action / next_owner are UNIVERSAL on open threads (a pure
# state projection — NOT feature-gated like responded_na etc.), so an OPEN
# thread row carries them as baseline. They are surfaced by the CLI layer
# (WP03), not Thread.to_dict; a CLOSED/terminal thread still omits them.
_OPEN_THREAD_KEYS = _PRE_014_THREAD_KEYS | {"next_action", "next_owner"}


def test_backcompat_json_shapes_without_new_features(tmp_path: Path, capsys) -> None:
    """A store driven only by pre-0.14.0 operations: every pre-existing
    key present, every new key ABSENT (strict additivity)."""
    root = _init_team(tmp_path, "alpha,beta")
    _run(["send", "--from", "alpha", "--to", "beta", "--kind", "question",
          "--meta", "request_id=q-old", "-m", "hello", "--quiet"], root)
    rows = _json_of(["threads", "--for", "alpha", "--all", "--json"], root, capsys)
    assert set(rows) == {"agent", "threads", "counts"}
    row = rows["threads"][0]
    # The q-old thread is OPEN (alpha's open-outbound question) -> carries the
    # universal next_action/next_owner hint; no OTHER new key leaks.
    assert set(row) == _OPEN_THREAD_KEYS
    assert row["next_action"] == "await-reply" and row["next_owner"] == "beta"
    # counts: the ONE documented always-present addition (WP01 NFR-001
    # carve-out, approved review round 3) - everything else unchanged.
    assert set(rows["counts"]) == {"reply-waiting", "owed-inbound",
                                   "open-outbound", "closed",
                                   "closed-superseded"}
    digest = _json_of(["sync", "--for", "beta", "--json"], root, capsys)
    assert set(digest) == {"agent", "roster", "roles", "groups", "counts",
                           "threads", "unread_fyi"}  # rescinded/escalations ABSENT
    status = _json_of(["status", "--json"], root, capsys)
    assert set(status) == {"root", "session_id", "project_id",
                           "signing_enforced", "message_count",
                           "invalid_messages", "agents",
                           "stale_threshold_seconds", "warnings"}
    for a in status["agents"]:
        assert set(a) == {"name", "role", "cursor", "unread", "heartbeat",
                          "last_seen_seconds", "stale", "health", "waiting",
                          "waiting_stale"}  # operator_facing ABSENT
    who = _json_of(["whoami", "--for", "alpha", "--json"], root, capsys)
    assert set(who) == {"root", "self", "self_in_roster", "peer", "role",
                        "groups", "roster", "unread", "owed", "warnings"}
    # liaison keys ABSENT (not null) when the feature is unused


def test_backcompat_old_reader_paths_with_new_traffic(tmp_path: Path, capsys) -> None:
    """A rescind + an escalation in the store: every PRE-EXISTING read
    path keeps working - rescinds print as ordinary transcript content,
    unread counts include them, cursor advance passes them, unrelated
    threads are unaffected."""
    root = _init_team(tmp_path, "lead,w1")
    _run(["send", "--from", "lead", "--to", "w1", "--kind", "question",
          "--meta", "request_id=q-1", "-m", "fire", "--quiet"], root)
    _run(["send", "--from", "lead", "--to", "w1", "--kind", "question",
          "--meta", "request_id=q-other", "-m", "unrelated", "--quiet"], root)
    assert _run(["rescind", "--from", "lead", "--to-request", "q-1",
                 "-m", "hold", "--quiet"], root) == 0
    assert _run(["roster", "set-operator-facing", "lead"], root) == 0
    assert _run(["escalate", "--from", "w1", "-m", "decision?", "--quiet"], root) == 0
    s = Store(root)
    assert len(s.unread_for("w1")) == 3  # q-1, q-other, rescind
    capsys.readouterr()
    assert _run(["drain", "--for", "w1"], root) == 0
    out = capsys.readouterr().out
    assert "kind=rescind" in out          # ordinary transcript content
    assert len(s.unread_for("w1")) == 0   # cursor advanced past everything
    # the unrelated thread is untouched by the rescind
    rows = _json_of(["threads", "--for", "w1", "--all", "--json"], root, capsys)
    other = next(t for t in rows["threads"] if t["request_id"] == "q-other")
    assert other["state"] == "owed-inbound"
    assert "rescind" not in other


def test_backcompat_marker_and_config_tolerance(tmp_path: Path, capsys) -> None:
    """Hand-corrupted composing marker + garbage operator_facing values:
    every command behaves exactly as if they were absent."""
    root = _init_team(tmp_path, "alpha,beta")
    _run(["send", "--from", "alpha", "--to", "beta", "--kind", "question",
          "--meta", "request_id=q-1", "-m", "x", "--quiet"], root)
    (root / ".agenttalk" / "state" / "beta.composing.json").write_text(
        "{corrupt", encoding="utf-8")
    s = Store(root)
    for garbage in (None, 123, ["alpha"]):
        cfg = s.load_config()
        cfg["operator_facing"] = garbage
        s._write_config(cfg)
        for argv in (["status", "--json"], ["sync", "--for", "alpha", "--json"],
                     ["threads", "--for", "alpha", "--json"],
                     ["whoami", "--for", "alpha", "--json"]):
            capsys.readouterr()
            assert _run(argv, root) == 0
        who = _json_of(["whoami", "--for", "alpha", "--json"], root, capsys)
        assert "liaison" not in who  # garbage config == feature unused: absent
    # doctor flags garbage config at most informationally, never crashes
    from agenttalk import doctor as _doctor
    report = _doctor.run(root)
    assert report.overall in ("ok", "warn", "error")  # i.e. it RAN


# ======================================================================
# 0.15.0 e2e gates (WP04): role routing, NA lifecycle, fan-out
# accounting, prune byte-identity
# ======================================================================

def _ageless(payload: dict) -> dict:
    """Strip wall-clock-dependent fields for snapshot equality."""
    out = json.loads(json.dumps(payload))
    for row in out.get("threads", []):
        row.pop("age_seconds", None)
    return out


def _roles_team(tmp_path: Path) -> Path:
    root = _init_team(tmp_path, "lead,rev-a,rev-b,impl-c")
    for a, r in (("rev-a", "reviewer"), ("rev-b", "reviewer"),
                 ("impl-c", "implementer")):
        assert _run(["roster", "set-role", a, r], root) == 0
    return root


# ----------------------------- T014: role routing + freeze (SC 1)

def test_role_routing_and_post_send_freeze(tmp_path: Path, capsys) -> None:
    root = _roles_team(tmp_path)
    assert _run(["broadcast", "--from", "lead", "--to-role", "reviewer",
                 "--kind", "question", "--meta", "request_id=b-r1",
                 "-m", "fresh eyes?", "--quiet"], root) == 0
    # exactly the reviewers; the implementer sees NOTHING
    s = Store(root)
    assert sorted(m.recipient for m in s.valid_messages()
                  if m.meta.get("broadcast_id") == "b-r1") == ["rev-a", "rev-b"]
    impl = _json_of(["threads", "--for", "impl-c", "--all", "--json"], root, capsys)
    assert all(t["request_id"] != "b-r1" for t in impl["threads"])
    # frozen meta on every copy
    for m in s.valid_messages():
        if m.meta.get("broadcast_id") == "b-r1":
            assert m.meta["audience_kind"] == "role"
            assert m.meta["audience_role"] == "reviewer"
            assert m.meta["audience_resolved"] == "rev-a,rev-b"
            assert m.meta["batch_total"] == "2"
    # snapshot derivation, change roles AFTER send, snapshot again
    before = _json_of(["threads", "--for", "lead", "--all", "--json"], root, capsys)
    assert _run(["roster", "set-role", "rev-b", "implementer"], root) == 0
    assert _run(["roster", "set-role", "impl-c", "reviewer"], root) == 0
    after = _json_of(["threads", "--for", "lead", "--all", "--json"], root, capsys)
    assert _ageless(before) == _ageless(after)   # zero historical drift (SC 1)
    row = next(t for t in after["threads"] if t["request_id"] == "b-r1")
    assert sorted(row["pending"]) == ["rev-a", "rev-b"]
    # unknown role still refuses with the known set named
    capsys.readouterr()
    _run_expect_exit(["broadcast", "--from", "lead", "--to-role", "ghost",
                      "-m", "x", "--quiet"], root, 2)
    assert "reviewer" in capsys.readouterr().err


# --------------------------------- T015: NA lifecycle (SC 2)

def test_na_lifecycle_both_perspectives(tmp_path: Path, capsys) -> None:
    root = _roles_team(tmp_path)
    assert _run(["broadcast", "--from", "lead", "--to-role", "reviewer",
                 "--kind", "question", "--meta", "request_id=b-na",
                 "-m", "thoughts?", "--quiet"], root) == 0
    assert _run(["reply", "--from", "rev-b", "--to-request", "b-na", "--na",
                 "--quiet"], root) == 0
    # broadcaster: responded includes rev-b, marked n/a; rev-a pending
    rows = _json_of(["threads", "--for", "lead", "--json"], root, capsys)
    row = next(t for t in rows["threads"] if t["request_id"] == "b-na")
    assert row["responded_na"] == ["rev-b"]
    assert "rev-b" in row["responded"] and row["pending"] == ["rev-a"]
    # human view shows the marker
    capsys.readouterr()
    assert _run(["threads", "--for", "lead"], root) == 0
    assert "na=[rev-b]" in capsys.readouterr().out
    # the NA replier is closed; never mistaken for substantive
    member = _json_of(["threads", "--for", "rev-b", "--all", "--json"], root, capsys)
    mrow = next(t for t in member["threads"] if t["request_id"] == "b-na")
    assert mrow["state"] == "closed"
    # FR-006 e2e: NA on a review-request refuses with the typed hint
    assert _run(["send", "--from", "lead", "--to", "rev-a",
                 "--kind", "review-request", "--meta", "request_id=rq-na",
                 "-m", "review", "--quiet"], root) == 0
    capsys.readouterr()
    _run_expect_exit(["reply", "--from", "rev-a", "--to-request", "rq-na",
                      "--na", "--quiet"], root, 2)
    assert "review-result" in capsys.readouterr().err


# ----------------------- T016: partial fan-out accounting (SC 3)

def _fail_at_e2e(k: int):
    calls = {"n": 0}
    original = Store.send

    def wrapper(self, **kwargs):
        calls["n"] += 1
        if calls["n"] == k:
            raise OSError("disk full (injected)")
        return original(self, **kwargs)

    return wrapper


@pytest.mark.parametrize("k,delivered,missed", [
    (1, [], ["rev-a", "rev-b", "impl-c"]),       # first
    (2, ["rev-a"], ["rev-b", "impl-c"]),         # mid
    (3, ["rev-a", "rev-b"], ["impl-c"]),         # last
])
def test_partial_fanout_position_independent(tmp_path: Path, capsys,
                                             monkeypatch, k, delivered, missed) -> None:
    root = _init_team(tmp_path, "lead,rev-a,rev-b,impl-c")
    monkeypatch.setattr(Store, "send", _fail_at_e2e(k))
    capsys.readouterr()
    rc = _run(["broadcast", "--from", "lead", "--all", "--kind", "question",
               "--meta", "request_id=b-f", "-m", "x", "--quiet"], root)
    assert rc == 5
    out = capsys.readouterr().out
    assert f"delivered=[{', '.join(delivered)}]" in out
    assert f"missed=[{', '.join(missed)}]" in out
    monkeypatch.undo()
    s = Store(root)
    assert len([m for m in s.valid_messages()
                if m.meta.get("broadcast_id") == "b-f"]) == k - 1


def test_partial_fanout_warning_then_resume_then_clear(tmp_path: Path, capsys,
                                                       monkeypatch) -> None:
    root = _roles_team(tmp_path)
    monkeypatch.setattr(Store, "send", _fail_at_e2e(2))
    assert _run(["broadcast", "--from", "lead", "--to-role", "reviewer",
                 "--kind", "question", "--meta", "request_id=b-w",
                 "-m", "x", "--quiet"], root) == 5
    monkeypatch.undo()
    status = _json_of(["status", "--json"], root, capsys)
    hit = [w for w in status["warnings"] if "incomplete fan-out" in w]
    assert len(hit) == 1 and "rev-b" in hit[0] and "--resume" in hit[0]
    # follow the printed remediation
    assert _run(["broadcast", "--from", "lead", "--resume", "b-w",
                 "--quiet"], root) == 0
    status = _json_of(["status", "--json"], root, capsys)
    assert not [w for w in status["warnings"] if "incomplete fan-out" in w]
    # the recovered member owes
    rows = _json_of(["threads", "--for", "rev-b", "--json"], root, capsys)
    assert next(t for t in rows["threads"]
                if t["request_id"] == "b-w")["state"] == "owed-inbound"


# --------------------- T017: prune byte-identity + additivity (SC 4)

def test_prune_byte_identity_sweep(tmp_path: Path, capsys) -> None:
    root = _init_team(tmp_path, "alpha,beta")
    # valid traffic incl. an open thread
    _run(["send", "--from", "alpha", "--to", "beta", "--kind", "question",
          "--meta", "request_id=q-keep", "-m", "keep", "--quiet"], root)
    _run(["send", "--from", "beta", "--to", "alpha", "--kind", "message",
          "--meta", "request_id=q-keep", "-m", "answer", "--quiet"], root)
    # invalid debris
    mdir = root / ".agenttalk" / "messages"
    for i in range(5):
        (mdir / f"junk{i}.json").write_text("{not json", encoding="utf-8")
    threads_before = _json_of(["threads", "--for", "alpha", "--all", "--json"],
                              root, capsys)
    hashes_before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in mdir.glob("*.json") if not p.name.startswith("junk")}
    state_before = {p.name: p.read_bytes()
                    for p in (root / ".agenttalk" / "state").iterdir() if p.is_file()}
    assert _run(["prune", "--invalid", "--quiet"], root) == 0
    status = _json_of(["status", "--json"], root, capsys)
    assert status["invalid_messages"] == []
    assert status["quarantined"] == 5
    qdir = root / ".agenttalk" / "quarantine"
    assert sum(1 for p in qdir.iterdir() if p.is_file()) == 5
    hashes_after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in mdir.glob("*.json")}
    assert hashes_after == hashes_before        # valid files byte-identical
    state_after = {p.name: p.read_bytes()
                   for p in (root / ".agenttalk" / "state").iterdir() if p.is_file()}
    assert state_after == state_before          # cursors/threadstate untouched
    threads_after = _json_of(["threads", "--for", "alpha", "--all", "--json"],
                             root, capsys)
    assert _ageless(threads_after) == _ageless(threads_before)  # derivation identical


def test_additivity_gates_extended_0150(tmp_path: Path, capsys) -> None:
    # 0.14.0-style strict set-equality, extended with the 0.15.0 keys.
    root = _init_team(tmp_path, "alpha,beta")
    _run(["send", "--from", "alpha", "--to", "beta", "--kind", "question",
          "--meta", "request_id=q-old", "-m", "hello", "--quiet"], root)
    rows = _json_of(["threads", "--for", "alpha", "--all", "--json"], root, capsys)
    # open thread -> baseline + universal next_* hint; nothing else leaks.
    assert set(rows["threads"][0]) == _OPEN_THREAD_KEYS
    status = _json_of(["status", "--json"], root, capsys)
    assert "quarantined" not in status
    assert set(status) == {"root", "session_id", "project_id",
                           "signing_enforced", "message_count",
                           "invalid_messages", "agents",
                           "stale_threshold_seconds", "warnings"}


def test_partial_fanout_warning_suppressed_by_rescind_e2e(tmp_path: Path, capsys,
                                                          monkeypatch) -> None:
    # T016 alternate resolution: rescinding the bid suppresses the
    # incomplete-fan-out warning (the batch is void, not incomplete).
    root = _roles_team(tmp_path)
    monkeypatch.setattr(Store, "send", _fail_at_e2e(2))
    assert _run(["broadcast", "--from", "lead", "--to-role", "reviewer",
                 "--kind", "question", "--meta", "request_id=b-void",
                 "-m", "x", "--quiet"], root) == 5
    monkeypatch.undo()
    status = _json_of(["status", "--json"], root, capsys)
    assert [w for w in status["warnings"] if "incomplete fan-out" in w]
    assert _run(["rescind", "--from", "lead", "--to-request", "b-void",
                 "-m", "voiding the partial batch", "--quiet"], root) == 0
    status = _json_of(["status", "--json"], root, capsys)
    assert not [w for w in status["warnings"] if "incomplete fan-out" in w]
    # and the delivered member sees the thread superseded, not pending
    rows = _json_of(["threads", "--for", "rev-a", "--all", "--json"], root, capsys)
    assert next(t for t in rows["threads"]
                if t["request_id"] == "b-void")["state"] == "closed-superseded"


# ===================================================== #19 Phase A (WP03/T017)
# End-to-end: barrier -> epoch_at_send -> check --epoch; broadcast snapshot.

def _opener_epoch(root: Path, rid: str):
    """The epoch_at_send recorded on the opener for `rid` (sentinel-free:
    returns the value, or raises if the opener is missing)."""
    for m in Store(root).valid_messages():
        if m.meta.get("request_id") == rid and m.kind in ("review-request",
                                                           "question", "proposal"):
            return m.meta.get("epoch_at_send", "__absent__")
    raise AssertionError(f"no opener for {rid}")


def test_epoch_e2e_barrier_stamp_and_check(tmp_path: Path, capsys) -> None:
    root = _init_team(tmp_path, "alpha,beta")
    # no barrier: opener has epoch_at_send == None (three-state null)
    _run(["send", "--from", "alpha", "--to", "beta", "--kind", "review-request",
          "--meta", "request_id=r0", "-m", "x", "--quiet"], root)
    assert _opener_epoch(root, "r0") is None
    assert _run(["check", "--for", "beta", "--to-request", "r0", "--epoch"], root) == 0

    # fire a barrier; a NEW opener is stamped with the barrier id
    b1 = _json_of(["barrier", "bump", "--from", "alpha", "--scope", "global",
                   "-m", "e1", "--json"], root, capsys)["epoch"]
    _run(["send", "--from", "alpha", "--to", "beta", "--kind", "review-request",
          "--meta", "request_id=r1", "-m", "x", "--quiet"], root)
    assert _opener_epoch(root, "r1") == b1
    assert _run(["check", "--for", "beta", "--to-request", "r1", "--epoch"], root) == 0

    # r0 (null stamp) is now older than the barrier -> previous-epoch (exit 3)
    _run_expect_exit(["check", "--for", "beta", "--to-request", "r0", "--epoch"], root, 3)

    # a second barrier makes r1 previous-epoch too
    _run(["barrier", "bump", "--from", "beta", "--scope", "global", "-m", "e2"], root)
    _run_expect_exit(["check", "--for", "beta", "--to-request", "r1", "--epoch"], root, 3)


def test_broadcast_opener_shares_one_epoch_stamp(tmp_path: Path, capsys) -> None:
    # B3: every copy of one broadcast_id opener carries the SAME epoch_at_send,
    # snapshotted once before fan-out.
    root = _init_team(tmp_path, "alpha,beta,gamma")
    b = _json_of(["barrier", "bump", "--from", "alpha", "--scope", "global",
                  "-m", "e", "--json"], root, capsys)["epoch"]
    assert _run(["broadcast", "--from", "alpha", "--all", "--kind", "question",
                 "--meta", "request_id=bq", "-m", "all?", "--quiet"], root) == 0
    stamps = {m.recipient: m.meta.get("epoch_at_send")
              for m in Store(root).valid_messages()
              if m.meta.get("broadcast_id") == "bq" and m.sender == "alpha"}
    assert set(stamps) == {"beta", "gamma"}
    assert set(stamps.values()) == {b}          # one shared stamp


def test_no_barrier_check_epoch_is_current(tmp_path: Path, capsys) -> None:
    root = _init_team(tmp_path, "alpha,beta")
    _run(["send", "--from", "alpha", "--to", "beta", "--kind", "review-request",
          "--meta", "request_id=r", "-m", "x", "--quiet"], root)
    # with no barrier in history, --epoch reports current (exit 0)
    assert _run(["check", "--for", "beta", "--to-request", "r", "--epoch"], root) == 0
