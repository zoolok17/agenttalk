"""#154: wrapped seats survive store-lock contention.

Store lock contention (another live holder keeping a store lock past the
acquisition deadline) used to escape the continuous wrapper loop as a
TimeoutError, which ``cmd_wrap`` maps to exit 2. The loop now retries it IN
PLACE, one idempotent store step at a time, only before admission and - after a
completed child turn - only for publication and cursor finalization. These tests
hold deterministic lock barriers past the deadline and release them, with a
counted fake driver: ONE drive, ONE publication, the right cursor, a live
wrapper, visible degradation, then recovery. Corruption, access denial, an
unsafe generation change, a lock-order inversion and ownership loss stay fatal.
"""

from __future__ import annotations

import collections
import contextlib
import inspect
import io
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from agenttalk import cli, reply_transport
from agenttalk import health as health_model
from agenttalk import store as store_mod
from agenttalk.store import LockContention, Store
from agenttalk.wrapper import loop, obligations
from agenttalk.wrapper.health import WrapperHealthWriter
from agenttalk.wrapper.obligations import (
    DETECTION_GRADE,
    DetectionCommitGate,
    PolicySnapshot,
)
from agenttalk.wrapper_logs import WrapperLifecycleLog

BOUND = loop.LOCK_CONTENTION_DIAGNOSTIC_AFTER_ATTEMPTS


# ------------------------------------------------------------------ fixtures


