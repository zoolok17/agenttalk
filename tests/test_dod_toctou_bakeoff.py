"""Deterministic bakeoff harness for the #66 DoD publish TOCTOU race.

This harness contains NO fix. It exists so the two candidate fixes for #66 can be
measured against ONE identical, fix-agnostic, deterministic set of tests:

  (a) re-resolve DoD evidence under the commit lock (serialize mutations), vs
  (b) a generation guard (re-check evidence at commit, refuse on change).

THE RACE. `agenttalk close publish` (cli.cmd_close, publish action) resolves the DoD
evidence -- `check_gates` + `_build_dod_eval` -> `close.compute_verdict` -- and THEN
persists the verdict via `close.record_publish` + `transaction.commit()`. That whole
span holds only the per-close lock (`close.close_transaction`); it does NOT hold the
lock that guards the evidence sources (gates.json / knowledge notes, mutated by
`gate set` / `knowledge curate`). So evidence read as GO-worthy can change before the
commit, and master persists a stale GO.

DETERMINISM. We do NOT race threads or sleep for timing. We inject a pause SEAM by
monkeypatching `close.compute_verdict` to block (on a threading.Event) on its first
call -- i.e. after the evidence was resolved, before the verdict is persisted. A
monotonic event counter stamps (i) when the concurrent evidence mutation COMPLETES
and (ii) when the GO is persisted (`record_publish`). The invariant is checked by
ORDER, not wall-clock:

    INVARIANT (fix-agnostic): if the evidence mutation completed BEFORE the commit,
    the publish must NOT have persisted GO.

  * master  -> mutation completes before commit AND GO persisted  => VIOLATION (fail).
  * fix (b) -> re-resolve at commit sees the change => publish refuses (HOLD)     => ok.
  * fix (a) -> mutation blocks on the commit-held lock, lands AFTER commit; the GO
               was valid AT commit                                                => ok.

The only bounded wait (`_MUTATE_BLOCK_PROBE_S`) is a "did the mutation block on a
lock?" probe (fix (a) serializes it), NOT a correctness race: a local gate write is
sub-millisecond, so 2s is an enormous, non-flaky margin.
"""

from __future__ import annotations

import itertools
import json
import threading
from pathlib import Path

import pytest

from agenttalk import cli, close, gates
from agenttalk.store import Store

SHA = "a" * 40

# Bounded probe: how long to wait for the concurrent mutation to COMPLETE before we
# resume the paused publish. Master/fix-b: the local gate write finishes in <1ms, so
# it always completes. Fix-a: it blocks on the lock the publish holds, so it does not
# complete until publish commits -> we time out and resume, and the event counter
# records that the mutation landed AFTER the commit. 2s is a huge margin over a
# sub-ms file write; it is a blocked-vs-not discriminator, not a timing assertion.
_MUTATE_BLOCK_PROBE_S = 2.0
# Hard watchdog so a genuine deadlock (a bad fix) fails the test instead of hanging CI.
_WATCHDOG_S = 30.0


# --------------------------------------------------------------- shared scaffolding

def _init(tmp_path: Path) -> Path:
    s = Store(tmp_path)
    s.init(["lead", "codex"])
    s.set_role("lead", "lead")
    return tmp_path


def _run(argv: list[str], root: Path) -> int:
    return cli.main(["--root", str(root), *argv])


def _write_dod(root: Path) -> None:
    close.dod_policy_path(Store(root)).write_text(json.dumps({
        "schema_version": 1,
        "scopes": {"release": {"assurance": {
            "gate": "assurance:release", "max_age_days": 14}}},
    }), encoding="utf-8")


def _open_and_accept(root: Path) -> None:
    assert _run(["close", "open", "--id", "rel", "--from", "lead",
                 "--scope", "release", "--revision", SHA,
                 "--lens", "sec", "--allow", "sec:codex",
                 "--non-lane-isolation-not-asserted"], root) == 0
    assert _run(["close", "ack", "--id", "rel", "--lens", "sec", "--status",
                 "accept", "--from", "codex", "--risk-class", "none",
                 "--release-blocker", "no", "--tests-referenced", "n/a",
                 "--tests-executed", "n/a", "--residual-risk", "n/a",
                 "--na-reason", "lw", "--evidence", "pointer:rq-1"], root) == 0


