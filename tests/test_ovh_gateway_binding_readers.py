"""AC5: what every existing reader of the child rows, the gateway ledger and its
status() shows on a child-cap schema-4 ledger.

The readers: ``SpendLedger.status()`` and master's own calls; the service projection
``gateway_status`` and the ``agenttalk gateway status`` command; ``doctor``; the
``agenttalk wrap`` preflight; the wrapper's gateway client (the child-turn open at
spawn and the dead-letter close); and attention and both consoles, which read
nothing from the gateway. The front's spend checks (reserve and settle) run on
schema-4 ledgers in tests/test_ovh_gateway_front.py, because a fresh install is now
schema 4.

All ledgers are temporary; the probes of the gateway's real ports and front are
replaced, so nothing here reaches a running gateway."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import gateway_binding_fixtures as fx
import gateway_schema3_scenario as scenario
import pytest

from agenttalk import cli
from agenttalk import doctor
from agenttalk import ovh_gateway as gateway
from agenttalk import ovh_gateway_service as service
from agenttalk.ovh_gateway import GatewayConfigError, SpendLedger
from agenttalk.ovh_gateway_service import (
    TaskCommands,
    gateway_status,
    initialize_install,
    install_task,
    project_task_name,
    runtime_marker_path,
)
from agenttalk.store import Store
from agenttalk.wrapper import run
from agenttalk.wrapper import session
from agenttalk.wrapper.loop import CLASS_GATEWAY_HELD

GOLDEN_SCHEMA3 = Path(__file__).resolve().parent / "golden" / "gateway_schema3_c80e1e5.json"
REPORT_KEYS = {
    "child_receipt_report_version",
    "child_receipts",
    "child_receipts_pending",
    "child_receipts_fallback",
    "child_receipts_through_seq",
}


@pytest.fixture(autouse=True)
def _never_reach_the_live_ledger(tmp_path_factory, monkeypatch) -> None:
    # A bare SpendLedger() resolves to the host's real ledger via LOCALAPPDATA.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path_factory.mktemp("no-live-appdata")))


class FakeCommands(TaskCommands):
    def __init__(self) -> None:
        self.tasks: dict[str, str] = {}

    def query_xml(self, task_name: str) -> str | None:
        return self.tasks.get(task_name)

    def install(self, task_name: str, xml_path: Path) -> None:
        self.tasks[task_name] = xml_path.read_text(encoding="utf-16")

    def stop(self, task_name: str) -> None:
        pass

    def start(self, task_name: str) -> None:
        pass


class _Install:
    """A schema-3 install with a running-gateway marker, as before the upgrade."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("OVH_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        self.root = tmp_path / "project"
        self.ledger = SpendLedger(tmp_path / "spend" / "ledger.sqlite3", tmp_path / "spend" / "install.json")
        executable = tmp_path / "litellm.exe"
        executable.write_bytes(b"fake")
        self.front_token = tmp_path / "secrets" / "front.txt"
        self.internal_token = tmp_path / "secrets" / "internal.txt"
        initialize_install(
            self.root,
            litellm_executable=executable,
            opening_micro_eur=0,
            opening_evidence=fx.OPENING_EVIDENCE,
            ledger=self.ledger,
            front_token_path=self.front_token,
            internal_token_path=self.internal_token,
        )
        fx.to_schema3(self.ledger)
        self.commands = FakeCommands()
        install_task(self.root, commands=self.commands, ledger=self.ledger)
        monkeypatch.setattr(
            service,
            "exclusive_bind_probe",
            lambda _host, _port: (_ for _ in ()).throw(GatewayConfigError("occupied")),
        )
        monkeypatch.setattr(service.GatewayFront, "internal_liveliness", lambda _self: True)
        monkeypatch.setattr(service, "_public_front_attested", lambda: True)
        self.start_runtime()

    @property
    def issuer(self) -> str:
        return gateway.read_secret_file(self.front_token)

    def start_runtime(self) -> None:
        """What the runner writes at each start: the marker pins the ledger's current
        child-cap policy hash."""
        manifest = service.load_install_manifest(self.root, ledger=self.ledger)
        start = service._process_start_token(os.getpid())
        ledger_status = self.ledger.status()
        service._durable_write_json(runtime_marker_path(self.root), {
            "schema_version": service.RUNTIME_SCHEMA_VERSION,
            "runner_pid": os.getpid(),
            "runner_start": start,
            "litellm_pid": os.getpid(),
            "litellm_start": start,
            "task_name": project_task_name(self.root),
            "price_policy_hash": ledger_status["policy_hash"],
            "child_cap_policy_hash": ledger_status["child_cap_policy_hash"],
            "config_sha256": manifest["litellm_config_sha256"],
            "public_bind": "127.0.0.1:4000",
            "internal_bind": "127.0.0.1:4001",
            "front_token_sha256": hashlib.sha256(self.issuer.encode("utf-8")).hexdigest(),
        })

    def status(self) -> dict:
        return gateway_status(
            self.root,
            commands=self.commands,
            ledger=self.ledger,
            front_token_path=self.front_token,
            internal_token_path=self.internal_token,
        )