class LockBarrier:
    """Deterministic lock barrier.

    While armed on a lock name, each acquisition of that lock raises exactly what
    the store raises once a real holder outlasts the acquisition deadline
    (LockContention, same message shape) - without the 10 s wait - and the
    barrier releases itself after ``hold`` contended acquisitions. ``skip`` lets
    that many acquisitions through first, to target a later site. ``error``
    replaces contention with a non-contention failure for the fatal-path proofs.
    The real-holder tests below prove the store itself raises LockContention.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.plans: dict[str, dict] = {}
        self.hits: collections.Counter[str] = collections.Counter()
        barrier = self
        orig_unordered = Store._exclusive_lock_unordered
        orig_publication = Store._message_publication_lock
        orig_retirement = Store._retirement_lock

        @contextlib.contextmanager
        def unordered(store_self, lock, *, timeout=10.0, poll=0.05, what="lock"):
            barrier._gate(lock.name, what, lock, timeout)
            with orig_unordered(store_self, lock, timeout=timeout, poll=poll, what=what):
                yield

        @contextlib.contextmanager
        def publication(store_self, *, timeout=10.0, poll=0.005):
            barrier._gate(
                "message-publication",
                "message publication",
                store_self.dir / "message-publication",
                timeout,
            )
            with orig_publication(store_self, timeout=timeout, poll=poll):
                yield

        @contextlib.contextmanager
        def retirement(store_self, *, timeout=10.0, poll=0.005):
            barrier._gate(
                "retirement",
                "retirement/message publication",
                store_self.dir / "retirement",
                timeout,
            )
            with orig_retirement(store_self, timeout=timeout, poll=poll):
                yield

        monkeypatch.setattr(Store, "_exclusive_lock_unordered", unordered)
        monkeypatch.setattr(Store, "_message_publication_lock", publication)
        monkeypatch.setattr(Store, "_retirement_lock", retirement)

    def arm(self, name: str, *, hold: int, skip: int = 0,
            error: BaseException | None = None) -> None:
        self.plans[name] = {"hold": hold, "skip": skip, "error": error}

    def remaining(self, name: str) -> int:
        plan = self.plans.get(name)
        return 0 if plan is None else plan["hold"]

    def _gate(self, name: str, what: str, path: Path, timeout: float) -> None:
        plan = self.plans.get(name)
        if plan is None or plan["hold"] <= 0:
            return
        if plan["skip"] > 0:
            plan["skip"] -= 1
            return
        plan["hold"] -= 1
        self.hits[name] += 1
        if plan["error"] is not None:
            raise plan["error"]
        raise LockContention(
            f"could not acquire the {what} at {path} within {timeout:g}s",
            what=what,
            lock_file=path.name,
        )


class CountedDriver:
    """The paid child turn, counted. Writes a reply draft unless ``reply`` is None;
    ``on_complete`` runs as the child finishes (arms a post-completion barrier).
    A second invocation fails at once: re-entering the paid driver for a
    completed turn is the defect under test, never an allowed recovery."""

    def __init__(self, *, reply: str | None = "the answer", on_complete=None) -> None:
        self.calls = 0
        self.reply = reply
        self.on_complete = on_complete

    def __call__(self, rec: dict) -> bool:
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("the paid driver was re-entered for a completed turn")
        if self.reply is not None:
            Path(rec["reply_draft"]["path"]).write_text(self.reply, encoding="utf-8")
        if self.on_complete is not None:
            self.on_complete()
        return True


class Health:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []
        self.diagnostics: list[dict] = []

    def idle(self, **kwargs) -> None:
        self.events.append(("idle", kwargs.get("reason_code")))

    def contended(self, phase: str) -> None:
        self.events.append(("contended", phase))

    def persisting(self, info: dict) -> None:
        self.diagnostics.append(dict(info))

    def contended_phases(self) -> list[object]:
        return [value for name, value in self.events if name == "contended"]

    def recovered(self) -> bool:
        """A non-contended health write follows the last contended one."""
        last = max(i for i, (name, _) in enumerate(self.events) if name == "contended")
        return any(name == "idle" for name, _ in self.events[last + 1:])


def _bus(tmp_path: Path, *, kind: str = "question"):
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    s.set_role("alpha", "lead")
    meta = {"request_id": "q-154"} if kind == "question" else {}
    m = s.send(sender="alpha", recipient="beta", kind=kind, body="please answer", meta=meta)
    return s, m


def _inactive_gate(s: Store) -> DetectionCommitGate:
    # Every live seat: no AGENTTALK_COMMIT_GATE_POLICY configured.
    return DetectionCommitGate(s, "beta", PolicySnapshot.inactive(agent="beta"),
                               fence="wrapper-1")


def _active_gate(s: Store, *, fence: str = "wrapper-1") -> DetectionCommitGate:
    policy = PolicySnapshot.from_mapping(
        {"schema_version": 1, "agents": {"beta": {"grade": DETECTION_GRADE}}}, "beta")
    return DetectionCommitGate(s, "beta", policy, fence=fence)


def _replies(s: Store) -> list:
    return [m for m in s.messages_for("alpha")
            if m.sender == "beta" and m.meta.get("reply_refusal") != "true"]


def _run(s: Store, gate, driver, health: Health, **kwargs) -> int:
    kwargs.setdefault("sleep", lambda _d: None)
    kwargs.setdefault("max_turns", 1)
    return loop.run_loop(
        s, "beta", driver,
        clock=lambda: 0.0,
        commit_gate=gate,
        on_health_idle=health.idle,
        on_health_contention=health.contended,
        on_contention_persisting=health.persisting,
        **kwargs,
    )


# ------------------------------------------------ the store's error taxonomy


def test_real_holder_past_deadline_raises_lock_contention_on_the_generation_guard(
    tmp_path,
) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    holding, release = threading.Event(), threading.Event()

    def hold() -> None:
        with Store(tmp_path)._message_publication_lock():
            holding.set()
            release.wait(10)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    try:
        assert holding.wait(5)
        with pytest.raises(LockContention) as err:
            with s._message_publication_lock(timeout=0.1):
                pass
    finally:
        release.set()
        t.join(5)
    assert isinstance(err.value, TimeoutError)   # existing except-TimeoutError boundaries hold
    assert err.value.what == "message publication"
    assert err.value.lock_file == ".message-publication.generation"
    with s._message_publication_lock(timeout=0.5):   # released -> acquirable again
        pass


def test_real_holder_past_deadline_raises_lock_contention_on_the_ownership_marker(
    tmp_path,
) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    lock = s.state_dir / "owed-action" / "ledger.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    holding, release = threading.Event(), threading.Event()

    def hold() -> None:
        with Store(tmp_path)._exclusive_lock(lock, timeout=5.0, what="ledger lock"):
            holding.set()
            release.wait(10)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    try:
        assert holding.wait(5)
        with pytest.raises(LockContention) as err:
            with s._exclusive_lock(lock, timeout=0.1, what="ledger lock"):
                pass
    finally:
        release.set()
        t.join(5)
    assert err.value.what == "ledger lock"
    assert err.value.lock_file == "ledger.lock"


def test_access_denied_generation_guard_is_not_contention(tmp_path, monkeypatch) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    real_open = store_mod.os.open

    def denied(path, *args, **kwargs):
        if str(path).endswith(".message-publication.generation"):
            raise PermissionError(13, "access denied", str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(store_mod.os, "open", denied)
    with pytest.raises(TimeoutError) as err:
        with s._message_publication_lock(timeout=0.05):
            pass
    assert not isinstance(err.value, LockContention)
    assert "could not open the generation guard" in str(err.value)


def test_unsafe_generation_change_is_not_contention(tmp_path, monkeypatch) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    monkeypatch.setattr(store_mod, "_same_file", lambda _a, _b: False)
    with pytest.raises(OSError) as err:
        with s._message_publication_lock(timeout=0.5):
            pass
    assert not isinstance(err.value, LockContention)
    assert "generation changed" in str(err.value)


def test_lock_order_inversion_is_not_contention(tmp_path) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    with pytest.raises(TimeoutError) as err:
        with s._message_publication_lock(), s._retirement_lock():
            pass
    assert not isinstance(err.value, LockContention)
    assert "lock order inversion" in str(err.value)


# ------------------------------------------ recovery: before admission


# Every lock acquisition the live (inactive-policy) path makes before a turn is
# admitted, in order: the gate's replay indexing (ledger), the landed-response
# replay (publication), then the drive authorization's publication lock, replay
# indexing and claim (ledger). ``skip`` walks the barrier onto each one; the
# diagnostic names the gate's reason where the gate folds the contention into a
# result, and the store's own lock where it propagates.
_ADMISSION_SITES = [
    pytest.param("ledger.lock", 0,
                 (obligations.LEDGER_REPLAY_CONTENDED, None), id="admission-replay-index"),
    pytest.param("message-publication", 0,
                 (obligations.LANDED_REPLAY_CONTENDED, None), id="landed-replay"),
    pytest.param("message-publication", 1,
                 ("message publication", "message-publication"), id="authorization-publication"),
    pytest.param("ledger.lock", 1,
                 (obligations.LEDGER_REPLAY_CONTENDED, None), id="authorization-replay-index"),
    # The claim contends first, but each retry re-runs the WHOLE authorization, so
    # by the bound the barrier meets its replay indexing first.
    pytest.param("ledger.lock", 2,
                 (obligations.LEDGER_REPLAY_CONTENDED, None), id="authorization-claim"),
]


@pytest.mark.parametrize(("lock_name", "skip", "diagnosed"), _ADMISSION_SITES)
def test_contention_before_admission_is_retried_then_recovers(
    tmp_path, monkeypatch, lock_name, skip, diagnosed,
) -> None:
    s, m = _bus(tmp_path)
    barrier = LockBarrier(monkeypatch)
    barrier.arm(lock_name, skip=skip, hold=BOUND + 1)
    held_at_drive: list[int] = []

    def drive(rec):
        held_at_drive.append(barrier.remaining(lock_name))
        return driver_impl(rec)

    driver_impl = CountedDriver()
    health = Health()
    turns = _run(s, _inactive_gate(s), drive, health)

    assert turns == 1                                   # a live wrapper, not an exit
    assert driver_impl.calls == 1 and held_at_drive == [0]   # driven once, after release
    assert barrier.hits[lock_name] == BOUND + 1
    assert len(_replies(s)) == 1
    assert s.cursor("beta") == m.id
    assert health.contended_phases() == [loop.LOCK_CONTENTION_PHASE_ADMISSION] * (BOUND + 1)
    assert health.recovered()
    assert health.diagnostics == [{
        "phase": loop.LOCK_CONTENTION_PHASE_ADMISSION,
        "lock": diagnosed[0],
        "lock_file": diagnosed[1],
        "attempts": BOUND,
    }]


def test_contended_inbox_peek_is_retried_in_place(tmp_path, monkeypatch) -> None:
    # next_record takes no store lock today; the admission wrap still covers it,
    # so a future lock-taking inbox read cannot reintroduce the exit.
    s, m = _bus(tmp_path)
    real_next = loop.recv_api.next_record
    calls = {"n": 0}

    def contended_peek(store, agent, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise LockContention("could not acquire the inbox", what="inbox")
        return real_next(store, agent, **kwargs)

    monkeypatch.setattr(loop.recv_api, "next_record", contended_peek)
    driver = CountedDriver()
    health = Health()
    assert _run(s, _inactive_gate(s), driver, health) == 1
    assert driver.calls == 1
    assert health.contended_phases()[:2] == [loop.LOCK_CONTENTION_PHASE_ADMISSION] * 2
    assert s.cursor("beta") == m.id


# ------------------------------------------ recovery: after the child turn


def test_contention_after_child_completion_is_retried_without_redriving(
    tmp_path, monkeypatch,
) -> None:
    s, m = _bus(tmp_path)
    barrier = LockBarrier(monkeypatch)
    driver = CountedDriver(on_complete=lambda: barrier.arm(
        "operation-publication.lock", hold=BOUND + 2))
    health = Health()
    turns = _run(s, _inactive_gate(s), driver, health)

    assert turns == 1
    assert driver.calls == 1                            # never re-enters the paid driver
    assert barrier.hits["operation-publication.lock"] == BOUND + 2
    assert len(_replies(s)) == 1                        # ONE publication
    assert s.cursor("beta") == m.id
    assert set(health.contended_phases()) == {loop.LOCK_CONTENTION_PHASE_PUBLICATION}
    assert health.recovered()
    assert [d["phase"] for d in health.diagnostics] == [loop.LOCK_CONTENTION_PHASE_PUBLICATION]
    assert not list(reply_transport.reply_draft_path(s, "beta", m.id).parent.glob("*.refused*"))


def test_contention_around_publication_reuses_one_operation_identity(
    tmp_path, monkeypatch,
) -> None:
    # The first contended acquisition here is INSIDE send_operation, after its
    # operation intent was recorded: a retry under a fresh nonce would orphan it.
    s, m = _bus(tmp_path)
    barrier = LockBarrier(monkeypatch)
    driver = CountedDriver(on_complete=lambda: barrier.arm("message-publication", hold=3))
    health = Health()
    turns = _run(s, _inactive_gate(s), driver, health)

    assert turns == 1 and driver.calls == 1
    replies = _replies(s)
    assert len(replies) == 1
    intents = sorted((s.state_dir / "operation-intents").glob("beta.*.json"))
    assert len(intents) == 1                            # one identity across 4 attempts
    intent = json.loads(intents[0].read_text(encoding="utf-8"))
    assert intent["state"] == "published"
    assert intent["message_id"] == replies[0].id
    assert replies[0].meta["operation_nonce"] == intent["operation_nonce"]
    assert s.cursor("beta") == m.id
    assert health.contended_phases() == [loop.LOCK_CONTENTION_PHASE_PUBLICATION] * 3
    assert health.diagnostics == []                     # below the bound: no diagnostic


def test_publication_that_landed_before_its_release_timed_out_is_not_duplicated(
    tmp_path, monkeypatch,
) -> None:
    # A publication can land and THEN report contention (the ownership lock's
    # release re-enters its generation guard). The retry must dedupe on the SAME
    # nonce instead of publishing the answer twice.
    s, m = _bus(tmp_path)
    real_send_operation = s.send_operation
    nonces: list[str] = []

    def landed_then_contended(**kwargs):
        nonces.append(kwargs["operation_nonce"])
        result = real_send_operation(**kwargs)
        if len(nonces) == 1:
            raise LockContention("could not acquire the generation guard for wrapper "
                                 "operation publication", what="wrapper operation publication")
        return result

    monkeypatch.setattr(s, "send_operation", landed_then_contended)
    driver = CountedDriver()
    health = Health()
    turns = _run(s, _inactive_gate(s), driver, health)

    assert turns == 1 and driver.calls == 1
    assert len(nonces) == 2 and nonces[0] == nonces[1]  # a STABLE operation identity
    assert len(_replies(s)) == 1
    assert s.cursor("beta") == m.id
    assert health.contended_phases() == [loop.LOCK_CONTENTION_PHASE_PUBLICATION]


def test_contention_around_finalization_is_retried_visibly(tmp_path, monkeypatch) -> None:
    # skip=1 lets the reply's own publication through; the NEXT publication-lock
    # acquisition is the landed-response replay that proves completion.
    s, m = _bus(tmp_path)
    barrier = LockBarrier(monkeypatch)
    driver = CountedDriver(on_complete=lambda: barrier.arm(
        "message-publication", skip=1, hold=BOUND))
    health = Health()
    turns = _run(s, _inactive_gate(s), driver, health)

    assert turns == 1 and driver.calls == 1
    assert barrier.hits["message-publication"] == BOUND
    assert len(_replies(s)) == 1
    assert s.cursor("beta") == m.id
    assert health.contended_phases() == [loop.LOCK_CONTENTION_PHASE_FINALIZATION] * BOUND
    assert health.recovered()
    assert [d["phase"] for d in health.diagnostics] == [loop.LOCK_CONTENTION_PHASE_FINALIZATION]


# An active gate with a not-owed head and no reply: the durable completion proof
# is the retained no-admission success, then the ledger-guarded cursor. After the
# child completes, the ledger is taken by: the retention's replay indexing (0) and
# claim (1), then the finalizer's replay indexing (2) and disposition (3).
@pytest.mark.parametrize("skip", [0, 1, 2, 3],
                         ids=["retention-index", "retention-claim",
                              "finalize-index", "finalize-disposition"])
def test_ledger_finalization_contention_retains_success_and_never_redrives(
    tmp_path, monkeypatch, skip,
) -> None:
    s, m = _bus(tmp_path, kind="message")
    barrier = LockBarrier(monkeypatch)
    driver = CountedDriver(reply=None, on_complete=lambda: barrier.arm(
        "ledger.lock", skip=skip, hold=BOUND + 1))
    health = Health()
    turns = _run(s, _active_gate(s), driver, health)

    assert turns == 1 and driver.calls == 1
    assert barrier.hits["ledger.lock"] == BOUND + 1
    assert s.cursor("beta") == m.id
    assert _replies(s) == []
    assert set(health.contended_phases()) == {loop.LOCK_CONTENTION_PHASE_FINALIZATION}
    assert health.recovered()
    assert len(health.diagnostics) == 1


@pytest.mark.parametrize(("lock_name", "skip"), [
    ("message-publication", 2),    # the retention's replay (after send + landed replay)
    ("ledger.lock", 1),            # the retention's replay indexing (after send's hook)
    ("ledger.lock", 2),            # the retention's claim read
], ids=["retention-publication", "retention-index", "retention-claim"])
def test_landed_reply_retention_contention_is_retried_visibly(
    tmp_path, monkeypatch, lock_name, skip,
) -> None:
    # An active gate, a not-owed task answered through the draft: finalization
    # RETAINS the landed reply as the completion proof before the cursor moves.
    s, m = _bus(tmp_path, kind="task")
    barrier = LockBarrier(monkeypatch)
    driver = CountedDriver(on_complete=lambda: barrier.arm(lock_name, skip=skip, hold=BOUND))
    health = Health()
    turns = _run(s, _active_gate(s), driver, health)

    assert turns == 1 and driver.calls == 1
    assert barrier.hits[lock_name] == BOUND
    assert len(_replies(s)) == 1
    assert s.cursor("beta") == m.id
    assert health.contended_phases() == [loop.LOCK_CONTENTION_PHASE_FINALIZATION] * BOUND
    assert health.diagnostics[0]["lock"] == obligations.LANDED_RETENTION_CONTENDED


def test_one_diagnostic_per_persistent_episode(tmp_path, monkeypatch) -> None:
    s, m = _bus(tmp_path)
    barrier = LockBarrier(monkeypatch)
    barrier.arm("ledger.lock", hold=3 * BOUND)
    health = Health()
    assert _run(s, _inactive_gate(s), CountedDriver(), health) == 1
    assert len(health.contended_phases()) == 3 * BOUND
    assert len(health.diagnostics) == 1                 # not one per attempt
    assert s.cursor("beta") == m.id


def test_contention_below_the_bound_emits_no_diagnostic(tmp_path, monkeypatch) -> None:
    s, m = _bus(tmp_path)
    barrier = LockBarrier(monkeypatch)
    barrier.arm("ledger.lock", hold=BOUND - 1)
    health = Health()
    assert _run(s, _inactive_gate(s), CountedDriver(), health) == 1
    assert len(health.contended_phases()) == BOUND - 1
    assert health.diagnostics == []


def test_contended_wrapper_keeps_its_heartbeat_fresh(tmp_path, monkeypatch) -> None:
    s, _m = _bus(tmp_path)
    barrier = LockBarrier(monkeypatch)
    barrier.arm("ledger.lock", hold=4)
    stamps: list[int] = []

    def heartbeat() -> None:
        stamps.append(barrier.remaining("ledger.lock"))

    _run(s, _inactive_gate(s), CountedDriver(), Health(), heartbeat=heartbeat)
    assert stamps[:4] == [3, 2, 1, 0]                   # one stamp per contended attempt


def test_contention_backoff_is_bounded_and_jittered() -> None:
    for attempt in range(1, 40):
        ceiling = min(loop.LOCK_CONTENTION_BACKOFF_CAP_SECONDS,
                      loop.LOCK_CONTENTION_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
        for _ in range(20):
            delay = loop._contention_backoff(attempt)
            assert ceiling / 2 <= delay <= ceiling
    assert loop.LOCK_CONTENTION_BACKOFF_CAP_SECONDS <= loop.HEARTBEAT_INTERVAL_SECONDS
    assert len({loop._contention_backoff(4) for _ in range(50)}) > 1


def test_contention_backoff_sleeps_between_attempts(tmp_path, monkeypatch) -> None:
    s, _m = _bus(tmp_path)
    barrier = LockBarrier(monkeypatch)
    barrier.arm("ledger.lock", hold=3)
    sleeps: list[float] = []
    _run(s, _inactive_gate(s), CountedDriver(), Health(), sleep=sleeps.append)
    base = loop.LOCK_CONTENTION_BACKOFF_BASE_SECONDS
    assert base / 2 <= sleeps[0] <= base
    assert base <= sleeps[1] <= 2 * base
    assert 2 * base <= sleeps[2] <= 4 * base


# ------------------------------------------------ what stays fatal


# Before admission, the first publication-lock acquisition is the landed-response
# replay (the gate folds ANY failure there into "unavailable": a re-peek); skip=1
# targets the next one - the drive authorization - which reaches the loop.
_ADMISSION_FATAL_ERRORS = [
    pytest.param(ValueError("owed-action ledger is corrupt"), id="corruption"),
    pytest.param(TimeoutError("could not open the generation guard for message "
                              "publication"), id="access-denial"),
    pytest.param(OSError("unsafe lock path message-publication: generation changed "
                         "while locking"), id="unsafe-generation-change"),
    pytest.param(TimeoutError("lock order inversion: message-publication rank 160 "
                              "after 170"), id="lock-order-inversion"),
]


@pytest.mark.parametrize("error", _ADMISSION_FATAL_ERRORS)
def test_non_contention_lock_failure_before_admission_stays_fatal(
    tmp_path, monkeypatch, error,
) -> None:
    s, _m = _bus(tmp_path)
    barrier = LockBarrier(monkeypatch)
    barrier.arm("message-publication", skip=1, hold=5, error=error)
    driver = CountedDriver()
    health = Health()
    with pytest.raises(type(error)) as err:
        _run(s, _inactive_gate(s), driver, health)
    assert err.value is error
    assert barrier.hits["message-publication"] == 1     # never retried
    assert driver.calls == 0
    assert s.cursor("beta") == ""
    assert health.contended_phases() == []
    assert health.diagnostics == []


def test_gate_folded_corruption_is_never_labelled_contention(tmp_path, monkeypatch) -> None:
    # Inside the commit gate, a corrupt read during replay indexing keeps its
    # pre-existing fail-closed BLOCKED result (a re-peek, recorded as a proof
    # failure); it must never take the contention path or its health.
    s, m = _bus(tmp_path)
    barrier = LockBarrier(monkeypatch)
    barrier.arm("ledger.lock", hold=2, error=ValueError("owed-action ledger is corrupt"))
    seen_proof_failures: list[str] = []
    gate = _inactive_gate(s)
    real_record = gate.record_proof_failure

    def record(**kwargs):
        seen_proof_failures.append(kwargs["error_class"])
        return real_record(**kwargs)

    monkeypatch.setattr(gate, "record_proof_failure", record)
    health = Health()
    assert _run(s, gate, CountedDriver(), health) == 1
    assert barrier.hits["ledger.lock"] == 2
    assert seen_proof_failures == ["LedgerUnreadable", "LedgerUnreadable"]
    assert health.contended_phases() == []
    assert s.cursor("beta") == m.id


def test_unsafe_generation_change_after_completion_stays_fatal(
    tmp_path, monkeypatch,
) -> None:
    s, _m = _bus(tmp_path, kind="message")
    barrier = LockBarrier(monkeypatch)
    # skip=1: the landed-response replay folds failures into "unavailable"; the
    # next acquisition is the no-admission success retention, which reaches the loop.
    driver = CountedDriver(reply=None, on_complete=lambda: barrier.arm(
        "message-publication", skip=1, hold=5, error=OSError(
            "unsafe lock path message-publication: generation changed while locking")))
    health = Health()
    with pytest.raises(OSError, match="generation changed"):
        _run(s, _active_gate(s), driver, health)
    assert barrier.hits["message-publication"] == 1
    assert driver.calls == 1
    assert s.cursor("beta") == ""                       # never advanced without proof
    assert health.contended_phases() == []


def test_ownership_loss_at_the_cursor_boundary_stays_fatal(tmp_path) -> None:
    s, _m = _bus(tmp_path)
    driver = CountedDriver()

    def lost() -> None:
        raise cli._LeadLoopLeaseLost("beta")

    with pytest.raises(cli._LeadLoopLeaseLost):
        _run(s, _inactive_gate(s), driver, Health(), pre_commit=lost)
    assert driver.calls == 1
    assert s.cursor("beta") == ""


def test_ownership_loss_while_contended_stays_fatal(tmp_path, monkeypatch) -> None:
    # The backoff stamps the heartbeat; for a managed lead-loop that renews the
    # lease. A LOST lease must end the loop even mid-retry - never become a retry.
    s, _m = _bus(tmp_path)
    barrier = LockBarrier(monkeypatch)
    driver = CountedDriver(on_complete=lambda: barrier.arm(
        "operation-publication.lock", hold=10))
    stamped = {"n": 0}

    def heartbeat() -> None:
        if barrier.remaining("operation-publication.lock") < 10:
            stamped["n"] += 1
            raise cli._LeadLoopLeaseLost("beta")

    with pytest.raises(cli._LeadLoopLeaseLost):
        _run(s, _inactive_gate(s), driver, Health(), heartbeat=heartbeat)
    assert stamped["n"] == 1
    assert barrier.hits["operation-publication.lock"] == 1
    assert driver.calls == 1
    assert _replies(s) == []
    assert s.cursor("beta") == ""


def test_unexpected_publish_error_keeps_the_never_raise_contract(
    tmp_path, monkeypatch,
) -> None:
    # A programming error before send_operation (not a refusal, not contention)
    # is swallowed exactly as without a retry: attempted once, no refusal, the
    # draft and any preserved progress untouched.
    s, m = _bus(tmp_path)
    calls = {"n": 0}

    def broken_digest(*args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("digest bug")

    monkeypatch.setattr(reply_transport, "operation_digest_for", broken_digest)
    record = loop._with_reply_draft(
        s, "beta", {"id": m.id, "from": "alpha", "kind": "question", "meta": {}})
    draft = Path(record["reply_draft"]["path"])
    draft.write_text("answer", encoding="utf-8")
    interrupted = loop._interrupted_draft_path(s, "beta", m.id)
    interrupted.write_text("earlier progress", encoding="utf-8")
    retried: list[str] = []

    def retry(phase, step, *args, **kwargs):
        retried.append(phase)
        return step(*args, **kwargs)

    assert loop._deliver_reply_draft(s, "beta", record, retry=retry) is None
    assert retried == [loop.LOCK_CONTENTION_PHASE_PUBLICATION]
    assert calls["n"] == 1
    assert draft.read_text(encoding="utf-8") == "answer"
    assert not reply_transport.refused_reason_path(draft).exists()
    assert interrupted.is_file()
    assert _replies(s) == []


def test_publication_failure_that_is_not_contention_is_refused_once(
    tmp_path, monkeypatch,
) -> None:
    # Unchanged #162 contract: a non-contention publish failure is refused LOUDLY
    # (preserved draft + lead notice), never retried.
    s, m = _bus(tmp_path)
    calls = {"n": 0}

    def denied(**kwargs):
        calls["n"] += 1
        raise PermissionError(13, "access denied")

    monkeypatch.setattr(s, "send_operation", denied)
    health = Health()
    assert _run(s, _inactive_gate(s), CountedDriver(), health) == 1
    assert calls["n"] == 1
    assert health.contended_phases() == []
    from agenttalk import reply_refusals
    assert [r["original_message_id"] for r in reply_refusals.list_reply_refusals(s, "beta")] == [m.id]


# ------------------------------------------------ a real holder, end to end


def test_real_publication_lock_held_past_the_deadline_then_released(
    tmp_path, monkeypatch,
) -> None:
    s, m = _bus(tmp_path)
    gate = _inactive_gate(s)
    real_lock = Store._message_publication_lock

    def short_deadline(self, *, timeout=10.0, poll=0.005):
        return real_lock(self, timeout=min(timeout, 0.05), poll=poll)

    monkeypatch.setattr(Store, "_message_publication_lock", short_deadline)
    holding, release = threading.Event(), threading.Event()

    def hold() -> None:
        with real_lock(Store(tmp_path)):               # the REAL lock, held past 0.05 s
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=hold, daemon=True)
    health = Health()

    def contended(phase: str) -> None:
        health.contended(phase)
        if len(health.contended_phases()) >= 3:
            release.set()

    def child_done() -> None:
        holder.start()
        assert holding.wait(5)

    driver = CountedDriver(on_complete=child_done)
    try:
        turns = loop.run_loop(
            s, "beta", driver, clock=lambda: 0.0, sleep=lambda _d: time.sleep(0.01),
            max_turns=1, commit_gate=gate, on_health_idle=health.idle,
            on_health_contention=contended, on_contention_persisting=health.persisting,
        )
    finally:
        release.set()
        holder.join(5)
    assert turns == 1 and driver.calls == 1
    assert len(health.contended_phases()) >= 3
    assert set(health.contended_phases()) == {loop.LOCK_CONTENTION_PHASE_PUBLICATION}
    assert health.recovered()
    assert len(_replies(s)) == 1
    assert s.cursor("beta") == m.id


# ------------------------------------------------ publication contract


def test_draft_reply_contention_without_a_nonce_is_refused_as_before(
    tmp_path, monkeypatch,
) -> None:
    s, m = _bus(tmp_path)

    def contended(**kwargs):
        raise LockContention("could not acquire the wrapper operation publication",
                             what="wrapper operation publication")

    monkeypatch.setattr(s, "send_operation", contended)
    record = loop._with_reply_draft(
        s, "beta", {"id": m.id, "from": "alpha", "kind": "question", "meta": {}})
    draft = Path(record["reply_draft"]["path"])
    draft.write_text("answer", encoding="utf-8")
    assert reply_transport.deliver_draft_reply(
        s, agent="beta", record=record, draft_path=draft) is None
    assert reply_transport.refused_reason_path(draft).is_file()


def test_draft_reply_contention_with_a_nonce_propagates_and_keeps_the_draft(
    tmp_path, monkeypatch,
) -> None:
    s, m = _bus(tmp_path)

    def contended(**kwargs):
        raise LockContention("could not acquire the wrapper operation publication",
                             what="wrapper operation publication")

    monkeypatch.setattr(s, "send_operation", contended)
    record = loop._with_reply_draft(
        s, "beta", {"id": m.id, "from": "alpha", "kind": "question", "meta": {}})
    draft = Path(record["reply_draft"]["path"])
    draft.write_text("answer", encoding="utf-8")
    with pytest.raises(LockContention):
        reply_transport.deliver_draft_reply(
            s, agent="beta", record=record, draft_path=draft, operation_nonce="a" * 32)
    assert draft.read_text(encoding="utf-8") == "answer"   # live, unrefused, retryable
    assert not reply_transport.refused_reason_path(draft).exists()


# ------------------------------------------------ health + durable diagnostic


def test_health_writer_reports_contention_in_the_existing_outage_state(tmp_path) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    writer = WrapperHealthWriter(s, "beta", "claude", mode="wrapper-loop", min_interval=0.0)
    writer.lock_contention(loop.LOCK_CONTENTION_PHASE_PUBLICATION)
    snap = s.read_health("beta")
    assert snap["state"] == health_model.STATE_RATE_LIMITED_OR_OUTAGE
    assert snap["reason_code"] == loop.LOCK_CONTENTION_REASON
    assert snap["warnings"] == ["lock_contention_publication"]
    writer.idle()
    assert s.read_health("beta")["state"] == health_model.STATE_IDLE_WAITING


def test_lifecycle_log_records_one_persisting_contention_row() -> None:
    stream = io.StringIO()
    log = WrapperLifecycleLog("beta", stream=stream, clock=lambda: 0.0, wrapper_pid=7)
    log.lock_contention_persisting(
        phase="admission", lock="message publication",
        lock_file=".message-publication.generation", attempts=BOUND)
    row = json.loads(stream.getvalue())
    assert row["event"] == "wrapper_lock_contention_persisting"
    assert row["phase"] == "admission"
    assert row["lock"] == "message publication"
    assert row["lock_file"] == ".message-publication.generation"
    assert row["attempts"] == BOUND


def test_cmd_wrap_wires_contention_health_and_the_durable_diagnostic(
    tmp_path, monkeypatch, capsys,
) -> None:
    s = Store(tmp_path)
    s.init(["lead", "beta"])
    captured: dict = {}
    rows = io.StringIO()
    monkeypatch.setattr(loop, "run_loop", lambda *a, **k: captured.update(k) or 0)
    cli._wrap_loop_mode(s, "beta", cli="codex", base_argv=["python", "-c", "pass"],
                        sender="beta", min_interval=0.0, render=False, lead_loop=False,
                        lifecycle_log=WrapperLifecycleLog("beta", stream=rows))
    captured["on_health_contention"](loop.LOCK_CONTENTION_PHASE_FINALIZATION)
    snap = s.read_health("beta")
    assert snap["state"] == health_model.STATE_RATE_LIMITED_OR_OUTAGE
    assert snap["reason_code"] == loop.LOCK_CONTENTION_REASON
    captured["on_contention_persisting"]({
        "phase": "finalization", "lock": "message publication",
        "lock_file": ".message-publication.generation", "attempts": BOUND})
    err = capsys.readouterr().err
    assert "store lock contention persists" in err
    assert "message publication" in err and "finalization phase" in err
    events = [json.loads(line) for line in rows.getvalue().splitlines()]
    persisting = [e for e in events if e["event"] == "wrapper_lock_contention_persisting"]
    assert len(persisting) == 1 and persisting[0]["attempts"] == BOUND


# ------------------------------------------ F1: an owner that cannot clean up


def _hold_guard_until(tmp_path: Path, lock: Path, held: threading.Event,
                      release: threading.Event) -> threading.Thread:
    """A second holder of ``lock``'s GENERATION GUARD (not its marker), so the
    owner's release cannot re-enter the guard to remove its own marker."""

    def run() -> None:
        other = Store(tmp_path)
        with other._lock_generation_guard(
            lock, deadline=time.monotonic() + 5.0, poll=0.005, what="guard holder",
        ):
            held.set()
            release.wait(10)

    return threading.Thread(target=run, daemon=True)


