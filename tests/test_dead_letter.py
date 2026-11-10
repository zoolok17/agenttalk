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
import os
import shutil
from pathlib import Path

import pytest

from agenttalk import attention as att
from agenttalk import cli, supervisor as sup
from agenttalk.redaction import normalize_child_output_tail, redact_diagnostic_text
from agenttalk.store import RequeueRefused, Store
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
        s.record_attempt_start("beta", rec, attempt_id="aaaaaaaaaaaa", at="t")
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


@pytest.mark.parametrize(("raw", "expected", "raw_tokens"), [
    ("Authorization: Bearer short", "Authorization: [REDACTED]", ["short"]),
    ("Authorization: Basic abc123", "Authorization: [REDACTED]", ["abc123"]),
    ("Authorization: Token tok123", "Authorization: [REDACTED]", ["tok123"]),
    ("authorization=Bearer short", "authorization=[REDACTED]", ["short"]),
    ("Bearer short", "Bearer [REDACTED]", ["short"]),
    ("token=\"abcdefghijklmnop\"", "token=[REDACTED]", ["abcdefghijklmnop"]),
    ("password='hunter2'", "password=[REDACTED]", ["hunter2"]),
    ("credential=\"abc def\"", "credential=[REDACTED]", ["abc def"]),
])
def test_child_output_redaction_covers_auth_and_quoted_assignments(
    raw: str,
    expected: str,
    raw_tokens: list[str],
) -> None:
    assert redact_diagnostic_text(raw) == expected

    tail = normalize_child_output_tail({"lines": [{"stream": "stderr", "text": raw}]})

    assert tail is not None
    rendered = json.dumps(tail, ensure_ascii=False)
    assert tail["lines"][0]["text"] == expected
    for token in raw_tokens:
        assert token not in rendered


def test_11b_dead_letter_stores_and_shows_redacted_child_output_tail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    s = _store(tmp_path)
    msg = _send(s, "poison")
    secret = "sk-FAKE-AGENTTALK-ABC123SECRET"  # gitleaks:allow
    bearer = "abcdefghijklmnop"
    short_bearer = "short"
    short_basic = "abc123"
    short_token = "tok123"
    token = "secret12345"
    password = "hunter2"
    tail = {
        "lines": [
            {"stream": "stdout", "text": "child booted"},
            {"stream": "stderr", "text": f"crashed token={secret}"},
            {"stream": "stderr", "text": f"Authorization: Bearer {bearer}"},
            {"stream": "stderr", "text": f"Authorization: Bearer {short_bearer}"},
            {"stream": "stderr", "text": f"Authorization: Basic {short_basic}"},
            {"stream": "stderr", "text": f"Authorization: Token {short_token}"},
            {"stream": "stdout", "text": f"token={token}"},
            {"stream": "stderr", "text": f"password='{password}'"},
        ]
    }
    drive = _always(DriveOutcome(
        ok=False,
        failure_class=CLASS_POISON,
        summary="terminal poison",
        child_output_tail=tail,
    ))

    _runloop(s, drive=drive, k_poison=1)

    entry = s.list_dead_letters("beta")[0]
    rendered = json.dumps(entry, ensure_ascii=False)
    assert "child_output_tail" in entry
    assert secret not in rendered
    assert bearer not in rendered
    assert short_bearer not in rendered
    assert short_basic not in rendered
    assert short_token not in rendered
    assert token not in rendered
    assert password not in rendered
    assert "[REDACTED]" in rendered

    rc = _run(["dead-letter", "show", "--agent", "beta", "--id", msg.id], tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "---- child output tail (redacted) ----" in out
    assert "[stdout] child booted" in out
    assert "[stderr] crashed token=[REDACTED]" in out
    assert "[stderr] Authorization: [REDACTED]" in out
    assert "[stdout] token=[REDACTED]" in out
    assert "[stderr] password=[REDACTED]" in out
    assert secret not in out
    assert bearer not in out
    assert short_bearer not in out
    assert short_basic not in out
    assert short_token not in out
    assert token not in out
    assert password not in out


def test_11c_dead_letter_child_output_tail_cap_holds(tmp_path: Path) -> None:
    s = _store(tmp_path)
    _send(s, "poison")
    rec = _rec(s)
    tail = {
        "lines": [
            {"stream": "stdout", "text": f"line-{idx} " + ("x" * 120)}
            for idx in range(100)
        ]
    }

    s.dead_letter("beta", rec, reason="x", failure_class=CLASS_POISON, at="t",
                  child_output_tail=tail)

    stored = s.list_dead_letters("beta")[0]["child_output_tail"]
    assert stored["truncated"] is True
    assert len(stored["lines"]) <= stored["max_lines"] == 64
    assert stored["bytes"] <= stored["max_bytes"] == 4096
    assert "line-0" not in json.dumps(stored)

    msg = _send(s, "unicode-poison")
    rec = _rec(s)
    s.dead_letter(
        "beta",
        rec,
        reason="x",
        failure_class=CLASS_POISON,
        at="t",
        child_output_tail={
            "lines": [{"stream": "stderr", "text": chr(0x1f600) * 10_000}]
        },
    )

    stored_unicode = next(
        item["child_output_tail"]
        for item in s.list_dead_letters("beta")
        if item["message_id"] == msg.id
    )
    assert stored_unicode["truncated"] is True
    assert stored_unicode["bytes"] <= stored_unicode["max_bytes"] == 4096


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
    s.record_attempt_start("beta", _rec(s), attempt_id="aaaaaaaaaaaa", at="t")   # a live attempt entry
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
        s.record_attempt_start("beta", rec, attempt_id="aaaaaaaaaaaa", at="t")
        s.reconcile_crash_in_progress("beta", rec["id"], at="t")
    after = s.attempt_record("beta", rec["id"])
    assert after["last_failure_class"] == CLASS_AMBIGUOUS       # NOT poison
    assert int(after["poison_eligible_failures"]) == 0          # crash never counts poison
    # at poison cap 3 it would NOT dispose (only 0 poison failures); the escalate cap does.
    esc = []
    drive = _always_false()
    # k_escalate=3: attempts_started already 2; reconcile of a 3rd started crash -> entry
    # escalate cap reached -> escalate + ambiguous dispose WITHOUT a fresh drive.
    s.record_attempt_start("beta", rec, attempt_id="aaaaaaaaaaaa", at="t")   # 3rd started -> CRASH
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
    s.record_attempt_start("beta", rec, attempt_id="aaaaaaaaaaaa", at="t")
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
    s.record_attempt_start("beta", rec, attempt_id="aaaaaaaaaaaa", at="t")
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


def test_interruption_policy_resolver_chain_and_corrupt_values() -> None:
    # #202 D2: per-agent -> global -> default, mirroring resolve_dead_letter_caps -
    # but a PRESENT-and-corrupt value is an ERROR (refuse-visibly), never a clamp.
    assert sup.resolve_interruption_policy({}, {}) == (60.0, 3, [])              # defaults
    assert sup.resolve_interruption_policy(
        {"interruption_redrive_seconds": 30, "k_interrupted": 5}, {}) == (30.0, 5, [])
    assert sup.resolve_interruption_policy(
        {"interruption_redrive_seconds": 30},
        {"interruption_redrive_seconds": 15, "k_interrupted": 2}) == (15.0, 2, [])  # per-agent wins
    assert sup.resolve_interruption_policy({}, {"k_interrupted": 0}) == (60.0, 0, [])  # 0 disables
    _, _, errors = sup.resolve_interruption_policy(
        {}, {"interruption_redrive_seconds": "fast"})
    assert errors and "interruption_redrive_seconds" in errors[0]
    _, _, errors = sup.resolve_interruption_policy({"k_interrupted": -1}, {})
    assert errors and "k_interrupted" in errors[0]
    # corrupt containers degrade to defaults without crashing (resolver-chain parity)
    assert sup.resolve_interruption_policy("junk", 7) == (60.0, 3, [])
    # #202 cold-review P2-4: a valid-JSON-but-absurd knob (a hand-edited config, or
    # the JSON extension `Infinity`) that the D2 backoff's base*2**(n-1) would
    # overflow toward inf must refuse (never silently clamp to the 900s runtime
    # cap) - same discipline as a non-numeric value.
    _, _, errors = sup.resolve_interruption_policy(
        {}, {"interruption_redrive_seconds": 1e308})
    assert errors and "interruption_redrive_seconds" in errors[0]
    _, _, errors = sup.resolve_interruption_policy(
        {}, {"interruption_redrive_seconds": float("inf")})
    assert errors and "interruption_redrive_seconds" in errors[0]
    # #7-fix-3: the resolver's bound is tightened to (0, 900] to match the
    # runtime's INTERRUPTION_BACKOFF_CAP_SECONDS - a base above 900 would
    # otherwise pass here but be silently reduced by the runtime cap on the
    # very first interruption, exactly the silent-clamp this resolver exists
    # to refuse instead of doing.
    _, _, errors = sup.resolve_interruption_policy(
        {}, {"interruption_redrive_seconds": 901})
    assert errors and "interruption_redrive_seconds" in errors[0]
    assert "900" in errors[0]
    # the upper bound itself is a VALID launch value
    assert sup.resolve_interruption_policy(
        {}, {"interruption_redrive_seconds": 900}) == (900.0, 3, [])


def test_interruption_ledger_counts_always_write_and_survive_relaunch(tmp_path: Path) -> None:
    # #202 D1: interrupted result -> counter+1 (both counters for turn_watchdog);
    # non-interrupted -> counters reset AND flags overwritten to False/None (the
    # stale-flag bug); the ledger is durable across a relaunch (new Store instance).
    s = _store(tmp_path)
    m = _send(s, "task")
    rec = _rec(s)
    for n in (1, 2):
        s.record_attempt_start("beta", rec, attempt_id=f"{n:012x}", at=f"t{n}")
        s.record_attempt_result("beta", rec["id"], failure_class=CLASS_AMBIGUOUS,
                                summary="turn watchdog killed hung tool descendant",
                                at=f"t{n}", interrupted=True,
                                interruption_kind="turn_watchdog")
        r = s.attempt_record("beta", rec["id"])
        assert r["interrupted_consecutive"] == n
        assert r["interrupted_watchdog_consecutive"] == n
        assert r["last_interrupted"] is True
        assert r["last_interruption_kind"] == "turn_watchdog"
    # relaunch: a NEW Store instance reads the same persisted counters
    r = Store(tmp_path).attempt_record("beta", rec["id"])
    assert r["interrupted_consecutive"] == 2
    # a non-interrupted result RESETS the run and overwrites the flags
    s.record_attempt_start("beta", rec, attempt_id=f"{3:012x}", at="t3")
    s.record_attempt_result("beta", rec["id"], failure_class=CLASS_AMBIGUOUS,
                            summary="ordinary failure", at="t3")
    r = s.attempt_record("beta", rec["id"])
    assert r["interrupted_consecutive"] == 0
    assert r["interrupted_watchdog_consecutive"] == 0
    assert r["last_interrupted"] is False
    assert r["last_interruption_kind"] is None
    assert m.id == rec["id"]


def test_crash_reconcile_increments_any_kind_but_never_the_watchdog_counter(
    tmp_path: Path,
) -> None:
    # #202 D1 + NEW-2: crash_mid_turn IS an interruption (backoff + rejoin apply),
    # but its cause is UNOBSERVED - it must never count toward the turn_watchdog-only
    # k_interrupted ceiling (the codified store ruling stands).
    s = _store(tmp_path)
    _send(s, "task")
    rec = _rec(s)
    for n in (1, 2, 3):
        s.record_attempt_start("beta", rec, attempt_id=f"{n:012x}", at=f"t{n}")
        assert s.reconcile_crash_in_progress("beta", rec["id"], at=f"t{n}") is True
        r = s.attempt_record("beta", rec["id"])
        assert r["interrupted_consecutive"] == n
        assert r.get("interrupted_watchdog_consecutive", 0) == 0   # never the ceiling counter
        assert r["last_interrupted"] is True
        assert r["last_interruption_kind"] == "crash_mid_turn"


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
    s.record_attempt_start("beta", rec, attempt_id="aaaaaaaaaaaa", at="2026-07-05T00:00:00Z")
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
    s.record_attempt_start("beta", rec, attempt_id="aaaaaaaaaaaa", at="t")
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
    s.record_attempt_start("beta", rec, attempt_id="aaaaaaaaaaaa", at="t")
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
    s.record_attempt_start("beta", rec, attempt_id="aaaaaaaaaaaa", at="t")
    data = s.dead_letter_attempts("beta")
    data["messages"][m.id]["poison_eligible_failures"] = 2   # mid poison run
    data["messages"][m.id]["in_progress"] = True             # crashed mid-turn
    s._write_attempts("beta", data)
    assert s.reconcile_crash_in_progress("beta", m.id, at="t2") is True
    rec2 = s.attempt_record("beta", m.id)
    assert int(rec2["poison_eligible_failures"]) == 0        # crash reset the consecutive run
    assert rec2["last_failure_class"] == "ambiguous_or_unknown"


def test_46b_crash_mid_turn_resets_never_started_tracking(tmp_path: Path) -> None:
    # #7-fix-4: a crash mid-turn breaks the "consecutive never-started" run by
    # definition (the process DID start this attempt - it crashed, which is why
    # it's here). Leaving never_started_first_at/never_started_consecutive
    # intact would let a LATER never-started failure promote using a
    # window/count that spans across the crash, not an actually-consecutive run.
    s = _store(tmp_path)
    m = _send(s, "never-started-then-crash")
    rec = _rec(s)
    s.record_attempt_start("beta", rec, attempt_id="aaaaaaaaaaaa", at="t")
    data = s.dead_letter_attempts("beta")
    data["messages"][m.id]["never_started_first_at"] = "t0"
    data["messages"][m.id]["never_started_consecutive"] = 1
    data["messages"][m.id]["in_progress"] = True             # crashed mid-turn
    s._write_attempts("beta", data)
    assert s.reconcile_crash_in_progress("beta", m.id, at="t2") is True
    rec2 = s.attempt_record("beta", m.id)
    assert rec2["never_started_first_at"] is None
    assert rec2["never_started_consecutive"] == 0


def test_47_infra_dominant_crash_at_ceiling_escalates_no_dispose(tmp_path: Path) -> None:
    # codex expected test: ~19 INFRA + one reconciled crash at K_escalate escalates but does NOT
    # dead-letter or advance the cursor - a healthy message stale-killed during a sustained OUTAGE
    # (its crash flips last_class to ambiguous) is protected by the infra-dominance predicate.
    s = _store(tmp_path)
    _send(s, "healthy-stale-killed-in-outage")
    rec = _rec(s)
    s.record_attempt_start("beta", rec, attempt_id="aaaaaaaaaaaa", at="t")
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


# #665: real CLI requeue, integrity checks and fan-out comparison.
_DL_KINDS = pytest.mark.parametrize("kind", ["task", "review-request"])
_DL_ALL_KINDS = pytest.mark.parametrize("kind", ["message", "task", "review-request"])
_DL_SIGNED = pytest.mark.parametrize("signed", [False, True], ids=["unsigned", "signed"])


def _requeue_store(tmp_path, monkeypatch, signed):
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "scratch.key"))
    root = tmp_path / "store"
    root.mkdir()
    s = Store(root)
    s.init(["lead", "beta", "gamma"])
    s.set_role("lead", "lead")
    s.set_model_vendor("beta", "openai")
    if signed:
        assert _run(["hmac-init"], root) == 0
    return s


