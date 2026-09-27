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