def _strand_own_marker(tmp_path: Path, s: Store, lock: Path):
    """Reviewer-1's F1 reproduction: acquire ``lock`` (80 ms), and while inside
    its body let a second thread hold the same path's generation guard, so the
    owner's release exceeds 80 ms. Returns (raised, holder, release)."""
    held, release = threading.Event(), threading.Event()
    holder = _hold_guard_until(tmp_path, lock, held, release)
    raised = False
    try:
        with s._exclusive_lock(lock, timeout=0.08, what="wrapper operation publication"):
            holder.start()
            assert held.wait(5)
    except LockContention:
        raised = True
    return raised, holder, release


def test_release_guard_contention_never_strands_an_unrecoverable_own_marker(
    tmp_path,
) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    lock = s.state_dir / "operation-publication.lock"
    raised, holder, release = _strand_own_marker(tmp_path, s, lock)
    try:
        # The owner could not remove its marker while the guard was held: the
        # marker is still there, owned by THIS live process.
        pid, _identity, _record = store_mod._read_lock_owner(lock)
        assert pid == store_mod.os.getpid()
    finally:
        release.set()
        holder.join(5)
    # The critical section completed, so the deferred cleanup is not reported
    # as contention (a retry would re-run completed work)...
    assert raised is False
    # ...and this process recovers its OWN marker (inode + generation + pid
    # validated under the guard) instead of waiting on itself forever.
    with s._exclusive_lock(lock, timeout=0.08, what="wrapper operation publication"):
        assert lock.exists()
    assert not lock.exists()