def _dead_payload(s, original, agent="beta"):
    _runloop(s, agent, drive=_always_false(), k_poison=1, max_polls=1)
    path = s.dead_letter_dir / agent / f"{original.id}.json"
    assert path.is_file()
    assert s.read_dead_letter_payload(agent, original.id) == path.read_bytes()
    return path


def _rewrite_payload(s, path, payload, signed=False):
    from agenttalk import signing

    if signed:
        project = s.project_id()
        payload = signing.sign_message(payload, signing.load_key(project), key_id=project)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _requeue(s, original, *flags):
    return _run(
        ["dead-letter", "requeue", "--agent", "beta", "--id", original.id, *flags],
        s.root,
    )


def _assert_requeue_refused(s, original, path, capsys, explanation):
    before = path.read_bytes()
    live = {p.name: p.read_bytes() for p in s.messages_dir.glob("*.json")}
    capsys.readouterr()
    assert _requeue(s, original) == 2
    err = capsys.readouterr().err
    assert explanation in err
    assert "Traceback" not in err
    assert path.read_bytes() == before
    assert {p.name: p.read_bytes() for p in s.messages_dir.glob("*.json")} == live
    return err


@_DL_KINDS
@_DL_SIGNED
@pytest.mark.parametrize("sender_flags", [[], ["--from", "lead"]], ids=["implicit", "explicit"])
@pytest.mark.parametrize("audience", [False, True], ids=["single", "frozen"])
def test_requeue_preserves_dispatch(tmp_path, monkeypatch, kind, signed, sender_flags, audience):
    from agenttalk import signing

    s = _requeue_store(tmp_path, monkeypatch, signed)
    meta = {"request_id": "tk-requeue", "epoch_at_send": "old-epoch"}
    vendors = None
    if audience:
        meta["audience_resolved"] = "beta,gamma"
        vendors = {"beta": "openai", "gamma": "unverified"}
    original = s.send(
        sender="lead",
        recipient="beta",
        kind=kind,
        subject="assignment",
        body="build",
        meta=meta,
        _dispatch_vendors=vendors,
    )
    path = _dead_payload(s, original)
    before = path.read_bytes()
    s.set_model_vendor("beta", "anthropic")
    assert _requeue(s, original, *sender_flags) == 0
    fresh = s.messages_for("beta")[-1]
    assert fresh.id != original.id
    assert (fresh.sender, fresh.recipient, fresh.kind, fresh.subject, fresh.body) == (
        "lead",
        "beta",
        kind,
        "assignment",
        "build",
    )
    copy_only = {"signature", "signed_at"}
    assert {k: v for k, v in fresh.meta.items() if k not in copy_only} == {
        k: v for k, v in dict(original.meta, requeued_from=original.id).items() if k not in copy_only
    }
    if signed:
        signing.verify_message(
            json.loads((s.messages_dir / f"{fresh.id}.json").read_bytes()),
            signing.load_key(s.project_id()),
            expected_key_id=s.project_id(),
        )
    assert s.attempt_record("beta", fresh.id) is None
    assert path.read_bytes() == before


@_DL_KINDS
@_DL_SIGNED
@pytest.mark.parametrize("field", ["assignee_model_vendor", "assignee_model_vendors"])
def test_requeue_caller_vendor_guard(tmp_path, monkeypatch, capsys, kind, signed, field):
    s = _requeue_store(tmp_path, monkeypatch, signed)
    with pytest.raises(ValueError, match="publisher-owned"):
        s.send(
            sender="lead",
            recipient="beta",
            kind=kind,
            body="spoof",
            meta={field: {"beta": "other"}},
        )
    assert (
        _run(
            [
                "task",
                "--from",
                "lead",
                "--to",
                "beta",
                "--force",
                "-m",
                "spoof",
                "--meta",
                field + "=other",
            ],
            s.root,
        )
        == 2
    )
    assert "publisher-owned" in capsys.readouterr().err
    assert not s.valid_messages()


def _dispatch_copies(s, kind, field=None):
    copies = []
    for index, who in enumerate(("beta", "gamma")):
        meta = {
            "work_item": "issue-665",
            "stage": "read",
            "work_cycle": "1",
            "request_id": "tk-fan",
            "audience_resolved": "beta,gamma",
        }
        body = "go"
        if field == "body":
            body = str(index)
        elif field:
            meta[field] = ("a" if index == 0 else "b") * (40 if field == "work_head" else 1)
        copies.append(
            s.send(
                sender="lead",
                recipient=who,
                kind=kind,
                body=body,
                meta=meta,
                _dispatch_vendors={"beta": "openai", "gamma": "unverified"},
            )
        )
    return copies


def _board_request(s):
    from agenttalk import work_board

    copies = [m for m in s.valid_messages() if m.kind in {"task", "review-request"}]
    return work_board._request("tk-fan", copies, [], frozenset(), {}, parse=False)


@_DL_KINDS
@_DL_SIGNED
@pytest.mark.parametrize("requeued", [False, True], ids=["intact", "requeued"])
def test_requeue_fanout_lookup_and_board(tmp_path, monkeypatch, kind, signed, requeued):
    from agenttalk import work_tags

    s = _requeue_store(tmp_path, monkeypatch, signed)
    first, second = _dispatch_copies(s, kind)
    if requeued:
        _dead_payload(s, first)
        assert _requeue(s, first) == 0
        first = s.messages_for("beta")[-1]
    assert len(work_tags._opener(s.valid_messages(), "tk-fan")) == 2
    assert not _board_request(s)["issues"]
    for who, opener in (("beta", first), ("gamma", second)):
        response = "task-response" if kind == "task" else "review-result"
        s.send(
            sender=who,
            recipient="lead",
            kind=response,
            body="ok",
            meta={
                "request_id": "tk-fan",
                "in_reply_to": opener.id,
                "status": "done" if kind == "task" else "approved",
                "verdict": "GO",
            },
        )
    assert not _board_request(s)["issues"]


@_DL_KINDS
@_DL_SIGNED
@pytest.mark.parametrize("field", ["work_head", "key_id", "body", "signature", "signed_at"])
def test_requeue_comparison_boundary(tmp_path, monkeypatch, kind, signed, field):
    from agenttalk import work_tags

    if signed and field in {"key_id", "signature", "signed_at"}:
        pytest.skip("signing writes these fields; unsigned copies pin the comparison boundary")
    s = _requeue_store(tmp_path, monkeypatch, signed)
    first, _ = _dispatch_copies(s, kind, field)
    if field in {"work_head", "key_id"}:
        with pytest.raises(ValueError, match="ambiguous"):
            work_tags._opener(s.valid_messages(), "tk-fan")
        with pytest.raises(ValueError, match="ambiguous"):
            s.send(
                sender="beta",
                recipient="lead",
                kind="task-response" if kind == "task" else "review-result",
                body="ok",
                meta={
                    "request_id": "tk-fan",
                    "in_reply_to": first.id,
                    "status": "done",
                },
            )
        assert "ambiguous fan-out openers" in [issue[0] for issue in _board_request(s)["issues"]]
    else:
        assert len(work_tags._opener(s.valid_messages(), "tk-fan")) == 2
        assert not _board_request(s)["issues"]


@_DL_ALL_KINDS
@pytest.mark.parametrize("edit", ["vendor", "body", "sender"])
def test_requeue_signed_tampering(tmp_path, monkeypatch, capsys, kind, edit):
    s = _requeue_store(tmp_path, monkeypatch, True)
    original = _send(s, "real", kind=kind, meta={"request_id": "tk-edited"})
    path = _dead_payload(s, original)
    payload = json.loads(path.read_bytes())
    if edit == "vendor":
        payload["meta"]["assignee_model_vendors"] = {"beta": "anthropic"}
    else:
        payload["body" if edit == "body" else "from"] = "forged" if edit == "body" else "gamma"
    _rewrite_payload(s, path, payload)
    _assert_requeue_refused(s, original, path, capsys, "failed its signature check")


@_DL_ALL_KINDS
@pytest.mark.parametrize("upgrade", ["enabled", "replaced"])
def test_requeue_signing_upgrade_limit(tmp_path, monkeypatch, capsys, kind, upgrade):
    s = _requeue_store(tmp_path, monkeypatch, upgrade == "replaced")
    original = _send(s, "old", kind=kind, meta={"request_id": "tk-upgrade"})
    path = _dead_payload(s, original)
    if upgrade == "enabled":
        assert _run(["hmac-init"], s.root) == 0
    else:
        (tmp_path / "scratch.key").write_text((b"different-test-key" * 4).hex(), encoding="utf-8")
    _assert_requeue_refused(s, original, path, capsys, "failed its signature check")


@_DL_SIGNED
def test_requeue_plain_message_vendor_field_refused(tmp_path, monkeypatch, capsys, signed):
    s = _requeue_store(tmp_path, monkeypatch, signed)
    original = _send(s, "plain")
    path = _dead_payload(s, original)
    payload = json.loads(path.read_bytes())
    payload["meta"]["assignee_model_vendors"] = {"beta": "openai"}
    _rewrite_payload(s, path, payload, signed)
    _assert_requeue_refused(s, original, path, capsys, "publisher-owned")


