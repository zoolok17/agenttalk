"""Tests for the assurance P2 milestone/release CLOSE protocol.

Two layers, mirroring gates/build_report/plan_actions:

* PURE verdict tests - `compute_verdict` over synthetic (record, gate_check)
  inputs, one per STABLE hold code plus the GO case. No I/O.
* CLI integration - `main(argv)` against a real store, proving gate state and
  waivers drive HOLD/GO, that a refused GO does not publish, and that a GO
  publish with --bump-barrier fires the release barrier (and a HOLD never does).

Integration tests use a full 40-char SHA as --revision so revision freeze is
hermetic: pytest's tmp_path is not a git repo, so `_resolve_revision` falls
through to the pinned-SHA branch and `_worktree_clean` reports None -> clean.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import threading

import pytest

from agenttalk import cli, close, gates
from agenttalk import knowledge as kn
from agenttalk.store import Store

SHA = "a" * 40
OTHER_SHA = "b" * 40


# --------------------------------------------------------------- pure helpers

def _gate_go(gates: list[dict] | None = None) -> dict:
    return {"verdict": "GO", "required_gates": [], "blockers": [],
            "gates": gates or []}


def _gate_hold() -> dict:
    item = {"name": "ci", "status": "red", "severity": "blocker", "blocks": True}
    return {"verdict": "HOLD", "required_gates": ["ci"], "blockers": [item],
            "gates": [item]}


def _satisfied() -> dict:
    rec = close.empty_close(
        "c1", scope="release", revision=SHA, revision_kind="sha",
        gate_scope="release", opened_by="lead", opened_at="t0", epoch_at_open=None,
        required_lenses=[close.validate_lens_spec(
            {"id": "sec", "allowed_agents": ["codex"]})],
        revision_clean=True, dirty_artifact=None,
        non_lane_isolation_not_asserted=True)
    close.apply_ack(rec, lens_id="sec", status="accept", agent="codex",
                    from_role=None, at="t1", evidence={"risk_class": "none"})
    return rec


def _codes(result: dict) -> set[str]:
    return {h["code"] for h in result["holds"]}


# --------------------------------------------------------------- pure: GO

def test_satisfied_close_is_go() -> None:
    result = close.compute_verdict(_satisfied(), _gate_go())
    assert result["verdict"] == close.VERDICT_GO
    assert result["ok"] is True
    assert result["holds"] == []


# --------------------------------------------------- pure: one per HOLD code

def test_hold_malformed_state() -> None:
    result = close.compute_verdict({"not": "a close"}, _gate_go())
    assert result["verdict"] == close.VERDICT_HOLD
    assert _codes(result) == {close.HOLD_MALFORMED}  # short-circuits everything else


def test_hold_publish_not_allowed_when_published() -> None:
    rec = _satisfied()
    rec["status"] = close.PUBLISHED
    assert close.HOLD_PUBLISH_NOT_ALLOWED in _codes(
        close.compute_verdict(rec, _gate_go()))


def test_hold_revision_dirty_without_artifact() -> None:
    rec = _satisfied()
    rec["revision_clean"] = False
    rec["dirty_artifact"] = None
    assert close.HOLD_REVISION in _codes(close.compute_verdict(rec, _gate_go()))


def test_dirty_with_artifact_clears_revision_hold() -> None:
    rec = _satisfied()
    rec["revision_clean"] = False
    rec["dirty_artifact"] = "pointer:diff-1"
    assert close.compute_verdict(rec, _gate_go())["verdict"] == close.VERDICT_GO


def test_hold_revision_unresolved_when_not_full_sha() -> None:
    rec = _satisfied()
    rec["revision"] = "deadbeef"  # short / unresolved
    assert close.HOLD_REVISION in _codes(close.compute_verdict(rec, _gate_go()))


def test_hold_gate_when_gate_check_holds() -> None:
    assert close.HOLD_GATE in _codes(close.compute_verdict(_satisfied(), _gate_hold()))


def test_hold_missing_lens() -> None:
    rec = _satisfied()
    rec["lens_acks"] = {}
    assert close.HOLD_MISSING_LENS in _codes(close.compute_verdict(rec, _gate_go()))


def test_malformed_persisted_lens_ack_cannot_satisfy_required_lens(
    tmp_path: Path,
) -> None:
    rec = _satisfied()
    rec["lens_acks"]["sec"]["status"] = "not-a-verdict"
    store = Store(tmp_path)
    store.init(["lead", "codex"])
    close.closes_dir(store).mkdir(parents=True)
    close.close_path(store, "c1").write_text(json.dumps(rec), encoding="utf-8")

    with pytest.raises(close.CloseError, match="malformed"):
        close.load_close(store, "c1")


def test_hold_unauthorized_lens_ack() -> None:
    rec = _satisfied()
    rec["lens_acks"]["sec"]["from"] = "mallory"  # not in allowed_agents
    assert close.HOLD_UNAUTHORIZED_ACK in _codes(close.compute_verdict(rec, _gate_go()))


def test_override_authorizes_otherwise_unauthorized_ack() -> None:
    rec = _satisfied()
    rec["lens_acks"]["sec"]["from"] = "mallory"
    rec["lens_acks"]["sec"]["override"] = True  # recorded lead/operator override
    assert close.compute_verdict(rec, _gate_go())["verdict"] == close.VERDICT_GO


def test_role_authorizes_lens_ack() -> None:
    rec = close.empty_close(
        "c2", scope="release", revision=SHA, revision_kind="sha",
        gate_scope="release", opened_by="lead", opened_at="t", epoch_at_open=None,
        required_lenses=[close.validate_lens_spec(
            {"id": "sec", "allowed_roles": ["reviewer"]})],
        revision_clean=True, dirty_artifact=None,
        non_lane_isolation_not_asserted=True)
    close.apply_ack(rec, lens_id="sec", status="accept", agent="anyone",
                    from_role="reviewer", at="t")
    assert close.compute_verdict(rec, _gate_go())["verdict"] == close.VERDICT_GO


def test_hold_stale_lens_ack_on_revision_change() -> None:
    rec = _satisfied()
    rec["lens_acks"]["sec"]["revision"] = OTHER_SHA  # ack reviewed a different SHA
    assert close.HOLD_STALE_ACK in _codes(close.compute_verdict(rec, _gate_go()))


def test_hold_undecided_counter() -> None:
    rec = _satisfied()
    close.apply_ack(rec, lens_id="sec", status="counter", agent="codex",
                    from_role=None, at="t2", counter_id="ctr-1",
                    evidence={"finding": "leak"})
    assert close.HOLD_UNDECIDED_COUNTER in _codes(close.compute_verdict(rec, _gate_go()))


def test_hold_accepted_counter_missing_remediation() -> None:
    rec = _satisfied()
    rec["counters"]["ctr-1"] = {
        "counter_id": "ctr-1", "lens": "sec", "decision": close.COUNTER_ACCEPTED,
        "remediation_id": None}
    assert close.HOLD_COUNTER_NO_REMEDIATION in _codes(
        close.compute_verdict(rec, _gate_go()))


def _blocker_rem_record() -> dict:
    rec = _satisfied()
    rec["counters"]["ctr-1"] = {
        "counter_id": "ctr-1", "lens": "sec", "decision": close.COUNTER_ACCEPTED,
        "remediation_id": "rem-1"}
    rec["remediation_items"]["rem-1"] = {
        "id": "rem-1", "blocker": True, "gate": "fix-gate"}
    return rec


def test_hold_open_blocker_remediation_until_gate_green() -> None:
    rec = _blocker_rem_record()
    # gate not present -> HOLD
    assert close.HOLD_OPEN_BLOCKER in _codes(close.compute_verdict(rec, _gate_go()))
    # name the gate green AND non-blocking -> GO
    green = _gate_go([{"name": "fix-gate", "status": "green", "blocks": False}])
    assert close.compute_verdict(rec, green)["verdict"] == close.VERDICT_GO
    # a waived (active) gate also resolves the blocker
    waived = _gate_go([{"name": "fix-gate", "status": "waived", "blocks": False}])
    assert close.compute_verdict(rec, waived)["verdict"] == close.VERDICT_GO


@pytest.mark.parametrize("gate", [
    {"name": "fix-gate", "status": "red", "severity": "warn", "blocks": False},
    {"name": "fix-gate", "status": "skipped", "severity": "blocker", "blocks": False},
    {"name": "fix-gate", "status": "unknown", "severity": "info", "blocks": False},
    {"name": "fix-gate", "blocks": False},  # status absent
])
def test_blocker_remediation_not_resolved_by_merely_nonblocking_gate(gate: dict) -> None:
    # codex finding: a non-blocking but NOT green/waived gate must NOT resolve a
    # blocker remediation (else a red/warn or skipped gate would yield a false GO).
    rec = _blocker_rem_record()
    result = close.compute_verdict(rec, _gate_go([gate]))
    assert result["verdict"] == close.VERDICT_HOLD
    assert close.HOLD_OPEN_BLOCKER in _codes(result)


def test_nonblocker_accepted_counter_with_remediation_is_go() -> None:
    rec = _satisfied()
    rec["counters"]["ctr-1"] = {
        "counter_id": "ctr-1", "lens": "sec", "decision": close.COUNTER_ACCEPTED,
        "remediation_id": "rem-1"}
    rec["remediation_items"]["rem-1"] = {"id": "rem-1", "blocker": False, "gate": None}
    assert close.compute_verdict(rec, _gate_go())["verdict"] == close.VERDICT_GO


# --------------------------------------------------- pure: transitions guard

def test_apply_ack_na_requires_reason() -> None:
    rec = _satisfied()
    with pytest.raises(close.CloseError):
        close.apply_ack(rec, lens_id="sec", status="na", agent="codex",
                        from_role=None, at="t")


def test_apply_ack_refused_after_publish() -> None:
    rec = _satisfied()
    rec["status"] = close.PUBLISHED
    with pytest.raises(close.CloseError):
        close.apply_ack(rec, lens_id="sec", status="accept", agent="codex",
                        from_role=None, at="t")


def test_apply_ack_rejects_duplicate_counter_id_without_mutating_ack() -> None:
    rec = _satisfied()
    close.apply_ack(rec, lens_id="sec", status="counter", agent="codex",
                    from_role=None, at="t1", counter_id="ctr-1")
    first_ack = dict(rec["lens_acks"]["sec"])

    with pytest.raises(close.CloseError, match="duplicate counter"):
        close.apply_ack(rec, lens_id="sec", status="counter", agent="codex",
                        from_role=None, at="t2", counter_id="ctr-1")

    assert rec["lens_acks"]["sec"] == first_ack


def test_decide_counter_accept_requires_remediation() -> None:
    rec = _satisfied()
    rec["counters"]["ctr-1"] = {"counter_id": "ctr-1", "decision": close.COUNTER_PENDING}
    with pytest.raises(close.CloseError):
        close.decide_counter(rec, counter_id="ctr-1", decision="accept", by="lead",
                             at="t", reason="ok", remediation=None)


def test_decide_counter_blocker_remediation_requires_gate() -> None:
    rec = _satisfied()
    rec["counters"]["ctr-1"] = {"counter_id": "ctr-1", "decision": close.COUNTER_PENDING}
    with pytest.raises(close.CloseError):
        close.decide_counter(
            rec, counter_id="ctr-1", decision="accept", by="lead", at="t",
            reason="fix it", remediation={
                "id": "rem-1", "owner": "dev", "fix": "patch",
                "verification": "tests", "blocker": True})  # no gate


def test_reopen_clears_final_and_revision_change_stales_acks() -> None:
    rec = _satisfied()
    close.record_publish(rec, verdict=close.VERDICT_GO, by="lead", at="t",
                         reason="ship", gate_check=_gate_go(), residual_risk=None,
                         barrier_epoch="ep-1")
    assert rec["status"] == close.PUBLISHED
    close.reopen(rec, by="op", at="t", revision=OTHER_SHA, revision_clean=True)
    assert rec["status"] == close.REOPENED
    assert rec["final"] is None
    # the prior ack reviewed SHA, the record now pins OTHER_SHA -> stale
    assert close.HOLD_STALE_ACK in _codes(close.compute_verdict(rec, _gate_go()))


def test_reopen_revision_change_clears_prior_dirty_artifact() -> None:
    rec = _satisfied()
    rec["dirty_artifact"] = "artifacts/old-revision.diff"

    close.reopen(rec, by="op", at="t", revision=OTHER_SHA, revision_clean=False)

    assert rec["dirty_artifact"] is None


# --------------------------------------------------------------- integration

def _init(tmp_path: Path) -> Path:
    s = Store(tmp_path)
    s.init(["lead", "codex", "dev2"])
    s.set_role("lead", "lead")
    return tmp_path


def _run(argv: list[str], root: Path) -> int:
    return cli.main(["--root", str(root), *argv])


def _open(root: Path, *extra: str) -> int:
    return _run(["close", "open", "--id", "rel", "--from", "lead",
                 "--scope", "release", "--revision", SHA,
                 "--lens", "sec", "--allow", "sec:codex",
                 "--non-lane-isolation-not-asserted", *extra], root)


def _accept(root: Path) -> int:
    return _run(["close", "ack", "--id", "rel", "--lens", "sec", "--status",
                 "accept", "--from", "codex", "--risk-class", "none",
                 "--release-blocker", "no", "--tests-referenced", "n/a",
                 "--tests-executed", "n/a", "--residual-risk", "n/a",
                 "--na-reason", "lw", "--evidence", "pointer:rq-1"], root)


def _release_barriers(store: Store) -> list:
    return [
        message for message in store.valid_messages()
        if isinstance((message.meta or {}).get("close_barrier"), dict)
    ]


def test_concurrent_close_saves_reject_one_stale_generation(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.init(["lead"])
    rec = close.empty_close(
        "rel", scope="release", revision=SHA, revision_kind="sha",
        gate_scope="release", opened_by="lead", opened_at="t0",
        epoch_at_open=None, required_lenses=[], revision_clean=True,
        dirty_artifact=None, non_lane_isolation_not_asserted=True)
    close.create_close(store, rec)
    generation = rec["generation"]
    instance_id = rec["instance_id"]
    ready = threading.Barrier(3)

    def update(body: str) -> str:
        local = close.load_close(store, "rel")
        close.set_draft(local, body=body, by="lead", at=body)
        ready.wait()
        try:
            close.save_close(
                store, local, expected_generation=generation,
                expected_instance_id=instance_id,
            )
        except close.CloseConflict:
            return "conflict"
        return "saved"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(update, body) for body in ("first", "second")]
        ready.wait()
        outcomes = sorted(f.result() for f in futures)

    assert outcomes == ["conflict", "saved"]
    stored = close.load_close(store, "rel")
    assert stored["generation"] == generation + 1
    assert stored["draft"]["body"] in {"first", "second"}


def test_existing_close_rejects_save_without_expected_generation(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.init(["lead"])
    rec = close.empty_close(
        "rel", scope="release", revision=SHA, revision_kind="sha",
        gate_scope="release", opened_by="lead", opened_at="t0",
        epoch_at_open=None, required_lenses=[], revision_clean=True,
        dirty_artifact=None, non_lane_isolation_not_asserted=True)
    close.create_close(store, rec)
    loaded = close.load_close(store, "rel")
    close.set_draft(loaded, body="unchecked", by="lead", at="t1")

    with pytest.raises(close.CloseConflict, match="expected_generation"):
        close.save_close(store, loaded)

    assert close.load_close(store, "rel")["draft"] is None


def test_existing_close_rejects_save_without_expected_instance(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.init(["lead"])
    rec = close.empty_close(
        "rel", scope="release", revision=SHA, revision_kind="sha",
        gate_scope="release", opened_by="lead", opened_at="t0",
        epoch_at_open=None, required_lenses=[], revision_clean=True,
        dirty_artifact=None, non_lane_isolation_not_asserted=True)
    close.create_close(store, rec)
    loaded = close.load_close(store, "rel")

    with pytest.raises(close.CloseConflict, match="expected_instance_id"):
        close.save_close(store, loaded, expected_generation=loaded["generation"])


def test_delete_recreate_rejects_stale_instance_at_same_generation(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.init(["lead"])
    original = close.empty_close(
        "rel", scope="release", revision=SHA, revision_kind="sha",
        gate_scope="release", opened_by="lead", opened_at="t0",
        epoch_at_open=None, required_lenses=[], revision_clean=True,
        dirty_artifact=None, non_lane_isolation_not_asserted=True)
    close.create_close(store, original)
    stale = close.load_close(store, "rel")
    stale_generation = stale["generation"]
    stale_instance = stale["instance_id"]
    close.close_path(store, "rel").unlink()
    replacement = close.empty_close(
        "rel", scope="release", revision=OTHER_SHA, revision_kind="sha",
        gate_scope="release", opened_by="lead", opened_at="t1",
        epoch_at_open=None, required_lenses=[], revision_clean=True,
        dirty_artifact=None, non_lane_isolation_not_asserted=True)
    close.create_close(store, replacement)
    assert replacement["generation"] == stale_generation
    assert replacement["instance_id"] != stale_instance
    close.set_draft(stale, body="stale overwrite", by="lead", at="t2")

    with pytest.raises(close.CloseConflict, match="instance"):
        close.save_close(
            store, stale, expected_generation=stale_generation,
            expected_instance_id=stale_instance,
        )

    stored = close.load_close(store, "rel")
    assert stored["revision"] == OTHER_SHA
    assert stored["draft"] is None


def test_create_close_is_exclusive(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.init(["lead"])
    first = close.empty_close(
        "rel", scope="release", revision=SHA, revision_kind="sha",
        gate_scope="release", opened_by="lead", opened_at="t0",
        epoch_at_open=None, required_lenses=[], revision_clean=True,
        dirty_artifact=None, non_lane_isolation_not_asserted=True)
    second = close.empty_close(
        "rel", scope="release", revision=OTHER_SHA, revision_kind="sha",
        gate_scope="release", opened_by="lead", opened_at="t1",
        epoch_at_open=None, required_lenses=[], revision_clean=True,
        dirty_artifact=None, non_lane_isolation_not_asserted=True)
    close.create_close(store, first)

    with pytest.raises(close.CloseConflict, match="already exists"):
        close.create_close(store, second)

    assert close.load_close(store, "rel")["revision"] == SHA


def test_legacy_close_requires_in_lock_upgrade_before_checked_save(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.init(["lead"])
    rec = close.empty_close(
        "rel", scope="release", revision=SHA, revision_kind="sha",
        gate_scope="release", opened_by="lead", opened_at="t0",
        epoch_at_open=None, required_lenses=[], revision_clean=True,
        dirty_artifact=None, non_lane_isolation_not_asserted=True)
    rec.pop("generation")
    rec.pop("instance_id")
    path = close.close_path(store, "rel")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec), encoding="utf-8")

    loaded = close.load_close(store, "rel")
    assert loaded["generation"] == 0
    assert loaded.get("instance_id") is None
    with pytest.raises(close.CloseConflict, match="legacy"):
        close.save_close(store, loaded, expected_generation=0,
                         expected_instance_id=None)

    upgraded = close.upgrade_legacy_close(store, "rel")
    assert upgraded["generation"] == 1
    assert isinstance(upgraded["instance_id"], str)
    close.set_draft(upgraded, body="migrated", by="lead", at="t1")
    close.save_close(
        store, upgraded, expected_generation=1,
        expected_instance_id=upgraded["instance_id"],
    )

    stored = close.load_close(store, "rel")
    assert stored["generation"] == 2
    assert stored["draft"]["body"] == "migrated"


def test_cli_mutation_upgrades_legacy_close_inside_transaction(tmp_path: Path) -> None:
    root = _init(tmp_path)
    assert _open(root) == 0
    store = Store(root)
    path = close.close_path(store, "rel")
    legacy = json.loads(path.read_text(encoding="utf-8"))
    legacy.pop("generation")
    legacy.pop("instance_id")
    path.write_text(json.dumps(legacy), encoding="utf-8")

    assert _run([
        "close", "draft", "--id", "rel", "--from", "lead", "-m", "latest",
    ], root) == 0

    stored = close.load_close(store, "rel")
    assert stored["generation"] == 2
    assert close.close_instance_id(stored) is not None
    assert stored["draft"]["body"] == "latest"


def test_cli_full_go_lifecycle(tmp_path: Path) -> None:
    root = _init(tmp_path)
    assert _open(root) == 0
    assert _run(["close", "check", "--id", "rel"], root) == 3  # missing lens
    assert _accept(root) == 0
    assert _run(["close", "check", "--id", "rel"], root) == 0  # GO
    assert _run(["close", "publish", "--id", "rel", "--from", "lead",
                 "--verdict", "go"], root) == 0
    rec = close.load_close(Store(root), "rel")
    assert rec["status"] == close.PUBLISHED
    assert rec["final"]["verdict"] == close.VERDICT_GO


@pytest.mark.parametrize("mutation", ["ack", "counter"])
def test_cli_publish_serializes_against_ack_and_counter(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str) -> None:
    root = _init(tmp_path)
    assert _open(root) == 0
    assert _accept(root) == 0
    store = Store(root)
    evaluated = threading.Event()
    release_evaluation = threading.Event()
    mutation_waiting = threading.Event()
    real_compute = close.compute_verdict
    real_update_lock = close._close_update_lock

    def paused_compute(*args, **kwargs):
        if threading.current_thread().name == "close-publisher":
            evaluated.set()
            if not release_evaluation.wait(5):
                raise AssertionError("publisher evaluation was not released")
        return real_compute(*args, **kwargs)

    def observed_update_lock(lock_store, close_id, *, timeout):
        if threading.current_thread().name == "close-mutation":
            mutation_waiting.set()
        return real_update_lock(lock_store, close_id, timeout=timeout)

    monkeypatch.setattr(close, "compute_verdict", paused_compute)
    monkeypatch.setattr(close, "_close_update_lock", observed_update_lock)
    results: dict[str, int] = {}
    errors: list[BaseException] = []

    def invoke(key: str, argv: list[str]) -> None:
        try:
            results[key] = _run(argv, root)
        except BaseException as error:  # surfaced in the test thread
            errors.append(error)

    publish = threading.Thread(
        target=invoke,
        args=("publish", [
            "close", "publish", "--id", "rel", "--from", "lead",
            "--verdict", "go",
        ]),
        name="close-publisher",
    )
    mutation_argv = [
        "close", "ack", "--id", "rel", "--lens", "sec", "--from", "codex",
    ]
    if mutation == "counter":
        mutation_argv += [
            "--status", "counter", "--counter", "late-counter",
            "--finding", "arrived during publish",
        ]
    else:
        mutation_argv += ["--status", "na", "--reason", "arrived during publish"]
    mutate = threading.Thread(
        target=invoke, args=("mutation", mutation_argv), name="close-mutation")

    publish.start()
    assert evaluated.wait(5)
    assert (close.closes_dir(store) / ".rel.lock").exists()
    mutate.start()
    assert mutation_waiting.wait(5)
    assert close.load_close(store, "rel")["status"] == close.OPEN
    release_evaluation.set()
    publish.join(10)
    mutate.join(10)

    assert not publish.is_alive()
    assert not mutate.is_alive()
    assert errors == []
    assert results == {"publish": 0, "mutation": 2}
    stored = close.load_close(store, "rel")
    assert stored["status"] == close.PUBLISHED
    assert stored["final"]["verdict"] == close.VERDICT_GO
    assert stored["lens_acks"]["sec"]["status"] == close.ACCEPT
    assert "late-counter" not in stored["counters"]


def test_cli_force_open_rejects_delete_recreate_aba(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    root = _init(tmp_path)
    assert _open(root) == 0
    store = Store(root)
    original = close.load_close(store, "rel")
    real_replace = close.replace_close

    def recreate_then_replace(replace_store, record, **expected):
        close.close_path(replace_store, "rel").unlink()
        recreated = close.empty_close(
            "rel", scope="release", revision=OTHER_SHA, revision_kind="sha",
            gate_scope="release", opened_by="lead", opened_at="recreated",
            epoch_at_open=None, required_lenses=[], revision_clean=True,
            dirty_artifact=None, non_lane_isolation_not_asserted=True)
        close.create_close(replace_store, recreated)
        return real_replace(replace_store, record, **expected)

    monkeypatch.setattr(close, "replace_close", recreate_then_replace)

    assert _open(root, "--force") == 3
    error = capsys.readouterr().err
    assert "HOLD" in error
    assert "retry" in error
    stored = close.load_close(store, "rel")
    assert stored["revision"] == OTHER_SHA
    assert stored["instance_id"] != original["instance_id"]


def test_m6_release_close_without_lane_artifact_holds(tmp_path: Path) -> None:
    root = _init(tmp_path)
    assert _run(["close", "open", "--id", "rel", "--from", "lead",
                 "--scope", "release", "--revision", SHA], root) == 0
    assert _run(["close", "check", "--id", "rel"], root) == 3
    rec = close.load_close(Store(root), "rel")
    result = close.compute_verdict(
        rec, {"verdict": "GO", "required_gates": [], "blockers": [], "gates": []})
    assert close.HOLD_WORKTREE_ISOLATION in _codes(result)


def test_release_close_waived_artifact_head_mismatch_holds() -> None:
    rec = close.empty_close(
        "rel", scope="release", revision=OTHER_SHA, revision_kind="sha",
        gate_scope="release", opened_by="lead", opened_at="t0",
        epoch_at_open=None, required_lenses=[], revision_clean=True,
        dirty_artifact=None)
    result = close.compute_verdict(
        rec, _gate_go(), worktree_eval={"status": "waived", "delivered_head": SHA})
    assert close.HOLD_WORKTREE_ISOLATION in _codes(result)


def test_cli_gate_hold_drives_close_hold(tmp_path: Path) -> None:
    root = _init(tmp_path)
    _open(root)
    _accept(root)
    # a required, red, release-scoped gate forces HOLD even with the lens satisfied
    _run(["gate", "set", "--from", "lead", "--name", "ci", "--status", "red",
          "--scope", "release", "--required"], root)
    assert _run(["close", "check", "--id", "rel", "--json"], root) == 3


def test_cli_waiver_drives_close_go(tmp_path: Path) -> None:
    root = _init(tmp_path)
    _open(root)
    _accept(root)
    _run(["gate", "set", "--from", "lead", "--name", "ci", "--status", "red",
          "--scope", "release", "--required"], root)
    assert _run(["close", "check", "--id", "rel"], root) == 3
    _run(["gate", "waive", "--from", "lead", "--name", "ci", "--operator", "boss",
          "--reason", "accepted", "--scope", "release", "--expires", "2099-01-01"], root)
    assert _run(["close", "check", "--id", "rel"], root) == 0


def test_cli_publish_go_refused_when_hold(tmp_path: Path) -> None:
    root = _init(tmp_path)
    _open(root)  # no ack yet -> HOLD
    assert _run(["close", "publish", "--id", "rel", "--from", "lead",
                 "--verdict", "go"], root) == 3
    rec = close.load_close(Store(root), "rel")
    assert rec["status"] == close.OPEN  # not published


def test_cli_publish_go_bump_barrier_fires_release_barrier(tmp_path: Path) -> None:
    root = _init(tmp_path)
    store = Store(root)
    assert store.current_epoch() is None
    _open(root)
    _accept(root)
    assert _run(["close", "publish", "--id", "rel", "--from", "lead",
                 "--verdict", "go", "--bump-barrier", "--reason", "ship"], root) == 0
    rec = close.load_close(store, "rel")
    epoch = rec["final"]["barrier_epoch"]
    assert epoch is not None
    assert rec["generation"] == 4  # open, ack, publish, checked barrier stamp
    assert Store(root).current_epoch() == epoch  # the release barrier is now current


def test_cli_publish_barrier_send_failure_resumes_exactly_once(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    root = _init(tmp_path)
    store = Store(root)
    assert _open(root, "--dirty-artifact", "test:working-tree") == 0
    assert _accept(root) == 0
    real_send = Store.send
    failed = False

    def fail_first_barrier(self, **kwargs):
        nonlocal failed
        message = real_send(self, **kwargs)
        if not failed and (kwargs.get("meta") or {}).get("close_barrier"):
            failed = True
            raise OSError("injected post-write barrier send failure")
        return message

    monkeypatch.setattr(Store, "send", fail_first_barrier)
    capsys.readouterr()
    publish = ["close", "publish", "--id", "rel", "--from", "lead",
               "--verdict", "go", "--bump-barrier", "--reason", "ship"]
    assert _run(publish, root) == 2
    assert "retry" in capsys.readouterr().err.lower()
    pending = close.load_close(store, "rel")
    assert pending["status"] == close.PUBLISHED
    assert pending["final"]["barrier_epoch"] is None
    binding = pending["final"]["barrier_binding"]
    assert binding == {
        "version": 1,
        "close_id": "rel",
        "instance_id": pending["instance_id"],
        "revision": pending["revision"],
        "generation": pending["generation"],
    }
    sent = _release_barriers(store)
    assert len(sent) == 1
    assert sent[0].meta["close_barrier"] == binding

    monkeypatch.setattr(Store, "send", real_send)
    assert _run(publish, root) == 0
    assert _run(publish, root) == 0
    stored = close.load_close(store, "rel")
    barriers = _release_barriers(store)
    assert len(barriers) == 1
    assert barriers[0].id == sent[0].id
    assert stored["final"]["barrier_epoch"] == barriers[0].id


def test_cli_publish_barrier_stamp_failure_resumes_existing_message(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    root = _init(tmp_path)
    store = Store(root)
    assert _open(root, "--dirty-artifact", "test:working-tree") == 0
    assert _accept(root) == 0
    real_commit = close.CloseTransaction.commit
    commits = 0

    def fail_second_commit(transaction):
        nonlocal commits
        commits += 1
        if commits == 2:
            raise OSError("injected barrier stamp failure")
        return real_commit(transaction)

    monkeypatch.setattr(close.CloseTransaction, "commit", fail_second_commit)
    capsys.readouterr()
    publish = ["close", "publish", "--id", "rel", "--from", "lead",
               "--verdict", "go", "--bump-barrier", "--reason", "ship"]
    assert _run(publish, root) == 2
    assert "retry" in capsys.readouterr().err.lower()
    pending = close.load_close(store, "rel")
    barriers = _release_barriers(store)
    assert pending["final"]["barrier_epoch"] is None
    assert len(barriers) == 1

    monkeypatch.setattr(close.CloseTransaction, "commit", real_commit)
    assert _run(publish, root) == 0
    stored = close.load_close(store, "rel")
    assert stored["final"]["barrier_epoch"] == barriers[0].id
    assert len(_release_barriers(store)) == 1


def test_cli_concurrent_publish_barrier_retries_send_once(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _init(tmp_path)
    store = Store(root)
    assert _open(root, "--dirty-artifact", "test:working-tree") == 0
    assert _accept(root) == 0
    real_send = Store.send

    def fail_barrier(self, **kwargs):
        if (kwargs.get("meta") or {}).get("close_barrier"):
            raise OSError("injected initial send failure")
        return real_send(self, **kwargs)

    monkeypatch.setattr(Store, "send", fail_barrier)
    publish = ["close", "publish", "--id", "rel", "--from", "lead",
               "--verdict", "go", "--bump-barrier", "--reason", "ship"]
    assert _run(publish, root) == 2

    send_started = threading.Event()
    release_send = threading.Event()
    second_waiting = threading.Event()
    send_count = 0
    real_update_lock = close._close_update_lock

    def paused_send(self, **kwargs):
        nonlocal send_count
        if (kwargs.get("meta") or {}).get("close_barrier"):
            send_count += 1
            send_started.set()
            if not release_send.wait(5):
                raise AssertionError("barrier send was not released")
        return real_send(self, **kwargs)

    def observed_update_lock(lock_store, close_id, *, timeout):
        if threading.current_thread().name == "close-retry-2":
            second_waiting.set()
        return real_update_lock(lock_store, close_id, timeout=timeout)

    monkeypatch.setattr(Store, "send", paused_send)
    monkeypatch.setattr(close, "_close_update_lock", observed_update_lock)
    results: list[int] = []
    errors: list[BaseException] = []

    def retry() -> None:
        try:
            results.append(_run(publish, root))
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=retry, name="close-retry-1")
    second = threading.Thread(target=retry, name="close-retry-2")
    first.start()
    assert send_started.wait(5)
    second.start()
    assert second_waiting.wait(5)
    release_send.set()
    first.join(10)
    second.join(10)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert sorted(results) == [0, 0]
    assert send_count == 1
    barriers = _release_barriers(store)
    assert len(barriers) == 1
    assert close.load_close(store, "rel")["final"]["barrier_epoch"] == barriers[0].id


def test_cli_publish_records_go_even_without_barrier_bump(tmp_path: Path) -> None:
    # the GO snapshot is persisted independently of the barrier bump (ordering
    # fix): publishing GO without --bump-barrier still records final GO + no epoch.
    root = _init(tmp_path)
    store = Store(root)
    _open(root)
    _accept(root)
    assert _run(["close", "publish", "--id", "rel", "--from", "lead",
                 "--verdict", "go"], root) == 0
    rec = close.load_close(store, "rel")
    assert rec["final"]["verdict"] == close.VERDICT_GO
    assert rec["final"]["barrier_epoch"] is None
    assert store.current_epoch() is None  # no barrier unless --bump-barrier


def test_cli_publish_hold_never_bumps_barrier(tmp_path: Path) -> None:
    root = _init(tmp_path)
    store = Store(root)
    _open(root)
    # publish HOLD is allowed even when check is HOLD (records the decision)
    assert _run(["close", "publish", "--id", "rel", "--from", "lead",
                 "--verdict", "hold", "--reason", "not ready"], root) == 3
    rec = close.load_close(store, "rel")
    assert rec["status"] == close.PUBLISHED
    assert rec["final"]["verdict"] == close.VERDICT_HOLD
    assert rec["final"]["barrier_epoch"] is None
    assert store.current_epoch() is None  # HOLD never bumps the global epoch


def test_cli_post_publish_ack_rejected_until_reopen(tmp_path: Path) -> None:
    root = _init(tmp_path)
    _open(root)
    _accept(root)
    _run(["close", "publish", "--id", "rel", "--from", "lead", "--verdict", "go"], root)
    # post-publish ack is refused (stale-proof without a global bump)...
    assert _run(["close", "ack", "--id", "rel", "--lens", "sec", "--status", "na",
                 "--from", "codex", "--reason", "late"], root) == 2
    # ...until a lead reopens
    assert _run(["close", "reopen", "--id", "rel", "--from", "lead"], root) == 0
    assert _run(["close", "ack", "--id", "rel", "--lens", "sec", "--status", "na",
                 "--from", "codex", "--reason", "late"], root) == 0


def test_cli_override_ignored_from_non_lead(tmp_path: Path) -> None:
    # reviewer-1 blocker: --override is a close-lead privilege. A non-lead passing
    # --override must NOT self-authorize a lens the open did not allow them.
    root = _init(tmp_path)  # lens "sec" allowed only to codex; lead has role=lead
    _open(root)
    # dev2 (not allowed, not a lead) acks with --override -> ignored -> stays HOLD
    assert _run(["close", "ack", "--id", "rel", "--lens", "sec", "--status", "na",
                 "--from", "dev2", "--reason", "x", "--override"], root) == 0
    assert _run(["close", "check", "--id", "rel"], root) == 3
    rec = close.load_close(Store(root), "rel")
    assert rec["lens_acks"]["sec"]["override"] is False
    result = close.compute_verdict(rec, _gate_go())
    assert close.HOLD_UNAUTHORIZED_ACK in _codes(result)


def test_cli_override_honored_from_lead(tmp_path: Path) -> None:
    root = _init(tmp_path)  # lead has role=lead -> in the close-lead set
    _open(root)
    # the lead overrides the lens (not in allowed_agents) -> authorized -> GO
    assert _run(["close", "ack", "--id", "rel", "--lens", "sec", "--status", "na",
                 "--from", "lead", "--reason", "lead sign-off", "--override"], root) == 0
    rec = close.load_close(Store(root), "rel")
    assert rec["lens_acks"]["sec"]["override"] is True
    assert _run(["close", "check", "--id", "rel"], root) == 0


def test_cli_open_refuses_duplicate_without_force(tmp_path: Path) -> None:
    root = _init(tmp_path)
    assert _open(root) == 0
    assert _open(root) == 2          # already exists
    assert _open(root, "--force") == 0


def test_cli_list_and_show(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _init(tmp_path)
    _open(root)
    capsys.readouterr()
    assert _run(["close", "list"], root) == 0
    assert "rel" in capsys.readouterr().out
    assert _run(["close", "show", "--id", "rel"], root) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["close_id"] == "rel"


# ================================================ #60 DoD forcing gate (inc-1: assurance)

def _assurance_bundle(**over) -> dict:
    """A fully-satisfied resolved assurance-gate bundle (green, CI-attested blocker, bound to
    SHA, fresh). Override any field to build the failing variants."""
    b = {"gate": "assurance:release", "present": True, "status": "green",
         "severity": "blocker", "evidence_source": "automation_ci",
         "revision": SHA, "waiver_active": False, "age_days": 1.0, "max_age_days": 14}
    b.update(over)
    return b


def _dod_eval(assurance: dict | None) -> dict:
    return {"policy_present": True, "policy_error": None,
            "required_dimensions": {"assurance": {"gate": "assurance:release",
                                                   "max_age_days": 14}},
            "assurance": assurance}


# ------------------------------------------------------ validate_dod_policy

def test_validate_dod_policy_valid_roundtrips_lowercased_and_normalized() -> None:
    pol = close.validate_dod_policy({
        "schema_version": 1,
        "scopes": {"Release": {"assurance": {"gate": "assurance:release", "max_age_days": 14}}},
    })
    assert pol == {"schema_version": 1,
                   "scopes": {"release": {"assurance": {"gate": "assurance:release",
                                                        "max_age_days": 14}}}}


def test_validate_dod_policy_allows_absent_max_age() -> None:
    pol = close.validate_dod_policy(
        {"schema_version": 1, "scopes": {"release": {"assurance": {"gate": "a:r"}}}})
    assert pol["scopes"]["release"]["assurance"]["max_age_days"] is None


@pytest.mark.parametrize("raw", [
    "not-a-dict",
    {"schema_version": "1", "scopes": {}},
    {"schema_version": True, "scopes": {}},
    {"schema_version": 1, "scopes": "nope"},
    {"schema_version": 1, "scopes": {"release": "nope"}},
    {"schema_version": 1, "scopes": {"release": {"coverage": {"floor_pct": 70}}}},  # unknown key
    {"schema_version": 1, "scopes": {"release": {"assurance": {}}}},                # missing gate
    {"schema_version": 1, "scopes": {"release": {"assurance": {"gate": ""}}}},      # empty gate
    {"schema_version": 1, "scopes": {"release": {"assurance": {"gate": "g",
                                                               "max_age_days": -1}}}},
])
def test_validate_dod_policy_malformed_raises(raw) -> None:
    with pytest.raises(close.CloseError):
        close.validate_dod_policy(raw)


def test_load_dod_policy_missing_is_empty(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["lead"])
    assert close.load_dod_policy(s) == (None, None)


def test_load_dod_policy_malformed_fails_closed(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["lead"])
    close.dod_policy_path(s).write_text('{"schema_version": 1, "scopes": {"release": '
                                        '{"coverage": {}}}}', encoding="utf-8")
    policy, err = close.load_dod_policy(s)
    assert policy is None and err and "coverage" in err


# ------------------------------------------------------ derive_required_dod

def test_derive_required_dod_no_policy_or_unmapped_scope_is_empty() -> None:
    assert close.derive_required_dod(None, "release") == {"dimensions": {}}
    pol = {"schema_version": 1, "scopes": {"release": {"assurance": {"gate": "a:r"}}}}
    assert close.derive_required_dod(pol, "feature") == {"dimensions": {}}


def test_derive_required_dod_mapped_scope_returns_dims_case_insensitive() -> None:
    pol = {"schema_version": 1, "scopes": {"release": {"assurance": {"gate": "a:r"}}}}
    assert close.derive_required_dod(pol, "RELEASE") == {
        "dimensions": {"assurance": {"gate": "a:r"}}}


# ------------------------------------------------------ evaluate_dod (pure)

def test_evaluate_dod_none_or_no_required_is_empty() -> None:
    assert close.evaluate_dod(_satisfied(), None) == []
    assert close.evaluate_dod(_satisfied(), {"required_dimensions": {}}) == []


def test_evaluate_dod_bundle_malformed_or_policy_error_holds_invalid() -> None:
    assert close.evaluate_dod(_satisfied(), "nope")[0][0] == close.HOLD_INVALID_DOD_POLICY
    err = close.evaluate_dod(_satisfied(), {"policy_error": "bad", "required_dimensions": {}})
    assert err == [(close.HOLD_INVALID_DOD_POLICY, "bad")]


def test_evaluate_dod_assurance_satisfied_is_empty() -> None:
    assert close.evaluate_dod(_satisfied(), _dod_eval(_assurance_bundle())) == []


def test_evaluate_dod_assurance_active_waiver_does_not_clear() -> None:
    # A gate WAIVER never clears the DoD assurance dimension - `gate waive --operator <text>` is
    # unauthenticated caller free text, so honoring it would be a one-command bypass of the forcing
    # gate (Codex re-review of 848841a). Even an active waiver bound to THIS revision HOLDs; only a
    # green CI-attested gate clears. The authenticated operator escape is task #65.
    a = _assurance_bundle(status="waived", waiver_active=True, evidence_source="operator_waiver",
                          severity="blocker", revision=SHA)
    assert close.HOLD_MISSING_ASSURANCE in _dcodes(close.evaluate_dod(_satisfied(), _dod_eval(a)))


@pytest.mark.parametrize("over,code", [
    ({"present": False}, close.HOLD_MISSING_ASSURANCE),
    ({"status": "red"}, close.HOLD_MISSING_ASSURANCE),
    ({"status": "skipped"}, close.HOLD_MISSING_ASSURANCE),
    ({"status": "waived", "waiver_active": False}, close.HOLD_MISSING_ASSURANCE),
    ({"severity": "warn"}, close.HOLD_UNATTESTED_ASSURANCE),
    ({"evidence_source": "manual_review"}, close.HOLD_UNATTESTED_ASSURANCE),
    ({"revision": OTHER_SHA}, close.HOLD_STALE_ASSURANCE),
    ({"age_days": 30.0, "max_age_days": 14}, close.HOLD_STALE_ASSURANCE),
])
def test_evaluate_dod_assurance_failure_variants(over, code) -> None:
    holds = close.evaluate_dod(_satisfied(), _dod_eval(_assurance_bundle(**over)))
    assert code in {c for c, _ in holds}


def test_evaluate_dod_assurance_missing_bundle_holds_missing() -> None:
    ev = {"policy_present": True, "policy_error": None,
          "required_dimensions": {"assurance": {}}, "assurance": None}
    assert close.evaluate_dod(_satisfied(), ev)[0][0] == close.HOLD_MISSING_ASSURANCE


# ------------------------------------------------------ compute_verdict integration

def test_compute_verdict_dod_none_is_backward_identical() -> None:
    base = close.compute_verdict(_satisfied(), _gate_go())
    explicit = close.compute_verdict(_satisfied(), _gate_go(), None, None, None)
    assert base == explicit and base["verdict"] == close.VERDICT_GO


def test_compute_verdict_unmet_assurance_flips_go_to_hold_with_only_the_dod_hold() -> None:
    # a close that is otherwise GO, plus a required-but-missing assurance dimension.
    result = close.compute_verdict(
        _satisfied(), _gate_go(), None, None, _dod_eval(_assurance_bundle(present=False)))
    assert result["verdict"] == close.VERDICT_HOLD
    assert _codes(result) == {close.HOLD_MISSING_ASSURANCE}   # reached the DoD fold, not MALFORMED


def test_compute_verdict_satisfied_assurance_stays_go() -> None:
    result = close.compute_verdict(
        _satisfied(), _gate_go(), None, None, _dod_eval(_assurance_bundle()))
    assert result["verdict"] == close.VERDICT_GO and result["holds"] == []


def test_compute_verdict_dod_is_additive_only() -> None:
    # a pre-existing gate hold PLUS an unmet DoD dimension -> both holds present (DoD only adds).
    result = close.compute_verdict(
        _satisfied(), _gate_hold(), None, None, _dod_eval(_assurance_bundle(present=False)))
    codes = _codes(result)
    assert close.HOLD_GATE in codes and close.HOLD_MISSING_ASSURANCE in codes


# ------------------------------------------------------ CLI integration (impure bridge)

def _write_dod(root: Path, gate: str = "assurance:release", max_age: int = 14) -> None:
    close.dod_policy_path(Store(root)).write_text(json.dumps({
        "schema_version": 1,
        "scopes": {"release": {"assurance": {"gate": gate, "max_age_days": max_age}}},
    }), encoding="utf-8")


def _check_holds(root: Path, capsys) -> tuple[int, set[str]]:
    capsys.readouterr()
    rc = _run(["close", "check", "--id", "rel", "--json"], root)
    out = json.loads(capsys.readouterr().out)
    return rc, {h["code"] for h in out["holds"]}


def test_cli_dod_absent_policy_is_byte_identical_go(tmp_path: Path, capsys) -> None:
    root = _init(tmp_path)
    assert _open(root) == 0
    assert _accept(root) == 0
    rc, codes = _check_holds(root, capsys)
    assert rc == 0 and close.HOLD_MISSING_ASSURANCE not in codes


def test_cli_dod_requires_assurance_gate_holds_until_ci_attested(tmp_path: Path, capsys) -> None:
    root = _init(tmp_path)
    _write_dod(root)
    assert _open(root) == 0
    assert _accept(root) == 0
    # no assurance:release gate yet -> the DoD forces a HOLD (the Papendal CVE-shipped-green gap).
    rc, codes = _check_holds(root, capsys)
    assert rc == 3 and close.HOLD_MISSING_ASSURANCE in codes
    # a green, CI-attested, revision-bound blocker gate clears the dimension -> GO.
    gates.set_gate(root, name="assurance:release", status="green", severity="blocker",
                   scope="release", actor="ci", evidence_source="automation_ci",
                   evidence=["run:ci-123"], revision=SHA)
    rc, codes = _check_holds(root, capsys)
    assert rc == 0 and close.HOLD_MISSING_ASSURANCE not in codes


def test_cli_dod_gate_waive_cannot_clear_assurance(tmp_path: Path, capsys) -> None:
    # Codex re-review of 848841a (BLOCKER): `gate waive --operator <text>` is UNAUTHENTICATED
    # caller free text (docs/ASSURANCE.md), so a single `gate waive` must NOT clear the DoD
    # assurance dimension - otherwise it is a one-command, revision-independent bypass of the
    # whole forcing gate. Prove the REAL CLI exploit path still HOLDs. Authenticated escape = #65.
    root = _init(tmp_path)
    _write_dod(root)
    assert _open(root) == 0
    assert _accept(root) == 0
    # waive the assurance gate with a CLAIMED operator - the command succeeds but confers no real
    # operator authority, so the DoD must still HOLD_MISSING_ASSURANCE (not a false GO).
    _run(["gate", "waive", "--from", "lead", "--name", "assurance:release", "--operator",
          "claimed-boss", "--reason", "bypass", "--scope", "release", "--expires",
          "2099-01-01"], root)
    rc, codes = _check_holds(root, capsys)
    assert rc == 3 and close.HOLD_MISSING_ASSURANCE in codes


def test_cli_dod_stale_when_gate_bound_to_other_revision(tmp_path: Path, capsys) -> None:
    root = _init(tmp_path)
    _write_dod(root)
    assert _open(root) == 0
    assert _accept(root) == 0
    gates.set_gate(root, name="assurance:release", status="green", severity="blocker",
                   scope="release", actor="ci", evidence_source="automation_ci",
                   evidence=["run:ci-9"], revision=OTHER_SHA)  # bound to the WRONG revision
    rc, codes = _check_holds(root, capsys)
    assert rc == 3 and close.HOLD_STALE_ASSURANCE in codes


def test_cli_dod_malformed_policy_fails_closed_without_crash(tmp_path: Path, capsys) -> None:
    root = _init(tmp_path)
    close.dod_policy_path(Store(root)).write_text(
        '{"schema_version": 1, "scopes": {"release": {"coverage": {}}}}', encoding="utf-8")
    assert _open(root) == 0
    assert _accept(root) == 0
    rc, codes = _check_holds(root, capsys)
    assert rc == 3 and close.HOLD_INVALID_DOD_POLICY in codes


# ---------------------------------- #60 inc-1 hardening regressions (Codex review of 46584e8)

def _dcodes(holds) -> set[str]:
    return {c for c, _ in holds}


@pytest.mark.parametrize("raw", [
    {"schema_version": 1, "scopes": []},                       # B2: present wrong-type != absent
    {"schema_version": 1, "scopes": 0},                        # B2: falsy wrong-type
    {"schema_version": 2, "scopes": {}},                       # B2: unsupported version
    {"schema_version": 1, "scopes": {}, "extra": 1},           # B2: unknown top-level key
    {"schema_version": 1, "scope": {"release": {}}},           # B2: misspelled 'scopes'
    {"schema_version": 1,
     "scopes": {"release": {"assurance": {"gate": "g", "max_age_day": 14}}}},  # B2: typo'd spec key
    {"schema_version": 1,
     "scopes": {"Release": {"assurance": {"gate": "g"}},
                "release": {"assurance": {"gate": "g2"}}}},    # B2: case-insensitive collision
])
def test_validate_dod_policy_failclosed_regressions(raw) -> None:
    with pytest.raises(close.CloseError):
        close.validate_dod_policy(raw)


@pytest.mark.parametrize("age_days,expect_hold", [
    (None, True),    # B3: freshness required but timestamp missing/unparseable -> HOLD
    (-1.0, True),    # B3: future-dated attestation is NOT fresh -> HOLD
    (99.0, True),    # older than max_age_days=14 -> HOLD (control)
    (1.0, False),    # genuinely fresh -> clears
])
def test_evaluate_dod_assurance_freshness_fails_closed(age_days, expect_hold) -> None:
    a = _assurance_bundle(age_days=age_days, max_age_days=14)
    holds = close.evaluate_dod(_satisfied(), _dod_eval(a))
    assert (close.HOLD_STALE_ASSURANCE in _dcodes(holds)) is expect_hold


def test_evaluate_dod_assurance_no_max_age_skips_freshness() -> None:
    # freshness is OPTIONAL: with max_age_days unset, a None/old age must NOT hold on freshness.
    a = _assurance_bundle(age_days=None, max_age_days=None)
    assert close.evaluate_dod(_satisfied(), _dod_eval(a)) == []


def test_evaluate_dod_assurance_waiver_never_clears_any_revision() -> None:
    # No gate waiver clears assurance, regardless of revision binding (unauthenticated - #65).
    for rev in (OTHER_SHA, SHA, None):
        a = _assurance_bundle(status="waived", waiver_active=True, revision=rev)
        assert close.HOLD_MISSING_ASSURANCE in _dcodes(
            close.evaluate_dod(_satisfied(), _dod_eval(a))), f"waiver rev={rev} must HOLD"


def test_evaluate_dod_assurance_gate_scope_must_apply() -> None:
    # M2: a gate scoped to another scope (or scope-less) cannot satisfy a scoped close.
    mismatch = _assurance_bundle(gate_scope="feature", close_gate_scope="release")
    assert close.HOLD_MISSING_ASSURANCE in _dcodes(
        close.evaluate_dod(_satisfied(), _dod_eval(mismatch)))
    scopeless = _assurance_bundle(gate_scope=None, close_gate_scope="release")
    assert close.HOLD_MISSING_ASSURANCE in _dcodes(
        close.evaluate_dod(_satisfied(), _dod_eval(scopeless)))
    # matching scope, and an explicit "global" gate, both clear (mirrors gates.check_gates).
    assert close.evaluate_dod(_satisfied(), _dod_eval(
        _assurance_bundle(gate_scope="release", close_gate_scope="release"))) == []
    assert close.evaluate_dod(_satisfied(), _dod_eval(
        _assurance_bundle(gate_scope="global", close_gate_scope="release"))) == []


def test_load_dod_policy_oversized_fails_closed(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["lead"])
    close.dod_policy_path(s).write_text(
        '{"schema_version":1,"scopes":{},"pad":"' + "a" * 70000 + '"}', encoding="utf-8")
    pol, err = close.load_dod_policy(s)
    assert pol is None and err and "size" in err.lower()


def test_load_dod_policy_deeply_nested_fails_closed_without_crash(tmp_path: Path) -> None:
    s = Store(tmp_path)
    s.init(["lead"])
    depth = 20000                                    # deep enough to blow json's recursion limit
    close.dod_policy_path(s).write_text("[" * depth + "]" * depth, encoding="utf-8")
    pol, err = close.load_dod_policy(s)              # must NOT raise (RecursionError caught)
    assert pol is None and err


def test_cli_dod_gate_scoped_to_other_scope_does_not_satisfy(tmp_path: Path, capsys) -> None:
    # M2 end-to-end: a green CI-attested blocker whose OWN scope is "feature" does not satisfy a
    # release close, even though it is named assurance:release and bound to the revision.
    root = _init(tmp_path)
    _write_dod(root)
    assert _open(root) == 0
    assert _accept(root) == 0
    gates.set_gate(root, name="assurance:release", status="green", severity="blocker",
                   scope="feature", actor="ci", evidence_source="automation_ci",
                   evidence=["run:x"], revision=SHA)
    rc, codes = _check_holds(root, capsys)
    assert rc == 3 and close.HOLD_MISSING_ASSURANCE in codes

# ---------------------------- #60 inc-1 round-3 regressions (Codex re-review of f44e268)

@pytest.mark.parametrize("body,label", [
    ('{"schema_version":1,'
     '"scopes":{"release":{"assurance":{"gate":"assurance:release","max_age_days":14}}},'
     '"scopes":{}}', "dup top-level scopes erasing requirements"),
    ('{"schema_version":1,'
     '"scopes":{"release":{"assurance":{"gate":"g"}},"release":{"assurance":{"gate":"g2"}}}}',
     "dup scope name"),
    ('{"schema_version":1,"scopes":{"release":'
     '{"assurance":{"gate":"g"},"assurance":{"gate":"g2"}}}}', "dup dimension key"),
    ('{"schema_version":1,"scopes":{"release":{"assurance":'
     '{"gate":"g","max_age_days":14,"max_age_days":null}}}}', "dup max_age_days disabling freshness"),
])
def test_load_dod_policy_rejects_duplicate_keys_fails_closed(tmp_path: Path, body, label) -> None:
    # A duplicated JSON key silently keeps its LAST value in the stdlib decoder, which could erase
    # a requirement or disable freshness -> false GO. The object_pairs_hook must fail CLOSED.
    s = Store(tmp_path)
    s.init(["lead"])
    close.dod_policy_path(s).write_text(body, encoding="utf-8")
    pol, err = close.load_dod_policy(s)
    assert pol is None and err and "duplicate" in err.lower(), label


def test_cli_dod_duplicate_scopes_does_not_false_go(tmp_path: Path, capsys) -> None:
    # End-to-end: a policy that duplicates "scopes" (2nd one empty) must NOT resolve to zero
    # requirements + GO; it must HOLD_INVALID_DOD_POLICY at `close check`.
    root = _init(tmp_path)
    close.dod_policy_path(Store(root)).write_text(
        '{"schema_version":1,'
        '"scopes":{"release":{"assurance":{"gate":"assurance:release","max_age_days":14}}},'
        '"scopes":{}}', encoding="utf-8")
    assert _open(root) == 0
    assert _accept(root) == 0
    rc, codes = _check_holds(root, capsys)
    assert rc == 3 and close.HOLD_INVALID_DOD_POLICY in codes


def test_evaluate_dod_assurance_revisionless_waiver_does_not_clear() -> None:
    # A revision-less `gate waive` (its --operator is unauthenticated caller text) must NOT clear
    # the DoD assurance dimension - otherwise a single command bypasses the forcing gate
    # (Codex re-review of 848841a, BLOCKER). Authenticated operator escape = task #65.
    a = _assurance_bundle(status="waived", waiver_active=True, revision=None)
    assert close.HOLD_MISSING_ASSURANCE in _dcodes(close.evaluate_dod(_satisfied(), _dod_eval(a)))


# ============================================================= #60 inc-3: coverage dimension

def _coverage_bundle(**over) -> dict:
    """A fully satisfied coverage gate bundle. Override fields for fail-closed variants."""
    b = {
        "gate": "coverage:release",
        "present": True,
        "status": "green",
        "severity": "blocker",
        "evidence_source": "automation_ci",
        "revision": SHA,
        "waiver_active": False,
        "gate_scope": "release",
        "coverage_percent": 85.0,
        "min_percent": 80.0,
        "age_days": 1.0,
        "max_age_days": 14,
    }
    b.update(over)
    return b


def _dod_eval_c(coverage: dict | None) -> dict:
    return {
        "policy_present": True,
        "policy_error": None,
        "required_dimensions": {
            "coverage": {
                "gate": "coverage:release",
                "min_percent": 80.0,
                "max_age_days": 14,
            },
        },
        "coverage": coverage,
    }


@pytest.mark.parametrize("min_percent", [0, 0.0, 72, 72.5, 100, 100.0])
def test_validate_dod_coverage_spec_roundtrips_numeric_floor(min_percent) -> None:
    pol = close.validate_dod_policy({
        "schema_version": 1,
        "scopes": {
            "Release": {
                "coverage": {
                    "gate": "coverage:release",
                    "min_percent": min_percent,
                }
            }
        },
    })
    assert pol["scopes"]["release"]["coverage"] == {
        "gate": "coverage:release",
        "min_percent": min_percent,
        "max_age_days": None,
    }


@pytest.mark.parametrize("spec", [
    {},
    {"gate": [], "min_percent": 80},
    {"gate": "", "min_percent": 80},
    {"gate": "coverage:Release", "min_percent": 80},
    {"gate": "coverage:unknown", "min_percent": 80},
    {"gate": "coverage:release", "min_percent": None},
    {"gate": "coverage:release", "min_percent": True},
    {"gate": "coverage:release", "min_percent": "80"},
    {"gate": "coverage:release", "min_percent": -0.01},
    {"gate": "coverage:release", "min_percent": 100.01},
    {"gate": "coverage:release", "min_percent": float("nan")},
    {"gate": "coverage:release", "min_percent": float("inf")},
    {"gate": "coverage:release", "min_percent": 10 ** 1000},
    {"gate": "coverage:release", "min_percent": 80, "max_age_days": True},
    {"gate": "coverage:release", "min_percent": 80, "max_age_days": -1},
    {"gate": "coverage:release", "min_percent": 80, "max_age_days": 1.5},
    {"gate": "coverage:release", "min_percent": 80, "minimum_percent": 70},
])
def test_validate_dod_coverage_spec_malformed_or_unknown_key_raises(spec) -> None:
    with pytest.raises(close.CloseError):
        close.validate_dod_policy({
            "schema_version": 1,
            "scopes": {"release": {"coverage": spec}},
        })


def test_evaluate_dod_coverage_satisfied_is_empty() -> None:
    assert close.evaluate_dod(_satisfied(), _dod_eval_c(_coverage_bundle())) == []


@pytest.mark.parametrize(
    ("floor_token", "coverage_percent", "expected_codes"),
    [
        ("80.000000000000000001", 80.0, {close.HOLD_LOW_COVERAGE}),
        ("80.000000000000000001", 80.00000000000001, set()),
        ("80", 80.0, set()),
        ("80", 85.0, set()),
        ("80.1", 80.1, set()),
    ],
    ids=[
        "exact-floor-above-measurement",
        "exact-floor-below-representable-measurement",
        "normal-floor-at-threshold",
        "normal-floor-legitimately-above-threshold",
        "normal-fractional-floor-at-threshold",
    ],
)
def test_loaded_coverage_floor_never_rounds_down_toward_passing(
    tmp_path: Path,
    floor_token: str,
    coverage_percent: float,
    expected_codes: set[str],
) -> None:
    store = Store(tmp_path)
    store.init(["lead"])
    close.dod_policy_path(store).write_text(
        '{"schema_version":1,"scopes":{"release":{"coverage":'
        '{"gate":"coverage:release","min_percent":'
        f"{floor_token}"
        ',"max_age_days":14}}}}',
        encoding="utf-8",
    )

    policy, error = close.load_dod_policy(store)

    assert error is None
    assert policy is not None
    required = close.derive_required_dod(policy, "release")["dimensions"]["coverage"]
    assert Decimal(str(required["min_percent"])) >= Decimal(floor_token)
    dod_eval = _dod_eval_c(_coverage_bundle(coverage_percent=coverage_percent))
    dod_eval["required_dimensions"]["coverage"] = required
    holds = close.evaluate_dod(_satisfied(), dod_eval)
    assert _dcodes(holds) == expected_codes
    if expected_codes:
        assert repr(coverage_percent) in holds[0][1]
        assert repr(required["min_percent"]) in holds[0][1]


@pytest.mark.parametrize(
    ("required_gate", "resolved_gate", "resolved_scope"),
    [
        ("coverage:release", "coverage:change", "change"),
        ("coverage:deep", "coverage:release", "release"),
    ],
)
def test_evaluate_dod_coverage_cannot_substitute_a_different_producer_gate(
    required_gate,
    resolved_gate,
    resolved_scope,
) -> None:
    dod_eval = _dod_eval_c(
        _coverage_bundle(gate=resolved_gate, gate_scope=resolved_scope)
    )
    dod_eval["required_dimensions"]["coverage"]["gate"] = required_gate

    holds = close.evaluate_dod(_satisfied(), dod_eval)

    assert _dcodes(holds) == {close.HOLD_MISSING_COVERAGE}


@pytest.mark.parametrize("coverage", [
    None,
    "malformed",
    [],
    _coverage_bundle(present=False),
    _coverage_bundle(present="yes"),
    _coverage_bundle(status="red"),
    _coverage_bundle(status="skipped"),
    _coverage_bundle(status="waived", waiver_active=True),
    _coverage_bundle(severity="warn"),
    _coverage_bundle(evidence_source="manual_review"),
    _coverage_bundle(revision=OTHER_SHA),
    _coverage_bundle(gate=None),
    _coverage_bundle(gate=[]),
    _coverage_bundle(gate="coverage:unknown"),
    _coverage_bundle(gate_scope="feature"),
    _coverage_bundle(gate_scope=None),
    _coverage_bundle(coverage_percent=None),
    _coverage_bundle(coverage_percent=True),
    _coverage_bundle(coverage_percent="85"),
    _coverage_bundle(coverage_percent=float("nan")),
    _coverage_bundle(coverage_percent=float("inf")),
    _coverage_bundle(coverage_percent=10 ** 1000),
    _coverage_bundle(coverage_percent=-1),
    _coverage_bundle(coverage_percent=101),
])
def test_evaluate_dod_coverage_unusable_evidence_holds_missing(coverage) -> None:
    holds = close.evaluate_dod(_satisfied(), _dod_eval_c(coverage))
    assert _dcodes(holds) == {close.HOLD_MISSING_COVERAGE}


def test_evaluate_dod_coverage_low_percent_holds_low() -> None:
    holds = close.evaluate_dod(
        _satisfied(), _dod_eval_c(_coverage_bundle(coverage_percent=79.99)))
    assert _dcodes(holds) == {close.HOLD_LOW_COVERAGE}


@pytest.mark.parametrize(
    "age_days", [None, "invalid", float("nan"), float("inf"), -1.0, 14.01, 10 ** 1000])
def test_evaluate_dod_coverage_freshness_fails_closed(age_days) -> None:
    holds = close.evaluate_dod(
        _satisfied(), _dod_eval_c(_coverage_bundle(age_days=age_days)))
    assert _dcodes(holds) == {close.HOLD_STALE_COVERAGE}


def test_evaluate_dod_coverage_no_max_age_skips_freshness() -> None:
    ev = _dod_eval_c(_coverage_bundle(age_days=None, max_age_days=None))
    ev["required_dimensions"]["coverage"]["max_age_days"] = None
    assert close.evaluate_dod(_satisfied(), ev) == []


def test_derived_age_never_rounds_toward_passing_extreme_freshness_floor() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    updated_at = (now - timedelta(days=200_000, microseconds=1)).isoformat()
    future_at = (now + timedelta(days=200_000, microseconds=1)).isoformat()

    age_days = cli._iso_age_days(updated_at, now)
    future_age_days = cli._iso_age_days(future_at, now)

    assert age_days is not None
    assert age_days > 200_000
    assert future_age_days is not None
    assert future_age_days < -200_000
    dod_eval = _dod_eval_c(
        _coverage_bundle(age_days=age_days, max_age_days=200_000)
    )
    dod_eval["required_dimensions"]["coverage"]["max_age_days"] = 200_000
    assert _dcodes(close.evaluate_dod(_satisfied(), dod_eval)) == {
        close.HOLD_STALE_COVERAGE
    }


def test_evaluate_dod_coverage_waiver_never_clears() -> None:
    for revision in (SHA, OTHER_SHA, None):
        coverage = _coverage_bundle(
            status="waived",
            waiver_active=True,
            evidence_source="operator_waiver",
            revision=revision,
        )
        holds = close.evaluate_dod(_satisfied(), _dod_eval_c(coverage))
        assert _dcodes(holds) == {close.HOLD_MISSING_COVERAGE}


def test_evaluate_dod_coverage_matching_or_global_scope_clears() -> None:
    for gate_scope in ("release", "global"):
        coverage = _coverage_bundle(gate_scope=gate_scope)
        assert close.evaluate_dod(_satisfied(), _dod_eval_c(coverage)) == []


def test_compute_verdict_unmet_coverage_flips_go_to_hold() -> None:
    result = close.compute_verdict(
        _satisfied(),
        _gate_go(),
        None,
        None,
        _dod_eval_c(_coverage_bundle(present=False)),
    )
    assert result["verdict"] == close.VERDICT_HOLD
    assert _codes(result) == {close.HOLD_MISSING_COVERAGE}


def test_compute_verdict_satisfied_coverage_stays_go() -> None:
    result = close.compute_verdict(
        _satisfied(), _gate_go(), None, None, _dod_eval_c(_coverage_bundle()))
    assert result["verdict"] == close.VERDICT_GO
    assert result["holds"] == []


def _write_coverage_dod(root: Path, *, min_percent: float = 80.0,
                        max_age_days: int | None = 14) -> None:
    close.dod_policy_path(Store(root)).write_text(json.dumps({
        "schema_version": 1,
        "scopes": {
            "release": {
                "coverage": {
                    "gate": "coverage:release",
                    "min_percent": min_percent,
                    "max_age_days": max_age_days,
                },
            },
        },
    }), encoding="utf-8")


def _set_coverage_gate(root: Path, *, percent: object, revision: str = SHA,
                       scope: str = "release") -> None:
    gates.set_gate(
        root,
        name="coverage:release",
        status="green",
        severity="blocker",
        scope=scope,
        actor="ci",
        evidence_source="automation_ci",
        evidence=["run:coverage-123"],
        revision=revision,
    )
    state = gates.load_gate_state(root)
    # The producer stores coverage_percent in the gate's latest EVIDENCE entry (via
    # set_gate evidence_details), NOT top-level. Inject it there directly so this helper can
    # still exercise invalid values for the fail-closed tests (real set_gate would reject them).
    g = state["gates"]["coverage:release"]
    entries = g.get("evidence")
    if not isinstance(entries, list) or not entries:
        entries = [{"source": "automation_ci", "refs": ["run:coverage-123"]}]
    entries[-1]["coverage_percent"] = percent
    g["evidence"] = entries
    gates.write_gate_state(root, state)


def test_resolve_dod_coverage_gate_reads_producer_evidence_seam(tmp_path: Path) -> None:
    # Producer↔consumer contract (#60 inc-3 integration): the producer writes the percent via the
    # REAL gates.set_gate(evidence_details={"coverage_percent": ...}) path (→ latest evidence
    # entry). Prove the floor resolver reads it from there, not top-level.
    root = _init(tmp_path)
    gates.set_gate(
        root, name="coverage:release", status="green", severity="blocker", scope="release",
        actor="ci", evidence_source="automation_ci", evidence=["run:cov-1"], revision=SHA,
        evidence_details={"coverage_percent": 91.0})
    resolved = cli._resolve_dod_coverage_gate(
        Store(root),
        {"gate": "coverage:release", "min_percent": 80.0, "max_age_days": 14},
        _satisfied(),
    )
    assert resolved["present"] is True
    assert resolved["coverage_percent"] == 91.0          # read from the evidence entry
    assert resolved["evidence_source"] == "automation_ci" and resolved["revision"] == SHA
    assert resolved["min_percent"] == 80.0 and isinstance(resolved["age_days"], float)


def test_resolve_dod_coverage_gate_no_backtrack_to_stale_percent(tmp_path: Path) -> None:
    # reviewer-1 #60 inc-3: a later percent-less GREEN must NOT inherit an older entry's percent.
    # green@A(95) -> red@B -> green@B(no percent): resolver must read only the latest entry -> None.
    root = _init(tmp_path)
    gates.set_gate(root, name="coverage:release", status="green", severity="blocker",
                   scope="release", actor="ci", evidence_source="automation_ci",
                   evidence=["run:A"], revision=SHA, evidence_details={"coverage_percent": 95.0})
    gates.set_gate(root, name="coverage:release", status="red", severity="blocker",
                   scope="release", actor="ci", evidence_source="automation_ci",
                   evidence=["run:B-red"], revision=OTHER_SHA)
    gates.set_gate(root, name="coverage:release", status="green", severity="blocker",
                   scope="release", actor="ci", evidence_source="automation_ci",
                   evidence=["run:B-green"], revision=OTHER_SHA)  # green, NO coverage_percent
    resolved = cli._resolve_dod_coverage_gate(
        Store(root),
        {"gate": "coverage:release", "min_percent": 80.0, "max_age_days": 14},
        _satisfied(),
    )
    assert resolved["coverage_percent"] is None   # NOT 95.0 from the stale A entry


def test_resolve_dod_coverage_gate_reads_own_fields(tmp_path: Path) -> None:
    root = _init(tmp_path)
    _set_coverage_gate(root, percent=87.25)
    resolved = cli._resolve_dod_coverage_gate(
        Store(root),
        {"gate": "coverage:release", "min_percent": 80.0, "max_age_days": 14},
        _satisfied(),
    )
    assert resolved["gate"] == "coverage:release"
    assert resolved["coverage_percent"] == 87.25
    assert resolved["revision"] == SHA
    assert resolved["gate_scope"] == "release"
    assert isinstance(resolved["age_days"], float)


def test_cli_dod_coverage_holds_until_attested_floor_is_met(tmp_path: Path, capsys) -> None:
    root = _init(tmp_path)
    _write_coverage_dod(root)
    assert _open(root) == 0
    assert _accept(root) == 0

    rc, codes = _check_holds(root, capsys)
    assert rc == 3 and codes == {close.HOLD_MISSING_COVERAGE}

    _set_coverage_gate(root, percent=79.9)
    rc, codes = _check_holds(root, capsys)
    assert rc == 3 and codes == {close.HOLD_LOW_COVERAGE}

    _set_coverage_gate(root, percent=80.0)
    rc, codes = _check_holds(root, capsys)
    assert rc == 0 and codes == set()


def test_cli_dod_coverage_cross_revision_does_not_satisfy(tmp_path: Path, capsys) -> None:
    root = _init(tmp_path)
    _write_coverage_dod(root)
    assert _open(root) == 0
    assert _accept(root) == 0
    _set_coverage_gate(root, percent=90.0, revision=OTHER_SHA)
    rc, codes = _check_holds(root, capsys)
    assert rc == 3 and codes == {close.HOLD_MISSING_COVERAGE}


def test_cli_dod_coverage_cross_scope_does_not_satisfy(tmp_path: Path, capsys) -> None:
    root = _init(tmp_path)
    _write_coverage_dod(root)
    assert _open(root) == 0
    assert _accept(root) == 0
    _set_coverage_gate(root, percent=90.0, scope="feature")
    rc, codes = _check_holds(root, capsys)
    assert rc == 3 and codes == {close.HOLD_MISSING_COVERAGE}


def test_cli_dod_coverage_gate_waiver_does_not_satisfy(tmp_path: Path, capsys) -> None:
    root = _init(tmp_path)
    _write_coverage_dod(root)
    assert _open(root) == 0
    assert _accept(root) == 0
    assert _run([
        "gate", "waive",
        "--from", "lead",
        "--name", "coverage:release",
        "--operator", "claimed-boss",
        "--reason", "bypass",
        "--scope", "release",
        "--expires", "2099-01-01",
    ], root) == 0
    rc, codes = _check_holds(root, capsys)
    assert rc == 3 and codes == {close.HOLD_MISSING_COVERAGE}


@pytest.mark.parametrize("updated_at", [None, "2000-01-01T00:00:00Z", "2099-01-01T00:00:00Z"])
def test_resolved_dod_coverage_timestamp_fails_closed(
    tmp_path: Path, updated_at: str | None,
) -> None:
    root = _init(tmp_path)
    _set_coverage_gate(root, percent=90.0)
    state = gates.load_gate_state(root)
    state["gates"]["coverage:release"]["updated_at"] = updated_at
    gates.write_gate_state(root, state)

    resolved = cli._resolve_dod_coverage_gate(
        Store(root),
        {"gate": "coverage:release", "min_percent": 80.0, "max_age_days": 14},
        _satisfied(),
    )
    holds = close._evaluate_dod_coverage(
        _satisfied(),
        resolved,
        {
            "gate": "coverage:release",
            "min_percent": 80.0,
            "max_age_days": 14,
        },
    )
    assert _dcodes(holds) == {close.HOLD_STALE_COVERAGE}


# ============================================================ #60 inc-2: knowledge dimension

def _knowledge_bundle(**over) -> dict:
    """A satisfied resolved knowledge bundle (required on remediation, one bound non-trivial
    note). Override any field to build the failing variants."""
    b = {"when": "on_remediation", "min_notes": 1, "min_body_chars": 40,
         "types": ["decision", "gotcha", "lesson"], "has_remediation": True,
         "bound_notes": [{"type": "lesson", "body_len": 60}]}
    b.update(over)
    return b


def _dod_eval_k(knowledge: dict | None) -> dict:
    return {"policy_present": True, "policy_error": None,
            "required_dimensions": {"knowledge": {}}, "knowledge": knowledge}


# ---- validate the knowledge spec

def test_validate_dod_knowledge_spec_defaults_roundtrip() -> None:
    pol = close.validate_dod_policy(
        {"schema_version": 1, "scopes": {"feature": {"knowledge": {}}}})
    assert pol["scopes"]["feature"]["knowledge"] == {
        "when": "on_remediation", "min_notes": 1,
        "types": ["decision", "gotcha", "lesson"], "min_body_chars": 40}


def test_validate_dod_knowledge_spec_explicit_roundtrip() -> None:
    pol = close.validate_dod_policy({"schema_version": 1, "scopes": {"release": {
        "knowledge": {"when": "always", "min_notes": 2, "types": ["gotcha"],
                      "min_body_chars": 10}}}})
    assert pol["scopes"]["release"]["knowledge"] == {
        "when": "always", "min_notes": 2, "types": ["gotcha"], "min_body_chars": 10}


@pytest.mark.parametrize("spec", [
    {"when": "sometimes"},
    {"min_notes": 0},
    {"min_notes": True},
    {"types": []},
    {"types": ["bogus"]},
    {"types": "gotcha"},
    {"min_body_chars": -1},
])
def test_validate_dod_knowledge_spec_malformed_raises(spec) -> None:
    with pytest.raises(close.CloseError):
        close.validate_dod_policy(
            {"schema_version": 1, "scopes": {"release": {"knowledge": spec}}})


@pytest.mark.parametrize("spec", [
    {"min_note": 2},            # BLOCKER 1: min_notes typo must RAISE, not silently default to 1
    {"min_body_char": 10},      # min_body_chars typo
    {"whens": "always"},        # when typo
    {"type": ["gotcha"]},       # types typo
    {"min_notes": 1, "extra": True},
])
def test_validate_dod_knowledge_spec_rejects_unknown_keys_failclosed(spec) -> None:
    # A typo'd key must fail CLOSED (HOLD_INVALID_DOD_POLICY at the CLI), never be dropped so the
    # intended (stricter) requirement silently reverts to a weaker default and a close GOes.
    with pytest.raises(close.CloseError):
        close.validate_dod_policy(
            {"schema_version": 1, "scopes": {"release": {"knowledge": spec}}})


@pytest.mark.parametrize("bad_type", ["seam", "pointer"])
def test_validate_dod_knowledge_spec_rejects_structural_note_types(bad_type) -> None:
    # MAJOR: seam/pointer are structural index notes, not captured-learning evidence. A policy
    # must not be able to satisfy the knowledge dimension with them, so they are DISALLOWED as
    # configurable types even though they are valid knowledge-note types generally.
    with pytest.raises(close.CloseError):
        close.validate_dod_policy({"schema_version": 1, "scopes": {"release": {
            "knowledge": {"types": [bad_type]}}}})


def test_validate_dod_knowledge_spec_allows_trio_subsets() -> None:
    for subset in (["lesson"], ["gotcha", "decision"], ["decision", "gotcha", "lesson"]):
        pol = close.validate_dod_policy({"schema_version": 1, "scopes": {"release": {
            "knowledge": {"types": subset}}}})
        assert pol["scopes"]["release"]["knowledge"]["types"] == sorted(set(subset))


# ---- evaluate_dod knowledge (pure)

def test_evaluate_dod_knowledge_not_required_when_no_remediation() -> None:
    ev = _dod_eval_k(_knowledge_bundle(when="on_remediation", has_remediation=False,
                                       bound_notes=[]))
    assert close.evaluate_dod(_satisfied(), ev) == []


def test_evaluate_dod_knowledge_always_with_no_notes_holds_missing() -> None:
    # when=always requires knowledge even with no remediation on the close.
    ev = _dod_eval_k(_knowledge_bundle(when="always", has_remediation=False, bound_notes=[]))
    holds = close.evaluate_dod(_satisfied(), ev)
    assert len(holds) == 1 and holds[0][0] == close.HOLD_MISSING_KNOWLEDGE


def test_evaluate_dod_knowledge_remediation_no_notes_holds_missing() -> None:
    ev = _dod_eval_k(_knowledge_bundle(bound_notes=[]))
    assert close.evaluate_dod(_satisfied(), ev)[0][0] == close.HOLD_MISSING_KNOWLEDGE


def test_evaluate_dod_knowledge_only_stubs_holds_trivial() -> None:
    ev = _dod_eval_k(_knowledge_bundle(min_body_chars=40,
                                       bound_notes=[{"type": "gotcha", "body_len": 12}]))
    assert close.evaluate_dod(_satisfied(), ev)[0][0] == close.HOLD_TRIVIAL_EVIDENCE


def test_evaluate_dod_knowledge_fewer_than_min_holds_missing() -> None:
    ev = _dod_eval_k(_knowledge_bundle(min_notes=2,
                                       bound_notes=[{"type": "lesson", "body_len": 60}]))
    assert close.evaluate_dod(_satisfied(), ev)[0][0] == close.HOLD_MISSING_KNOWLEDGE


def test_evaluate_dod_knowledge_satisfied_is_empty() -> None:
    assert close.evaluate_dod(_satisfied(), _dod_eval_k(_knowledge_bundle())) == []


def test_evaluate_dod_knowledge_missing_bundle_holds_missing() -> None:
    ev = {"policy_present": True, "policy_error": None,
          "required_dimensions": {"knowledge": {}}, "knowledge": None}
    assert close.evaluate_dod(_satisfied(), ev)[0][0] == close.HOLD_MISSING_KNOWLEDGE


# ---- compute_verdict integration (knowledge)

def test_compute_verdict_unmet_knowledge_flips_go_to_hold() -> None:
    result = close.compute_verdict(
        _satisfied(), _gate_go(), None, None, _dod_eval_k(_knowledge_bundle(bound_notes=[])))
    assert result["verdict"] == close.VERDICT_HOLD
    assert _codes(result) == {close.HOLD_MISSING_KNOWLEDGE}   # reached the DoD fold, not MALFORMED


def test_compute_verdict_satisfied_knowledge_stays_go() -> None:
    result = close.compute_verdict(
        _satisfied(), _gate_go(), None, None, _dod_eval_k(_knowledge_bundle()))
    assert result["verdict"] == close.VERDICT_GO and result["holds"] == []


# ---- resolver against REAL knowledge events (impure bridge, real note shape)

def _add_note(store, *, key: str, ntype: str, body: str, anchor: dict | None,
              vsha: str | None, curate: bool = True) -> None:
    pub = kn.new_publish_event(
        note_id="kn-" + key.replace(".", "-"), key=key, type=ntype, domain_id="cli",
        body=body, anchor=anchor, verified_against_sha=vsha,
        domain_registry_hash="rh1", domain_definition_hash="d" * 64,
        author="dev", resolved_from="active_agent", at="t1")
    kn.append_event(store, pub)
    if curate:
        kn.append_event(store, kn.new_curate_event(
            base=pub, action="verify", curated_by="lead", resolved_from="lead",
            at="t2", reason=None, domain_registry_hash="rh1"))


def test_resolve_dod_knowledge_binds_curated_notes_by_sha_and_vsha(tmp_path: Path) -> None:
    root = _init(tmp_path)
    s = Store(root)
    long_body = "a genuinely non-trivial lesson body well over forty characters long"
    # bound by sha anchor (curated, allowed type) -> counts
    _add_note(s, key="dod.bysha", ntype="gotcha", body=long_body,
              anchor={"kind": "sha", "sha": SHA}, vsha=None)
    # bound by verified_against_sha -> counts (path anchor, but vsha matches revision)
    _add_note(s, key="dod.byvsha", ntype="decision", body=long_body,
              anchor={"kind": "path", "path": "src/x.py"}, vsha=SHA)
    # anchored to a DIFFERENT sha -> excluded
    _add_note(s, key="dod.other", ntype="gotcha", body=long_body,
              anchor={"kind": "sha", "sha": OTHER_SHA}, vsha=None)
    # bound but UNCURATED -> excluded
    _add_note(s, key="dod.uncur", ntype="gotcha", body=long_body,
              anchor={"kind": "sha", "sha": SHA}, vsha=None, curate=False)
    # bound + curated but WRONG type (seam not in the allowed set) -> excluded
    _add_note(s, key="dod.wrongtype", ntype="seam", body=long_body,
              anchor={"kind": "sha", "sha": SHA}, vsha=None)

    spec = {"when": "on_remediation", "min_notes": 1, "min_body_chars": 40,
            "types": ["decision", "gotcha", "lesson"]}
    rec = close.empty_close(
        "c", scope="release", revision=SHA, revision_kind="sha", gate_scope="release",
        opened_by="lead", opened_at="t0", epoch_at_open=None, required_lenses=[],
        revision_clean=True, dirty_artifact=None)
    rec["remediation_items"] = {"r1": {"id": "r1", "blocker": False}}
    k = cli._resolve_dod_knowledge(s, spec, rec)
    assert k["has_remediation"] is True
    kinds = sorted(n["type"] for n in k["bound_notes"])
    assert kinds == ["decision", "gotcha"]   # only the two curated+bound+allowed-type notes
    # and it clears the pure gate
    assert close._evaluate_dod_knowledge(rec, k) == []


@pytest.mark.parametrize("body,expect_len,label", [
    ("x" + " " * 39, 1, "trailing-spaces"),                 # BLOCKER 3 (original)
    ("x" + " " * 39 + "y", 2, "interior-spaces"),           # re-review: interior padding
    ("​" * 40, 0, "zero-width-space-only"),            # re-review: invisible U+200B (Cf)
    ("x " * 20, 20, "alternating-space"),                   # only the 20 visible x's count
    ("\t\n" + "z" * 3 + " " * 30, 3, "mixed-ws-nbsp"),  # NBSP (Zs) + control -> 3
])
def test_resolve_dod_knowledge_padding_never_clears_floor(
        tmp_path: Path, body, expect_len, label) -> None:
    # min_body_chars is a SUBSTANTIVE-content floor: padding ANYWHERE - trailing/interior
    # whitespace, invisible U+200B, NBSP, control chars - must not buy past it (Codex re-review).
    root = _init(tmp_path)
    s = Store(root)
    _add_note(s, key=f"dod.pad.{label}", ntype="gotcha", body=body,
              anchor={"kind": "sha", "sha": SHA}, vsha=None)
    spec = {"when": "on_remediation", "min_notes": 1, "min_body_chars": 40,
            "types": ["decision", "gotcha", "lesson"]}
    rec = close.empty_close(
        "c", scope="release", revision=SHA, revision_kind="sha", gate_scope="release",
        opened_by="lead", opened_at="t0", epoch_at_open=None, required_lenses=[],
        revision_clean=True, dirty_artifact=None)
    rec["remediation_items"] = {"r1": {"id": "r1", "blocker": False}}
    k = cli._resolve_dod_knowledge(s, spec, rec)
    assert k["bound_notes"] == [{"type": "gotcha", "body_len": expect_len}]
    # bound-but-not-qualifying -> HOLD_TRIVIAL_EVIDENCE, never a GO
    assert close._evaluate_dod_knowledge(rec, k)[0][0] == close.HOLD_TRIVIAL_EVIDENCE


def test_substantive_len_counts_only_visible_chars() -> None:
    assert cli._substantive_len("hello world") == 10          # space excluded
    assert cli._substantive_len("​​​") == 0    # zero-width spaces
    assert cli._substantive_len("  x\t\n") == 1
    assert cli._substantive_len("café ☃!") == 6               # accented + symbol count


@pytest.mark.parametrize("body,label", [
    ("͏" * 40, "combining-grapheme-joiner"),   # Mn, default-ignorable
    ("️" * 40, "variation-selector-16"),        # Mn
    ("\U000e0100" * 40, "variation-selector-17"),    # Mn (supplementary)
    ("᠋" * 40, "mongolian-fvs-one"),            # Mn
    ("ㅤ" * 40, "hangul-filler"),                # Lo, blank glyph
    ("⠀" * 40, "braille-blank"),                # So, blank glyph
    ("ᅟ" * 40, "hangul-choseong-filler"),       # Lo, blank glyph
    ("﻿" * 40, "bom-zwnbsp"),                   # Cf (regression: still 0)
])
def test_substantive_len_default_ignorable_and_blank_glyphs_are_zero(body, label) -> None:
    # Re-review round 3: general category is NOT a visibility predicate. Default-ignorable
    # combining marks (Mn) and blank-glyph fillers (Lo/So) render empty but escaped the Z*/C*
    # filter. A body of 40 of any of them must count as 0 substantive chars.
    assert cli._substantive_len(body) == 0


@pytest.mark.parametrize("body,expect", [
    ("é" * 30, 30),        # base letter + combining acute: the 30 bases count, marks don't
    ("abc️", 3),            # emoji variation selector after 3 letters -> 3
    ("x" * 40 + "​" * 99, 40),   # padding after real content doesn't inflate OR deflate
])
def test_substantive_len_counts_visible_base_despite_marks(body, expect) -> None:
    assert cli._substantive_len(body) == expect


@pytest.mark.parametrize("body,label", [
    ("͏" * 40, "combining-grapheme-joiner"),
    ("️" * 40, "variation-selector-16"),
    ("ㅤ" * 40, "hangul-filler"),
    ("⠀" * 40, "braille-blank"),
])
def test_resolve_dod_knowledge_default_ignorable_body_does_not_clear(
        tmp_path: Path, body, label) -> None:
    # End-to-end (real publish/curate): an effectively-invisible note must NOT clear the floor.
    root = _init(tmp_path)
    s = Store(root)
    _add_note(s, key=f"dod.di.{label}", ntype="gotcha", body=body,
              anchor={"kind": "sha", "sha": SHA}, vsha=None)
    spec = {"when": "on_remediation", "min_notes": 1, "min_body_chars": 40,
            "types": ["decision", "gotcha", "lesson"]}
    rec = close.empty_close(
        "c", scope="release", revision=SHA, revision_kind="sha", gate_scope="release",
        opened_by="lead", opened_at="t0", epoch_at_open=None, required_lenses=[],
        revision_clean=True, dirty_artifact=None)
    rec["remediation_items"] = {"r1": {"id": "r1", "blocker": False}}
    k = cli._resolve_dod_knowledge(s, spec, rec)
    assert k["bound_notes"] == [{"type": "gotcha", "body_len": 0}]
    assert close._evaluate_dod_knowledge(rec, k)[0][0] == close.HOLD_TRIVIAL_EVIDENCE


@pytest.mark.parametrize("cp,label", [
    ("\U0001D159", "musical-null-notehead"),   # So
    ("\U00013441", "egyptian-full-blank"),      # Lo, rendered as whitespace (Unicode ch.11)
    ("\U00013442", "egyptian-half-blank"),      # Lo
])
def test_substantive_len_maintained_blank_fillers_are_zero(cp, label) -> None:
    # Blank glyphs in Lo/So that no category rule can catch (they are deliberately categorized as
    # letters/symbols) — covered only by the maintained _BLANK_GLYPH_FILLERS blocklist. Added when
    # Codex found them; the bounded residual (obscure unlisted blanks) is documented, not claimed
    # closed.
    assert ord(cp) in cli._BLANK_GLYPH_FILLERS         # it IS in the maintained set
    assert cli._substantive_len(cp * 40) == 0          # 40 blanks -> 0 substantive
    assert cli._substantive_len(cp * 39 + "z") == 1    # a single real char still counts