def _set_green_ci_gate(root: Path, *, revision: str = SHA) -> None:
    """A green, CI-attested, revision-bound blocker gate -> assurance dimension clears."""
    gates.set_gate(root, name="assurance:release", status="green", severity="blocker",
                   scope="release", actor="ci", evidence_source="automation_ci",
                   evidence=["run:ci-1"], revision=revision)


def _setup_go(tmp_path: Path) -> Path:
    """A close that, on a clean publish, is GO."""
    root = _init(tmp_path)
    _write_dod(root)
    _open_and_accept(root)
    _set_green_ci_gate(root)
    return root


def _persisted_final_verdict(root: Path) -> str | None:
    p = Path(root) / ".agenttalk" / "closes" / "rel.json"
    if not p.exists():
        return None
    rec = json.loads(p.read_text(encoding="utf-8"))
    final = rec.get("final") or {}
    return final.get("verdict")


# --------------------------------------------------------------- the TOCTOU driver

class _Bakeoff:
    """Drives one paused-publish + concurrent-mutation interleave, fix-agnostically."""

    def __init__(self, root: Path, mutate) -> None:
        self.root = root
        self._mutate = mutate            # callable(root) -> mutate the evidence invalid
        self._seq = itertools.count(1)
        self._seq_lock = threading.Lock()
        self.resolved = threading.Event()
        self.proceed = threading.Event()
        self.mutate_done = threading.Event()
        self.commit_seq: int | None = None
        self.mutate_seq: int | None = None
        self.publish_rc: int | None = None
        self._first = True

    def _next(self) -> int:
        with self._seq_lock:
            return next(self._seq)

    def _verdict_seam(self, real):
        def wrapper(*a, **k):
            if self._first:
                self._first = False
                self.resolved.set()               # evidence resolved; commit not yet done
                self.proceed.wait(_WATCHDOG_S)     # bounded so a deadlock fails, not hangs
            return real(*a, **k)
        return wrapper

    def _record_publish_seam(self, real):
        def wrapper(record, *a, verdict=None, **k):
            if verdict == close.VERDICT_GO:
                self.commit_seq = self._next()     # a GO is being persisted
            return real(record, *a, verdict=verdict, **k)
        return wrapper

    def _mutate_thread(self) -> None:
        if not self.resolved.wait(_WATCHDOG_S):
            return
        self._mutate(self.root)                    # may BLOCK on a lock under fix (a)
        self.mutate_seq = self._next()
        self.mutate_done.set()

    def run(self, monkeypatch) -> None:
        monkeypatch.setattr(close, "compute_verdict",
                            self._verdict_seam(close.compute_verdict))
        monkeypatch.setattr(close, "record_publish",
                            self._record_publish_seam(close.record_publish))

        def publish() -> None:
            self.publish_rc = _run(
                ["close", "publish", "--id", "rel", "--verdict", "go",
                 "--from", "lead"], self.root)

        pub = threading.Thread(target=publish, name="publish")
        mut = threading.Thread(target=self._mutate_thread, name="mutate")
        pub.start()
        mut.start()
        # Wait until the publish has resolved the evidence and is paused.
        assert self.resolved.wait(_WATCHDOG_S), "publish never reached the resolve seam"
        # Give the concurrent mutation a bounded chance to COMPLETE before we let the
        # publish commit. If it blocks on a lock (fix a), it will not complete and we
        # resume anyway -> the counter records it landed after the commit.
        self.mutate_done.wait(_MUTATE_BLOCK_PROBE_S)
        self.proceed.set()
        pub.join(_WATCHDOG_S)
        mut.join(_WATCHDOG_S)
        assert not pub.is_alive(), "DEADLOCK: publish did not finish (watchdog)"
        assert not mut.is_alive(), "DEADLOCK: mutation did not finish (watchdog)"

    @property
    def persisted_go(self) -> bool:
        return self.publish_rc == 0 and _persisted_final_verdict(self.root) == close.VERDICT_GO

    @property
    def mutation_before_commit(self) -> bool:
        # The mutation completed AND it did so before the GO was persisted.
        return (self.mutate_seq is not None and self.commit_seq is not None
                and self.mutate_seq < self.commit_seq)

    def assert_invariant(self) -> None:
        # FIX-AGNOSTIC: a GO must never be persisted when the evidence was already
        # invalidated before the commit. Master violates this; both fixes must not.
        assert not (self.persisted_go and self.mutation_before_commit), (
            "TOCTOU: publish persisted GO although the evidence was mutated invalid "
            f"before the commit (mutate_seq={self.mutate_seq} < "
            f"commit_seq={self.commit_seq}, publish_rc={self.publish_rc})")