@_DL_KINDS
@_DL_SIGNED
@pytest.mark.parametrize("stamp", [None, {}, [], "", 0, False, {"gamma": "openai"}, {"beta": "nonsense"}])
def test_requeue_invalid_stamp(tmp_path, monkeypatch, capsys, kind, signed, stamp):
    s = _requeue_store(tmp_path, monkeypatch, signed)
    original = _send(s, "old", kind=kind, meta={"request_id": "tk-stamp"})
    path = _dead_payload(s, original)
    s.set_model_vendor("beta", "anthropic")
    payload = json.loads(path.read_bytes())
    payload["meta"]["assignee_model_vendors"] = stamp
    _rewrite_payload(s, path, payload, signed)
    _assert_requeue_refused(s, original, path, capsys, "agenttalk dead-letter requeue:")


@_DL_KINDS
@_DL_SIGNED
@pytest.mark.parametrize("audience", [["beta"], {}, 7, None, True, False])
def test_requeue_intact_bad_audience(tmp_path, monkeypatch, capsys, kind, signed, audience):
    s = _requeue_store(tmp_path, monkeypatch, signed)
    original = _send(
        s,
        "old",
        kind=kind,
        meta={"request_id": "tk-audience", "audience_resolved": audience},
    )
    assert original in s.valid_messages()
    path = _dead_payload(s, original)
    _assert_requeue_refused(s, original, path, capsys, "unsupported shape")


@_DL_KINDS
@_DL_SIGNED
def test_requeue_legacy_missing_stamp(tmp_path, monkeypatch, kind, signed):
    s = _requeue_store(tmp_path, monkeypatch, signed)
    original = _send(s, "old", kind=kind, meta={"request_id": "tk-legacy"})
    path = _dead_payload(s, original)
    payload = json.loads(path.read_bytes())
    del payload["meta"]["assignee_model_vendors"]
    _rewrite_payload(s, path, payload, signed)
    s.set_model_vendor("beta", "anthropic")
    assert _requeue(s, original) == 0
    assert s.messages_for("beta")[-1].meta["assignee_model_vendors"] == {"beta": "anthropic"}


@_DL_ALL_KINDS
@_DL_SIGNED
def test_requeue_unedited_message(tmp_path, monkeypatch, kind, signed):
    s = _requeue_store(tmp_path, monkeypatch, signed)
    original = _send(s, "intact", kind=kind, meta={"request_id": "tk-intact"})
    _dead_payload(s, original)
    assert _requeue(s, original) == 0
    assert s.messages_for("beta")[-1].body == "intact"


_MALFORMED = [
    ("raw", []),
    ("raw", "x"),
    ("raw", 7),
    ("raw", None),
    ("meta", [1]),
    ("meta", "x"),
    ("meta", 7),
    ("meta", []),
    ("meta", ""),
    ("meta", 0),
    ("meta", False),
    ("meta", None),
    ("kind", []),
    ("kind", {}),
    ("kind", 7),
    ("kind", None),
    ("body", 7),
    ("subject", 7),
    ("from", 7),
    ("to", None),
    ("id", None),
    ("ts", 7),
]


@_DL_ALL_KINDS
@_DL_SIGNED
@pytest.mark.parametrize("field,value", _MALFORMED)
def test_requeue_malformed_envelope(tmp_path, monkeypatch, capsys, kind, signed, field, value):
    s = _requeue_store(tmp_path, monkeypatch, signed)
    original = _send(s, "old", kind=kind, meta={"request_id": "tk-bad"})
    path = _dead_payload(s, original)
    payload = json.loads(path.read_bytes())
    if field == "raw":
        payload = value
    elif field == "id":
        del payload[field]
    else:
        payload[field] = value
    _rewrite_payload(s, path, payload)
    err = _assert_requeue_refused(s, original, path, capsys, "corrupt payload")
    assert "top-level" in err if field == "raw" else repr(field) in err
    # The strict reader, rather than a later Python operation, explains the bad field.
    from agenttalk.store import Message

    with pytest.raises(ValueError):
        Message.from_raw(payload)


@_DL_ALL_KINDS
@pytest.mark.parametrize(
    "field,value",
    [
        ("signature", 7),
        ("signature", []),
        ("signature", "\u00e9"),
        ("key_id", []),
        ("signature_alg", []),
    ],
)
def test_requeue_bad_signature_shape(tmp_path, monkeypatch, capsys, kind, field, value):
    s = _requeue_store(tmp_path, monkeypatch, True)
    original = _send(s, "old", kind=kind, meta={"request_id": "tk-signature"})
    path = _dead_payload(s, original)
    payload = json.loads(path.read_bytes())
    payload["meta"][field] = value
    _rewrite_payload(s, path, payload)
    _assert_requeue_refused(s, original, path, capsys, "failed its signature check")


@_DL_ALL_KINDS
@_DL_SIGNED
@pytest.mark.parametrize("missing", [False, True], ids=["empty", "missing"])
def test_requeue_empty_or_missing_meta(tmp_path, monkeypatch, capsys, kind, signed, missing):
    s = _requeue_store(tmp_path, monkeypatch, signed)
    original = _send(s, "old", kind=kind, meta={"request_id": "tk-meta"})
    path = _dead_payload(s, original)
    payload = json.loads(path.read_bytes())
    if missing:
        del payload["meta"]
    else:
        payload["meta"] = {}
    _rewrite_payload(s, path, payload)
    if signed:
        _assert_requeue_refused(s, original, path, capsys, "missing signature")
    else:
        assert _requeue(s, original) == 0
        assert s.messages_for("beta")[-1].body == "old"


@_DL_KINDS
@_DL_SIGNED
@pytest.mark.parametrize("dead_target", [False, True])
def test_requeue_replacement_order(tmp_path, monkeypatch, capsys, kind, signed, dead_target):
    s = _requeue_store(tmp_path, monkeypatch, signed)
    tags = {"work_item": "issue-665", "stage": "read", "work_cycle": "1"}
    original = _send(s, "first", kind=kind, meta=dict(tags, request_id="tk-first"))
    replacement = _send(
        s,
        "replacement",
        kind=kind,
        meta=dict(tags, request_id="tk-replacement", supersedes="tk-first"),
    )
    # Replay a replacement with its target live or already dead-lettered.
    if dead_target:
        _dead_payload(s, original)
    else:
        s.advance_cursor("beta", original.id)
    path = _dead_payload(s, replacement)
    if dead_target:
        _assert_requeue_refused(s, replacement, path, capsys, "no available opener")
        assert _requeue(s, original) == 0
    assert _requeue(s, replacement) == 0


_ODD_KEYS = [
    "request_id",
    "broadcast_id",
    "work_item",
    "stage",
    "work_cycle",
    "work_round",
    "work_head",
    "supersedes",
    "work_title",
    "external_deliverable",
    "relay_to",
    "epoch_at_send",
    "status",
    "in_reply_to",
    "requeued_from",
    "audience",
    "origin_request_id",
    "origin_inbound_id",
    "target_msg_id",
    "expected_roster_revision",
    "wait_for",
]


@_DL_KINDS
@_DL_SIGNED
@pytest.mark.parametrize("key", _ODD_KEYS)
@pytest.mark.parametrize("value", [["beta"], {"a": 1}, 7, None, True])
def test_requeue_other_metadata_shapes(tmp_path, monkeypatch, kind, signed, key, value):
    s = _requeue_store(tmp_path, monkeypatch, signed)
    try:
        original = _send(s, "odd", kind=kind, meta={"request_id": "tk-sweep", key: value})
    except (ValueError, TypeError, AttributeError):
        assert not list(s.messages_dir.glob("*.json"))
        return  # Not storable: this combination cannot reach requeue intact.
    path = _dead_payload(s, original)
    before = path.read_bytes()
    assert _requeue(s, original) in {0, 2}
    assert path.read_bytes() == before


@_DL_KINDS
@_DL_SIGNED
def test_requeue_refuses_second_live_copy_without_jamming_work(tmp_path, monkeypatch, capsys, kind, signed):
    from agenttalk import work_tags

    s = _requeue_store(tmp_path, monkeypatch, signed)
    original, other = _dispatch_copies(s, kind)
    path = _dead_payload(s, original)
    assert _requeue(s, original) == 0
    fresh = s.messages_for("beta")[-1]

    _assert_requeue_refused(s, original, path, capsys, "already has a live copy")

    assert len(work_tags._opener(s.valid_messages(), "tk-fan")) == 2
    assert not _board_request(s)["issues"]
    for who, opener in (("beta", fresh), ("gamma", other)):
        s.send(
            sender=who,
            recipient="lead",
            kind="task-response" if kind == "task" else "review-result",
            body="ok",
            meta={
                "request_id": "tk-fan",
                "in_reply_to": opener.id,
                "status": "done" if kind == "task" else "approved",
                "verdict": "GO",
            },
        )
    replacement = s.send(
        sender="lead",
        recipient="beta",
        kind=kind,
        body="replacement",
        meta={
            "work_item": "issue-665",
            "stage": "read",
            "work_cycle": "1",
            "request_id": "tk-replacement",
            "supersedes": "tk-fan",
        },
    )
    assert replacement.meta["supersedes"] == "tk-fan"


@_DL_KINDS
@_DL_SIGNED
@pytest.mark.parametrize("replay", ["original", "fresh"])
def test_requeue_allowed_again_after_live_copy_is_dead_lettered(tmp_path, monkeypatch, kind, signed, replay):
    from agenttalk import work_tags

    s = _requeue_store(tmp_path, monkeypatch, signed)
    original, _ = _dispatch_copies(s, kind)
    _dead_payload(s, original)
    assert _requeue(s, original) == 0
    fresh = s.messages_for("beta")[-1]
    _dead_payload(s, fresh)

    # #706: the original was already requeued, so repeating it is a replay (refused); the failed copy is a retry
    assert _requeue(s, original if replay == "original" else fresh) == (2 if replay == "original" else 0)

    assert len(work_tags._opener(s.valid_messages(), "tk-fan")) == (1 if replay == "original" else 2)
    assert not _board_request(s)["issues"]


@_DL_KINDS
@_DL_SIGNED
def test_requeue_without_request_id_keeps_legacy_repeat_behavior(tmp_path, monkeypatch, kind, signed):
    s = _requeue_store(tmp_path, monkeypatch, signed)
    original = _send(s, "legacy", kind=kind)
    assert "request_id" not in original.meta
    _dead_payload(s, original)

    assert _requeue(s, original) == 0
    assert _requeue(s, original) == 0

    assert len(s.messages_for("beta")) == 2


@_DL_SIGNED
def test_requeue_plain_message_keeps_repeat_behavior(tmp_path, monkeypatch, signed):
    s = _requeue_store(tmp_path, monkeypatch, signed)
    original = _send(s, "plain", meta={"request_id": "tk-plain"})
    _dead_payload(s, original)

    assert _requeue(s, original) == 0
    assert _requeue(s, original) == 0

    assert len(s.messages_for("beta")) == 2


def test_requeue_copy_only_metadata_is_exact():
    from agenttalk import work_tags

    assert work_tags.COPY_ONLY_META == {"requeued_from", "signature", "signed_at"}


@_DL_ALL_KINDS
def test_requeue_unreadable_signing_key_is_plain_refusal(tmp_path, monkeypatch, capsys, kind):
    s = _requeue_store(tmp_path, monkeypatch, True)
    original = _send(s, "signed", kind=kind, meta={"request_id": "tk-key"})
    path = _dead_payload(s, original)
    key_directory = tmp_path / "key-directory"
    key_directory.mkdir()
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(key_directory))

    _assert_requeue_refused(s, original, path, capsys, "failed its signature check")


