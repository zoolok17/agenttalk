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
import io
import json
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
    ``on_complete`` runs as the child finishes (arms a post-completion barrier)."""

    def __init__(self, *, reply: str | None = "the answer", on_complete=None) -> None:
        self.calls = 0
        self.reply = reply
        self.on_complete = on_complete

    def __call__(self, rec: dict) -> bool:
        self.calls += 1
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


def _active_gate(s: Store) -> DetectionCommitGate:
    policy = PolicySnapshot.from_mapping(
        {"schema_version": 1, "agents": {"beta": {"grade": DETECTION_GRADE}}}, "beta")
    return DetectionCommitGate(s, "beta", policy, fence="wrapper-1")


def _replies(s: Store) -> list:
    return [m for m in s.messages_for("alpha")
            if m.sender == "beta" and m.meta.get("reply_refusal") != "true"]


def _run(s: Store, gate, driver, health: Health, **kwargs) -> int:
    kwargs.setdefault("sleep", lambda _d: None)
    return loop.run_loop(
        s, "beta", driver,
        clock=lambda: 0.0,
        max_turns=1,
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


@pytest.mark.parametrize("lock_name", ["ledger.lock", "message-publication"])
def test_contention_before_admission_is_retried_then_recovers(
    tmp_path, monkeypatch, lock_name,
) -> None:
    s, m = _bus(tmp_path)
    barrier = LockBarrier(monkeypatch)
    barrier.arm(lock_name, hold=BOUND + 1)
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
    # The first acquisition of either lock is inside the commit gate, which folds
    # contention into a fail-closed result: the diagnostic names that gate reason.
    assert health.diagnostics == [{
        "phase": loop.LOCK_CONTENTION_PHASE_ADMISSION,
        "lock": (obligations.LEDGER_REPLAY_CONTENDED if lock_name == "ledger.lock"
                 else obligations.LANDED_REPLAY_CONTENDED),
        "lock_file": None,
        "attempts": BOUND,
    }]


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


def test_ledger_finalization_contention_retains_success_and_never_redrives(
    tmp_path, monkeypatch,
) -> None:
    # An active gate with a not-owed head and no reply: the durable completion
    # proof is the retained no-admission success, then the ledger-guarded cursor.
    s, m = _bus(tmp_path, kind="message")
    barrier = LockBarrier(monkeypatch)
    driver = CountedDriver(reply=None, on_complete=lambda: barrier.arm(
        "ledger.lock", hold=BOUND + 1))
    health = Health()
    turns = _run(s, _active_gate(s), driver, health)

    assert turns == 1 and driver.calls == 1
    assert barrier.hits["ledger.lock"] == BOUND + 1
    assert s.cursor("beta") == m.id
    assert _replies(s) == []
    assert set(health.contended_phases()) == {loop.LOCK_CONTENTION_PHASE_FINALIZATION}
    assert health.recovered()
    assert len(health.diagnostics) == 1


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
