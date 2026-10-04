"""The downgrade fence, proved with the real code from before quota lease binding.

The gateway module of master c80e1e5 is loaded beside the current one. Every one of
its entry points must refuse a ledger that holds child-cap schema 4 (a migrated
ledger, a fresh install, and a migration whose install marker was not moved yet), and
must leave the ledger and its install marker exactly as they were.

The module comes from a recorded copy, tests/golden/ovh_gateway_c80e1e5.py.golden,
made with `git show c80e1e5:src/agenttalk/ovh_gateway.py`. The dev gate tests an
exported tree without git history, so reading the commit itself skipped every one of
these tests in CI (#320). The copy's SHA-256 is pinned below, and where git history
is present it is compared byte for byte with the commit.

All ledgers are temporary files; nothing touches a real gateway."""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import gateway_binding_fixtures as fx
import pytest

from agenttalk import ovh_gateway as gateway

BASE_COMMIT = "c80e1e5c7c42a2f5de4dcd899ae790a4daa3ff11"
BASE_MODULE = Path(__file__).resolve().parent / "golden" / "ovh_gateway_c80e1e5.py.golden"
BASE_MODULE_SHA256 = "62058a5ce6e429b762847e4133d1f51c9025fe5e006201d03e6caf2a2403ed32"


@pytest.fixture(autouse=True)
def _never_reach_the_live_ledger(tmp_path_factory, monkeypatch) -> None:
    # A bare SpendLedger() resolves to the host's real ledger via LOCALAPPDATA.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path_factory.mktemp("no-live-appdata")))


def _recorded_base_module() -> bytes:
    source = BASE_MODULE.read_bytes()
    assert hashlib.sha256(source).hexdigest() == BASE_MODULE_SHA256, (
        "the recorded base module is not the one this test pins"
    )
    return source


def test_the_recorded_base_module_is_the_base_commits_own():
    """The golden is the module at BASE_COMMIT, byte for byte. Where git history is
    absent (the dev gate's exported tree) the pinned SHA-256 above still holds it."""
    source = _recorded_base_module()
    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not available to compare with the base commit")
    shown = subprocess.run(  # noqa: S603 - fixed arguments, no shell
        [git, "-C", str(Path(__file__).resolve().parents[1]), "show",
         f"{BASE_COMMIT}:src/agenttalk/ovh_gateway.py"],
        capture_output=True,
        check=False,
    )
    if shown.returncode != 0:
        pytest.skip("the base commit is not in this checkout's history; the SHA-256 still holds")
    assert shown.stdout == source


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    """The gateway module as it was before quota lease binding."""
    path = tmp_path_factory.mktemp("base") / "ovh_gateway_c80e1e5.py"
    path.write_bytes(_recorded_base_module())
    name = "agenttalk._ovh_gateway_c80e1e5"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        assert not hasattr(module, "QuotaLeaseReferenceMismatch")  # really the old code
        yield module
    finally:
        sys.modules.pop(name, None)


def _activity(ledger) -> dict:
    """Work under the current code: a pending receipt (an unresolved attempt of a
    closed bound turn), a written receipt, and an open unbound turn."""
    done = fx.open_bound(ledger, "msg-done", reference="ref-done")
    ledger.reserve_for_child(fx.attempt(2), capability=done.token)
    fx.settle(ledger, fx.attempt(2))
    fx.close_bound(ledger, "msg-done", reference="ref-done")
    unbound = fx.open_unbound(ledger, "msg-unbound")
    # last: an unresolved attempt blocks every later open
    pending = fx.open_bound(ledger, "msg-pending", reference="ref-pending")
    ledger.reserve_for_child(fx.attempt(1), capability=pending.token)
    fx.close_bound(ledger, "msg-pending", reference="ref-pending")
    return {"capability": unbound.token}


def _migrated(tmp_path, clock):
    ledger = fx.make_schema3_ledger(tmp_path, clock)
    ledger.install_child_cap_binding(issuer_token=fx.ISSUER)
    return ledger, _activity(ledger)


def _fresh(tmp_path, clock):
    ledger = fx.make_ledger(tmp_path, clock)
    return ledger, _activity(ledger)