@_DL_KINDS
@_DL_SIGNED
def test_requeue_race_publishes_only_one_live_copy(tmp_path, monkeypatch, kind, signed):
    import subprocess
    import sys
    import time

    from agenttalk import work_tags

    s = _requeue_store(tmp_path, monkeypatch, signed)
    original, _ = _dispatch_copies(s, kind)
    payload = _dead_payload(s, original)
    before = payload.read_bytes()
    go = tmp_path / "go"
    ready = [tmp_path / f"ready-{index}" for index in range(2)]
    script = r"""
import json
import sys
import time
from pathlib import Path
from agenttalk import cli
from agenttalk import store as store_module

root, original, ready, go = sys.argv[1:]
real_write = store_module._write_text_exclusive
def write(path, text):
    identity = real_write(path, text)
    if Path(path).name.endswith(".pending") and json.loads(text)["meta"].get("requeued_from") == original:
        Path(ready).write_text(json.loads(text)["meta"]["request_id"], encoding="utf-8")
        deadline = time.monotonic() + 40
        while not Path(go).exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("the parent did not release both requeues")
            time.sleep(0.01)
    return identity
store_module._write_text_exclusive = write
raise SystemExit(cli.main(["--root", root, "dead-letter", "requeue", "--agent", "beta", "--id", original]))
"""
    children = []
    results = []
    try:
        for signal in ready:
            children.append(
                subprocess.Popen(
                    [sys.executable, "-c", script, str(s.root), original.id, str(signal), str(go)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
            )
        deadline = time.monotonic() + 40
        while not all(signal.is_file() and signal.read_text(encoding="utf-8") == "tk-fan" for signal in ready):
            assert all(child.poll() is None for child in children), "a requeue exited before the send boundary"
            assert time.monotonic() < deadline, "both requeues must pass the CLI check before publication"
            time.sleep(0.01)
        assert [signal.read_text(encoding="utf-8") for signal in ready] == ["tk-fan", "tk-fan"]
        assert len(list(s.messages_dir.glob("*.pending"))) == 2
        go.write_text("publish", encoding="utf-8")
        for child in children:
            stdout, stderr = child.communicate(timeout=40)
            results.append((child.returncode, stdout, stderr))
    finally:
        go.touch(exist_ok=True)
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)

    assert sorted(code for code, _, _ in results) == [0, 2], results
    loser = next(stderr for code, _, stderr in results if code == 2)
    assert "already has a live copy" in loser and "new request id" in loser
    assert "Traceback" not in loser
    copies = [m for m in s.messages_for("beta") if m.meta.get("request_id") == "tk-fan"]
    assert len(copies) == 1 and copies[0].meta["requeued_from"] == original.id
    assert len(work_tags._opener(s.valid_messages(), "tk-fan")) == 2
    assert not _board_request(s)["issues"]
    assert payload.read_bytes() == before
    assert not list(s.messages_dir.glob("*.pending"))


@_DL_SIGNED
@pytest.mark.parametrize(
    "requeued_kind,live_kind",
    [
        ("task", "message"),
        ("review-request", "message"),
        ("message", "task"),
        ("message", "review-request"),
    ],
)
def test_requeue_live_copy_guard_only_matches_openers(tmp_path, monkeypatch, signed, requeued_kind, live_kind):
    s = _requeue_store(tmp_path, monkeypatch, signed)
    original = _send(s, "dead", kind=requeued_kind, meta={"request_id": "tk-kinds"})
    payload = _dead_payload(s, original)
    before = payload.read_bytes()
    live = _send(s, "keep", kind=live_kind, meta={"request_id": "tk-kinds"})

    assert _requeue(s, original) == 0

    copies = s.messages_for("beta")
    assert len(copies) == 2 and live.id in {m.id for m in copies}
    fresh = next(m for m in copies if m.id != live.id)
    assert fresh.kind == requeued_kind and fresh.meta["requeued_from"] == original.id
    assert payload.read_bytes() == before


@_DL_KINDS
@_DL_SIGNED
def test_requeue_live_copy_refusal_explains_next_step(tmp_path, monkeypatch, capsys, kind, signed):
    s = _requeue_store(tmp_path, monkeypatch, signed)
    original = _send(s, "dead", kind=kind, meta={"request_id": "tk-help"})
    payload = _dead_payload(s, original)
    assert _requeue(s, original) == 0

    text = _assert_requeue_refused(s, original, payload, capsys, "already has a live copy")

    assert "Use the existing copy" in text and "new request id" in text


@_DL_KINDS
@_DL_SIGNED
def test_regular_send_keeps_duplicate_request_behavior(tmp_path, monkeypatch, kind, signed):
    s = _requeue_store(tmp_path, monkeypatch, signed)
    first = _send(s, "first", kind=kind, meta={"request_id": "tk-legacy"})
    second = _send(s, "second", kind=kind, meta={"request_id": "tk-legacy"})

    assert first.id != second.id
    assert {m.id for m in s.messages_for("beta")} == {first.id, second.id}


@_DL_KINDS
@_DL_SIGNED
def test_705_unstamped_fanout_refusal_preserves_survivor_reply(tmp_path, monkeypatch, capsys, kind, signed):
    from agenttalk import work_tags

    s = _requeue_store(tmp_path, monkeypatch, signed)
    first, survivor = _dispatch_copies(s, kind)
    for message in (first, survivor):
        path = s.messages_dir / (message.id + ".json")
        payload = json.loads(path.read_bytes())
        payload["meta"].pop("assignee_model_vendors")
        _rewrite_payload(s, path, payload, signed=signed)
    path = _dead_payload(s, first)
    survivor_path = s.messages_dir / (survivor.id + ".json")
    survivor_bytes = survivor_path.read_bytes()

    _assert_requeue_refused(
        s, first, path, capsys, "unsupported shape; nothing was published. Review it and resend by hand."
    )

    assert survivor_path.read_bytes() == survivor_bytes
    assert len(work_tags._opener(s.valid_messages(), "tk-fan")) == 1
    response = s.send(
        sender="gamma",
        recipient="lead",
        kind="task-response" if kind == "task" else "review-result",
        body="ok",
        meta={
            "request_id": "tk-fan",
            "in_reply_to": survivor.id,
            "status": "done" if kind == "task" else "approved",
            "verdict": "GO",
        },
    )
    assert response.meta["in_reply_to"] == survivor.id
    assert survivor_path.read_bytes() == survivor_bytes


@_DL_KINDS
@_DL_SIGNED
@pytest.mark.parametrize("audience", [["beta"], {"beta": True}])
def test_705_unstamped_audience_type_is_checked(tmp_path, monkeypatch, capsys, kind, signed, audience):
    s = _requeue_store(tmp_path, monkeypatch, signed)
    original = _send(s, "old", kind=kind, meta={"request_id": "tk-old-shape"})
    path = _dead_payload(s, original)
    payload = json.loads(path.read_bytes())
    payload["meta"].pop("assignee_model_vendors")
    payload["meta"]["audience_resolved"] = audience
    _rewrite_payload(s, path, payload, signed=signed)
    _assert_requeue_refused(
        s, original, path, capsys, "unsupported shape; nothing was published. Review it and resend by hand."
    )


@_DL_KINDS
@_DL_SIGNED
@pytest.mark.parametrize("audience", [None, "beta"])
def test_705_unstamped_single_recipient_still_requeues(tmp_path, monkeypatch, kind, signed, audience):
    s = _requeue_store(tmp_path, monkeypatch, signed)
    original = _send(s, "old", kind=kind, meta={"request_id": "tk-old-single"})
    path = _dead_payload(s, original)
    payload = json.loads(path.read_bytes())
    payload["meta"].pop("assignee_model_vendors")
    if audience is not None:
        payload["meta"]["audience_resolved"] = audience
    _rewrite_payload(s, path, payload, signed=signed)
    before = path.read_bytes()
    assert _requeue(s, original) == 0
    fresh = s.messages_for("beta")[-1]
    assert fresh.kind == kind and fresh.meta["request_id"] == "tk-old-single"
    assert fresh.meta["requeued_from"] == original.id
    assert path.read_bytes() == before


@_DL_KINDS
@_DL_SIGNED
def test_705_origin_id_route_refuses_duplicate_request(tmp_path, monkeypatch, kind, signed):
    from agenttalk.wrapper.obligations import _roster_snapshot

    s = _requeue_store(tmp_path, monkeypatch, signed)
    meta = {
        "request_id": "tk-origin-copy",
        "origin_request_id": "tk-parent",
        "origin_inbound_id": "inbound-parent",
        "expected_roster_revision": _roster_snapshot(s.load_config())["revision"],
    }
    first = s.send(sender="beta", recipient="lead", kind=kind, body="original", meta=meta, _refuse_live_request=True)
    live = {p.name: p.read_bytes() for p in s.messages_dir.glob("*.json")}
    with pytest.raises(ValueError, match="already has a live copy"):
        s.send(sender="beta", recipient="lead", kind=kind, body="duplicate", meta=meta, _refuse_live_request=True)
    assert {p.name: p.read_bytes() for p in s.messages_dir.glob("*.json")} == live
    assert len(s.messages_for("lead")) == 1 and s.messages_for("lead")[0].id == first.id
    assert not list(s.messages_dir.glob("*.pending"))


# ----------------------------------------------- #706: a dead letter cannot be requeued twice (prevention)
def _706_answer(s, sender, message):
    kind = "task-response" if message.kind == "task" else "review-result"
    status = "done" if message.kind == "task" else "approved"
    return s.send(
        sender=sender,
        recipient="lead",
        kind=kind,
        body="ok",
        meta={"request_id": message.meta["request_id"], "in_reply_to": message.id, "status": status, "verdict": "GO"},
    )


def _706_copy_of(s, original):
    return next(m for m in s.messages_for("beta") if m.meta.get("requeued_from") == original.id)


def _706_requeued(s, kind="task", rid="tk-c"):
    """A dead-lettered original that has been requeued once: returns (original, the live copy)."""
    original = _send(s, "dead", kind=kind, meta={"request_id": rid})
    _dead_payload(s, original)
    assert _requeue(s, original) == 0
    return original, _706_copy_of(s, original)


def _706_compact_away(s, live):
    last = _706_answer(s, "beta", live)
    for agent in ("lead", "beta", "gamma"):
        s.set_cursor(agent, last.id)
    assert _run(["compact", "--keep-count", "1", "--keep-age-days", "0"], s.root) == 0
    assert not (s.messages_dir / f"{live.id}.json").is_file()


def _706_strip(s, message, signed=False):
    path = s.messages_dir / f"{message.id}.json"
    payload = json.loads(path.read_bytes())
    payload["meta"].pop("assignee_model_vendors")
    _rewrite_payload(s, path, payload, signed=signed)


def _706_published(s):
    return [m for m in s.messages_for("beta") if m.meta.get("request_id") == "tk-c"]


@_DL_KINDS
@_DL_SIGNED
def test_706_a_requeue_cannot_repeat_after_compaction(tmp_path, monkeypatch, capsys, kind, signed):
    s = _requeue_store(tmp_path, monkeypatch, signed)
    original, live = _706_requeued(s, kind)
    payload = s.dead_letter_dir / "beta" / f"{original.id}.json"
    before = payload.read_bytes()
    _706_compact_away(s, live)
    capsys.readouterr()

    assert _requeue(s, original) == 2

    err = capsys.readouterr().err
    assert live.id in err and "compacted away" in err and "new request id" in err
    assert payload.read_bytes() == before and not _706_published(s)


def test_706_retry_is_not_replay(tmp_path, monkeypatch, capsys):
    s = _requeue_store(tmp_path, monkeypatch, False)
    original, first = _706_requeued(s)
    _dead_payload(s, first)
    capsys.readouterr()

    assert _requeue(s, original) == 2  # the same dead letter again: a replay
    err = capsys.readouterr().err
    assert first.id in err and f"dead-letter requeue --agent beta --id {first.id}" in err

    assert _requeue(s, first) == 0  # the later copy that itself failed: a retry
    second = _706_copy_of(s, first)
    _dead_payload(s, second)
    assert _requeue(s, second) == 0  # and again: no chain of failures gets stuck


@pytest.mark.parametrize("case", ["plain", "already-retried", "resolved", "unreadable-record", "crashed-successor"])
def test_706_a_refusal_names_a_retry_only_when_it_will_be_accepted(tmp_path, monkeypatch, capsys, case):
    s = _requeue_store(tmp_path, monkeypatch, False)
    original, first = _706_requeued(s)
    _dead_payload(s, first)
    state = s._requeue_state_path("beta", first.id)
    if case == "already-retried":
        assert _requeue(s, first) == 0
        _dead_payload(s, _706_copy_of(s, first))
    elif case == "resolved":
        assert (
            _run(
                ["dead-letter", "resolve", "--agent", "beta", "--id", first.id, "--reason", "done", "--from", "lead"],
                s.root,
            )
            == 0
        )
    elif case == "unreadable-record":
        state.write_text("{", encoding="utf-8")
    elif case == "crashed-successor":
        _706_record(s, first.id, "20261109-000000-000000-Gone")
    capsys.readouterr()

    assert _requeue(s, original) == 2

    advised = f"dead-letter requeue --agent beta --id {first.id}" in capsys.readouterr().err
    assert advised == (case == "plain")
    if advised:
        assert _requeue(s, first) == 0  # the advice is accepted


@pytest.mark.parametrize("fate", ["compacted", "dead-lettered"])
def test_706_a_paused_requeue_cannot_publish_after_the_work_moved_into_history(tmp_path, monkeypatch, capsys, fate):
    import subprocess
    import sys
    import time

    s = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(s, "dead", kind="task", meta={"request_id": "tk-c"})
    _dead_payload(s, original)
    ready, go = tmp_path / "ready", tmp_path / "go"
    child = subprocess.Popen(
        [sys.executable, "-c", _706_PAUSED, str(s.root), original.id, str(ready), str(go)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        deadline = time.monotonic() + 40
        while not ready.is_file():
            assert child.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        assert _requeue(s, original) == 0
        if fate == "compacted":
            _706_compact_away(s, _706_copy_of(s, original))
        else:
            _dead_payload(s, _706_copy_of(s, original))
        before = {m.id for m in s.messages_for("beta")}
        go.write_text("x", encoding="utf-8")
        _out, err = child.communicate(timeout=40)
    finally:
        go.touch(exist_ok=True)
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)

    assert child.returncode == 2 and "already requeued" in err and "Traceback" not in err
    assert {m.id for m in s.messages_for("beta")} == before and not list(s.messages_dir.glob("*.pending"))


def test_706_a_purged_failed_retry_still_blocks_the_replay_of_the_original(tmp_path, monkeypatch, capsys):
    s = _requeue_store(tmp_path, monkeypatch, False)
    original, failed = _706_requeued(s)
    _dead_payload(s, failed)
    assert _requeue(s, failed) == 0
    _706_compact_away(s, _706_copy_of(s, failed))
    assert (
        _run(
            ["dead-letter", "resolve", "--agent", "beta", "--id", failed.id, "--reason", "done", "--from", "lead"],
            s.root,
        )
        == 0
    )
    assert _run(["dead-letter", "purge", "--resolved", "--from", "lead"], s.root) == 0
    assert not (s.dead_letter_dir / "beta" / f"{failed.id}.json").exists()
    capsys.readouterr()

    assert _requeue(s, original) == 2

    err = capsys.readouterr().err
    assert (
        failed.id in err
        and "which is now purged" in err
        and "new request id" in err
        and "dead-letter requeue --agent" not in err
    )
    assert not _706_published(s)


def test_706_a_purge_leaves_the_record_behind(tmp_path, monkeypatch, capsys):
    s = _requeue_store(tmp_path, monkeypatch, False)
    original, live = _706_requeued(s)
    assert (
        _run(
            ["dead-letter", "resolve", "--agent", "beta", "--id", original.id, "--reason", "done", "--from", "lead"],
            s.root,
        )
        == 0
    )
    assert _run(["dead-letter", "purge", "--resolved", "--from", "lead"], s.root) == 0

    assert s._requeue_state_path("beta", original.id).is_file()
    assert [e["message_id"] for e in s.list_dead_letters("beta")] == []  # the record is not a phantom dead letter


def test_706_an_attempt_that_stopped_before_publishing_is_retried_and_the_record_rewritten(
    tmp_path, monkeypatch, capsys
):
    s = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(s, "dead", kind="task", meta={"request_id": "tk-c"})
    _dead_payload(s, original)
    _706_crash_at_publish(monkeypatch, s)
    with pytest.raises(_Crash):
        _requeue(s, original)
    monkeypatch.undo()
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "scratch.key"))
    record = s._requeue_state_path("beta", original.id)
    claimed = json.loads(record.read_text(encoding="utf-8"))
    assert claimed["state"] == "claimed" and claimed["session_id"] == s.load_config()["session_id"]
    assert not _706_published(s)

    assert _requeue(s, original) == 0  # nothing was published, nothing is anywhere, same session: retry proceeds

    (copy,) = _706_published(s)
    rewritten = json.loads(record.read_text(encoding="utf-8"))
    assert rewritten["requeued_as"] == copy.id != claimed["requeued_as"] and rewritten["state"] == "published"
    capsys.readouterr()
    assert _requeue(s, original) == 2  # and now the repeat is refused


