"""Dead-letter / poison-message handling (the 0.30.0 restart-loop fix).

Drives the wrapper CONTINUOUS loop with a fixture Store + injected clock/sleep/now_iso
and a controllable drive (a bare bool or a typed loop.DriveOutcome). Covers the 20
acceptance-rubric cases: the durable attempt ledger (survives relaunch), the three-way
failure taxonomy (poison auto-DL at K_poison; infra never auto-DL + escalate at K_escalate;
ambiguous escalate+DL), the replay-safe disposition (move -> advance-last, idempotent), the
scan-invisible sink, control-never-counts, one-shot-unchanged, requeue, and reset lifecycle.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agenttalk import attention as att
from agenttalk import cli, supervisor as sup
from agenttalk.store import Store
from agenttalk.wrapper import loop, recv_api, run, session
from agenttalk.wrapper.loop import CLASS_AMBIGUOUS, CLASS_INFRA, CLASS_POISON, DriveOutcome


def _store(tmp_path: Path) -> Store:
    s = Store(tmp_path)
    s.init(["lead", "beta"])
    return s


def _send(s: Store, body: str, *, sender="lead", recipient="beta", kind="message",
          meta=None):
    return s.send(sender=sender, recipient=recipient, body=body, kind=kind, meta=meta or {})


def _rec(s: Store, agent="beta"):
    return recv_api.next_record(s, agent)


def _run(argv: list[str], root: Path) -> int:
    return cli.main(["--root", str(root), *argv])


def _runloop(s, agent="beta", *, drive, max_polls=12, k_poison=3, k_escalate=20,
             on_dead_letter=None, on_escalate=None, **kw):
    now_iso = kw.pop("now_iso", lambda: "t")
    return loop.run_loop(s, agent, drive, clock=lambda: 0.0, sleep=lambda d: None,
                         max_polls=max_polls, k_poison=k_poison, k_escalate=k_escalate,
                         on_dead_letter=on_dead_letter, on_escalate=on_escalate,
                         now_iso=now_iso, **kw)


def _always_false():
    calls = []

    def drive(rec):
        calls.append(rec["id"])
        return False                       # normalized to a poison_eligible failure

    drive.calls = calls
    return drive


def _always(outcome):
    calls = []

    def drive(rec):
        calls.append(rec["id"])
        return outcome

    drive.calls = calls
    return drive


# ---- codex stream helpers (for the resume-retry attempt-count test) ----

def _codex_turn_lines(thread_id="t-1", text="done"):
    return [json.dumps(o) for o in [
        {"type": "thread.started", "thread_id": thread_id},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
        {"type": "turn.completed"},
    ]]


def _failed_turn_lines(msg="no session"):
    return [json.dumps({"type": "turn.failed", "error": {"message": msg}})]


# ============================================================ the 20 rubric tests

def test_01_v0300_regression_dead_letters_after_k(tmp_path: Path) -> None:
    s = _store(tmp_path)
    p = _send(s, "poison")
    drive = _always_false()
    _runloop(s, drive=drive, k_poison=3)
    assert len(drive.calls) == 3                   # dead-lettered after EXACTLY 3 attempts
    assert s.dead_lettered_count("beta") == 1
    assert s.cursor("beta") == p.id                # cursor advanced once past the poison
    assert recv_api.next_record(s, "beta") is None  # poison never re-peeked


def test_02_durability_across_relaunch(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _send(s, "poison")
    # run 1: two failures, no dead-letter yet (bounded at 2 polls).
    _runloop(s, drive=_always_false(), k_poison=3, max_polls=2)
    assert s.dead_lettered_count("beta") == 0
    assert int(s.dead_letter_attempts("beta")["messages"][_rec(s)["id"]]
               ["poison_eligible_failures"]) == 2     # durable on disk, not RAM
    # run 2: a FRESH _run_continuous over the SAME store -> the 3rd attempt dead-letters.
    _runloop(s, drive=_always_false(), k_poison=3)
    assert s.dead_lettered_count("beta") == 1


def test_03_write_ahead_cap_disposes_without_drive(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _send(s, "poison")
    rec = _rec(s)
    for _ in range(3):                              # seed 3 prior poison failures
        s.record_attempt_start("beta", rec, attempt_id="a", at="t")
        s.record_attempt_result("beta", rec["id"], failure_class=CLASS_POISON,
                                summary="x", at="t")
    drive = _always_false()
    _runloop(s, drive=drive, k_poison=3)
    assert drive.calls == []                        # disposed WITHOUT calling drive()
    assert s.dead_lettered_count("beta") == 1


def test_03b_drive_sees_incremented_attempts_at_entry(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _send(s, "poison")
    seen = []

    def drive(rec):
        seen.append(int(s.attempt_record("beta", rec["id"])["attempts_started"]))
        return False

    _runloop(s, drive=drive, k_poison=99, max_polls=1)   # high cap -> no dispose
    assert seen == [1]                              # write-ahead incremented BEFORE drive


def test_04_dead_letter_is_progress(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _send(s, "poison")
    _runloop(s, drive=_always_false(), k_poison=3)
    assert s.read_heartbeat("beta") is not None     # DL stamped a FRESH heartbeat (progress)
    # the supervisor sees fresh progress -> HEALTHY_IDLE, never STUCK_RECOVER.
    report = {"agents": {"beta": {"protected": False, "heartbeat_stale": False,
                                  "heartbeat_age_seconds": 1.0, "waiting_pid_alive": False,
                                  "restart_request": None}}}
    state = {"agents": {"beta": {"readiness_seen": True, "resume_available": True,
                                 "launching": False, "backoff_next_epoch": 0}}}
    cfg = {"agents": {"beta": {"auto_restart": True, "cli": "codex"}}}
    plan = sup.plan_actions(report, state, cfg, now_epoch=1_000_000.0, snapshot=[])
    beta = plan["agents"]["beta"]
    assert beta["state"] == "HEALTHY_IDLE"      # POSITIVE: progress -> healthy, not stuck
    assert beta["action"] == sup.NONE           # no restart action taken


def test_05_head_of_line_unblocked(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _send(s, "poison")
    h = _send(s, "healthy")
    seen = []

    def drive(rec):
        seen.append(rec["body"])
        return rec["body"] != "poison"             # poison fails; healthy succeeds

    _runloop(s, drive=drive, k_poison=3)
    assert s.dead_lettered_count("beta") == 1       # poison gone
    assert "healthy" in seen                        # H driven after P dead-lettered
    assert s.cursor("beta") == h.id                 # committed past both


def test_06_transient_poison_below_cap_not_dead_lettered(tmp_path: Path) -> None:
    s = _store(tmp_path)
    m = _send(s, "flaky")
    outcomes = iter([DriveOutcome(ok=False, failure_class=CLASS_POISON, summary="x"),
                     DriveOutcome(ok=False, failure_class=CLASS_POISON, summary="x"),
                     DriveOutcome(ok=True)])

    def drive(rec):
        return next(outcomes)

    _runloop(s, drive=drive, k_poison=3, max_polls=5)
    assert s.dead_lettered_count("beta") == 0       # 2 fails (cap-1) then success -> no DL
    assert s.cursor("beta") == m.id                 # committed
    assert s.attempt_record("beta", m.id) is None   # attempt cleared on success


def test_07_known_global_infra_never_auto_dead_letter(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _send(s, "fine-but-api-down")
    escalations = []
    drive = _always(DriveOutcome(ok=False, failure_class=CLASS_INFRA, summary="api 5xx"))
    # on_escalate returns True (ROUTED) -> deduped to one notice (dedup is route-based).
    _runloop(s, drive=drive, k_poison=3, k_escalate=5,
             on_escalate=lambda info: escalations.append(info) or True, max_polls=12)
    assert s.dead_lettered_count("beta") == 0       # infra is NEVER auto-dead-lettered
    assert s.cursor("beta") == ""                   # cursor NOT advanced
    assert len(escalations) == 1                    # escalated once (deduped on ROUTED) at K_escalate
    assert len(drive.calls) >= 5                    # kept retrying (never frozen)


def test_08_ambiguous_auto_dead_letters_at_ceiling(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _send(s, "unknown-failure")
    escalations = []
    drive = _always(DriveOutcome(ok=False, failure_class=CLASS_AMBIGUOUS, summary="?"))
    _runloop(s, drive=drive, k_poison=99, k_escalate=4,
             on_escalate=lambda info: escalations.append(info), max_polls=12)
    assert s.dead_lettered_count("beta") == 1       # escalate AND dead-letter at ceiling
    assert len(escalations) == 1


def test_09_transient_across_ids_does_not_accumulate(tmp_path: Path) -> None:
    # Per-ID counting: one failure on each of three DISTINCT heads must NOT accumulate
    # toward the cap (the counter is keyed by msg_id). Each head fails once then succeeds,
    # so the loop advances through all three and none reaches K_poison=3.
    s = _store(tmp_path)
    _send(s, "A")
    _send(s, "B")
    _send(s, "C")
    seen: dict[str, int] = {}

    def drive(rec):
        seen[rec["id"]] = seen.get(rec["id"], 0) + 1
        return seen[rec["id"]] > 1                   # fail once per id, then succeed

    _runloop(s, drive=drive, k_poison=3, max_polls=20)
    assert s.dead_lettered_count("beta") == 0       # one failure each (per-id) -> none DL


def test_10_torn_counter_degrades_low(tmp_path: Path) -> None:
    s = _store(tmp_path)
    p = s._attempts_path("beta")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not valid json", encoding="utf-8")
    data = s.dead_letter_attempts("beta")           # NEVER raises, NEVER errs high
    assert data["messages"] == {}
    assert s.attempt_record("beta", "x") is None


def test_11_no_loss_on_move_and_collision_safe(tmp_path: Path) -> None:
    s = _store(tmp_path)
    p = _send(s, "poison-body")
    original = (s.messages_dir / f"{p.id}.json").read_bytes()
    rec = _rec(s)
    s.dead_letter("beta", rec, reason="x", failure_class=CLASS_POISON, at="t")
    assert (s.dead_letter_dir / "beta" / f"{p.id}.json").read_bytes() == original  # byte-identical
    assert not (s.messages_dir / f"{p.id}.json").exists()                          # source gone
    # a pre-existing sink file with the same name -> a timestamp-suffixed sibling (no overwrite)
    p2 = _send(s, "poison-2")
    orig2 = (s.messages_dir / f"{p2.id}.json").read_bytes()
    (s.dead_letter_dir / "beta" / f"{p2.id}.json").write_text("PRIOR", encoding="utf-8")
    rec2 = _rec(s)
    s.dead_letter("beta", rec2, reason="x", failure_class=CLASS_POISON, at="t")
    assert (s.dead_letter_dir / "beta" / f"{p2.id}.json").read_text() == "PRIOR"   # never overwritten
    sib = [x for x in (s.dead_letter_dir / "beta").glob(f"{p2.id}.*.json")          # suffixed sibling
           if not x.name.endswith(".deadletter.json")]                             # exclude its sidecar
    assert len(sib) == 1
    # lead C1: the collision sibling is named <mid>.<iso>.json so the endswith('.json') readers
    # SURFACE it (the old <mid>.json.<iso> was invisible -> unrecoverable). It is counted,
    # listed under its unique file STEM, requeueable by that stem, and audits the source id.
    stem = sib[0].name[:-len(".json")]
    listed = s.list_dead_letters("beta")
    assert s.dead_lettered_count("beta") == len(listed)                            # count agrees
    entry = next((m for m in listed if m["message_id"] == stem), None)
    assert entry is not None                                                       # surfaced
    assert entry.get("original_message_id") == p2.id                               # audit trail
    assert s.read_dead_letter_payload("beta", stem) == orig2                       # recoverable bytes


def test_12_atomic_ordering_idempotent_replay(tmp_path: Path, monkeypatch) -> None:
    s = _store(tmp_path)
    p = _send(s, "poison")
    rec = _rec(s)
    calls = {"n": 0}
    real_advance = s.advance_cursor

    def boom(agent, mid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("crash AFTER move, BEFORE advance")
        return real_advance(agent, mid)

    monkeypatch.setattr(s, "advance_cursor", boom)
    with pytest.raises(RuntimeError):
        s.dead_letter("beta", rec, reason="x", failure_class=CLASS_POISON, at="t")
    assert (s.dead_letter_dir / "beta" / f"{p.id}.json").exists()   # bytes in sink (recoverable)
    assert s.cursor("beta") != p.id                                 # cursor NOT advanced
    # re-run completes the advance (no-op move; idempotent).
    s.dead_letter("beta", rec, reason="x", failure_class=CLASS_POISON, at="t")
    assert s.cursor("beta") == p.id


def test_13_cursor_lands_exactly_on_poison_id(tmp_path: Path) -> None:
    s = _store(tmp_path)
    p = _send(s, "poison")
    m2 = _send(s, "m2")
    m3 = _send(s, "m3")

    def drive(rec):
        return rec["body"] != "poison"

    _runloop(s, drive=drive, k_poison=3)
    assert s.dead_lettered_count("beta") == 1
    assert s.cursor("beta") == m3.id                # ends past all; m2/m3 delivered in order
    assert m2.id < m3.id and p.id < m2.id           # ordering sanity


def test_14_cursor_via_live_head_not_sidecar(tmp_path: Path) -> None:
    s = _store(tmp_path)
    p = _send(s, "poison")
    sink = s.dead_letter_dir / "beta"
    sink.mkdir(parents=True, exist_ok=True)
    bogus = "99999999-999999-999999-zzzz"
    (sink / f"{bogus}.deadletter.json").write_text(
        json.dumps({"message_id": bogus}), encoding="utf-8")   # malformed/far-future sidecar
    s.dead_letter("beta", _rec(s), reason="x", failure_class=CLASS_POISON, at="t")
    assert s.cursor("beta") == p.id                 # advanced via the LIVE head id, not the sidecar


def test_15_sink_is_scan_invisible(tmp_path: Path) -> None:
    s = _store(tmp_path)
    p = _send(s, "poison")
    s.dead_letter("beta", _rec(s), reason="x", failure_class=CLASS_POISON, at="t")
    assert all(m.id != p.id for m in s.valid_messages())
    assert all(m.id != p.id for m in s.messages_for("beta"))
    assert all(ident != p.id for ident, _ in s.list_invalid_messages())
    assert s.dead_lettered_count() == 1


def test_16_control_never_counts(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    s.send(sender="lead", recipient="beta", kind="release", body="unmarked")  # -> invalid_control
    drive = _always_false()
    _runloop(s, drive=drive, k_poison=3, max_polls=4)
    assert drive.calls == []                         # control never driven
    assert s.dead_lettered_count("beta") == 0
    assert s.dead_letter_attempts("beta")["messages"] == {}   # no attempt recorded


def test_17_codex_resume_give_up_then_fresh_is_bounded_attempts(tmp_path: Path, monkeypatch) -> None:
    s = _store(tmp_path)
    _send(s, "hi")
    st = session.SessionState(cli="codex", codex_thread_id="t-old", resume_available=True)
    spawns = []

    def fake_spawn(argv, stdin):
        spawns.append(argv)
        return _failed_turn_lines() if "resume" in argv else _codex_turn_lines("t-new")

    drive = run.make_drive(s, "beta", "codex", st, ["codex"], spawn=fake_spawn,
                           clock=lambda: 0.0, render=False)
    counts = {"n": 0}
    real = s.record_attempt_start

    def counting(*a, **k):
        counts["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(s, "record_attempt_start", counting)
    loop.run_loop(s, "beta", drive, clock=lambda: 0.0, sleep=lambda d: None, max_turns=1)
    assert len(spawns) == 3                          # two resumes, then fresh
    assert spawns[:2] == [
        ["codex", "exec", "resume", "--json", "t-old"],
        ["codex", "exec", "resume", "--json", "t-old"],
    ]
    assert spawns[2] == ["codex", "exec", "--json"]
    assert counts["n"] == 3                          # bounded retries, no spawn-loop burst


def test_18_one_shot_unchanged_no_dead_letter(tmp_path: Path) -> None:
    # Uses a TYPED DriveOutcome(ok=False) - NOT a bare False - so it exercises the real
    # contract: a FAILED one-shot turn must take the failure branch (turns==0, not committed),
    # not be treated as success by a bare `if drive(record):` (DriveOutcome has no __bool__ ->
    # always truthy). A bare False is falsy and MASKED this regression (lead 4th-verify P1 #1).
    s = _store(tmp_path)
    _send(s, "task", meta={"request_id": "rq-1"})
    drive = _always(DriveOutcome(ok=False, failure_class=CLASS_POISON, summary="boom"))
    turns = loop.run_loop(s, "beta", drive, clock=lambda: 0.0,
                          sleep=lambda d: None, max_polls=10, only_request_id="rq-1")
    assert turns == 0                                # FAILED turn is NOT counted as success
    assert s.dead_lettered_count("beta") == 0        # one-shot is NEVER dead-lettered
    assert s.cursor("beta") == ""                     # one-shot never advances the global cursor
    # the scoped request was NOT committed as seen (a failed turn leaves it for retry/relaunch)
    assert recv_api.poll(s, "beta", scoped_request_id="rq-1").get("record") is not None


def test_19_requeue_redelivers_without_repoison(tmp_path: Path) -> None:
    s = _store(tmp_path)
    p = _send(s, "poison", meta={"request_id": "rq-9"})
    _runloop(s, drive=_always_false(), k_poison=3)
    assert s.dead_lettered_count("beta") == 1
    rc = cli.main(["--root", str(tmp_path), "dead-letter", "requeue",
                   "--agent", "beta", "--id", p.id])
    assert rc == 0
    nxt = recv_api.next_record(s, "beta")
    assert nxt is not None and nxt["id"] != p.id                 # a FRESH-id message
    assert nxt["request_id"] == "rq-9"                           # thread continuity preserved
    assert s.attempt_record("beta", nxt["id"]) is None           # own fresh attempt count (0)


def test_20_reset_clears_attempts_preserves_sink(tmp_path: Path) -> None:
    s = _store(tmp_path)
    p = _send(s, "poison")
    s.dead_letter("beta", _rec(s), reason="x", failure_class=CLASS_POISON, at="t")
    m2 = _send(s, "live")
    s.record_attempt_start("beta", _rec(s), attempt_id="a", at="t")   # a live attempt entry
    assert s._attempts_path("beta").exists()
    assert (s.dead_letter_dir / "beta" / f"{p.id}.json").exists()
    s.reset()
    assert not s._attempts_path("beta").exists()                      # state/ cleared by reset
    assert (s.dead_letter_dir / "beta" / f"{p.id}.json").exists()     # sink PRESERVED (like quarantine)
    assert m2 is not None


# ---- verify-workflow coverage additions (caps disable + crash reconcile) ----

def test_21_k_poison_zero_disables_poison_cap(tmp_path: Path) -> None:
    # k_poison=0 disables the poison auto-dead-letter cap (debug only): a poison message
    # is never auto-dead-lettered and the cursor never advances (the pre-0.40.x behavior).
    s = _store(tmp_path)
    _send(s, "poison")
    _runloop(s, drive=_always_false(), k_poison=0, k_escalate=0, max_polls=8)
    assert s.dead_lettered_count("beta") == 0
    assert s.cursor("beta") == ""


def test_22_k_escalate_zero_disables_backstop(tmp_path: Path) -> None:
    # k_escalate=0 disables the high-attempt backstop: an ambiguous failure is never
    # escalated and never dead-lettered by the ceiling.
    s = _store(tmp_path)
    _send(s, "ambiguous")
    esc = []
    _runloop(s, drive=_always(DriveOutcome(ok=False, failure_class=CLASS_AMBIGUOUS,
                                           summary="?")),
             k_poison=99, k_escalate=0, on_escalate=lambda i: esc.append(i), max_polls=30)
    assert s.dead_lettered_count("beta") == 0
    assert esc == []


def test_23_crash_mid_turn_is_ambiguous_disposes_at_escalate(tmp_path: Path) -> None:
    # codex ruling: crash_mid_turn (unobserved cause - could be a healthy-but-slow message
    # the supervisor stale-killed) is AMBIGUOUS, NOT poison@3. It is reconciled to ambiguous
    # and disposes only at the high K_escalate ceiling (escalate + last-resort DL), never
    # false-DL@3 a healthy-but-slow message.
    s = _store(tmp_path)
    _send(s, "slow")
    rec = _rec(s)
    # simulate 2 prior crash-mid-turns: attempts_started accrues; reconciled to ambiguous.
    for _ in range(2):
        s.record_attempt_start("beta", rec, attempt_id="a", at="t")
        s.reconcile_crash_in_progress("beta", rec["id"], at="t")
    after = s.attempt_record("beta", rec["id"])
    assert after["last_failure_class"] == CLASS_AMBIGUOUS       # NOT poison
    assert int(after["poison_eligible_failures"]) == 0          # crash never counts poison
    # at poison cap 3 it would NOT dispose (only 0 poison failures); the escalate cap does.
    esc = []
    drive = _always_false()
    # k_escalate=3: attempts_started already 2; reconcile of a 3rd started crash -> entry
    # escalate cap reached -> escalate + ambiguous dispose WITHOUT a fresh drive.
    s.record_attempt_start("beta", rec, attempt_id="a", at="t")   # 3rd started -> CRASH
    _runloop(s, drive=drive, k_poison=3, k_escalate=3,
             on_escalate=lambda i: esc.append(i) or True)
    assert drive.calls == []                                    # disposed via the escalate cap
    assert s.dead_lettered_count("beta") == 1
    assert len(esc) == 1                                        # escalated (ambiguous, not poison)


# ---- consolidated re-review folds (P1 terminal-infra + F1-F4 + reviewer-1 request_id) ----

def test_24_terminal_infra_not_dead_lettered_at_poison_cap(tmp_path: Path) -> None:
    # lead-verify P1 end-to-end: a terminal-result INFRA failure (DriveOutcome class INFRA,
    # as make_drive now produces for a 529/rate-limit terminal) is NEVER auto-dead-lettered,
    # even past K_poison - it retries under backoff (a sustained outage cannot drop a healthy
    # message at cap 3).
    s = _store(tmp_path)
    _send(s, "fine-but-overloaded")
    drive = _always(DriveOutcome(ok=False, failure_class=CLASS_INFRA, summary="529 overloaded"))
    _runloop(s, drive=drive, k_poison=3, k_escalate=99, max_polls=8)
    assert s.dead_lettered_count("beta") == 0
    assert s.cursor("beta") == ""                    # never advanced


def test_25_f1_orphan_payload_visible_and_count_agrees(tmp_path: Path) -> None:
    # F1: a payload with no sidecar (sidecar write interrupted) is still surfaced by
    # list_dead_letters, and dead_lettered_count agrees with len(list).
    s = _store(tmp_path)
    sink = s.dead_letter_dir / "beta"
    sink.mkdir(parents=True, exist_ok=True)
    (sink / "20260101-000000-000000-aaaa.json").write_text('{"from":"x"}', encoding="utf-8")
    items = s.list_dead_letters("beta")
    assert s.dead_lettered_count("beta") == 1 == len(items)
    assert items[0]["message_id"] == "20260101-000000-000000-aaaa"
    assert items[0].get("orphan_no_sidecar") is True


def test_26_f4_bad_value_ledger_degrades_low(tmp_path: Path) -> None:
    # F4: a hand-edited / forward-incompat ledger with a non-numeric counter must err LOW
    # (degrade), never crash the loop.
    s = _store(tmp_path)
    _send(s, "poison")
    rec = _rec(s)
    s.record_attempt_start("beta", rec, attempt_id="a", at="t")
    # corrupt the counter VALUE to a string (valid JSON, bad value)
    data = s.dead_letter_attempts("beta")
    data["messages"][rec["id"]]["poison_eligible_failures"] = "lots"
    data["messages"][rec["id"]]["attempts_started"] = None
    s._write_attempts("beta", data)
    # loop must not crash; the bad values read as 0 -> a fresh attempt is recorded + driven
    drive = _always_false()
    _runloop(s, drive=drive, k_poison=99, k_escalate=99, max_polls=1)
    assert len(drive.calls) == 1                      # drove (no crash, counters read low)


def test_27_reviewer1_notice_threads_as_operator_input(tmp_path: Path, capsys) -> None:
    # reviewer-1 blocker: a dead-letter notice must mint a request_id so the liaison's sync
    # surfaces it as OPERATOR INPUT NEEDED, not an unread FYI.
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    p = _send(s, "poison")
    notifier = cli._dead_letter_notifier(s, "beta")
    routed = notifier({"agent": "beta", "msg_id": p.id, "from": "lead", "kind": "message",
                       "attempts": 3, "failure_class": CLASS_POISON}, disposed=True)
    assert routed is True
    notice = s.messages_for("lead")[-1]
    assert notice.meta.get("needs_operator") == "true"
    assert notice.meta.get("request_id", "").startswith("esc-")   # threads as operator input
    capsys.readouterr()
    assert _run(["sync", "--for", "lead"], tmp_path) == 0
    assert "OPERATOR INPUT NEEDED" in capsys.readouterr().out


def test_28_notifier_unrouted_when_no_target(tmp_path: Path) -> None:
    # F2/codex-P2: no operator target -> notifier returns False (not routed).
    s = _store(tmp_path)               # no operator_facing, no sole lead (plain pair)
    p = _send(s, "poison")
    notifier = cli._dead_letter_notifier(s, "beta")
    assert notifier({"agent": "beta", "msg_id": p.id, "from": "lead", "kind": "message",
                     "attempts": 20, "failure_class": CLASS_INFRA}, disposed=False) is False


def test_config_blocked_notice_includes_command_error_and_remediation(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    p = _send(s, "needs reply")
    notifier = cli._dead_letter_notifier(s, "beta")
    routed = notifier(
        {
            "agent": "beta",
            "msg_id": p.id,
            "from": "lead",
            "kind": "message",
            "attempts": 1,
            "failure_class": loop.CLASS_CONFIG_BLOCKED,
            "summary": ("command=agenttalk reply --from beta --to-request rq-1; "
                        "error=Access is denied; remediation=use $env:AGENTTALK_PY -m agenttalk"),
        },
        disposed=False,
    )
    assert routed is True
    notice = s.messages_for("lead")[-1]
    assert notice.subject == "wrapper config-blocked"
    assert "agenttalk reply --from beta" in notice.body
    assert "Access is denied" in notice.body
    assert "$env:AGENTTALK_PY -m agenttalk" in notice.body
    assert "dead-lettered" not in notice.body.lower()


def test_29_f2_doctor_loud_on_unrouted_escalation(tmp_path: Path) -> None:
    # F2/codex-P2: an escalated-but-UNROUTED backstop record makes doctor go LOUD ERROR -
    # a known-infra message can't silently loop with no operator signal.
    s = _store(tmp_path)               # no liaison/lead -> no escalation target
    _send(s, "infra")
    rec = _rec(s)
    s.record_attempt_start("beta", rec, attempt_id="a", at="t")
    s.mark_attempt_escalated("beta", rec["id"], routed=False)
    assert any(u["message_id"] == rec["id"] for u in s.list_unrouted_escalations())
    from agenttalk import doctor
    chk = doctor._check_dead_letter_escalations(s)
    assert chk is not None and chk.status == "error"


def test_30_f3_supervisor_config_caps_take_effect(tmp_path: Path) -> None:
    # F3/codex-P1: resolve_dead_letter_caps reads a per-agent override (then global, then
    # default), so supervisor.json caps actually drive the wrapped loop.
    from agenttalk import supervisor as sup
    assert sup.resolve_dead_letter_caps({}, {}) == (3, 20)                       # defaults
    assert sup.resolve_dead_letter_caps({"dead_letter": {"max_attempts": 7}}, {}) == (7, 20)
    assert sup.resolve_dead_letter_caps(
        {"dead_letter": {"max_attempts": 7}},
        {"dead_letter": {"max_attempts": 2, "escalate_after_attempts": 9}}) == (2, 9)  # per-agent wins
    assert sup.resolve_dead_letter_caps({"dead_letter": {"max_attempts": 0}}, {}) == (0, 20)  # 0 disables


def test_31_unrouted_escalation_retries_after_target_configured(tmp_path: Path) -> None:
    # codex re-review P2: an UNROUTED escalation (no liaison/lead) must NOT be permanently
    # deduped - once an operator target is configured, the loop retries and the notice
    # routes. Dedup is keyed on escalation_routed, not merely "attempted".
    s = _store(tmp_path)                              # no operator_facing, no sole lead
    m = _send(s, "infra-down")
    notifier = cli._dead_letter_notifier(s, "beta")
    drive = _always(DriveOutcome(ok=False, failure_class=CLASS_INFRA, summary="529 overloaded"))
    _runloop(s, drive=drive, k_poison=99, k_escalate=2,
             on_escalate=lambda i: notifier(i, disposed=False), max_polls=6)
    rec = s.attempt_record("beta", m.id)
    assert rec["escalated"] is True and rec["escalation_routed"] is False   # tried, unrouted
    assert s.messages_for("lead") == []              # nothing routed yet
    # operator configures a liaison -> the next loop retries the notice and it ROUTES.
    s.set_operator_facing("lead")
    _runloop(s, drive=drive, k_poison=99, k_escalate=2,
             on_escalate=lambda i: notifier(i, disposed=False), max_polls=3)
    assert s.attempt_record("beta", m.id)["escalation_routed"] is True
    assert any(msg.meta.get("dead_letter") == "true" for msg in s.messages_for("lead"))


def test_b1_dead_letter_notice_replies_do_not_release_latch(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    m = _send(s, "infra")
    notifier = cli._dead_letter_notifier(s, "beta")
    info = {
        "agent": "beta",
        "msg_id": m.id,
        "from": "lead",
        "kind": "message",
        "attempts": 20,
        "attempts_bucket": "escalate_backstop",
        "first_started_at": "2026-07-05T00:00:00Z",
        "failure_class": CLASS_INFRA,
    }

    assert notifier(info, disposed=False) is True
    first = s.messages_for("lead")[-1]
    for n in range(3):
        s.send(sender="lead", recipient="beta", body=f"ack {n}", kind="message",
               meta={"request_id": first.meta["request_id"]})
        assert notifier(info, disposed=False) is True

    notices = [msg for msg in s.messages_for("lead")
               if msg.subject == "dead-letter notice"]
    assert len(notices) == 1


def test_b1_dead_letter_worsening_state_emits_new_notice(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    m = _send(s, "mixed")
    notifier = cli._dead_letter_notifier(s, "beta")

    assert notifier({
        "agent": "beta", "msg_id": m.id, "from": "lead", "kind": "message",
        "attempts": 20, "attempts_bucket": "escalate_backstop",
        "first_started_at": "2026-07-05T00:00:00Z",
        "failure_class": CLASS_INFRA,
    }, disposed=False) is True
    assert notifier({
        "agent": "beta", "msg_id": m.id, "from": "lead", "kind": "message",
        "attempts": 21, "attempts_bucket": "quarantined",
        "first_started_at": "2026-07-05T00:00:00Z",
        "failure_class": CLASS_AMBIGUOUS,
        "quarantined": True,
    }, disposed=True) is True

    notices = [msg for msg in s.messages_for("lead")
               if msg.subject == "dead-letter notice"]
    assert len(notices) == 2


def test_b1_corrupt_notice_log_fails_open_once(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    m = _send(s, "poison")
    p = att.attention_dir(s) / "notices.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{torn\n", encoding="utf-8")
    notifier = cli._dead_letter_notifier(s, "beta")
    info = {
        "agent": "beta", "msg_id": m.id, "from": "lead", "kind": "message",
        "attempts": 3, "attempts_bucket": "quarantined",
        "first_started_at": "2026-07-05T00:00:00Z",
        "failure_class": CLASS_POISON,
        "quarantined": True,
    }

    assert notifier(info, disposed=True) is True
    assert notifier(info, disposed=True) is True

    notices = [msg for msg in s.messages_for("lead")
               if msg.subject == "dead-letter notice"]
    assert len(notices) == 1


def test_b1_clean_notice_then_corrupt_log_stays_latched(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    m = _send(s, "poison")
    notifier = cli._dead_letter_notifier(s, "beta")
    info = {
        "agent": "beta", "msg_id": m.id, "from": "lead", "kind": "message",
        "attempts": 3, "attempts_bucket": "quarantined",
        "first_started_at": "2026-07-05T00:00:00Z",
        "failure_class": CLASS_POISON,
        "quarantined": True,
    }

    assert notifier(info, disposed=True) is True
    p = att.attention_dir(s) / "notices.jsonl"
    p.write_text(p.read_text(encoding="utf-8") + "{torn\n", encoding="utf-8")
    for _ in range(20):
        assert notifier(info, disposed=True) is True

    notices = [msg for msg in s.messages_for("lead")
               if msg.subject == "dead-letter notice"]
    assert len(notices) == 1


def test_b1_resolve_requeue_refail_fresh_id_emits_new_notice(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    original = _send(s, "poison", meta={"request_id": "rq-retry"})
    rec = _rec(s)
    s.dead_letter("beta", rec, reason="poison", failure_class=CLASS_POISON, at="t")
    notifier = cli._dead_letter_notifier(s, "beta")
    first_info = {
        "agent": "beta", "msg_id": original.id, "from": "lead", "kind": "message",
        "attempts": 3, "attempts_bucket": "quarantined",
        "first_started_at": "2026-07-05T00:00:00Z",
        "failure_class": CLASS_POISON,
        "quarantined": True,
    }

    assert notifier(first_info, disposed=True) is True
    assert _run(["dead-letter", "resolve", "--agent", "beta", "--id", original.id,
                 "--reason", "handled", "--from", "lead"], tmp_path) == 0
    assert _run(["dead-letter", "requeue", "--agent", "beta", "--id", original.id,
                 "--force-resolved", "--reason", "retry", "--from", "lead"],
                tmp_path) == 0
    requeued = _rec(s)
    assert requeued is not None and requeued["id"] != original.id
    s.dead_letter("beta", requeued, reason="poison", failure_class=CLASS_POISON, at="t")
    second_info = dict(first_info, msg_id=requeued["id"])

    assert notifier(second_info, disposed=True) is True

    notices = [msg for msg in s.messages_for("lead")
               if msg.subject == "dead-letter notice"]
    assert len(notices) == 2
    assert {msg.meta.get("dl_msg_id") for msg in notices} == {original.id, requeued["id"]}


def test_b2_infra_raw_attempt_churn_before_elapsed_floor_does_not_quarantine(tmp_path: Path) -> None:
    s = _store(tmp_path)
    m = _send(s, "infra")
    drive = _always(DriveOutcome(ok=False, failure_class=CLASS_INFRA, summary="529"))
    _runloop(
        s,
        drive=drive,
        k_poison=99,
        k_escalate=2,
        infra_exhaust_after_seconds=3600,
        infra_exhaust_min_attempts=3,
        on_escalate=lambda i: True,
        now_iso=lambda: "2026-07-05T00:01:00Z",
        max_polls=6,
    )
    assert s.dead_lettered_count("beta") == 0
    assert s.cursor("beta") != m.id
    assert len(drive.calls) >= 3


def test_b2_infra_elapsed_and_min_attempts_quarantines_once_with_notice(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.set_operator_facing("lead")
    m = _send(s, "infra")
    rec = _rec(s)
    s.record_attempt_start("beta", rec, attempt_id="a", at="2026-07-05T00:00:00Z")
    data = s.dead_letter_attempts("beta")
    data["messages"][m.id].update({
        "attempts_started": 3,
        "infra_failures": 3,
        "last_failure_class": CLASS_INFRA,
        "last_failure_summary": "529",
        "in_progress": False,
    })
    s._write_attempts("beta", data)
    notifier = cli._dead_letter_notifier(s, "beta")
    drive = _always(DriveOutcome(ok=False, failure_class=CLASS_INFRA, summary="529"))

    _runloop(
        s,
        drive=drive,
        k_poison=99,
        k_escalate=2,
        infra_exhaust_after_seconds=60,
        infra_exhaust_min_attempts=3,
        on_dead_letter=lambda i: notifier(i, disposed=True),
        now_iso=lambda: "2026-07-05T00:02:00Z",
        max_polls=2,
    )

    assert drive.calls == []
    assert s.dead_lettered_count("beta") == 1
    item = s.list_dead_letters("beta")[0]
    assert item["class"] == loop.CLASS_INFRA_RETRY_EXHAUSTED
    notices = [msg for msg in s.messages_for("lead")
               if msg.subject == "dead-letter notice"]
    assert len(notices) == 1
    assert "Requeue:" in notices[0].body


def test_b2_infra_exhaustion_zero_negative_config_falls_back() -> None:
    caps = sup.resolve_infra_retry_exhaustion(
        {"dead_letter": {
            "infra_exhaust_after_seconds": 0,
            "infra_exhaust_min_attempts": -1,
            "noninfra_sub_ceiling": 0,
        }},
        {},
        k_escalate=2,
    )
    assert caps == {
        "infra_exhaust_after_seconds": 14400.0,
        "infra_exhaust_min_attempts": 100,
        "noninfra_sub_ceiling": 4,
    }


def test_b2_mixed_poison_infra_hits_noninfra_sub_ceiling(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _send(s, "mixed")
    drive = _cycle_outcomes([CLASS_INFRA, CLASS_POISON, CLASS_INFRA, CLASS_AMBIGUOUS])
    _runloop(
        s,
        drive=drive,
        k_poison=99,
        k_escalate=99,
        noninfra_sub_ceiling=2,
        max_polls=5,
    )
    assert s.dead_lettered_count("beta") == 1
    assert s.list_dead_letters("beta")[0]["class"] == CLASS_AMBIGUOUS


def test_32_corrupt_ledger_value_at_cap_disposes_without_crash(tmp_path: Path) -> None:
    # reviewer-1 major: a corrupt counter VALUE at the cap must dispose + NOTIFY without
    # crashing - _info (the notice builder) must degrade-LOW too, not just the cap checks.
    s = _store(tmp_path)
    m = _send(s, "poison")
    rec = _rec(s)
    s.record_attempt_start("beta", rec, attempt_id="a", at="t")
    data = s.dead_letter_attempts("beta")
    data["messages"][m.id]["attempts_started"] = "NaN"        # corrupt VALUE
    data["messages"][m.id]["poison_eligible_failures"] = 3    # at cap
    data["messages"][m.id]["in_progress"] = False             # realistic post-failed-drive state
    s._write_attempts("beta", data)
    notices = []
    drive = _always_false()
    _runloop(s, drive=drive, k_poison=3,
             on_dead_letter=lambda i: notices.append(i) or True, max_polls=3)
    assert drive.calls == []                     # cap reached (via _safe_int) -> no drive
    assert s.dead_lettered_count("beta") == 1
    assert len(notices) == 1 and notices[0]["attempts"] == 0   # _info degraded NaN -> 0, no crash


def test_33_c3_none_attempts_at_cap_disposes_without_crash(tmp_path: Path) -> None:
    # lead C3: the _info notice builder must _safe_int attempts_started even when it is None
    # (not just non-numeric strings). A dispose driven on a None-attempts ledger at the cap
    # must NOT raise out of run_loop (err-LOW-never-raise invariant).
    s = _store(tmp_path)
    m = _send(s, "poison")
    rec = _rec(s)
    s.record_attempt_start("beta", rec, attempt_id="a", at="t")
    data = s.dead_letter_attempts("beta")
    data["messages"][m.id]["attempts_started"] = None         # non-coercible (TypeError)
    data["messages"][m.id]["poison_eligible_failures"] = 3    # at cap -> dispose path
    data["messages"][m.id]["in_progress"] = False             # realistic post-failed-drive state
    s._write_attempts("beta", data)
    notices = []
    _runloop(s, drive=_always_false(), k_poison=3,
             on_dead_letter=lambda i: notices.append(i) or True, max_polls=3)
    assert s.dead_lettered_count("beta") == 1
    assert len(notices) == 1 and notices[0]["attempts"] == 0   # None degraded to 0, no crash


def test_34_c4_proxy_block_class_infra_not_dead_lettered(tmp_path: Path) -> None:
    # lead C4: a gateway/WAF/reverse-proxy block ("Request blocked by upstream proxy") is an
    # OUTAGE signature -> CLASS_INFRA, so the loop NEVER dead-letters it at the poison cap and,
    # at the escalate ceiling, it escalates but keeps retrying (0 dead-letters during the outage).
    s = _store(tmp_path)
    _send(s, "healthy-but-edge-blocked")
    escalations = []
    drive = _always(DriveOutcome(ok=False, failure_class=CLASS_INFRA,
                                 summary="Request blocked by upstream proxy"))
    _runloop(s, drive=drive, k_poison=3, k_escalate=3,
             on_escalate=lambda i: escalations.append(i) or True, max_polls=8)
    assert s.dead_lettered_count("beta") == 0           # 3x (and beyond) -> still 0 DL
    assert s.cursor("beta") == ""                        # never advanced past a healthy message
    assert len(escalations) >= 1                         # escalated at the ceiling


def test_35_c5_solo_wrapped_lead_doctor_loud_not_silent(tmp_path: Path) -> None:
    # lead C5 (no silent disposal): a SOLO wrapped lead is its OWN only escalation target, so
    # the dead-letter notice cannot route (an agent can't escalate to itself). doctor must go
    # LOUD ERROR for that case - mirror the notifier's reachability - not a benign WARN.
    s = _store(tmp_path)
    s.set_role("beta", "lead")                           # beta is the sole lead == disposing agent
    assert s.operator_facing() is None and s.sole_lead() == "beta"
    _send(s, "poison")
    rec = _rec(s)
    s.dead_letter("beta", rec, reason="x", failure_class=CLASS_POISON, at="t")
    from agenttalk import doctor
    chk = doctor._check_dead_letter(s)
    assert chk is not None and chk.status == "error"     # NOT a silent WARN
    assert "beta" in chk.details
    # a DIFFERENT liaison (not the disposing agent) makes it routable -> recoverable WARN.
    s.set_operator_facing("lead")                        # target='lead' != disposing 'beta'
    assert doctor._check_dead_letter(s).status == "warn"


def test_36_verify_doctor_loud_when_list_unreadable(tmp_path: Path, monkeypatch) -> None:
    # verify C5-P1: dead-letters exist (count>0) but list_dead_letters FAILS (items=[]) ->
    # routing is unverifiable -> doctor must go LOUD ERROR even though a target resolves, not
    # infer a benign WARN (a silent disposal we just cannot see is still a silent disposal).
    s = _store(tmp_path)
    s.set_operator_facing("lead")                        # a routable target DOES resolve
    sink = s.dead_letter_dir / "beta"
    sink.mkdir(parents=True, exist_ok=True)
    (sink / "20260101-000000-000000-aaaa.json").write_text('{"from":"x"}', encoding="utf-8")
    assert s.dead_lettered_count("beta") == 1

    def _boom(*a, **k):
        raise OSError("list read failed")

    monkeypatch.setattr(s, "list_dead_letters", _boom)
    from agenttalk import doctor
    chk = doctor._check_dead_letter(s)
    assert chk is not None and chk.status == "error"
    assert "unverifiable" in chk.details


def test_37_verify_corrupt_sidecar_flagged_distinct_from_missing(tmp_path: Path) -> None:
    # verify C1-P2: a sidecar that EXISTS but is unreadable (corrupt JSON) loses metadata just
    # like a missing one - list must flag orphan_no_sidecar AND sidecar_unreadable so the
    # operator is not misled into thinking the metadata was simply absent.
    s = _store(tmp_path)
    sink = s.dead_letter_dir / "beta"
    sink.mkdir(parents=True, exist_ok=True)
    mid = "20260101-000000-000000-bbbb"
    (sink / f"{mid}.json").write_text('{"body":"x"}', encoding="utf-8")
    (sink / f"{mid}.deadletter.json").write_text("NOT VALID JSON", encoding="utf-8")
    items = s.list_dead_letters("beta")
    assert len(items) == 1 and items[0]["message_id"] == mid
    assert items[0].get("orphan_no_sidecar") is True
    assert items[0].get("sidecar_unreadable") is True


def test_38_verify_raising_dead_letter_callback_does_not_crash_loop(tmp_path: Path) -> None:
    # verify C3/fail-closed: a notice callback that RAISES must not crash the loop after a
    # successful disposal - the message is still dead-lettered (progress stamped) and doctor is
    # the backstop. The production notifier is self-guarding; this hardens arbitrary callbacks.
    s = _store(tmp_path)
    _send(s, "poison")

    def _boom(info):
        raise RuntimeError("notifier blew up")

    drive = _always_false()
    _runloop(s, drive=drive, k_poison=3, on_dead_letter=_boom, max_polls=6)   # must not raise
    assert s.dead_lettered_count("beta") == 1          # disposed despite the raising callback
    assert len(drive.calls) == 3


def test_39_verify_raising_escalate_callback_marks_unrouted_not_crash(tmp_path: Path) -> None:
    # verify C3/fail-closed: a raising on_escalate is treated as UNROUTED (routed=False), never
    # a crash - the ledger records escalated + unrouted so the notice retries and doctor goes
    # LOUD; a known-infra message is NEVER disposed (keeps retrying through the outage).
    s = _store(tmp_path)
    m = _send(s, "infra")

    def _boom(info):
        raise RuntimeError("escalate blew up")

    drive = _always(DriveOutcome(ok=False, failure_class=CLASS_INFRA, summary="529"))
    _runloop(s, drive=drive, k_poison=99, k_escalate=2, on_escalate=_boom, max_polls=6)
    rec = s.attempt_record("beta", m.id)
    assert rec["escalated"] is True and rec["escalation_routed"] is False
    assert s.dead_lettered_count("beta") == 0          # infra never disposed


def _claude_ok_lines():
    return [json.dumps({"type": "stream_event", "event": {"type": "message_start"}}),
            json.dumps({"type": "stream_event", "event": {"type": "message_stop"}})]


def _claude_fail_lines(msg="session is full"):
    return [json.dumps({"type": "result", "is_error": True, "result": msg})]


def test_40_claude_resume_give_up_two_healthy_messages_zero_dead_letters(tmp_path: Path) -> None:
    # B4 loop level: claude --resume fails twice, then the fresh session succeeds. Healthy
    # queued messages commit with 0 dead-letters, but there is no same-poll spawn burst.
    s = _store(tmp_path)
    _send(s, "healthy-1")
    _send(s, "healthy-2")
    calls = []

    def spawn(argv, stdin):
        calls.append(list(argv))
        if "--resume" in argv:
            return _claude_fail_lines("prompt is too long")     # stale, full session
        return _claude_ok_lines()                                # fresh session succeeds

    state = session.SessionState(cli="claude", claude_session_id="sess-1",
                                 turns=1, resume_available=True)
    drive = run.make_drive(s, "beta", "claude", state, ["claude"], spawn=spawn,
                           clock=lambda: 0.0, render=False)
    _runloop(s, drive=drive, k_poison=3, max_polls=6)
    assert s.dead_lettered_count("beta") == 0            # both healthy -> NEVER dead-lettered
    assert recv_api.next_record(s, "beta") is None       # both committed (cursor past both)
    # each message: two failed --resume attempts, then a successful fresh --session-id.
    assert sum(1 for c in calls if "--resume" in c) == 4
    assert sum(1 for c in calls if "--session-id" in c) == 2


def _cycle_outcomes(classes):
    """A drive that returns DriveOutcome(ok=False, failure_class=classes[i]) per call,
    clamping to the last class if over-driven (keeps the loop bounded-safe)."""
    calls = []
    seq = list(classes)

    def drive(rec):
        c = seq[len(calls)] if len(calls) < len(seq) else seq[-1]
        calls.append(rec["id"])
        return DriveOutcome(ok=False, failure_class=c, summary=c)

    drive.calls = calls
    return drive


def test_41_poison_counter_consecutive_interleaved_not_dead_lettered(tmp_path: Path) -> None:
    # lead 5th-verify P2: poison_eligible_failures is CONSECUTIVE (reset on any non-poison
    # outcome), not a cumulative lifetime count. An INTERLEAVED outage [poison, infra, poison,
    # infra, poison] for the SAME id must NOT dead-letter at K_poison=3 - each infra resets the
    # poison run, so the count never reaches 3 CONSECUTIVE.
    s = _store(tmp_path)
    m = _send(s, "edge-flapping")
    drive = _cycle_outcomes([CLASS_POISON, CLASS_INFRA, CLASS_POISON, CLASS_INFRA, CLASS_POISON])
    _runloop(s, drive=drive, k_poison=3, k_escalate=99, max_polls=5)
    assert s.dead_lettered_count("beta") == 0            # interleaved never reaches 3 CONSECUTIVE
    rec = s.attempt_record("beta", m.id)
    assert int(rec["poison_eligible_failures"]) == 1     # reset by each infra; last poison -> 1
    assert int(rec["infra_failures"]) == 2               # the two infra outcomes counted


def test_42_poison_counter_consecutive_run_dead_letters_at_k(tmp_path: Path) -> None:
    # the other half: K CONSECUTIVE poison classifications DO auto-DL@K_poison.
    s = _store(tmp_path)
    p = _send(s, "deterministic-poison")
    drive = _cycle_outcomes([CLASS_POISON, CLASS_POISON, CLASS_POISON])
    _runloop(s, drive=drive, k_poison=3, k_escalate=99, max_polls=5)
    assert s.dead_lettered_count("beta") == 1            # 3 consecutive poison -> disposed
    assert s.cursor("beta") == p.id
    assert len(drive.calls) == 3                          # exactly K drives, then dispose


def test_43_infra_dominant_ledger_not_disposed_at_ceiling(tmp_path: Path) -> None:
    # codex ruling: at K_escalate, a DOMINANTLY-INFRA ledger (infra > poison + ambiguous) must
    # escalate + keep RETRYING, never take the ambiguous last-resort disposal - so a healthy
    # message that fails (incl. a stale-kill -> ambiguous) during a sustained OUTAGE is never
    # dead-lettered even past the ceiling. infra-never-DL holds for a dominantly-infra history.
    s = _store(tmp_path)
    m = _send(s, "healthy-during-outage")
    escalations = []
    drive = _cycle_outcomes([CLASS_INFRA, CLASS_INFRA, CLASS_AMBIGUOUS, CLASS_INFRA,
                             CLASS_INFRA, CLASS_AMBIGUOUS])
    _runloop(s, drive=drive, k_poison=99, k_escalate=3,
             on_escalate=lambda i: escalations.append(i) or True, max_polls=6)
    assert s.dead_lettered_count("beta") == 0            # infra-dominant -> NOT disposed at ceiling
    assert len(escalations) >= 1                          # but DID escalate (operator-visible)
    rec = s.attempt_record("beta", m.id)
    assert (int(rec["infra_failures"])
            > int(rec["poison_eligible_failures"]) + int(rec["ambiguous_failures"]))


def test_44_ambiguous_dominant_ledger_disposes_at_ceiling(tmp_path: Path) -> None:
    # contrast: when NOT infra-dominant (ambiguous dominates), the K_escalate ceiling DOES
    # last-resort dispose (the misclassified-poison escape hatch). The guard is SPECIFIC to a
    # dominantly-infra history, not a blanket no-dispose.
    s = _store(tmp_path)
    _send(s, "ambiguous-stuck")
    drive = _cycle_outcomes([CLASS_AMBIGUOUS, CLASS_AMBIGUOUS, CLASS_AMBIGUOUS])
    _runloop(s, drive=drive, k_poison=99, k_escalate=3, max_polls=6)
    assert s.dead_lettered_count("beta") == 1            # ambiguous-dominant -> disposed at ceiling


def test_45_poison_run_broken_by_infra_not_dead_lettered(tmp_path: Path) -> None:
    # codex expected test: POISON, INFRA, POISON, POISON does NOT dead-letter - the infra breaks
    # the consecutive poison run, so the trailing poisons are only 2 CONSECUTIVE (< K_poison=3).
    s = _store(tmp_path)
    m = _send(s, "flapping")
    drive = _cycle_outcomes([CLASS_POISON, CLASS_INFRA, CLASS_POISON, CLASS_POISON])
    _runloop(s, drive=drive, k_poison=3, k_escalate=99, max_polls=4)
    assert s.dead_lettered_count("beta") == 0            # infra broke the run -> only 2 trailing
    assert int(s.attempt_record("beta", m.id)["poison_eligible_failures"]) == 2


def test_46_crash_mid_turn_resets_poison_counter(tmp_path: Path) -> None:
    # codex expected test: a crash_mid_turn (reconcile) is a non-poison outcome -> it RESETS the
    # consecutive poison run to 0, so a poison run interrupted by a crash never accumulates to K.
    s = _store(tmp_path)
    m = _send(s, "poison-then-crash")
    rec = _rec(s)
    s.record_attempt_start("beta", rec, attempt_id="a", at="t")
    data = s.dead_letter_attempts("beta")
    data["messages"][m.id]["poison_eligible_failures"] = 2   # mid poison run
    data["messages"][m.id]["in_progress"] = True             # crashed mid-turn
    s._write_attempts("beta", data)
    assert s.reconcile_crash_in_progress("beta", m.id, at="t2") is True
    rec2 = s.attempt_record("beta", m.id)
    assert int(rec2["poison_eligible_failures"]) == 0        # crash reset the consecutive run
    assert rec2["last_failure_class"] == "ambiguous_or_unknown"


def test_47_infra_dominant_crash_at_ceiling_escalates_no_dispose(tmp_path: Path) -> None:
    # codex expected test: ~19 INFRA + one reconciled crash at K_escalate escalates but does NOT
    # dead-letter or advance the cursor - a healthy message stale-killed during a sustained OUTAGE
    # (its crash flips last_class to ambiguous) is protected by the infra-dominance predicate.
    s = _store(tmp_path)
    _send(s, "healthy-stale-killed-in-outage")
    rec = _rec(s)
    s.record_attempt_start("beta", rec, attempt_id="a", at="t")
    data = s.dead_letter_attempts("beta")
    data["messages"][rec["id"]]["infra_failures"] = 4        # dominantly-infra history
    data["messages"][rec["id"]]["attempts_started"] = 4      # at the K_escalate ceiling
    data["messages"][rec["id"]]["in_progress"] = True        # stale-killed mid-drive (crash)
    s._write_attempts("beta", data)
    escalations = []
    drive = _always(DriveOutcome(ok=False, failure_class=CLASS_INFRA, summary="529 outage"))
    _runloop(s, drive=drive, k_poison=99, k_escalate=4,
             on_escalate=lambda i: escalations.append(i) or True, max_polls=3)
    assert s.dead_lettered_count("beta") == 0            # infra-dominant + crash -> NOT disposed
    assert s.cursor("beta") == ""                         # cursor NOT advanced
    assert len(escalations) >= 1                           # but escalated (operator-visible)