def test_a_body_failure_still_propagates_when_the_release_is_contended(
    tmp_path,
) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    lock = s.state_dir / "operation-publication.lock"
    held, release = threading.Event(), threading.Event()
    holder = _hold_guard_until(tmp_path, lock, held, release)
    try:
        with pytest.raises(ValueError, match="corrupt"):
            with s._exclusive_lock(lock, timeout=0.08, what="wrapper operation publication"):
                holder.start()
                assert held.wait(5)
                raise ValueError("corrupt payload")      # never masked as contention
    finally:
        release.set()
        holder.join(5)
    with s._exclusive_lock(lock, timeout=0.08, what="wrapper operation publication"):
        pass
    assert not lock.exists()


def test_stranded_own_marker_is_swept_once_its_guard_frees(tmp_path) -> None:
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    lock = s.state_dir / "operation-publication.lock"
    _raised, holder, release = _strand_own_marker(tmp_path, s, lock)
    try:
        started = time.monotonic()
        assert s.complete_stranded_lock_releases() == 0   # guard still held: kept
        assert time.monotonic() - started < 1.0          # and the sweep never waits
        assert lock.exists()
    finally:
        release.set()
        holder.join(5)
    assert Store(tmp_path).complete_stranded_lock_releases() == 1   # any Store, same process
    assert not lock.exists()
    assert s.complete_stranded_lock_releases() == 0