# --------------------------------------------------------------- evidence mutations

def _mutate_gate_red(root: Path) -> None:
    gates.set_gate(root, name="assurance:release", status="red", severity="blocker",
                   scope="release", actor="ci", evidence_source="automation_ci",
                   reason="flipped")


def _mutate_gate_wrong_revision(root: Path) -> None:
    gates.set_gate(root, name="assurance:release", status="green", severity="blocker",
                   scope="release", actor="ci", evidence_source="automation_ci",
                   evidence=["run:ci-2"], revision="c" * 40)


def _mutate_gate_non_ci(root: Path) -> None:
    # A green gate that is no longer a CI attestation (manual_review) must not clear.
    gates.set_gate(root, name="assurance:release", status="green", severity="warn",
                   scope="release", actor="dev", evidence_source="manual_review",
                   evidence=["local"])


_VECTORS = {
    "gate_red": _mutate_gate_red,
    "gate_wrong_revision": _mutate_gate_wrong_revision,
    "gate_non_ci_attestation": _mutate_gate_non_ci,
}


# ------------------------------------------------------------------------- tests

def test_toctou_gate_flip_between_resolve_and_commit(tmp_path: Path, monkeypatch) -> None:
    """Headline race: gate flipped red after resolve, before commit. FAILS on master."""
    root = _setup_go(tmp_path)
    bake = _Bakeoff(root, _mutate_gate_red)
    bake.run(monkeypatch)
    bake.assert_invariant()


@pytest.mark.parametrize("vector", sorted(_VECTORS))
def test_toctou_each_evidence_vector(tmp_path: Path, monkeypatch, vector: str) -> None:
    """Guard-COMPLETENESS: every mutation that invalidates assurance must be caught.

    Fix (b) must catch ALL of these at re-resolve, not just the headline gate flip.
    Each FAILS on master (stale GO persisted)."""
    root = _setup_go(tmp_path)
    bake = _Bakeoff(root, _VECTORS[vector])
    bake.run(monkeypatch)
    bake.assert_invariant()


def test_publish_no_pause_still_go(tmp_path: Path, capsys) -> None:
    """Control: with no interleaved mutation, a satisfied close publishes GO.

    Guards against a fix that 'passes' the race tests by breaking publish outright."""
    root = _setup_go(tmp_path)
    capsys.readouterr()
    rc = _run(["close", "check", "--id", "rel", "--json"], root)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["holds"] == [], f"clean check should be GO: {out['holds']}"
    assert _run(["close", "publish", "--id", "rel", "--verdict", "go",
                 "--from", "lead"], root) == 0
    assert _persisted_final_verdict(root) == close.VERDICT_GO


def test_deadlock_probe_publish_vs_gate_mutation(tmp_path: Path, monkeypatch) -> None:
    """Deadlock probe. Forces a publish (paused mid-transaction, holding the per-close
    lock) to overlap a concurrent gate mutation, bounded by a hard watchdog.

    On master there is no lock shared between publish and `gate set`, so this completes
    (PASS). Its value is against a FIX: if fix (a) makes publish hold a lock that the
    mutation also needs in the opposite order, this overlap becomes a cycle and the
    watchdog assertion fires. LIMITATION: a true cycle requires a config->close lock
    ordering elsewhere; this probe forces the overlap and proves no-hang, it does not
    exhaustively search all interleavings."""
    root = _setup_go(tmp_path)
    bake = _Bakeoff(root, _mutate_gate_red)
    bake.run(monkeypatch)          # the run() watchdog asserts neither thread hangs
    # Reaching here means publish + concurrent mutation both completed (no deadlock).
    assert bake.publish_rc is not None