@pytest.fixture
def install(tmp_path, monkeypatch) -> _Install:
    return _Install(tmp_path, monkeypatch)


def _without(mapping: dict, *keys: str) -> dict:
    return {key: value for key, value in mapping.items() if key not in keys}


# --- SpendLedger.status() and master's calls ---------------------------------------------------


def test_masters_calls_on_schema4_differ_from_master_only_as_documented(tmp_path, monkeypatch):
    # The same calls as the AC1 golden, on a schema-4 ledger with the flag off.
    monkeypatch.setattr(gateway, "CHILD_TURN_MAX_CALLS", scenario.MAX_CALLS)
    clock = fx.Clock()
    ledger = fx.make_ledger(tmp_path, clock)
    record = json.loads(json.dumps(scenario.run(ledger, clock)))
    golden = json.loads(GOLDEN_SCHEMA3.read_text(encoding="utf-8"))
    assert [step[:2] for step in record] == [step[:2] for step in golden]
    for got, want in zip(record, golden, strict=True):
        if not got[0].startswith("status"):
            assert got == want  # every result and every refusal message is master's
            continue
        new, old = got[2], want[2]
        # 1. the five report keys, on schema 4 only
        assert set(new) == set(old) | REPORT_KEYS
        assert {key: new[key] for key in REPORT_KEYS} == {
            "child_receipt_report_version": 1, "child_receipts": 0, "child_receipts_pending": 0,
            "child_receipts_fallback": 0, "child_receipts_through_seq": 0,
        }
        # 2. the schema numbers and the child-cap policy hash
        assert (old["schema_version"], new["schema_version"]) == (2, 3)
        assert (old["child_cap_schema_version"], new["child_cap_schema_version"]) == (3, 4)
        assert new["child_cap_policy_hash"] == gateway.child_cap_policy_hash(
            child_turn_max_micro_eur=gateway.TRIAL_CUTOFF_MICRO_EUR)
        # 3. a finished row keeps its first ending (3.2): master turned the capped
        #    message msg-a into "expired" once its wall time also ran out
        rows_new = {row["message_id"]: row for row in new["active_child_turns"]}
        rows_old = {row["message_id"]: row for row in old["active_child_turns"]}
        if got[0] == "status after expiry":
            assert (rows_old["msg-a"]["state"], rows_new["msg-a"]["state"]) == ("expired", "capped")
            rows_new["msg-a"] = dict(rows_new["msg-a"], state="expired")
        assert rows_new == rows_old
        assert _without(new, *REPORT_KEYS, "schema_version", "child_cap_schema_version",
                        "child_cap_policy_hash", "active_child_turns") == _without(
            old, "schema_version", "child_cap_schema_version", "child_cap_policy_hash",
            "active_child_turns")


# --- gateway_status, `agenttalk gateway status`, the wrap preflight, doctor ---------------------