@pytest.mark.parametrize("where", ["compacted", "sink", "archive", "quarantine", "quarantine-exact"])
def test_706_a_collision_named_copy_is_found(tmp_path, monkeypatch, capsys, where):
    s = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(s, "dead", kind="task", meta={"request_id": "tk-c"})
    _dead_payload(s, original)
    copy_id = "20261109-000000-000000-Coll"
    _706_record(s, original.id, copy_id)
    folder = {
        "compacted": s.compacted_dir,
        "sink": s.dead_letter_dir / "beta",
        "archive": s.dir / "dead-letter-archive" / "20261109T000000Z" / "beta",
        "quarantine": s.quarantine_dir,
        "quarantine-exact": s.quarantine_dir,
    }[where]
    name = {
        "compacted": f"{copy_id}.json.2026-11-09T00-00-00Z",
        "sink": f"{copy_id}.2026-11-09T00-00-00Z.json",
        "archive": f"{copy_id}.json",
        "quarantine": f"{copy_id}.json.2026-11-09T00-00-00Z",
        "quarantine-exact": f"{copy_id}.json",
    }[where]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text("{}", encoding="utf-8")

    assert _requeue(s, original) == 2

    found_in = {
        "compacted": "compacted away",
        "sink": "dead-lettered",
        "archive": "purged",
        "quarantine": "quarantined",
        "quarantine-exact": "quarantined",
    }[where]
    assert f"{copy_id}, which is now {found_in}" in capsys.readouterr().err and not _706_published(s)


_706_GONE = "20261109-000000-000000-Gone"


def _706_json(drop=(), **changes):
    record = {
        "schema_version": 1,
        "requeued_as": _706_GONE,
        "session_id": "20261109T000000Z",
        "state": "claimed",
        **changes,
    }
    return json.dumps({k: v for k, v in record.items() if k not in drop})


_706_DAMAGE = {
    "empty": "",
    "torn": "{",
    "list": "[]",
    "unknown": '{"other": 1}',
    "schema": _706_json(schema_version=2),
    "target": _706_json(requeued_as="../escape"),
    "no-session": _706_json(drop=("session_id",)),
    "bad-session": _706_json(session_id="../x"),
    "no-state": _706_json(drop=("state",)),
    "bad-state": _706_json(state="done"),
    "self": "SELF",
    "directory": None,
}


@pytest.mark.parametrize("damage", list(_706_DAMAGE))
def test_706_a_damaged_record_refuses_only_its_own_dead_letter(tmp_path, monkeypatch, capsys, damage):
    s = _requeue_store(tmp_path, monkeypatch, False)
    damaged = _send(s, "dead", kind="task", meta={"request_id": "tk-a"})
    _dead_payload(s, damaged)
    path = s._requeue_state_path("beta", damaged.id)
    if _706_DAMAGE[damage] is None:
        path.mkdir()
    elif _706_DAMAGE[damage] == "SELF":  # a record naming its own dead letter would loop
        path.write_text(_706_json(requeued_as=damaged.id), encoding="utf-8")
    else:
        path.write_text(_706_DAMAGE[damage], encoding="utf-8")
    capsys.readouterr()

    assert _requeue(s, damaged) == 2

    err = capsys.readouterr().err
    assert f"{damaged.id}.requeue-state" in err and "nothing was published" in err and "Traceback" not in err
    unreadable = damage in ("empty", "torn", "directory")  # an untrusted record is named as such, not as a lost copy
    assert ("cannot be read" if unreadable else "damaged or not supported") in err
    assert not [m for m in s.messages_for("beta") if m.meta.get("request_id") == "tk-a"]
    other = _send(s, "dead", kind="task", meta={"request_id": "tk-b"})
    _dead_payload(s, other)
    assert _requeue(s, other) == 0


def test_706_an_unreadable_record_refuses_and_publishes_nothing(tmp_path, monkeypatch, capsys):
    s = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(s, "dead", kind="task", meta={"request_id": "tk-c"})
    _dead_payload(s, original)
    state = s._requeue_state_path("beta", original.id)
    state.write_text("{}", encoding="utf-8")
    real = Path.read_text

    def read(path, *a, **k):
        if path == state:
            raise PermissionError("held by another program")
        return real(path, *a, **k)

    monkeypatch.setattr(Path, "read_text", read)

    assert _requeue(s, original) == 2

    assert "cannot be read" in capsys.readouterr().err and not _706_published(s)


def test_706_an_ordinary_orphan_still_requeues_and_gets_a_record(tmp_path, monkeypatch, capsys):
    s = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(s, "dead", kind="task", meta={"request_id": "tk-c"})
    path = _dead_payload(s, original)
    (s.dead_letter_dir / "beta" / f"{original.id}.deadletter.json").unlink()
    before = path.read_bytes()
    assert not s._requeue_state_path("beta", original.id).exists()

    assert _requeue(s, original) == 0

    assert path.read_bytes() == before and s._requeue_state_path("beta", original.id).is_file()


def test_706_the_record_names_the_copy_once_and_ends_in_no_json(tmp_path, monkeypatch, capsys):
    s = _requeue_store(tmp_path, monkeypatch, False)
    original, live = _706_requeued(s)
    path = s._requeue_state_path("beta", original.id)

    assert path.name == f"{original.id}.requeue-state" and not path.name.endswith(".json")
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "requeued_as": live.id,
        "session_id": s.load_config()["session_id"],
        "state": "published",
    }


def test_706_the_lookup_on_large_directories_is_bounded_and_does_not_starve_a_send(tmp_path, monkeypatch, capsys):
    import threading
    import time

    s = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(s, "dead", kind="task", meta={"request_id": "tk-c"})
    _dead_payload(s, original)
    s.compacted_dir.mkdir(parents=True, exist_ok=True)
    for i in range(5000):  # a miss lists the compacted folder, the sink and the purge archive once each
        (s.compacted_dir / f"20200101-000000-{i:06d}-Zzzz.json").write_bytes(b"{}")
        (s.dead_letter_dir / "beta" / f"20200101-000000-{i:06d}-Zzzz.json").write_bytes(b"{}")
    gone = "20261109-000000-000000-Gone"
    started = time.monotonic()
    assert s._find_message("beta", gone) is None
    one_lookup = time.monotonic() - started
    _706_record(s, original.id, gone)
    sent, errors = [], []

    def send():
        try:
            start = time.monotonic()
            _send(s, "meanwhile")
            sent.append(time.monotonic() - start)
        except Exception as exc:  # noqa: BLE001 - the test reports it
            errors.append(exc)

    thread = threading.Thread(target=send)
    thread.start()
    assert _requeue(s, original) == 2  # the lookup runs under the lock; a copy found nowhere is refused
    thread.join(30)

    assert not errors and len(sent) == 1
    print(f"#706 lookup miss over 5000+5000 names: {one_lookup:.3f}s; concurrent send waited {sent[0]:.3f}s")
    assert one_lookup < 5 and sent[0] < 15


@_DL_SIGNED
def test_706_unstamped_copies_without_an_audience_are_refused(tmp_path, monkeypatch, capsys, signed):
    from agenttalk import work_tags

    s = _requeue_store(tmp_path, monkeypatch, signed)
    meta = {"request_id": "tk-noaud"}
    first = _send(s, "go", kind="task", meta=dict(meta))
    survivor = _send(s, "go", recipient="gamma", kind="task", meta=dict(meta))
    _706_strip(s, first, signed), _706_strip(s, survivor, signed)
    path = _dead_payload(s, first)
    survivor_bytes = (s.messages_dir / f"{survivor.id}.json").read_bytes()

    _assert_requeue_refused(s, first, path, capsys, "no vendor stamp; nothing was published.")

    assert (s.messages_dir / f"{survivor.id}.json").read_bytes() == survivor_bytes
    assert len(work_tags._opener(s.valid_messages(), "tk-noaud")) == 1
    assert _706_answer(s, "gamma", survivor).meta["in_reply_to"] == survivor.id


def test_706_a_missing_audience_alone_is_not_a_duplicate(tmp_path, monkeypatch, capsys):
    s = _requeue_store(tmp_path, monkeypatch, False)
    only = _send(s, "go", kind="task", meta={"request_id": "tk-alone"})
    _706_strip(s, only)
    _dead_payload(s, only)

    assert _requeue(s, only) == 0


def test_706_a_missing_audience_does_not_bypass_the_live_copy_check(tmp_path, monkeypatch, capsys):
    s = _requeue_store(tmp_path, monkeypatch, False)
    meta = {"request_id": "tk-noaud"}
    first = _send(s, "go", kind="task", meta=dict(meta))
    _706_strip(s, first)
    _dead_payload(s, first)
    still = _send(s, "go", kind="task", meta=dict(meta))
    _706_strip(s, still)

    assert _requeue(s, first) == 2

    assert "already has a live copy" in capsys.readouterr().err