def test_a_live_marker_this_process_did_not_strand_is_never_unlinked(
    tmp_path,
) -> None:
    # Same PID, different owner (another thread): a stale stranded entry for the
    # path must not license removing it - identity and generation must match.
    s = Store(tmp_path)
    s.init(["alpha", "beta"])
    lock = s.state_dir / "operation-publication.lock"
    _raised, holder, release = _strand_own_marker(tmp_path, s, lock)
    release.set()
    holder.join(5)
    lock.unlink()                                      # the stranded marker is gone
    holding, done = threading.Event(), threading.Event()

    def hold_marker() -> None:
        with Store(tmp_path)._exclusive_lock(lock, timeout=5.0, what="publication"):
            holding.set()
            done.wait(10)

    t = threading.Thread(target=hold_marker, daemon=True)
    t.start()
    try:
        assert holding.wait(5)
        live = store_mod._read_lock_owner(lock)[2]
        with pytest.raises(LockContention):
            with s._exclusive_lock(lock, timeout=0.1, what="publication"):
                pass
        assert s.complete_stranded_lock_releases() == 0
        assert store_mod._read_lock_owner(lock)[2] == live   # the live marker survived
    finally:
        done.set()
        t.join(5)


def test_wrapper_publication_with_a_contended_release_publishes_once_and_cleans_up(
    tmp_path, monkeypatch,
) -> None:
    # F1 end to end: the reply's send_operation lands, then its
    # operation-publication.lock release cannot re-enter the guard. Before the
    # fix the in-place retry waited on its own live marker forever.
    s, m = _bus(tmp_path)
    lock = s.state_dir / "operation-publication.lock"
    real_exclusive = Store._exclusive_lock

    def short_publication_lock(self, path, *, timeout=10.0, poll=0.05, what="lock"):
        if Path(path).name == "operation-publication.lock":
            timeout = min(timeout, 0.08)
        return real_exclusive(self, path, timeout=timeout, poll=poll, what=what)

    monkeypatch.setattr(Store, "_exclusive_lock", short_publication_lock)
    held, release = threading.Event(), threading.Event()
    holder = _hold_guard_until(tmp_path, lock, held, release)
    real_send = s.send
    started: list[bool] = []

    def send_then_hold_the_guard(**kwargs):
        message = real_send(**kwargs)
        if kwargs.get("sender") == "beta" and not started:
            started.append(True)
            holder.start()
            assert held.wait(5)
        return message

    monkeypatch.setattr(s, "send", send_then_hold_the_guard)
    sleeps = {"n": 0}

    def sleep(_delay: float) -> None:
        sleeps["n"] += 1
        if sleeps["n"] == 1:
            release.set()                            # the guard frees after the turn
            holder.join(5)
        if sleeps["n"] > 20:
            raise AssertionError("the wrapper is retrying against its own marker")

    driver = CountedDriver()
    health = Health()
    try:
        turns = loop.run_loop(
            s, "beta", driver, clock=lambda: 0.0, sleep=sleep, max_polls=4,
            commit_gate=_inactive_gate(s), on_health_idle=health.idle,
            on_health_contention=health.contended,
            on_contention_persisting=health.persisting,
        )
    finally:
        release.set()
        holder.join(5)
    assert turns == 1 and driver.calls == 1
    assert len(_replies(s)) == 1
    assert s.cursor("beta") == m.id
    assert not lock.exists()                         # no marker left to block other seats


# ------------------------------------------ F2: owed heads, the admitted path


def _owed_bus(tmp_path: Path):
    s = Store(tmp_path)
    s.init(["alpha", "beta", "lead"])
    s.set_operator_facing("lead")
    m = s.send(sender="alpha", recipient="beta", kind="question",
               body="What is 19 * 21?", meta={"request_id": "q-154"})
    return s, m


class OwedDriver:
    """The paid admitted dispatch, counted: answers through the owed-action
    transport exactly like a child would. ``on_complete`` runs after the answer
    landed. A second invocation fails at once."""

    def __init__(self, tmp_path: Path, *, on_complete=None) -> None:
        self.tmp_path = tmp_path
        self.calls = 0
        self.on_complete = on_complete

    def __call__(self, rec: dict) -> loop.DriveOutcome:
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("the paid driver was re-entered for a completed turn")
        owed = rec["owed_action"]
        Path(owed["draft_path"]).write_text("399", encoding="utf-8")
        assert cli.main(["--root", str(self.tmp_path), *owed["argv"][3:], "--quiet"]) == 0
        if self.on_complete is not None:
            self.on_complete()
        return loop.DriveOutcome(ok=True, bus_action_attempted=True)


def _answers(s: Store) -> list:
    return [m for m in s.messages_for("alpha") if m.sender == "beta"]


# Every lock the admitted path takes BEFORE its paid dispatch, in order: the
# admission's replay index and claim, the dispatch-purpose read, the
# reservation's publication lock, replay index and ledger CAS, then the dispatch
# arming's acceptance-writer, config and ledger locks.
_OWED_PRE_DISPATCH_SITES = [
    pytest.param("ledger.lock", 0, id="admission-replay-index"),
    pytest.param("ledger.lock", 1, id="admission-claim"),
    pytest.param("ledger.lock", 2, id="dispatch-purpose"),
    pytest.param("message-publication", 0, id="reservation-publication"),
    pytest.param("ledger.lock", 3, id="reservation-replay-index"),
    pytest.param("ledger.lock", 4, id="reservation-cas"),
    pytest.param(".acceptance-write.lock", 0, id="arming-acceptance-writer"),
    pytest.param("config.lock", 0, id="arming-config"),
    pytest.param("ledger.lock", 5, id="arming-ledger"),
]


@pytest.mark.parametrize(("lock_name", "skip"), _OWED_PRE_DISPATCH_SITES)
def test_owed_head_contention_before_the_paid_dispatch_is_retried_in_place(
    tmp_path, monkeypatch, lock_name, skip,
) -> None:
    s, m = _owed_bus(tmp_path)
    barrier = LockBarrier(monkeypatch)
    barrier.arm(lock_name, skip=skip, hold=BOUND + 1)
    held_at_drive: list[int] = []
    driver = OwedDriver(tmp_path)

    def drive(rec):
        held_at_drive.append(barrier.remaining(lock_name))
        return driver(rec)

    health = Health()
    turns = _run(s, _active_gate(s), drive, health, max_polls=6)

    assert turns == 1                                   # a live wrapper, not an exit
    assert driver.calls == 1 and held_at_drive == [0]   # ONE paid dispatch, after release
    assert barrier.hits[lock_name] == BOUND + 1
    assert len(_answers(s)) == 1                        # ONE publication
    assert s.cursor("beta") == m.id
    assert health.contended_phases() == [loop.LOCK_CONTENTION_PHASE_ADMISSION] * (BOUND + 1)
    assert health.recovered()
    assert len(health.diagnostics) == 1


# After the paid dispatch answered: the result replay (index, pending broadcast
# close), the dispatch-result record, the finalizer's disposition CAS, and the
# compliance-streak reset. skip=5 passes the finalizer's best-effort completion
# telemetry (4), which swallows its own failures by design.
_OWED_POST_DISPATCH_SITES = [
    pytest.param(0, id="result-replay-index"),
    pytest.param(1, id="result-replay-broadcast-close"),
    pytest.param(2, id="dispatch-result"),
    pytest.param(3, id="finalize-disposition"),
    pytest.param(5, id="satisfied-streak"),
]


@pytest.mark.parametrize("skip", _OWED_POST_DISPATCH_SITES)
def test_owed_head_contention_after_the_answer_landed_never_redrives(
    tmp_path, monkeypatch, skip,
) -> None:
    s, m = _owed_bus(tmp_path)
    barrier = LockBarrier(monkeypatch)
    driver = OwedDriver(tmp_path, on_complete=lambda: barrier.arm(
        "ledger.lock", skip=skip, hold=BOUND + 1))
    health = Health()
    turns = _run(s, _active_gate(s), driver, health, max_polls=6)

    assert turns == 1
    assert driver.calls == 1                            # never re-enters the paid driver
    assert barrier.hits["ledger.lock"] == BOUND + 1
    assert len(_answers(s)) == 1
    assert s.cursor("beta") == m.id
    assert health.contended_phases() == [
        loop.LOCK_CONTENTION_PHASE_FINALIZATION] * (BOUND + 1)
    assert health.recovered()
    assert len(health.diagnostics) == 1