def test_gateway_status_after_the_migration_adds_five_ledger_keys_once_restarted(install):
    before = install.status()
    assert before["ready"] is True
    assert "child_receipts" not in before["ledger"]

    install.ledger.install_child_cap_binding(issuer_token=install.issuer)
    stale = install.status()
    # the running gateway pinned the old policy hash: not ready until it restarts
    assert stale["ready"] is False and stale["errors"] == ["runtime_marker_invalid"]

    install.start_runtime()
    after = install.status()
    assert after["ready"] is True
    assert set(after) == set(before)
    assert _without(after, "ledger", "runtime", "child_cap_policy_hash") == _without(
        before, "ledger", "runtime", "child_cap_policy_hash")
    assert _without(after["runtime"], "child_cap_policy_hash") == _without(
        before["runtime"], "child_cap_policy_hash")
    assert set(after["ledger"]) == set(before["ledger"]) | REPORT_KEYS
    assert after["ledger"]["child_receipt_report_version"] == 1
    assert (before["ledger"]["schema_version"], after["ledger"]["schema_version"]) == (2, 3)
    assert _without(after["ledger"], *REPORT_KEYS, "schema_version", "child_cap_schema_version",
                    "child_cap_policy_hash") == _without(
        before["ledger"], "schema_version", "child_cap_schema_version", "child_cap_policy_hash")
    # the fields the wrap preflight reads are unchanged
    for key in ("ready", "operational_ready", "errors", "worker_spend_ready", "worker_spend_errors"):
        assert after[key] == before[key]