def test_706_the_unstamped_rule_is_checked_inside_publication(tmp_path, monkeypatch, capsys):
    s = _requeue_store(tmp_path, monkeypatch, False)
    _send(s, "go", recipient="gamma", kind="task", meta={"request_id": "tk-noaud"})

    with pytest.raises(ValueError, match="no vendor stamp"):
        s.send(
            sender="lead",
            recipient="beta",
            kind="task",
            body="go",
            meta={"request_id": "tk-noaud"},
            _refuse_live_request=True,
        )


def test_706_a_folder_the_lookup_cannot_examine_refuses_and_names_it(tmp_path, monkeypatch, capsys):
    from agenttalk import store as store_module

    s = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(s, "dead", kind="task", meta={"request_id": "tk-c"})
    _dead_payload(s, original)
    _706_record(s, original.id, "20261109-000000-000000-Gone")
    s.compacted_dir.mkdir(parents=True, exist_ok=True)
    real = store_module.os.listdir

    def listdir(path, *a, **k):
        if Path(path) == s.compacted_dir:
            raise PermissionError(13, "held by another program", str(path))
        return real(path, *a, **k)

    monkeypatch.setattr(store_module.os, "listdir", listdir)
    capsys.readouterr()

    assert _requeue(s, original) == 2

    err = capsys.readouterr().err
    assert repr(str(s.compacted_dir))[1:-1] in err and "nothing was published" in err and "Traceback" not in err
    assert not _706_published(s)


def _706_record(s, dead_letter_id, target, state="published", session=None):
    """Write a record by hand. "claimed": the requeue named the copy it was going to publish; "published": it did."""
    s._write_requeue_state(
        "beta",
        dead_letter_id,
        {"requeued_as": target, "session_id": session or s.load_config()["session_id"], "state": state},
    )


class _Crash(BaseException):
    pass


def _706_crash_at_publish(monkeypatch, s):
    from agenttalk import store as store_module

    real = store_module.os.replace

    def replace(src, dst, *a, **k):
        if Path(dst).parent == s.messages_dir:
            raise _Crash
        return real(src, dst, *a, **k)

    monkeypatch.setattr(store_module.os, "replace", replace)


def _706_interrupt_reset(s, archive):
    """The first half of reset: live messages cleared or archived, the new session id not yet saved."""
    if archive:
        s._archive_session(s.load_config()["session_id"], subdirs=("messages",))
    else:
        shutil.rmtree(s.messages_dir)
    s.messages_dir.mkdir(exist_ok=True)


@pytest.mark.parametrize("archive", [False, True], ids=["cleared", "archived"])
def test_706_an_interrupted_reset_never_lets_completed_work_replay(tmp_path, monkeypatch, capsys, archive):
    s = _requeue_store(tmp_path, monkeypatch, False)
    original, live = _706_requeued(s)
    _706_answer(s, "beta", live)
    session, record = s.load_config()["session_id"], s._requeue_state_path("beta", original.id)
    before = record.read_bytes()
    _706_interrupt_reset(s, archive)
    assert s.load_config()["session_id"] == session and not _706_published(s)
    capsys.readouterr()

    assert _requeue(s, original) == 2

    assert live.id in capsys.readouterr().err
    assert not _706_published(s) and record.read_bytes() == before


@pytest.mark.parametrize("archive", [False, True], ids=["reset", "reset-archive"])
def test_706_after_a_reset_a_pending_claim_is_still_refused_never_requeued(tmp_path, monkeypatch, capsys, archive):
    s = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(s, "dead", kind="task", meta={"request_id": "tk-c"})
    _dead_payload(s, original)
    _706_crash_at_publish(monkeypatch, s)
    with pytest.raises(_Crash):
        _requeue(s, original)
    monkeypatch.undo()
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "scratch.key"))
    s.reset(archive=archive)
    capsys.readouterr()

    assert _requeue(s, original) == 2

    err = capsys.readouterr().err
    assert "nothing was published" in err and not _706_published(s)
    assert not s.health_path("beta").exists()  # the state a reset leaves
    assert _706_follow_route(s, capsys, err, tmp_path, extra=("--force",)) == 0


def test_706_a_kill_while_the_record_is_being_written_leaves_it_absent(tmp_path, monkeypatch, capsys):
    from agenttalk import store as store_module

    s = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(s, "dead", kind="task", meta={"request_id": "tk-c"})
    _dead_payload(s, original)
    real = store_module._write_text_exclusive

    def write(path, text):
        if ".requeue-state" in Path(path).name:
            Path(path).write_text(text[:5], encoding="utf-8")  # a torn temporary file
            raise _Crash
        return real(path, text)

    monkeypatch.setattr(store_module, "_write_text_exclusive", write)
    with pytest.raises(_Crash):
        _requeue(s, original)
    monkeypatch.undo()
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "scratch.key"))
    assert not s._requeue_state_path("beta", original.id).exists() and not _706_published(s)

    assert _requeue(s, original) == 0


@pytest.mark.parametrize("why", ["rename-blocked", "disk-full"])
def test_706_a_record_that_cannot_be_written_safely_refuses_and_keeps_the_evidence(tmp_path, monkeypatch, capsys, why):
    """The Codex-sandbox mode of the shared writer overwrites the final path directly; a record must not be."""
    import errno

    from agenttalk import _atomic
    from agenttalk import store as store_module

    s = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(s, "dead", kind="task", meta={"request_id": "tk-c"})
    path = _dead_payload(s, original)
    state = s._requeue_state_path("beta", original.id)
    payload_before = path.read_bytes()
    real = store_module.os.replace
    error = (
        PermissionError(5, "Access is denied") if why == "rename-blocked" else OSError(errno.ENOSPC, "No space left")
    )

    def replace(src, dst, *a, **k):
        if Path(dst).name.endswith(".requeue-state"):
            raise error
        return real(src, dst, *a, **k)

    monkeypatch.setattr(store_module.os, "replace", replace)
    monkeypatch.setattr(_atomic, "_sandbox_direct_write", True)
    capsys.readouterr()

    assert _requeue(s, original) == 2

    err = capsys.readouterr().err
    assert "nothing was published" in err and "no record was changed" in err and not _706_published(s)
    assert ("renam" in err) == (why == "rename-blocked")
    assert not state.exists() and not list(state.parent.glob("*.tmp"))  # no record, no temporary file left
    assert path.read_bytes() == payload_before
    monkeypatch.undo()
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "scratch.key"))
    assert _requeue(s, original) == 0  # the way out for a host that cannot keep the record: where it can
    assert _706_follow_route(s, capsys, err, tmp_path, extra=("--force",)) == 0