def test_owed_head_corruption_after_the_answer_landed_stays_fatal(
    tmp_path, monkeypatch,
) -> None:
    s, _m = _owed_bus(tmp_path)
    barrier = LockBarrier(monkeypatch)
    driver = OwedDriver(tmp_path, on_complete=lambda: barrier.arm(
        "ledger.lock", skip=2, hold=5, error=ValueError("owed-action ledger is corrupt")))
    health = Health()
    with pytest.raises(ValueError, match="corrupt"):
        _run(s, _active_gate(s), driver, health, max_polls=6)
    assert barrier.hits["ledger.lock"] == 1             # the dispatch result: never retried
    assert driver.calls == 1
    assert len(_answers(s)) == 1
    assert s.cursor("beta") == ""
    assert health.contended_phases() == []


class _Crash(BaseException):
    """The wrapper process dies right after the paid dispatch answered."""


def test_owed_relaunch_after_a_landed_answer_settles_through_contention_without_redriving(
    tmp_path, monkeypatch,
) -> None:
    s, m = _owed_bus(tmp_path)

    def answer_then_die(rec):
        OwedDriver(tmp_path)(rec)
        raise _Crash()

    with pytest.raises(_Crash):
        _run(s, _active_gate(s), answer_then_die, Health(), max_polls=6)
    assert len(_answers(s)) == 1 and s.cursor("beta") == ""

    # The relaunch finds the landed answer at admission and finalizes it; the
    # compliance-streak reset that follows is contended.
    barrier = LockBarrier(monkeypatch)
    relaunched = _active_gate(s, fence="wrapper-2")
    real_mark_satisfied = relaunched.mark_satisfied
    armed: list[bool] = []

    def contended_mark_satisfied(key):
        if not armed:
            armed.append(True)
            barrier.arm("ledger.lock", hold=BOUND + 1)
        return real_mark_satisfied(key)

    monkeypatch.setattr(relaunched, "mark_satisfied", contended_mark_satisfied)
    driver = OwedDriver(tmp_path)
    health = Health()
    turns = _run(s, relaunched, driver, health, max_polls=6)

    assert turns == 1
    assert driver.calls == 0                            # the landed answer is never re-paid
    assert barrier.hits["ledger.lock"] == BOUND + 1
    assert len(_answers(s)) == 1
    assert s.cursor("beta") == m.id
    assert health.contended_phases() == [
        loop.LOCK_CONTENTION_PHASE_FINALIZATION] * (BOUND + 1)


def test_a_stranded_publication_marker_is_cleared_during_a_later_in_place_retry(
    tmp_path, monkeypatch,
) -> None:
    # The reply's publication strands its operation-publication.lock marker; the
    # turn's finalization then contends. Before the next poll, each in-place
    # retry first sweeps this process's stranded markers, so other seats are not
    # kept behind the marker for the whole finalization episode.
    s, m = _bus(tmp_path)
    lock = s.state_dir / "operation-publication.lock"
    real_exclusive = Store._exclusive_lock

    def short_publication_lock(self, path, *, timeout=10.0, poll=0.05, what="lock"):
        if Path(path).name == "operation-publication.lock":
            timeout = min(timeout, 0.08)
        return real_exclusive(self, path, timeout=timeout, poll=poll, what=what)

    monkeypatch.setattr(Store, "_exclusive_lock", short_publication_lock)
    held, release = threading.Event(), threading.Event()
    holder = _hold_guard_until(tmp_path, lock, held, release)
    barrier = LockBarrier(monkeypatch)
    real_send = s.send
    started: list[bool] = []

    def send_then_hold_the_guard(**kwargs):
        message = real_send(**kwargs)
        if kwargs.get("sender") == "beta" and not started:
            started.append(True)
            holder.start()
            assert held.wait(5)
            # the landed-reply replay right after this publication contends
            barrier.arm("message-publication", hold=4)
        return message

    monkeypatch.setattr(s, "send", send_then_hold_the_guard)
    marker_during_retries: list[bool] = []
    health = Health()

    def contended(phase: str) -> None:
        health.contended(phase)
        count = len(health.contended_phases())
        if count == 1:
            release.set()                            # the guard frees mid-episode
            holder.join(5)
        if count == 3:
            marker_during_retries.append(lock.exists())

    try:
        turns = loop.run_loop(
            s, "beta", CountedDriver(), clock=lambda: 0.0, sleep=lambda _d: None,
            max_turns=1, commit_gate=_inactive_gate(s), on_health_idle=health.idle,
            on_health_contention=contended,
            on_contention_persisting=health.persisting,
        )
    finally:
        release.set()
        holder.join(5)
    assert turns == 1
    assert health.contended_phases() == [loop.LOCK_CONTENTION_PHASE_FINALIZATION] * 4
    assert marker_during_retries == [False]          # swept before the episode ended
    assert len(_replies(s)) == 1
    assert s.cursor("beta") == m.id


# ------------------------------------------ the gate-method caller contract
#
# A gate method never raises anything new and never changes which STATE it
# returns because of store lock contention. Where it used to fold a failure into
# a fail-closed result, contention returns that same state with a reason in
# obligations.CONTENDED_REASONS and persists nothing (no obligation block, no
# disposition block). Callers without an in-place retry (the one-shot loop) see
# the result they always handled and simply try again next poll; the continuous
# loop recognizes the reason and retries the call in place. Every site is pinned
# with a paired contention / control fixture, and every caller class of the
# failed-delivery disposition by its own loop test.


def _owed_admission(s: Store, gate) -> dict:
    return next(iter(json.loads(gate.path.read_text(encoding="utf-8"))["obligations"].values()))


def _transitions(gate) -> list[str]:
    return [row["transition"]
            for row in json.loads(gate.path.read_text(encoding="utf-8"))["transitions"]]