def test_gateway_status_command_prints_the_new_keys_on_schema4(install, monkeypatch, capsys):
    install.ledger.install_child_cap_binding(issuer_token=install.issuer)
    install.start_runtime()
    store = Store(install.root.parent / "cli-store")
    store.init(["lead"])
    monkeypatch.setattr(service, "gateway_status", lambda _root: install.status())
    assert cli.main(["--root", str(store.root), "gateway", "status"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert REPORT_KEYS <= set(printed["ledger"])
    assert printed["ledger"]["child_receipt_report_version"] == 1


def _doctor_check(tmp_path, monkeypatch, install) -> doctor.Check:
    store = Store(tmp_path / "doctor-store")
    store.init(["lead", "qwen-dev-1"])
    store.set_trust_class("qwen-dev-1", "external-worker")
    (store.dir / "supervisor.json").write_text(json.dumps({"agents": {"qwen-dev-1": {
        "backend_profile": "ovh-qwen", "cli": "claude", "model": gateway.MODEL_ALIAS,
        "trust_class": "external-worker", "wrapped": True,
    }}}), encoding="utf-8")
    monkeypatch.setattr(service, "gateway_status", lambda _root: install.status())
    return doctor._check_ovh_qwen_gateway(store)


def test_doctor_reports_the_same_on_schema3_and_schema4(tmp_path, monkeypatch, install):
    # doctor's words come from the projection's errors only; its JSON data is the
    # projection itself, so it gains the same five ledger keys as gateway status.
    before = _doctor_check(tmp_path / "3", monkeypatch, install)
    assert before.status == "ok"
    install.ledger.install_child_cap_binding(issuer_token=install.issuer)
    stale = _doctor_check(tmp_path / "stale", monkeypatch, install)
    assert stale.status == "error" and "runtime_marker_invalid" in stale.details
    install.start_runtime()
    after = _doctor_check(tmp_path / "4", monkeypatch, install)
    assert (after.name, after.status, after.details, after.fix) == (
        before.name, before.status, before.details, before.fix)
    assert set(after.data["ledger"]) == set(before.data["ledger"]) | REPORT_KEYS


# --- the wrapper's gateway client --------------------------------------------------------------


def test_the_dead_letter_close_on_schema4_is_unchanged_for_its_unbound_rows(tmp_path, monkeypatch):
    ledger = fx.make_ledger(tmp_path)
    monkeypatch.setattr(gateway, "SpendLedger", lambda: ledger)
    fx.open_unbound(ledger, "msg-a")
    run.close_ovh_child_turn_on_dead_letter(
        "qwen-dev-1", {"id": "msg-a"}, backend_profile="ovh-qwen",
        profile_env={"ANTHROPIC_AUTH_TOKEN": fx.ISSUER},
    )
    assert fx.rows(ledger, "SELECT state, reason FROM child_turns") == [("expired", "dead_letter")]
    assert fx.rows(ledger, "SELECT COUNT(*) FROM child_receipts") == [(0,)]


def test_the_dead_letter_close_leaves_a_bound_row_to_its_owner(tmp_path, monkeypatch):
    # This patch adds no wrapper change; the wrapper never opens a bound row yet. If
    # one exists, today's close (no reference, no outcome) is refused and, as for any
    # ledger refusal there, swallowed: the row keeps its binding and stays open.
    ledger = fx.make_ledger(tmp_path)
    monkeypatch.setattr(gateway, "SpendLedger", lambda: ledger)
    fx.open_bound(ledger, "msg-a")
    run.close_ovh_child_turn_on_dead_letter(
        "qwen-dev-1", {"id": "msg-a"}, backend_profile="ovh-qwen",
        profile_env={"ANTHROPIC_AUTH_TOKEN": fx.ISSUER},
    )
    assert fx.rows(ledger, "SELECT state FROM child_turns") == [("open",)]


def test_with_the_flag_on_the_wrappers_unbound_open_parks_as_held(tmp_path, monkeypatch):
    ledger = fx.make_ledger(tmp_path)
    ledger.set_quota_lease_binding_required(required=True, issuer_token=fx.ISSUER)
    store = Store(tmp_path / "store")
    store.init(["lead", "qwen-dev-1"])

    def must_not_spawn(*_args, **_kwargs):
        raise AssertionError("paid child spawned while an unbound open was refused")

    monkeypatch.setattr(gateway, "SpendLedger", lambda: ledger)
    monkeypatch.setattr(run, "_ProcStream", must_not_spawn)
    drive = run.make_drive(
        store, "qwen-dev-1", "claude",
        session.SessionState(cli="claude", claude_session_id="session-1"),
        ["claude"], render=False, backend_profile="ovh-qwen",
        profile_env={"ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
                     "ANTHROPIC_AUTH_TOKEN": fx.ISSUER},
    )
    outcome = drive({"id": "msg-a", "from": "lead", "kind": "question", "body": "do work",
                     "meta": {"request_id": "q-parent"}})
    assert (outcome.ok, outcome.failure_class) == (False, CLASS_GATEWAY_HELD)
    assert fx.rows(ledger, "SELECT COUNT(*) FROM child_turns") == [(0,)]


# --- attention and both consoles ---------------------------------------------------------------


def test_attention_and_both_consoles_read_nothing_from_the_gateway():
    # The web console and the /dashboard landing are both served by web.py and
    # web_static; attention has its own module. None of them reads the gateway, its
    # ledger or its status, so they show the same before and after.
    package = Path(gateway.__file__).resolve().parent
    readers = [package / "attention.py", package / "web.py", *sorted((package / "web_static").rglob("*"))]
    pattern = re.compile(r"ovh_gateway|gateway_status|SpendLedger|child_turns|child_receipts|"
                         r"receipt_pending|quota_lease")
    touched = [
        path.relative_to(package).as_posix()
        for path in readers
        if path.is_file() and pattern.search(path.read_text(encoding="utf-8", errors="replace"))
    ]
    assert touched == []


def test_the_status_projection_is_built_from_the_ledger_status_whole():
    # gateway_status copies status() into "ledger" whole, so a new ledger key reaches
    # `agenttalk gateway status`, doctor's source and the wrap preflight's source
    # without a second allowlist that could drop or rename it.
    source = Path(service.__file__).read_text(encoding="utf-8")
    assert 'result["ledger"] = ledger.status()' in source