def test_706_retry_advice_uses_the_resolution_at_the_decision_not_at_the_start(tmp_path, monkeypatch, capsys):
    import subprocess
    import sys
    import time

    s = _requeue_store(tmp_path, monkeypatch, False)
    original, first = _706_requeued(s)
    _dead_payload(s, first)
    ready, go = tmp_path / "ready", tmp_path / "go"
    child = subprocess.Popen(
        [sys.executable, "-c", _706_PAUSED, str(s.root), original.id, str(ready), str(go)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        deadline = time.monotonic() + 40
        while not ready.is_file():
            assert child.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        resolve = ["dead-letter", "resolve", "--agent", "beta", "--id", first.id, "--reason", "done", "--from", "lead"]
        assert _run(resolve, s.root) == 0
        go.write_text("x", encoding="utf-8")
        _out, err = child.communicate(timeout=40)
    finally:
        go.touch(exist_ok=True)
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)

    assert child.returncode == 2 and "already requeued" in err
    assert "dead-letter requeue --agent" not in err and "new request id" in err


def test_706_records_that_name_each_other_are_followed_one_step_and_never_loop(tmp_path, monkeypatch, capsys):
    s = _requeue_store(tmp_path, monkeypatch, False)
    original, first = _706_requeued(s)
    _dead_payload(s, first)
    _706_record(s, first.id, original.id)  # the retry's record names the original: a two-step cycle
    capsys.readouterr()

    assert _requeue(s, original) == 2

    err = capsys.readouterr().err
    assert "already requeued" in err and "RecursionError" not in err and "dead-letter requeue --agent" not in err


def _706_follow_route(s, capsys, err, tmp_path, who="lead", extra=()):
    """Run the commands a refusal names for sending the work again as new work, placeholders filled in.

    It sets nothing up: `agenttalk task` needs the seat to have a health record (a reset removes it), so a caller
    either has made one, or adds the deliberate --force the refusal names, through `extra`."""
    import shlex

    show, send = [shlex.split(line) for line in err.splitlines() if line.startswith("  agenttalk ")]
    capsys.readouterr()
    assert _run(show[1:], s.root) == 0
    body_file = tmp_path / "body.txt"
    body_file.write_text(json.loads(capsys.readouterr().out)["body"], encoding="utf-8")
    fill = {"<body-file>": str(body_file), "<lead-or-liaison>": who, "<you>": who}
    return _run([*[fill.get(arg, arg) for arg in send[1:]], *extra], s.root)


@_DL_KINDS
@_DL_SIGNED
@pytest.mark.parametrize("health", ["after-a-reset", "seat-running"])
def test_706_the_route_a_refusal_names_sends_the_work_again_as_new_work(
    tmp_path, monkeypatch, capsys, kind, signed, health
):
    s = _requeue_store(tmp_path, monkeypatch, signed)
    original = _send(s, "the work", kind=kind, meta={"request_id": "tk-c"})
    _dead_payload(s, original)
    _706_record(s, original.id, _706_GONE)
    if health == "after-a-reset":
        s.reset()  # a reset removes the seat's heartbeat and health record
        assert not s.health_path("beta").exists()
    else:
        s.write_health("beta", {"agenttalk_version": "0.104.0"})  # a seat that is running and has said so
    capsys.readouterr()

    assert _requeue(s, original) == 2

    err = capsys.readouterr().err
    assert "new request id" in err and f"dead-letter show --agent beta --id {original.id} --json" in err
    task_needs_health = kind == "task" and health == "after-a-reset"
    if kind == "task":  # the refusal says what `agenttalk task` needs, and the deliberate way around it
        assert "recorded its health" in err and "start or restart the seat" in err and "--force" in err
    else:
        assert "--force" not in err
    code = _706_follow_route(s, capsys, err, tmp_path)  # the command exactly as printed
    if task_needs_health:
        assert code == 2 and "has not started yet" in capsys.readouterr().err
        assert not [m for m in s.messages_for("beta") if m.body == "the work"]
        code = _706_follow_route(s, capsys, err, tmp_path, extra=("--force",))  # the deliberate lead override
    assert code == 0
    fresh = [m for m in s.messages_for("beta") if m.kind == kind and m.body == "the work"]
    assert len(fresh) == 1 and fresh[0].meta["request_id"] != "tk-c" and "requeued_from" not in fresh[0].meta
    assert _706_answer(s, "beta", fresh[0]).meta["in_reply_to"] == fresh[0].id  # the new work is answerable


# ------------------------------------------------ #725: the printed recovery command is safe and the advice is true
_725_IDS = ["tk; Write-Output SAFE_MARKER; #", "tk with spaces", "tk\nnewline", "tk$(calc)`x", "tk-plain"]


def _725_route_lines(err):
    return [line.strip() for line in err.splitlines() if line.startswith("  agenttalk ")]


def _725_filled(line):
    return line.replace("<you>", "lead").replace("<lead-or-liaison>", "lead").replace("<body-file>", "body.txt")


@pytest.mark.parametrize("request_id", _725_IDS, ids=["separator", "spaces", "newline", "substitution", "plain"])
def test_725_the_new_request_command_is_one_command_and_never_copies_the_old_id(
    tmp_path, monkeypatch, capsys, request_id
):
    import re
    import shlex
    import shutil
    import subprocess

    s = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(s, "the work", kind="review-request", meta={"request_id": request_id})
    _dead_payload(s, original)
    _706_record(s, original.id, _706_GONE)
    capsys.readouterr()

    assert _requeue(s, original) == 2

    err = capsys.readouterr().err
    show, send = _725_route_lines(err)
    assert request_id not in show and request_id not in send and "SAFE_MARKER" not in err
    # every stored value is one literal word; the new id is minted from safe characters only
    assert re.fullmatch(
        r"agenttalk send --from <you> --to beta --kind review-request --meta request_id=tk-[0-9a-f]{12} "
        r"--file <body-file>",
        send,
    )
    for line in (show, send):
        words = shlex.split(_725_filled(line))
        assert words[0] == "agenttalk" and not [w for w in words if any(c in w for c in ";&|`$\n\r#")]
    pwsh = shutil.which("pwsh")
    if pwsh:  # PowerShell's own parser: exactly one statement, no errors
        script = (
            "$e=$null;$t=$null;"
            "$ast=[System.Management.Automation.Language.Parser]::ParseInput($env:CMD,[ref]$t,[ref]$e);"
            '"$($ast.EndBlock.Statements.Count) $($e.Count)"'
        )
        for line in (show, send):
            done = subprocess.run(
                [pwsh, "-NoProfile", "-NonInteractive", "-Command", script],
                env={**os.environ, "CMD": _725_filled(line)},
                capture_output=True,
                text=True,
            )
            assert done.stdout.strip() == "1 0", (line, done.stdout, done.stderr)


def test_725_the_task_command_names_no_request_id_at_all(tmp_path, monkeypatch, capsys):
    s = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(s, "the work", kind="task", meta={"request_id": "tk; Write-Output SAFE_MARKER; #"})
    _dead_payload(s, original)
    _706_record(s, original.id, _706_GONE)
    capsys.readouterr()

    assert _requeue(s, original) == 2

    err = capsys.readouterr().err
    assert "SAFE_MARKER" not in err and "request_id" not in "".join(_725_route_lines(err))


@pytest.mark.parametrize("problem", ["unreadable", "corrupt", "unsupported-shape"])
def test_725_a_copy_that_cannot_be_retried_is_not_advised_as_a_retry(tmp_path, monkeypatch, capsys, problem):
    s = _requeue_store(tmp_path, monkeypatch, False)
    original, first = _706_requeued(s)
    path = _dead_payload(s, first)
    advertised = f"dead-letter requeue --agent beta --id {first.id}"
    if problem == "corrupt":
        path.write_text("{", encoding="utf-8")
    elif problem == "unsupported-shape":
        payload = json.loads(path.read_bytes())
        payload["meta"]["assignee_model_vendors"] = ["not", "a", "map"]
        path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        real = Path.read_bytes

        def read(self, *args, **kwargs):
            if self == path:
                raise PermissionError(13, "held by another program", str(self))
            return real(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_bytes", read)
    capsys.readouterr()

    assert _requeue(s, first) == 2  # the command the old advice named does fail
    capsys.readouterr()
    assert _requeue(s, original) == 2

    err = capsys.readouterr().err
    assert advertised not in err and "Retry that copy" not in err
    assert first.id in err and "needs attention" in err and len(_725_route_lines(err)) == 2


@pytest.mark.parametrize("signed", [True])
def test_725_a_copy_that_fails_its_signature_check_is_not_advised_as_a_retry(tmp_path, monkeypatch, capsys, signed):
    s = _requeue_store(tmp_path, monkeypatch, signed)
    original, first = _706_requeued(s)
    path = _dead_payload(s, first)
    payload = json.loads(path.read_bytes())
    payload["body"] = "edited after signing"
    path.write_text(json.dumps(payload), encoding="utf-8")
    capsys.readouterr()

    assert _requeue(s, first) == 2
    assert "signature check" in capsys.readouterr().err
    assert _requeue(s, original) == 2

    err = capsys.readouterr().err
    assert f"dead-letter requeue --agent beta --id {first.id}" not in err and "needs attention" in err


# ---------------------------- #725 P2-2: the record says claimed or published; quarantine is searched too
def _725_published_then_pruned(s):
    """Copy X is published, then damaged and moved to quarantine by the supported `prune --invalid`."""
    original = _send(s, "the work", kind="task", meta={"request_id": "tk-c"})
    _dead_payload(s, original)
    assert _requeue(s, original) == 0
    copy = _706_copy_of(s, original)
    (s.messages_dir / f"{copy.id}.json").write_text("{", encoding="utf-8")  # a torn copy is delivery-invalid
    assert _run(["prune", "--invalid"], s.root) == 0
    assert (s.quarantine_dir / f"{copy.id}.json").is_file() and not _706_published(s)
    return original, copy


def test_725_a_published_copy_that_prune_moved_to_quarantine_still_refuses_the_retry(tmp_path, monkeypatch, capsys):
    s = _requeue_store(tmp_path, monkeypatch, False)
    original, copy = _725_published_then_pruned(s)
    capsys.readouterr()

    assert _requeue(s, original) == 2

    err = capsys.readouterr().err
    assert f"{copy.id}, which is now quarantined" in err and not _706_published(s)


def test_725_the_published_mark_alone_refuses_it_when_quarantine_is_not_searched(tmp_path, monkeypatch, capsys):
    s = _requeue_store(tmp_path, monkeypatch, False)
    original, copy = _725_published_then_pruned(s)
    monkeypatch.setattr(Store, "quarantine_dir", property(lambda self: self.dir / "no-such-quarantine"))
    capsys.readouterr()

    assert _requeue(s, original) == 2

    err = capsys.readouterr().err
    assert copy.id in err and "which the record says was published" in err and not _706_published(s)
    assert json.loads(s._requeue_state_path("beta", original.id).read_text(encoding="utf-8"))["state"] == "published"


@pytest.mark.parametrize("session", ["same-session", "other-session"])
def test_725_a_published_record_refuses_whatever_the_session_when_the_copy_is_nowhere(
    tmp_path, monkeypatch, capsys, session
):
    s = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(s, "dead", kind="task", meta={"request_id": "tk-c"})
    _dead_payload(s, original)
    _706_record(s, original.id, _706_GONE, "published", None if session == "same-session" else "20200101T000000Z")
    capsys.readouterr()

    assert _requeue(s, original) == 2

    assert "nothing was published" in capsys.readouterr().err and not _706_published(s)


def test_725_a_claimed_record_from_another_session_refuses_even_with_the_copy_nowhere(tmp_path, monkeypatch, capsys):
    s = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(s, "dead", kind="task", meta={"request_id": "tk-c"})
    _dead_payload(s, original)
    _706_record(s, original.id, _706_GONE, "claimed", "20200101T000000Z")
    capsys.readouterr()

    assert _requeue(s, original) == 2

    err = capsys.readouterr().err
    assert "earlier session" in err and "cannot tell" in err and not _706_published(s)


def test_725_a_claimed_record_in_this_session_with_the_copy_nowhere_is_retried(tmp_path, monkeypatch, capsys):
    s = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(s, "dead", kind="task", meta={"request_id": "tk-c"})
    _dead_payload(s, original)
    _706_record(s, original.id, _706_GONE, "claimed")

    assert _requeue(s, original) == 0

    assert len(_706_published(s)) == 1


def _725_unmarked_copy(tmp_path, monkeypatch, s):
    """A requeue that crashed between the publish and the published mark: (original, its live copy)."""
    original = _send(s, "dead", kind="task", meta={"request_id": "tk-c"})
    _dead_payload(s, original)
    real = Store._write_requeue_state

    def write(self, agent, message_id, state):
        if state["state"] == "published":
            raise _Crash
        return real(self, agent, message_id, state)

    monkeypatch.setattr(Store, "_write_requeue_state", write)
    with pytest.raises(_Crash):
        _requeue(s, original)
    monkeypatch.undo()
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "scratch.key"))
    (copy,) = _706_published(s)
    assert json.loads(s._requeue_state_path("beta", original.id).read_text(encoding="utf-8"))["state"] == "claimed"
    return original, copy


@pytest.mark.parametrize("where", ["live", "compacted", "quarantined", "reset-archive", "reset-archive-collision"])
def test_725_a_crash_after_the_publish_but_before_the_mark_refuses(tmp_path, monkeypatch, capsys, where):
    s = _requeue_store(tmp_path, monkeypatch, False)
    original, copy = _725_unmarked_copy(tmp_path, monkeypatch, s)
    if where == "compacted":
        _706_compact_away(s, copy)
    elif where == "quarantined":
        (s.messages_dir / f"{copy.id}.json").write_text("{", encoding="utf-8")
        assert _run(["prune", "--invalid"], s.root) == 0
    elif where.startswith("reset-archive"):  # an interrupted `reset --archive`: the session id is not yet replaced
        if where.endswith("collision"):
            (s.dir / "archived" / s.load_config()["session_id"] / "messages").mkdir(parents=True)
        _706_interrupt_reset(s, True)
        assert not (s.messages_dir / f"{copy.id}.json").exists()
    capsys.readouterr()

    assert _requeue(s, original) == 2

    assert len(_706_published(s)) == (1 if where == "live" else 0)


@pytest.mark.parametrize("unreadable", ["archive-folder", "archived-copy"])
def test_725_a_read_error_while_looking_for_the_copy_refuses_never_republishes(
    tmp_path, monkeypatch, capsys, unreadable
):
    import errno
    import genericpath

    s = _requeue_store(tmp_path, monkeypatch, False)
    original, copy = _725_unmarked_copy(tmp_path, monkeypatch, s)
    session = s.load_config()["session_id"]
    _706_interrupt_reset(s, True)  # the copy is now only in archived/<session>/messages
    target = {
        "archive-folder": s.dir / "archived",
        "archived-copy": s.dir / "archived" / session / "messages" / f"{copy.id}.json",
    }[unreadable]

    def deny(real):
        def call(path, *a, **k):
            if Path(path) == target:
                raise PermissionError(errno.EACCES, "Access is denied", str(path))
            return real(path, *a, **k)

        return call

    for name in ("stat", "listdir"):
        monkeypatch.setattr(os, name, deny(getattr(os, name)))
    for name, plain in (("exists", genericpath.exists), ("is_dir", genericpath.isdir), ("is_file", genericpath.isfile)):
        # the path predicates of Python 3.14 answer False when a read fails
        monkeypatch.setattr(Path, name, lambda self, *a, _f=plain, **k: _f(self))
    capsys.readouterr()

    assert _requeue(s, original) == 2

    err = capsys.readouterr().err
    assert "nothing was published" in err
    assert "could not check the earlier copy or update its record" in err  # not a "record read or write" problem
    assert "Access is denied" in err and target.name in err  # the underlying error and its path stay
    monkeypatch.undo()
    assert not _706_published(s)


@pytest.mark.parametrize("valid", [True, False], ids=["valid", "refused"])
@_DL_SIGNED
def test_725_validating_a_send_takes_no_config_lock_and_writes_nothing(tmp_path, monkeypatch, signed, valid):
    from agenttalk.wrapper.obligations import _roster_snapshot

    s = _requeue_store(tmp_path, monkeypatch, signed)
    meta = {
        "request_id": "tk-origin",
        "origin_request_id": "tk-parent",
        "origin_inbound_id": "inbound-parent",
        "expected_roster_revision": _roster_snapshot(s.load_config())["revision"] if valid else "stale",
    }
    args = {"sender": "beta", "recipient": "lead", "kind": "task", "body": "x", "meta": meta}

    def files():
        return {str(p.relative_to(s.dir)): p.read_bytes() for p in s.dir.rglob("*") if p.is_file()}

    before = files()

    verdicts = []
    for validate_only in (True, False):
        try:
            s.send(**args, _validate_only=validate_only)
            verdicts.append("accepted")
        except ValueError as e:
            verdicts.append(str(e))
        if validate_only:
            assert files() == before  # no config.lock, no generation file, nothing written
    expected = "accepted" if valid else "roster revision changed before authorized transition append"
    assert verdicts == [expected, expected]


def test_725_a_mark_that_cannot_be_written_leaves_the_published_copy_and_a_claimed_record(
    tmp_path, monkeypatch, capsys
):
    import errno

    s = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(s, "dead", kind="task", meta={"request_id": "tk-c"})
    _dead_payload(s, original)
    real = Store._write_requeue_state

    def write(self, agent, message_id, state):
        if state["state"] == "published":
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(self, agent, message_id, state)

    monkeypatch.setattr(Store, "_write_requeue_state", write)
    capsys.readouterr()

    assert _requeue(s, original) == 0  # the publish is not undone

    captured = capsys.readouterr()
    assert "could not be marked published" in captured.err and "claimed" in captured.err
    (copy,) = _706_published(s)
    assert copy.meta["requeued_from"] == original.id
    record = json.loads(s._requeue_state_path("beta", original.id).read_text(encoding="utf-8"))
    assert record["state"] == "claimed" and record["requeued_as"] == copy.id
    monkeypatch.undo()
    monkeypatch.setenv("AGENTTALK_HMAC_KEY_FILE", str(tmp_path / "scratch.key"))
    assert _requeue(s, original) == 2  # the copy is live, so the repeat is refused