def _disposition_blocked(gate) -> bool:
    try:
        health = json.loads(gate.proof_health_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    return health.get("disposition_block") is True


def _fresh_owed(tmp_path: Path):
    """An admitted, open owed obligation owned by this fence, outside the loop."""
    s, m = _owed_bus(tmp_path)
    s.write_waiting("beta", {"mode": "wrapper-loop", "wrapper_generation": "wrapper-1",
                             "wait_token": "wrapper-1", "pid": store_mod.os.getpid()})
    gate = _active_gate(s)
    record = loop.recv_api.next_record(s, "beta")
    resolution = gate.admit_or_finalize(record)
    assert resolution.state == obligations.ResolverState.OWED_UNSATISFIED
    return s, m, gate, record, resolution


def _calling_loop_line() -> int:
    """The loop line (continuous or one-shot) the current gate call came from."""
    frame = sys._getframe(2)
    while frame is not None and frame.f_code.co_name not in {
            "_run_continuous", "_run_one_shot"}:
        frame = frame.f_back
    return frame.f_lineno if frame is not None else -1


def _failed_delivery_calls(monkeypatch, gate, barrier, lock_name: str, hold: int) -> list[int]:
    """Arm ``hold`` contended acquisitions of ``lock_name`` just before the FIRST
    real fail_delivery_or_block; record the loop line every call comes from."""
    real_fail = gate.fail_delivery_or_block
    lines: list[int] = []

    def contended_fail(*args, **kwargs):
        lines.append(_calling_loop_line())
        if len(lines) == 1:
            barrier.arm(lock_name, hold=hold)
        return real_fail(*args, **kwargs)

    monkeypatch.setattr(gate, "fail_delivery_or_block", contended_fail)
    return lines


def _loop_call_lines(function, needle: str) -> list[int]:
    """Line numbers of ``needle`` inside a loop.py function, in source order."""
    source, first = inspect.getsourcelines(function)
    return [first + offset for offset, line in enumerate(source) if needle in line]


def _scoped_consumed(s: Store, m) -> bool:
    return loop.recv_api.consume_boundary_complete(
        s, "beta", {"id": m.id, "mode": "scoped", "scoped": {"request_id": "q-154"}})


def _unanswered(drives: list[str]):
    def drive(rec):
        drives.append(rec["owed_action"]["purpose"])
        return True                                  # no legal owed reply
    return drive


def _assert_failed_delivery_disposed(s: Store, gate, m) -> None:
    admission = _owed_admission(s, gate)
    assert admission["state"] == "delivery_failed"
    assert admission["terminal_state"] == "delivery_exhausted"
    assert "OBLIGATION_BLOCKED" not in _transitions(gate)
    assert not _disposition_blocked(gate)


# --- fail_delivery_or_block, continuous loop, post-dispatch site (reviewer-1 N1)

@pytest.mark.parametrize(("hold", "in_place_retries"), [
    pytest.param(0, 0, id="control"),
    pytest.param(1, 0, id="one-transient-recovers-inside-the-call"),
    pytest.param(2, 1, id="contended-past-the-internal-retry"),
    pytest.param(2 * BOUND + 1, BOUND, id="persisting"),
])
def test_continuous_post_dispatch_failed_delivery_under_contention(
    tmp_path, monkeypatch, hold, in_place_retries,
) -> None:
    s, m = _owed_bus(tmp_path)
    gate = _active_gate(s)
    barrier = LockBarrier(monkeypatch)
    lines = _failed_delivery_calls(monkeypatch, gate, barrier, "message-publication", hold)
    drives: list[str] = []
    health = Health()
    _run(s, gate, _unanswered(drives), health, max_turns=None, max_polls=6)

    assert drives == ["initial", "recovery"]            # never an extra paid dispatch
    _assert_failed_delivery_disposed(s, gate, m)
    assert s.cursor("beta") == m.id
    assert barrier.hits["message-publication"] == hold
    # Each call absorbs one contention itself (its own replay-and-retry); only a
    # second one inside the same call comes back as an in-place retry.
    assert health.contended_phases() == [
        loop.LOCK_CONTENTION_PHASE_FINALIZATION] * in_place_retries
    assert len(health.diagnostics) == (1 if in_place_retries >= BOUND else 0)
    post_site = _loop_call_lines(loop._run_continuous, "settled_gate.fail_delivery_or_block(")[1]
    assert set(lines) == {post_site}


# --- fail_delivery_or_block, continuous loop, pre-dispatch site (an exhausted
# head found at entry, e.g. after a relaunch)

def test_continuous_pre_dispatch_failed_delivery_under_contention_retries_in_place(
    tmp_path, monkeypatch,
) -> None:
    # The post-dispatch disposition is deferred once (the head stays open and
    # unconsumed), so the next poll reaches the pre-dispatch site; there the
    # contention outlasts the call's own retry and the loop retries it in place.
    s, m = _owed_bus(tmp_path)
    gate = _active_gate(s)
    barrier = LockBarrier(monkeypatch)
    real_fail = gate.fail_delivery_or_block
    lines: list[int] = []

    def deferred_then_contended(*args, **kwargs):
        lines.append(_calling_loop_line())
        if len(lines) == 1:
            return obligations.Resolution(obligations.ResolverState.INDETERMINATE,
                                          "deferred by the test")
        if len(lines) == 2:
            barrier.arm("message-publication", hold=2)
        return real_fail(*args, **kwargs)

    monkeypatch.setattr(gate, "fail_delivery_or_block", deferred_then_contended)
    drives: list[str] = []
    health = Health()
    _run(s, gate, _unanswered(drives), health, max_turns=None, max_polls=6)

    assert drives == ["initial", "recovery"]            # the next poll never pays again
    _assert_failed_delivery_disposed(s, gate, m)
    assert s.cursor("beta") == m.id
    assert barrier.hits["message-publication"] == 2
    assert health.contended_phases() == [loop.LOCK_CONTENTION_PHASE_FINALIZATION]
    pre_site, post_site = _loop_call_lines(
        loop._run_continuous, "settled_gate.fail_delivery_or_block(")
    assert lines == [post_site, pre_site, pre_site]      # deferred, contended, retried


# --- fail_delivery_or_block, ONE-SHOT loop (reviewer-1 N2): both call sites

def test_one_shot_failed_delivery_recovers_one_transient_timeout_as_at_baseline(
    tmp_path, monkeypatch,
) -> None:
    # Reviewer-1's N2 probe, which passes at 18d366a: ONE transient timeout just
    # before the first real fail_delivery_or_block is absorbed by the method's
    # own replay-and-retry, and the one-shot completes the disposition.
    s, m = _owed_bus(tmp_path)
    gate = _active_gate(s)
    barrier = LockBarrier(monkeypatch)
    lines = _failed_delivery_calls(monkeypatch, gate, barrier, "message-publication", 1)
    drives: list[str] = []
    loop.run_loop(s, "beta", _unanswered(drives), clock=lambda: 0.0,
                  sleep=lambda _d: None, max_polls=6, only_request_id="q-154",
                  commit_gate=gate)

    assert drives == ["initial", "recovery"]
    _assert_failed_delivery_disposed(s, gate, m)
    assert barrier.hits["message-publication"] == 1
    post_site = _loop_call_lines(loop._run_one_shot, "commit_gate.fail_delivery_or_block(")[1]
    assert lines == [post_site]                          # recovered inside one call


def test_one_shot_failed_delivery_contended_past_its_own_retry_recovers_next_poll(
    tmp_path, monkeypatch,
) -> None:
    # Contention outlasts the method's own retry at the post-dispatch site. The
    # one-shot has no in-place retry: it gets the BLOCKED state it always
    # handled, but nothing is persisted, so its next poll reaches the
    # pre-dispatch site with the head still open and disposes it there.
    s, m = _owed_bus(tmp_path)
    gate = _active_gate(s)
    barrier = LockBarrier(monkeypatch)
    lines = _failed_delivery_calls(monkeypatch, gate, barrier, "message-publication", 2)
    drives: list[str] = []
    loop.run_loop(s, "beta", _unanswered(drives), clock=lambda: 0.0,
                  sleep=lambda _d: None, max_polls=6, only_request_id="q-154",
                  commit_gate=gate)

    assert drives == ["initial", "recovery"]            # the next poll pays nothing
    _assert_failed_delivery_disposed(s, gate, m)
    pre_site, post_site = _loop_call_lines(
        loop._run_one_shot, "commit_gate.fail_delivery_or_block(")
    assert lines == [post_site, pre_site]


# --- fail_delivery_or_block, unit: the internal second chance

_FAILED_DELIVERY_SECOND_CHANCE = [
    pytest.param("ledger.lock", 2, "LEDGER_REPLAY_CONTENDED", id="replay"),
    pytest.param("message-publication", 1, "DISPOSITION_CONTENDED", id="retry"),
]


@pytest.mark.parametrize(("lock_name", "skip", "reason"), _FAILED_DELIVERY_SECOND_CHANCE)
@pytest.mark.parametrize("contended", [False, True], ids=["control", "contended"])
def test_failed_delivery_second_chance_returns_contention_unpersisted(
    tmp_path, monkeypatch, lock_name, skip, reason, contended,
) -> None:
    # The first disposition misses on a stale revision (not contention); then its
    # replay, or its one retry at the fresh revision, is contended.
    s, m, gate, record, resolution = _fresh_owed(tmp_path)
    stale = resolution.scoped_revision - 1
    barrier = LockBarrier(monkeypatch)
    if contended:
        barrier.arm(lock_name, skip=skip, hold=1)
        result = gate.fail_delivery_or_block(record, resolution.key, reason="exhausted",
                                             expected_revision=stale)
        assert result.state == obligations.ResolverState.BLOCKED   # the state it always had
        assert result.reason == getattr(obligations, reason)       # ...reporting contention
        assert barrier.hits[lock_name] == 1
        assert _owed_admission(s, gate)["state"] == "open"         # nothing persisted
        assert "OBLIGATION_BLOCKED" not in _transitions(gate)
        assert not _disposition_blocked(gate)
    settled = gate.fail_delivery_or_block(record, resolution.key, reason="exhausted",
                                          expected_revision=stale)
    assert settled.state == obligations.ResolverState.DELIVERY_EXHAUSTED
    assert _owed_admission(s, gate)["state"] == "delivery_failed"
    assert s.cursor("beta") == m.id


def test_failed_delivery_non_contention_failure_keeps_the_fail_closed_block(
    tmp_path, monkeypatch,
) -> None:
    s, _m, gate, record, resolution = _fresh_owed(tmp_path)
    barrier = LockBarrier(monkeypatch)
    # both the first disposition and its retry hit a real I/O failure
    barrier.arm("message-publication", hold=2, error=OSError("publication I/O failed"))
    blocked = gate.fail_delivery_or_block(record, resolution.key, reason="exhausted",
                                          expected_revision=resolution.scoped_revision)
    assert blocked.state == obligations.ResolverState.BLOCKED
    assert blocked.reason == "failed-delivery disposition unavailable: OSError"
    assert _owed_admission(s, gate)["state"] == "blocked"


# --- settle_retry_exhaustion

@pytest.mark.parametrize("contended", [False, True], ids=["control", "contended"])
def test_retry_exhaustion_settlement_returns_a_contended_replay_unpersisted(
    tmp_path, monkeypatch, contended,
) -> None:
    s, m, gate, record, resolution = _fresh_owed(tmp_path)
    barrier = LockBarrier(monkeypatch)
    if contended:
        barrier.arm("ledger.lock", hold=1)                # the settlement's replay index
        result = gate.settle_retry_exhaustion(record, resolution.key,
                                              category="finalization", reason="bound spent")
        assert result.state == obligations.ResolverState.BLOCKED
        assert result.reason == obligations.LEDGER_REPLAY_CONTENDED
        assert _owed_admission(s, gate)["state"] == "open"
        assert "OBLIGATION_BLOCKED" not in _transitions(gate)
    settled = gate.settle_retry_exhaustion(record, resolution.key,
                                           category="finalization", reason="bound spent")
    assert settled.state == obligations.ResolverState.DELIVERY_EXHAUSTED
    assert _owed_admission(s, gate)["state"] == "delivery_failed"
    assert s.cursor("beta") == m.id


@pytest.mark.parametrize("contended", [False, True], ids=["control", "contended"])
def test_settlement_race_replay_contention_never_becomes_the_race_block(
    tmp_path, monkeypatch, contended,
) -> None:
    s, _m, gate, record, resolution = _fresh_owed(tmp_path)
    terminal = obligations.Resolution(
        obligations.ResolverState.DELIVERY_EXHAUSTED, "terminal", resolution.key,
        ledger_revision=resolution.scoped_revision,
    )
    raced = (
        obligations.Resolution(obligations.ResolverState.BLOCKED,
                               obligations.LEDGER_REPLAY_CONTENDED)
        if contended else
        obligations.Resolution(obligations.ResolverState.OWED_UNSATISFIED,
                               "still owed", resolution.key)
    )
    replays = iter([terminal, raced])
    monkeypatch.setattr(gate, "resolve", lambda _record: next(replays))
    monkeypatch.setattr(gate, "finalize", lambda *_a, **_k: obligations.Resolution(
        obligations.ResolverState.INDETERMINATE, "finalization CAS miss"))
    result = gate.settle_retry_exhaustion(record, resolution.key,
                                          category="finalization", reason="bound spent")
    assert result.state == obligations.ResolverState.BLOCKED
    if contended:
        assert result is raced                                    # returned as is
        assert _owed_admission(s, gate)["state"] == "open"
    else:
        assert _owed_admission(s, gate)["state"] == "blocked"     # unchanged fail-closed


def _relaunch_with_contended_settlement(tmp_path, monkeypatch):
    """An owed answer landed and the process died before finalizing it. The
    relaunch finds the terminal at admission; its finalize reports "CAS
    contention exhausted" once, so the loop settles, and that settlement's
    replay is contended."""
    s, m = _owed_bus(tmp_path)

    def answer_then_die(rec):
        OwedDriver(tmp_path)(rec)
        raise _Crash()

    with pytest.raises(_Crash):
        _run(s, _active_gate(s), answer_then_die, Health(), max_polls=6)
    relaunched = _active_gate(s, fence="wrapper-2")
    barrier = LockBarrier(monkeypatch)
    real_finalize = relaunched.finalize
    exhausted: list[bool] = []

    def cas_exhausted_once(*args, **kwargs):
        if not exhausted:
            exhausted.append(True)
            barrier.arm("ledger.lock", hold=1)            # the settlement's replay
            return obligations.Resolution(obligations.ResolverState.INDETERMINATE,
                                          "finalization CAS contention exhausted")
        return real_finalize(*args, **kwargs)

    monkeypatch.setattr(relaunched, "finalize", cas_exhausted_once)
    real_settle = relaunched.settle_retry_exhaustion
    settled: list[obligations.Resolution] = []

    def spy_settle(*args, **kwargs):
        result = real_settle(*args, **kwargs)
        settled.append(result)
        return result

    monkeypatch.setattr(relaunched, "settle_retry_exhaustion", spy_settle)
    return s, m, relaunched, barrier, settled


def test_continuous_settlement_contention_retries_in_place(tmp_path, monkeypatch) -> None:
    s, m, gate, barrier, settled = _relaunch_with_contended_settlement(tmp_path, monkeypatch)
    driver = OwedDriver(tmp_path)
    health = Health()
    _run(s, gate, driver, health, max_polls=6)

    assert driver.calls == 0                            # the landed answer is never re-paid
    assert barrier.hits["ledger.lock"] == 1
    assert settled[0].reason == obligations.LEDGER_REPLAY_CONTENDED
    assert "OBLIGATION_BLOCKED" not in _transitions(gate)
    assert health.contended_phases() == [loop.LOCK_CONTENTION_PHASE_FINALIZATION]
    assert s.cursor("beta") == m.id


def test_one_shot_settlement_contention_recovers_next_poll(tmp_path, monkeypatch) -> None:
    s, m, gate, barrier, settled = _relaunch_with_contended_settlement(tmp_path, monkeypatch)
    driver = OwedDriver(tmp_path)
    loop.run_loop(s, "beta", driver, clock=lambda: 0.0, sleep=lambda _d: None,
                  max_polls=6, only_request_id="q-154", commit_gate=gate)

    assert driver.calls == 0
    assert settled[0].reason == obligations.LEDGER_REPLAY_CONTENDED   # nothing persisted...
    assert "OBLIGATION_BLOCKED" not in _transitions(gate)
    assert _owed_admission(s, gate)["state"] != "blocked"              # ...so it finished later
    assert _scoped_consumed(s, m)


# --- record_retry_barrier: master's contract kept for every caller

@pytest.mark.parametrize("contended", [False, True], ids=["control", "contended"])
def test_retry_barrier_keeps_its_contract_under_contention(
    tmp_path, monkeypatch, contended,
) -> None:
    s, _m, gate, _record, resolution = _fresh_owed(tmp_path)
    barrier = LockBarrier(monkeypatch)
    if contended:
        barrier.arm("ledger.lock", hold=1)
        assert gate.record_retry_barrier(resolution.key, category="operation_infra",
                                         expected_revision=resolution.scoped_revision) is False
        assert int(_owed_admission(s, gate).get("operation_infra_attempts", 0)) == 0
    assert gate.record_retry_barrier(resolution.key, category="operation_infra",
                                     expected_revision=resolution.scoped_revision) is True
    assert _owed_admission(s, gate)["operation_infra_attempts"] == 1   # counted once


# --- _block_retry_exhaustion

@pytest.mark.parametrize(
    "error",
    [None, OSError("owed-action ledger write failed")],
    ids=["contended", "non-contention-control"],
)
def test_a_contended_block_write_is_returned_never_escalated_to_a_disposition_block(
    tmp_path, monkeypatch, error,
) -> None:
    s, _m, gate, _record, resolution = _fresh_owed(tmp_path)
    barrier = LockBarrier(monkeypatch)
    barrier.arm("ledger.lock", hold=1, error=error)      # mark_blocked's ledger lock
    blocked = gate._block_retry_exhaustion(resolution.key, reason="exhausted")
    assert blocked.state == obligations.ResolverState.BLOCKED
    if error is None:
        assert blocked.reason == obligations.BLOCK_WRITE_CONTENDED
        assert not _disposition_blocked(gate)             # nothing escalated
        assert _owed_admission(s, gate)["state"] == "open"
        assert gate._block_retry_exhaustion(
            resolution.key, reason="exhausted").reason == "exhausted"
        assert _owed_admission(s, gate)["state"] == "blocked"
    else:
        assert blocked.reason == "exhausted"              # unchanged fail-closed rule
        assert _disposition_blocked(gate)


# --- fail_head_local_corruption_or_block

@pytest.mark.parametrize(
    "error",
    [None, ValueError("owed-action ledger is corrupt")],
    ids=["contended", "non-contention-control"],
)
def test_head_local_disposition_returns_a_contended_replay_unpersisted(
    tmp_path, monkeypatch, error,
) -> None:
    s, _m, gate, record, resolution = _fresh_owed(tmp_path)
    permit = obligations.DispatchPermit(
        resolution.key.digest, "0" * 32, "0" * 32, "recovery", 1,
        tmp_path / "unused-draft.txt", "",
    )
    barrier = LockBarrier(monkeypatch)
    barrier.arm("ledger.lock", hold=1, error=error)      # the proof replay's index
    result = gate.fail_head_local_corruption_or_block(
        record, resolution.key, permit, reason="corrupt artifact",
        expected_revision=resolution.scoped_revision)
    assert result.state == obligations.ResolverState.BLOCKED
    if error is None:
        assert result.reason == obligations.LEDGER_REPLAY_CONTENDED
        assert _owed_admission(s, gate)["state"] == "open"
        assert "OBLIGATION_BLOCKED" not in _transitions(gate)
    else:
        assert result.reason.startswith("head-local proof unavailable")
        assert _owed_admission(s, gate)["state"] == "blocked"


# --- finalize: cursor-projection miss accounting

@pytest.mark.parametrize("contended", [False, True], ids=["control", "contended"])
def test_cursor_projection_miss_accounting_contention_never_blocks_dispositions(
    tmp_path, monkeypatch, contended,
) -> None:
    # The finalizer's cursor write fails once (an I/O miss it must account for);
    # contention on that accounting used to escalate into a disposition block.
    s, m = _bus(tmp_path, kind="message")
    gate = _active_gate(s)
    barrier = LockBarrier(monkeypatch)
    real_advance = s.advance_cursor
    failed: list[bool] = []

    def advance_once_failing(agent, msg_id):
        if not failed:
            failed.append(True)
            if contended:
                barrier.arm("ledger.lock", hold=1)        # the miss accounting
            raise OSError("cursor write failed")
        return real_advance(agent, msg_id)

    monkeypatch.setattr(s, "advance_cursor", advance_once_failing)
    driver = CountedDriver(reply=None)
    health = Health()
    _run(s, gate, driver, health, max_turns=None, max_polls=6)

    assert driver.calls == 1
    assert failed == [True]
    assert not _disposition_blocked(gate)
    assert s.cursor("beta") == m.id
    assert barrier.hits["ledger.lock"] == (1 if contended else 0)
    assert health.contended_phases() == (
        [loop.LOCK_CONTENTION_PHASE_FINALIZATION] if contended else [])


def test_cursor_projection_miss_accounting_contention_unit(tmp_path, monkeypatch) -> None:
    s, _m, gate, record, resolution = _fresh_owed(tmp_path)
    barrier = LockBarrier(monkeypatch)

    def advance_fails(_agent, _msg_id):
        barrier.arm("ledger.lock", hold=1)
        raise OSError("cursor write failed")

    monkeypatch.setattr(s, "advance_cursor", advance_fails)
    terminal = gate.delivery_failed(record, resolution.key, reason="exhausted",
                                    expected_revision=resolution.scoped_revision)
    assert terminal.state == obligations.ResolverState.INDETERMINATE   # the state it always had
    assert terminal.reason == obligations.PROJECTION_ACCOUNTING_CONTENDED
    assert not _disposition_blocked(gate)