def _marker_not_moved(tmp_path, clock):
    ledger = fx.make_schema3_ledger(tmp_path, clock)
    unbound = fx.open_unbound(ledger, "msg-unbound")
    real_write = gateway._durable_write_json

    def fail(_path, _value):
        raise OSError("injected marker projection failure")

    gateway._durable_write_json = fail
    try:
        with pytest.raises(OSError):
            ledger.install_child_cap_binding(issuer_token=fx.ISSUER)
    finally:
        gateway._durable_write_json = real_write
    return ledger, {"capability": unbound.token}


def _calls(context: dict) -> dict:
    agent = "qwen-dev-1"
    return {
        "status": lambda old: old.status(),
        "policy_hashes": lambda old: old.policy_hashes(),
        "verify_child_cap_issuer": lambda old: old.verify_child_cap_issuer(fx.ISSUER),
        "open a new child turn": lambda old: old.open_child_turn(
            agent=agent, message_id="msg-new", issuer_token=fx.ISSUER),
        "open an existing child turn": lambda old: old.open_child_turn(
            agent=agent, message_id="msg-unbound", issuer_token=fx.ISSUER),
        "close an unbound child turn": lambda old: old.close_child_turn(
            agent=agent, message_id="msg-unbound", reason="dead_letter", issuer_token=fx.ISSUER),
        "close a bound child turn": lambda old: old.close_child_turn(
            agent=agent, message_id="msg-pending", reason="dead_letter", issuer_token=fx.ISSUER),
        "reserve": lambda old: old.reserve("e" * 32),
        "reserve_for_child": lambda old: old.reserve_for_child(
            "f" * 32, capability=context["capability"]),
        "mark_uncertain": lambda old: old.mark_uncertain(fx.attempt(1), reason="usage lost"),
        "settle": lambda old: old.settle(
            fx.attempt(1), model=gateway.MODEL_ALIAS, input_tokens=1_000, output_tokens=100),
        "reconcile charge-reserve": lambda old: old.reconcile(
            fx.attempt(1), outcome="charge-reserve", reason="operator"),
        "reconcile no-send": lambda old: old.reconcile(
            fx.attempt(1), outcome="no-send", reason="operator"),
        "place_hold": lambda old: old.place_hold(reason="operator"),
        "clear_hold": lambda old: old.clear_hold(reason="operator"),
        "verify_dashboard_canary": lambda old: old.verify_dashboard_canary(
            fx.attempt(2), observed_delta_micro_eur=670),
        "install_child_caps": lambda old: old.install_child_caps(issuer_token=fx.ISSUER),
        "initialize": lambda old: old.initialize(
            opening_micro_eur=0, opening_evidence=fx.OPENING_EVIDENCE,
            generation=fx.GENERATION, child_cap_issuer_token=fx.ISSUER),
    }


CALL_NAMES = sorted(_calls({"capability": ""}))


@pytest.mark.parametrize("make", [_migrated, _fresh, _marker_not_moved],
                         ids=["migrated", "fresh install", "marker not moved yet"])
@pytest.mark.parametrize("call", CALL_NAMES)
def test_code_from_before_binding_refuses_every_operation_and_changes_nothing(
    tmp_path, base, make, call
):
    clock = fx.Clock()
    ledger, context = make(tmp_path, clock)
    before = (fx.dump(ledger), ledger.marker_path.read_bytes())
    old = base.SpendLedger(ledger.db_path, ledger.marker_path, now=clock)
    # the refusal is the ledger schema fence (initialize: the ledger already exists)
    reason = "both database and install marker to be absent" if call == "initialize" else "schema"
    with pytest.raises(base.GatewayError, match=reason):
        _calls(context)[call](old)
    assert (fx.dump(ledger), ledger.marker_path.read_bytes()) == before


def test_the_base_code_still_serves_a_ledger_that_was_not_migrated(tmp_path, base):
    # The other side of the fence: a schema-3 ledger is the base code's own shape.
    clock = fx.Clock()
    ledger = fx.make_schema3_ledger(tmp_path, clock)
    old = base.SpendLedger(ledger.db_path, ledger.marker_path, now=clock)
    assert old.status()["child_cap_schema_version"] == 3
    old.place_hold(reason="operator")
    assert ledger.status()["service_hold"] == "manual: operator"