# --------------------- #725 round 6: a dead-letter file name is untrusted too, and the preflight is the real one
def _725_parse_one_statement(lines):
    import shutil
    import subprocess

    pwsh = shutil.which("pwsh")
    if not pwsh:
        return
    script = (
        "$e=$null;$t=$null;"
        "$ast=[System.Management.Automation.Language.Parser]::ParseInput($env:CMD,[ref]$t,[ref]$e);"
        '"$($ast.EndBlock.Statements.Count) $($e.Count)"'
    )
    for line in lines:
        done = subprocess.run(
            [pwsh, "-NoProfile", "-NonInteractive", "-Command", script],
            env={**os.environ, "CMD": _725_filled(line)},
            capture_output=True,
            text=True,
        )
        assert done.stdout.strip() == "1 0", (line, done.stdout, done.stderr)


@pytest.mark.parametrize("signed", [False, True], ids=["unsigned", "signed"])
@pytest.mark.parametrize(
    "stem", ["; Write-Output SAFE_MARKER; #", " with spaces", "$(calc)"], ids=["separator", "space", "sub"]
)
def test_725_a_dead_letter_file_name_is_never_printed_as_shell_syntax(tmp_path, monkeypatch, capsys, stem, signed):
    s = _requeue_store(tmp_path, monkeypatch, signed)
    original = _send(s, "the work", kind="review-request", meta={"request_id": "tk-c"})
    path = _dead_payload(s, original)
    odd = f"{original.id}{stem}"
    for sidecar in s.dead_letter_dir.joinpath("beta").glob(f"{original.id}.*"):
        sidecar.rename(sidecar.with_name(sidecar.name.replace(original.id, odd, 1)))
    assert not path.exists() and [e["message_id"] for e in s.list_dead_letters("beta")] == [odd]
    assert _run(["dead-letter", "requeue", "--agent", "beta", "--id", odd], s.root) == 0
    copy = next(m for m in s.messages_for("beta") if m.meta.get("requeued_from") == odd)
    _706_compact_away(s, copy)
    capsys.readouterr()

    assert _run(["dead-letter", "requeue", "--agent", "beta", "--id", odd], s.root) == 2

    err = capsys.readouterr().err
    lines = _725_route_lines(err)
    assert len(lines) == 1 and lines[0].startswith("agenttalk send ")  # no show command: its id is not a plain id
    assert "SAFE_MARKER" not in "".join(lines) and "calc" not in "".join(lines)
    assert "not a plain id" in err and "needs attention" in err
    _725_parse_one_statement(lines)


@pytest.mark.parametrize("problem", ["unsupported-vendor", "audience-disagrees", "unknown-kind", "unregistered-sender"])
def test_725_a_copy_whose_stored_dispatch_the_requeue_refuses_is_not_advised_as_a_retry(
    tmp_path, monkeypatch, capsys, problem
):
    s = _requeue_store(tmp_path, monkeypatch, False)
    original, first = _706_requeued(s)
    path = _dead_payload(s, first)
    payload = json.loads(path.read_bytes())
    if problem == "unsupported-vendor":
        payload["meta"]["assignee_model_vendors"] = {"beta": "nonsense"}
    elif problem == "audience-disagrees":
        payload["meta"]["audience_resolved"] = "beta,gamma"
    elif problem == "unknown-kind":
        payload["kind"] = "nonsense"
    else:
        payload["from"] = "ghost"
    path.write_text(json.dumps(payload), encoding="utf-8")
    capsys.readouterr()

    assert _requeue(s, first) == 2  # the command the old advice named fails
    reason = capsys.readouterr().err
    assert _requeue(s, original) == 2

    err = capsys.readouterr().err
    assert f"dead-letter requeue --agent beta --id {first.id}" not in err and "Retry that copy" not in err
    assert first.id in err and "needs attention" in err and len(_725_route_lines(err)) == 2
    assert reason.split(": ", 1)[1].strip().rstrip(".")[:40] in err  # the same reason the command gives


# ------------------------------------------------ #728: escaped refusal text, one scan of the archived folders
def test_728_escape_controls_writes_every_line_forging_character_as_text():
    from agenttalk.store import escape_controls

    forged = "a\nb\rc\td\x0b\x0c\x1b\x7f\x85\u2028\u2029\u202e\ud800e\\f \U0001f600"
    out = escape_controls(forged)
    assert out == "a\\x0ab\\x0dc\\x09d\\x0b\\x0c\\x1b\\x7f\\x85\\u2028\\u2029\\u202e\\ud800e\\f \U0001f600"
    assert out.isprintable() and len(out.splitlines()) == 1
    assert escape_controls("C:\\store\\café\\ok.json") == "C:\\store\\café\\ok.json"  # a Windows path reads as before
    assert escape_controls(Path("x") / "y") == str(Path("x") / "y")


class _OddPath:
    """A record path whose text holds a line end, as a POSIX file name can."""

    def __init__(self, real, unreadable=False):
        self.real, self.unreadable = real, unreadable

    def __str__(self):
        return f"{self.real}\nFORGED record line"

    def read_text(self, *args, **kwargs):
        if self.unreadable:
            raise OSError("denied\nFORGED error line")
        return self.real.read_text(*args, **kwargs)


@pytest.mark.parametrize("case", ["published-copy-gone", "claimed-earlier-session", "unreadable", "damaged"])
def test_728_the_record_path_and_errors_in_a_refusal_never_hold_a_line_end(tmp_path, monkeypatch, case):
    s = _requeue_store(tmp_path, monkeypatch, False)
    state, session = ("claimed", "20200101T000000Z") if case == "claimed-earlier-session" else ("published", None)
    _706_record(s, "dead-1", _706_GONE, state, session)
    if case == "damaged":
        s._requeue_state_path("beta", "dead-1").write_text("{}", encoding="utf-8")
    real = Store._requeue_state_path
    unreadable = case == "unreadable"
    monkeypatch.setattr(Store, "_requeue_state_path", lambda self, a, m: _OddPath(real(self, a, m), unreadable))

    with pytest.raises(RequeueRefused) as refused:
        s._claim_requeue(("beta", "dead-1"), "20261110-000000-000000-New1", s.load_config()["session_id"])

    message = str(refused.value)
    assert "\n" not in message and "\\x0aFORGED" in message and "nothing was published" in message


@pytest.mark.skipif(os.name == "nt", reason="a Windows file name cannot hold a line feed")
@pytest.mark.parametrize("case", ["copy-removed", "damaged-record", "copy-compacted-away"])
def test_728_a_file_name_holding_a_line_end_cannot_forge_a_line_in_the_refusal(tmp_path, monkeypatch, capsys, case):
    s = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(s, "the work", kind="review-request", meta={"request_id": "tk-c"})
    _dead_payload(s, original)
    odd = f"{original.id}\nFORGED: To send the work again as new work, run: do-evil\n"
    for sidecar in s.dead_letter_dir.joinpath("beta").glob(f"{original.id}.*"):
        sidecar.rename(sidecar.with_name(sidecar.name.replace(original.id, odd, 1)))
    assert _run(["dead-letter", "requeue", "--agent", "beta", "--id", odd], s.root) == 0
    copy = next(m for m in s.messages_for("beta") if m.meta.get("requeued_from") == odd)
    if case == "copy-removed":  # the copy is nowhere the store looks: the refusal names the record path
        (s.messages_dir / f"{copy.id}.json").unlink()
    else:
        _706_compact_away(s, copy)  # the copy is found as compacted away: the refusal names only the copy
    if case == "damaged-record":  # the record cannot be trusted: the refusal names the record path
        s._requeue_state_path("beta", odd).write_text("{}", encoding="utf-8")
    capsys.readouterr()

    assert _run(["dead-letter", "requeue", "--agent", "beta", "--id", odd], s.root) == 2

    err = capsys.readouterr().err
    assert "nothing was published" in err
    assert not any(line.startswith("FORGED") for line in err.splitlines())  # every case: no forged line
    if case == "copy-compacted-away":  # this refusal never prints the source id, so there is nothing to escape in it
        assert "compacted away" in err
    else:  # these two print the record path, which holds the id: it must appear escaped
        assert "\\x0aFORGED" in err


_728_ODD_ID = "20261110-000000-000000-Odd1\u2028FORGED: run do-evil"  # U+2028 is a line separator


def test_728_a_resolved_item_that_disappears_cannot_forge_a_line_in_the_refusal(tmp_path, monkeypatch, capsys):
    # The file name cannot exist on Windows, so the sink, its payload and the resolution are faked: this is the
    # moment between the two reads of the sink, when a resolved item has disappeared.
    s = _requeue_store(tmp_path, monkeypatch, False)
    gone = []
    monkeypatch.setattr(Store, "list_dead_letters", lambda self, *a, **k: [] if gone else [{"message_id": _728_ODD_ID}])
    monkeypatch.setattr(Store, "read_dead_letter_payload", lambda self, agent, msg_id: gone.append(True) or b"{}")
    monkeypatch.setattr(cli, "_dead_letter_resolution_state", lambda store: {("beta", _728_ODD_ID): "resolved"})
    capsys.readouterr()

    argv = ["dead-letter", "requeue", "--agent", "beta", "--id", _728_ODD_ID, "--force-resolved", "--reason", "retry"]
    assert _run([*argv, "--from", "lead"], s.root) == 2

    err = capsys.readouterr().err
    assert "no dead-letter" in err and "\\u2028FORGED" in err
    assert "\u2028" not in err and len(err.splitlines()) == 1


@pytest.mark.skipif(os.name == "nt", reason="a Windows file name cannot hold U+2028")
def test_728_the_requeued_line_never_holds_a_line_separator(tmp_path, monkeypatch, capsys):
    s = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(s, "the work", kind="review-request", meta={"request_id": "tk-c"})
    _dead_payload(s, original)
    odd = f"{original.id}\u2028FORGED: run do-evil"
    for sidecar in s.dead_letter_dir.joinpath("beta").glob(f"{original.id}.*"):
        sidecar.rename(sidecar.with_name(sidecar.name.replace(original.id, odd, 1)))
    capsys.readouterr()

    assert _run(["dead-letter", "requeue", "--agent", "beta", "--id", odd], s.root) == 0

    out = capsys.readouterr().out
    assert out.startswith("requeued dead-letter ") and "\\u2028FORGED" in out
    assert "\u2028" not in out and len(out.splitlines()) == 1


def test_728_a_failed_mark_warning_never_holds_a_line_end(tmp_path, monkeypatch, capsys):
    s = _requeue_store(tmp_path, monkeypatch, False)
    original = _send(s, "dead", kind="task", meta={"request_id": "tk-c"})
    _dead_payload(s, original)
    real = Store._write_requeue_state

    def write(self, agent, message_id, state):
        if state["state"] == "published":
            raise OSError("no room\nFORGED line")
        return real(self, agent, message_id, state)

    monkeypatch.setattr(Store, "_write_requeue_state", write)
    capsys.readouterr()

    assert _requeue(s, original) == 0

    err = capsys.readouterr().err
    assert "no room\\x0aFORGED line" in err and not any(line.startswith("FORGED") for line in err.splitlines())


def test_728_the_archived_folders_are_listed_once_and_a_reset_archived_copy_is_still_found(tmp_path, monkeypatch):
    s = _requeue_store(tmp_path, monkeypatch, False)
    kept = _send(s, "kept by a reset")
    s.compacted_dir.mkdir(parents=True, exist_ok=True)  # `archived/compacted`, a child of `archived`
    _706_interrupt_reset(s, True)  # kept now lives in archived/<session>/messages
    listed = []
    real = os.listdir
    monkeypatch.setattr(os, "listdir", lambda p=".": (listed.append(Path(p)), real(p))[1])

    assert s._find_message("beta", "20261110-000000-000000-Nowh") is None

    assert listed.count(s.compacted_dir) == 1  # once as the compacted folder, not again as an archived child
    assert s._find_message("beta", kept.id) == "archived by a reset"


_706_PAUSED = r"""
import json
import sys
import time
from pathlib import Path
from agenttalk import cli
from agenttalk import store as store_module

root, original, ready, go = sys.argv[1:]
real_write = store_module._write_text_exclusive
def write(path, text):
    identity = real_write(path, text)
    if Path(path).name.endswith(".pending") and json.loads(text)["meta"].get("requeued_from") == original:
        Path(ready).write_text("paused", encoding="utf-8")
        deadline = time.monotonic() + 40
        while not Path(go).exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("not released")
            time.sleep(0.01)
    return identity
store_module._write_text_exclusive = write
raise SystemExit(cli.main(["--root", root, "dead-letter", "requeue", "--agent", "beta", "--id", original]))
"""
