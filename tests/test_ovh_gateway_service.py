from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agenttalk import ovh_gateway_service as service
from agenttalk.ovh_gateway import (
    GatewayConfigError,
    LedgerBlocked,
    MODEL_ALIAS,
    SpendLedger,
    child_cap_policy_hash,
    price_policy_hash,
)
from agenttalk.ovh_gateway_service import (
    MAX_TASK_RESTARTS,
    TaskCommands,
    _safe_gateway_environment,
    expected_task_identity,
    exclusive_bind_probe,
    gateway_status,
    initialize_install,
    install_task,
    kill_switch_path,
    litellm_config_path,
    project_task_name,
    render_task_xml,
    run_service,
    runtime_marker_path,
    start_task,
    stop_task,
    task_identity_path,
    task_xml_matches,
)


class FakeCommands(TaskCommands):
    def __init__(self) -> None:
        self.tasks: dict[str, str] = {}
        self.stopped: list[str] = []
        self.started: list[str] = []

    def query_xml(self, task_name: str) -> str | None:
        return self.tasks.get(task_name)

    def install(self, task_name: str, xml_path: Path) -> None:
        self.tasks[task_name] = xml_path.read_text(encoding="utf-16")

    def stop(self, task_name: str) -> None:
        self.stopped.append(task_name)

    def start(self, task_name: str) -> None:
        self.started.append(task_name)


class RacingInstallCommands(FakeCommands):
    def __init__(self, identity) -> None:
        super().__init__()
        self.identity = identity

    def install(self, task_name: str, _xml_path: Path) -> None:
        self.tasks[task_name] = render_task_xml(self.identity)
        raise GatewayConfigError("concurrent installer won")


class FakeRuntimeProbeCommands:
    def __init__(
        self,
        *,
        returncode: int = 0,
        error: Exception | None = None,
        stdout: bytes = b"\r\nLiteLLM: Current Version = 1.91.3\r\n\r\n",
        side_effect=None,
    ) -> None:
        self.returncode = returncode
        self.error = error
        self.stdout = stdout
        self.side_effect = side_effect
        self.calls: list[dict[str, object]] = []

    def run(self, argv, *, cwd, env):
        self.calls.append({"argv": list(argv), "cwd": cwd, "env": dict(env)})
        if self.error is not None:
            raise self.error
        if self.side_effect is not None:
            self.side_effect()
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout)


def _write_windows_batch_forwarder(candidate: Path, implementation: Path) -> None:
    assert candidate.parent.resolve() == implementation.parent.resolve()
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(
        f'@echo off\r\n"{sys.executable}" "%~dp0{implementation.name}" %*\r\n',
        encoding="utf-8",
    )


def _write_windows_litellm_identity_shim(
    directory: Path,
    *,
    suffix: str = ".cmd",
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    implementation = directory / "litellm_identity.py"
    implementation.write_text(
        """import sys

if sys.argv[1:] != [\"--version\"]:
    raise SystemExit(2)
print()
print(\"LiteLLM: Current Version = 1.91.3\")
print()
""",
        encoding="utf-8",
    )
    candidate = directory / f"litellm{suffix}"
    _write_windows_batch_forwarder(candidate, implementation)
    return candidate


def test_project_task_name_is_canonical_and_collision_resistant(tmp_path) -> None:
    first = project_task_name(tmp_path / "Project")
    same = project_task_name(tmp_path / "Project" / ".." / "Project")
    other = project_task_name(tmp_path / "Other")
    assert first == same
    assert first != other
    assert first.startswith("agenttalk-qwen-gateway-")


def test_task_xml_pins_one_least_privilege_action_and_bounded_restart(tmp_path) -> None:
    identity = expected_task_identity(
        tmp_path,
        execute=tmp_path / "python.exe",
        principal="DOMAIN\\operator",
    )
    xml = render_task_xml(identity)
    assert task_xml_matches(xml, identity)
    assert xml.count("<Exec>") == 1
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml
    assert "<RunLevel>LeastPrivilege</RunLevel>" in xml
    assert f"<Count>{MAX_TASK_RESTARTS}</Count>" in xml
    assert "OVH_KEY" not in xml
    assert "api_key" not in xml


def test_task_xml_matches_sid_normalized_scheduler_fixture(tmp_path, monkeypatch) -> None:
    identity = expected_task_identity(
        tmp_path,
        execute=tmp_path / "python.exe",
        principal="DOMAIN\\operator",
    )
    sid = "S-1-5-21-111-222-333-1001"
    scheduler_xml = render_task_xml(identity).replace(
        "<UserId>DOMAIN\\operator</UserId>",
        f"<UserId>{sid}</UserId>",
    )
    monkeypatch.setattr(
        service,
        "_resolve_principal_sid",
        lambda principal: sid if principal in {identity.principal, sid} else None,
        raising=False,
    )

    assert task_xml_matches(scheduler_xml, identity)


@pytest.mark.skipif(os.name != "nt", reason="Windows account SID lookup")
def test_current_windows_principal_round_trips_as_scheduler_sid(tmp_path) -> None:
    identity = expected_task_identity(tmp_path)
    sid = service._resolve_principal_sid(identity.principal)
    assert sid is not None
    assert sid.startswith("S-1-")
    scheduler_xml = render_task_xml(identity).replace(
        f"<UserId>{identity.principal}</UserId>",
        f"<UserId>{sid}</UserId>",
    )

    assert task_xml_matches(scheduler_xml, identity)


def test_install_is_idempotent_and_refuses_foreign_task(tmp_path) -> None:
    commands = FakeCommands()
    execute = tmp_path / "python.exe"
    execute.write_bytes(b"")
    identity = expected_task_identity(tmp_path, execute=execute, principal="D\\u")

    result = install_task(
        tmp_path,
        commands=commands,
        execute=execute,
        principal="D\\u",
    )
    assert result["changed"] is True
    assert task_identity_path(tmp_path).is_file()
    result = install_task(
        tmp_path,
        commands=commands,
        execute=execute,
        principal="D\\u",
    )
    assert result["changed"] is False

    commands.tasks[identity.task_name] = render_task_xml(
        expected_task_identity(tmp_path, execute=tmp_path / "other.exe", principal="D\\u")
    )
    with pytest.raises(GatewayConfigError, match="foreign or mismatched"):
        install_task(
            tmp_path,
            commands=commands,
            execute=execute,
            principal="D\\u",
        )


def test_concurrent_exact_task_installer_converges_without_overwrite(tmp_path) -> None:
    execute = tmp_path / "python.exe"
    execute.write_bytes(b"")
    identity = expected_task_identity(tmp_path, execute=execute, principal="D\\u")
    commands = RacingInstallCommands(identity)

    result = install_task(
        tmp_path,
        commands=commands,
        execute=execute,
        principal="D\\u",
    )

    assert result["installed"] is True
    assert result["changed"] is False
    assert task_xml_matches(commands.tasks[identity.task_name], identity)


def test_exclusive_bind_probe_refuses_occupied_listener() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = int(listener.getsockname()[1])
    try:
        with pytest.raises(GatewayConfigError, match="occupied"):
            exclusive_bind_probe("127.0.0.1", port)
    finally:
        listener.close()
    exclusive_bind_probe("127.0.0.1", port)


def test_one_time_install_writes_nonsecret_config_and_tokens_outside_project(tmp_path) -> None:
    ledger = SpendLedger(tmp_path / "spend" / "ledger.sqlite3", tmp_path / "spend" / "install.json")
    executable = tmp_path / "litellm.exe"
    executable.write_bytes(b"fake")
    front_token = tmp_path / "secrets" / "front.txt"
    internal_token = tmp_path / "secrets" / "internal.txt"
    result = initialize_install(
        tmp_path / "project",
        litellm_executable=executable,
        opening_micro_eur=580_000,
        opening_evidence="test dashboard, observed 2026-07-16",
        ledger=ledger,
        front_token_path=front_token,
        internal_token_path=internal_token,
    )
    assert result["price_policy_hash"] == price_policy_hash()
    assert result["opening_micro_eur"] == 580_000
    assert result["opening_observed_at"]
    config = litellm_config_path(tmp_path / "project").read_text(encoding="utf-8")
    assert "use_chat_completions_url_for_anthropic_messages: true" in config
    assert "store: false" in config
    assert "callback" not in config
    assert "atgw-" not in config
    assert front_token.read_text(encoding="utf-8").startswith("atgw-")
    assert internal_token.read_text(encoding="utf-8").startswith("atgw-")
    with pytest.raises(GatewayConfigError, match="replacement gateway initialization"):
        initialize_install(
            tmp_path / "project",
            litellm_executable=executable,
            opening_micro_eur=580_000,
            opening_evidence="test dashboard, observed 2026-07-16",
            ledger=ledger,
            front_token_path=front_token,
            internal_token_path=internal_token,
        )


def test_gateway_process_environment_is_allowlisted_and_secrets_are_env_only(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-pass")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "must-not-pass")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-pass")
    monkeypatch.setenv("PATH", "safe-path")
    env = _safe_gateway_environment("provider-key", "internal-token")
    assert env["PATH"] == "safe-path"
    assert env["OVH_KEY"] == "provider-key"
    assert env["LITELLM_MASTER_KEY"] == "internal-token"
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "UNRELATED_SECRET" not in env


def test_runner_uses_env_only_secrets_and_can_start_under_manual_hold(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "project"
    ledger = SpendLedger(tmp_path / "spend" / "ledger.sqlite3", tmp_path / "spend" / "install.json")
    executable = tmp_path / "litellm.exe"
    executable.write_bytes(b"fake")
    key = tmp_path / "secrets" / "provider.txt"
    front_token = tmp_path / "secrets" / "front.txt"
    internal_token = tmp_path / "secrets" / "internal.txt"
    initialize_install(
        root,
        litellm_executable=executable,
        opening_micro_eur=0,
        opening_evidence="test dashboard, observed 2026-07-16",
        ledger=ledger,
        front_token_path=front_token,
        internal_token_path=internal_token,
    )
    ledger.place_hold(reason="restart gateway before enabling paid proof")
    key.write_text("provider-secret\n", encoding="utf-8")
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = os.getpid()

        def __init__(self, output: str) -> None:
            self.stdout = io.StringIO(output)

        def poll(self):
            return None

        def terminate(self) -> None:
            return

        def kill(self) -> None:
            return

        def wait(self, timeout=None) -> int:
            return 0

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["env"] = dict(kwargs["env"])
        captured["stdout"] = kwargs["stdout"]
        captured["stderr"] = kwargs["stderr"]
        output = (
            "oversized diagnostic " + "x" * 2_048 + "\n"
            + "bounded startup diagnostic\n" * 80
            + f"raw provider {kwargs['env']['OVH_KEY']}\n"
            f"Authorization: Bearer {kwargs['env']['LITELLM_MASTER_KEY']}\n"
        )
        return FakeProcess(output)

    class FakeServer:
        def serve_forever(self, **_kwargs) -> None:
            return

        def shutdown(self) -> None:
            return

        def server_close(self) -> None:
            return

    class FakeFront:
        def __init__(self, _config, _ledger) -> None:
            return

        def make_server(self):
            return FakeServer()

        def drain(self, _timeout) -> bool:
            return True

    monkeypatch.setattr(service, "exclusive_bind_probe", lambda _host, _port: None)
    monkeypatch.setattr(service, "_wait_liveliness", lambda _front, _process: None)
    monkeypatch.setattr(service, "GatewayFront", FakeFront)
    monkeypatch.setattr(service, "LITELLM_LOG_MAX_BYTES", 256)
    monkeypatch.setattr(service, "LITELLM_LOG_BACKUP_COUNT", 2)
    child_log = tmp_path / "local" / "agenttalk-ovh" / "gateway" / "litellm.log"

    rc = run_service(
        root,
        ledger=ledger,
        key_path=key,
        front_token_path=front_token,
        internal_token_path=internal_token,
        child_log_path=child_log,
        popen=fake_popen,
    )

    assert rc == 0
    argv = captured["argv"]
    encoded_argv = " ".join(argv)
    assert "provider-secret" not in encoded_argv
    assert "atgw-" not in encoded_argv
    assert argv[-2:] == ["--num_workers", "1"]
    assert ["--host", "127.0.0.1", "--port", "4001"] == argv[3:7]
    env = captured["env"]
    assert env["OVH_KEY"] == "provider-secret"
    assert env["LITELLM_MASTER_KEY"].startswith("atgw-")
    assert captured["stdout"] is subprocess.PIPE
    assert captured["stderr"] is subprocess.STDOUT
    logs = sorted(child_log.parent.glob("litellm.log*"))
    assert child_log in logs
    assert child_log.with_suffix(".log.1") in logs
    assert len(logs) <= 3
    assert all(path.stat().st_size <= 256 for path in logs)
    logged = "".join(path.read_text(encoding="utf-8") for path in logs)
    held = ledger.status()
    assert held["service_hold"] == "manual: restart gateway before enabling paid proof"
    assert held["ready"] is False
    assert "provider-secret" not in logged
    assert env["LITELLM_MASTER_KEY"] not in logged
    assert "[REDACTED]" in logged
    assert not runtime_marker_path(root).exists()


def test_run_service_lock_refusal_preserves_an_existing_runtime_marker(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "project"
    marker = runtime_marker_path(root)
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"existing live-owner marker")
    holder = service.LifecycleLockContended({
        "pid": os.getpid(),
        "process_identity": {
            "scheme": "win32-filetime-v1",
            "value": "123",
        },
        "operation": "run-service-startup",
        "acquired_at": "2026-08-17T12:00:00.000000Z",
    })

    class RefusingLock:
        def __enter__(self):
            raise service.GatewayLifecycleContended(holder)

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        service,
        "_gateway_lifecycle_lock",
        lambda _root, _operation: RefusingLock(),
    )

    with pytest.raises(service.GatewayLifecycleContended):
        run_service(root)

    assert marker.read_bytes() == b"existing live-owner marker"


def test_run_service_startup_failure_finishes_cleanup_before_releasing_lock(
    tmp_path,
    monkeypatch,
) -> None:
    root, _, ledger, front_token, internal_token = _install_for_runtime_rebind(
        tmp_path
    )
    provider_key = tmp_path / "secrets" / "provider.txt"
    provider_key.write_text("provider-secret\n", encoding="utf-8")
    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()
    runner_error: list[BaseException] = []

    class FakeProcess:
        pid = os.getpid()

        def __init__(self) -> None:
            self.stdout = io.StringIO("")
            self.alive = True

        def poll(self):
            return None if self.alive else 0

        def terminate(self) -> None:
            cleanup_started.set()
            if not allow_cleanup.wait(timeout=10.0):
                raise AssertionError("test did not allow startup cleanup")
            self.alive = False

        def kill(self) -> None:
            self.alive = False

        def wait(self, timeout=None) -> int:
            self.alive = False
            return 0

    class FakeFront:
        def __init__(self, _config, _ledger) -> None:
            return

    def fail_startup(_front, _process) -> None:
        raise GatewayConfigError("injected startup failure")

    def run() -> None:
        try:
            run_service(
                root,
                ledger=ledger,
                key_path=provider_key,
                front_token_path=front_token,
                internal_token_path=internal_token,
                child_log_path=tmp_path / "litellm.log",
                popen=lambda *_args, **_kwargs: FakeProcess(),
            )
        except BaseException as exc:  # noqa: BLE001 - asserted across thread boundary
            runner_error.append(exc)

    monkeypatch.setattr(service, "exclusive_bind_probe", lambda _host, _port: None)
    monkeypatch.setattr(service, "_wait_liveliness", fail_startup)
    monkeypatch.setattr(service, "GatewayFront", FakeFront)
    monkeypatch.setattr(service, "GATEWAY_LIFECYCLE_LOCK_TIMEOUT_SECONDS", 0.1)
    runner = threading.Thread(target=run, daemon=True)
    runner.start()
    assert cleanup_started.wait(timeout=10.0)

    with pytest.raises(service.GatewayLifecycleContended) as exc_info:
        with service._gateway_lifecycle_lock(root, "reconfigure"):
            raise AssertionError("cleanup released the lock too early")
    assert exc_info.value.holder_operation == "run-service-startup"

    allow_cleanup.set()
    runner.join(timeout=10.0)
    assert not runner.is_alive()
    assert len(runner_error) == 1
    assert isinstance(runner_error[0], GatewayConfigError)
    assert "injected startup failure" in str(runner_error[0])
    with service._gateway_lifecycle_lock(root, "reconfigure"):
        pass


def test_litellm_readiness_allows_cold_start_beyond_thirty_seconds(
    monkeypatch,
) -> None:
    now = [0.0]

    class Process:
        @staticmethod
        def poll():
            return None

    class Front:
        calls = 0

        def internal_liveliness(self) -> bool:
            self.calls += 1
            return self.calls == 3

    front = Front()
    monkeypatch.setattr(service.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(service.time, "sleep", lambda _seconds: now.__setitem__(0, now[0] + 31))

    service._wait_liveliness(front, Process())

    assert service.LITELLM_READINESS_TIMEOUT_SECONDS == 120.0
    assert front.calls == 3
    assert now[0] == 62.0


def test_litellm_readiness_fails_immediately_when_process_exits() -> None:
    class Process:
        @staticmethod
        def poll():
            return 1

    class Front:
        @staticmethod
        def internal_liveliness() -> bool:
            raise AssertionError("liveliness probe ran after process exit")

    with pytest.raises(GatewayConfigError, match="exited before readiness"):
        service._wait_liveliness(Front(), Process())


def test_operator_stop_uses_gateway_kill_switch_before_bounded_task_end(tmp_path) -> None:
    commands = FakeCommands()
    identity = expected_task_identity(tmp_path)
    commands.tasks[identity.task_name] = render_task_xml(identity)
    result = stop_task(tmp_path, commands=commands, timeout_seconds=0)
    assert result == {"stopped": True, "forced": True, "task_present": True}
    assert kill_switch_path(tmp_path).read_text(encoding="ascii") == "operator-stop\n"
    assert commands.stopped == [identity.task_name]


def test_task_action_and_status_artifacts_contain_no_secret_values(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OVH_KEY", "provider-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
    identity = expected_task_identity(tmp_path)
    text = render_task_xml(identity)
    assert "provider-key" not in text
    assert "anthropic-key" not in text
    assert os.environ["OVH_KEY"] not in identity.arguments


def test_public_front_attestation_uses_only_a_nonsecret_negative_auth_probe(
    monkeypatch,
) -> None:
    captured: dict = {}

    class Response:
        status = 401

        @staticmethod
        def read(limit: int) -> bytes:
            assert limit == 4097
            return b'{"type":"error","error":{"type":"ATGW_CONFIG_ERROR","message":"ATGW_CONFIG_ERROR"}}'

    class Connection:
        def request(self, method, path, *, body, headers) -> None:
            captured.update(method=method, path=path, body=body, headers=headers)

        @staticmethod
        def getresponse():
            return Response()

        @staticmethod
        def close() -> None:
            return

    monkeypatch.setattr(
        service.http.client,
        "HTTPConnection",
        lambda host, port, *, timeout: (
            captured.update(host=host, port=port, timeout=timeout) or Connection()
        ),
    )

    assert service._public_front_attested() is True
    assert captured["headers"]["Authorization"] == (
        "Bearer atgw-status-probe-not-a-secret"
    )
    assert "front" not in captured["headers"]["Authorization"]


def test_status_requires_attested_runtime_front_and_internal_liveliness(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("OVH_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    root = tmp_path / "project"
    ledger = SpendLedger(tmp_path / "spend" / "ledger.sqlite3", tmp_path / "spend" / "install.json")
    executable = tmp_path / "litellm.exe"
    executable.write_bytes(b"fake")
    front_token = tmp_path / "secrets" / "front.txt"
    internal_token = tmp_path / "secrets" / "internal.txt"
    initialize_install(
        root,
        litellm_executable=executable,
        opening_micro_eur=580_000,
        opening_evidence="test dashboard, observed 2026-07-16",
        ledger=ledger,
        front_token_path=front_token,
        internal_token_path=internal_token,
    )
    commands = FakeCommands()
    install_task(root, commands=commands)
    manifest = service.load_install_manifest(root)
    start = service._process_start_token(os.getpid())
    runtime = {
        "schema_version": service.RUNTIME_SCHEMA_VERSION,
        "runner_pid": os.getpid(),
        "runner_start": start,
        "litellm_pid": os.getpid(),
        "litellm_start": start,
        "task_name": project_task_name(root),
        "price_policy_hash": price_policy_hash(),
        "child_cap_policy_hash": child_cap_policy_hash(),
        "config_sha256": manifest["litellm_config_sha256"],
        "public_bind": "127.0.0.1:4000",
        "internal_bind": "127.0.0.1:4001",
        "front_token_sha256": hashlib.sha256(
            front_token.read_text(encoding="utf-8").strip().encode("utf-8")
        ).hexdigest(),
    }
    service._durable_write_json(runtime_marker_path(root), runtime)
    monkeypatch.setattr(
        service,
        "exclusive_bind_probe",
        lambda _host, _port: (_ for _ in ()).throw(GatewayConfigError("occupied")),
    )
    monkeypatch.setattr(service.GatewayFront, "internal_liveliness", lambda _self: True)
    monkeypatch.setattr(service, "_public_front_attested", lambda: True)

    status = gateway_status(
        root,
        commands=commands,
        ledger=ledger,
        front_token_path=front_token,
        internal_token_path=internal_token,
    )

    assert status["ready"] is True
    assert status["operational_ready"] is True
    assert status["worker_spend_ready"] is False
    assert status["worker_spend_errors"] == ["dashboard_canary_absent"]
    assert status["ledger"]["opening_micro_eur"] == 580_000
    assert status["ledger"]["current_committed_micro_eur"] == 580_000
    assert status["ledger"]["opening_evidence"] == (
        "test dashboard, observed 2026-07-16"
    )
    encoded = str(status)
    assert "atgw-" not in encoded
    assert "provider-key" not in encoded

    ledger.reserve("1" * 32)
    ledger.settle(
        "1" * 32,
        model=MODEL_ALIAS,
        input_tokens=1_000,
        output_tokens=100,
    )
    ledger.verify_dashboard_canary("1" * 32, observed_delta_micro_eur=960)
    accepted = gateway_status(
        root,
        commands=commands,
        ledger=ledger,
        front_token_path=front_token,
        internal_token_path=internal_token,
    )
    assert accepted["operational_ready"] is True
    assert accepted["worker_spend_ready"] is True
    assert accepted["worker_spend_errors"] == []

    replacement_token = service.generate_token()
    front_token.write_text(replacement_token + "\n", encoding="utf-8")
    replaced = gateway_status(
        root,
        commands=commands,
        ledger=ledger,
        front_token_path=front_token,
        internal_token_path=internal_token,
    )
    assert replaced["ready"] is False
    assert "child_cap_issuer_mismatch" in replaced["errors"]
    assert "runtime_marker_invalid" in replaced["errors"]

    runtime["front_token_sha256"] = hashlib.sha256(
        replacement_token.encode("utf-8")
    ).hexdigest()
    service._durable_write_json(runtime_marker_path(root), runtime)
    clean_restart = gateway_status(
        root,
        commands=commands,
        ledger=ledger,
        front_token_path=front_token,
        internal_token_path=internal_token,
    )
    assert clean_restart["runtime_marker_present"] is True
    assert clean_restart["ready"] is False
    assert clean_restart["worker_spend_ready"] is False
    assert "child_cap_issuer_mismatch" in clean_restart["errors"]
    assert "child_cap_issuer_mismatch" in clean_restart["worker_spend_errors"]


def test_runner_refuses_replaced_front_token_before_child_spawn(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "project"
    ledger = SpendLedger(
        tmp_path / "spend" / "ledger.sqlite3",
        tmp_path / "spend" / "install.json",
    )
    executable = tmp_path / "litellm.exe"
    executable.write_bytes(b"fake")
    key = tmp_path / "secrets" / "provider.txt"
    front_token = tmp_path / "secrets" / "front.txt"
    internal_token = tmp_path / "secrets" / "internal.txt"
    initialize_install(
        root,
        litellm_executable=executable,
        opening_micro_eur=0,
        opening_evidence="test dashboard, observed 2026-07-16",
        ledger=ledger,
        front_token_path=front_token,
        internal_token_path=internal_token,
    )
    key.write_text("provider-secret\n", encoding="utf-8")
    front_token.write_text(service.generate_token() + "\n", encoding="utf-8")
    spawned = False

    def fake_popen(*_args, **_kwargs):
        nonlocal spawned
        spawned = True
        raise AssertionError("provider child must not spawn")

    monkeypatch.setattr(service, "exclusive_bind_probe", lambda _host, _port: None)

    with pytest.raises(LedgerBlocked, match="front token"):
        run_service(
            root,
            ledger=ledger,
            key_path=key,
            front_token_path=front_token,
            internal_token_path=internal_token,
            popen=fake_popen,
        )

    assert spawned is False


def test_start_waits_for_attested_readiness(tmp_path, monkeypatch) -> None:
    root = tmp_path / "project"
    ledger = SpendLedger(tmp_path / "spend" / "ledger.sqlite3", tmp_path / "spend" / "install.json")
    executable = tmp_path / "litellm.exe"
    executable.write_bytes(b"fake")
    initialize_install(
        root,
        litellm_executable=executable,
        opening_micro_eur=0,
        opening_evidence="test dashboard, observed 2026-07-16",
        ledger=ledger,
        front_token_path=tmp_path / "secrets" / "front.txt",
        internal_token_path=tmp_path / "secrets" / "internal.txt",
    )
    ledger.place_hold(reason="start watched service before enabling paid proof")
    commands = FakeCommands()
    install_task(root, commands=commands)
    monkeypatch.setattr(service, "exclusive_bind_probe", lambda _host, _port: None)
    statuses = iter([
        {"ready": False, "errors": ["runtime_marker_missing"]},
        {"ready": True, "errors": []},
    ])
    monkeypatch.setattr(service, "gateway_status", lambda *_args, **_kwargs: next(statuses))

    result = start_task(
        root,
        commands=commands,
        ledger=ledger,
        readiness_timeout_seconds=1,
    )

    assert result["ready"] is True
    assert commands.started == [project_task_name(root)]
    held = ledger.status()
    assert held["service_hold"] == "manual: start watched service before enabling paid proof"
    assert held["ready"] is False


def test_start_refuses_child_cap_issuer_mismatch_before_task_start(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "project"
    ledger = SpendLedger(
        tmp_path / "spend" / "ledger.sqlite3",
        tmp_path / "spend" / "install.json",
    )
    executable = tmp_path / "litellm.exe"
    executable.write_bytes(b"fake")
    initialize_install(
        root,
        litellm_executable=executable,
        opening_micro_eur=0,
        opening_evidence="test dashboard, observed 2026-07-16",
        ledger=ledger,
        front_token_path=tmp_path / "secrets" / "front.txt",
        internal_token_path=tmp_path / "secrets" / "internal.txt",
    )
    commands = FakeCommands()
    install_task(root, commands=commands)
    monkeypatch.setattr(
        service,
        "gateway_status",
        lambda *_args, **_kwargs: {
            "ready": False,
            "errors": ["child_cap_issuer_mismatch"],
        },
    )

    with pytest.raises(LedgerBlocked, match="front token"):
        start_task(root, commands=commands, ledger=ledger)

    assert commands.started == []


def test_start_is_idempotent_when_attested_service_is_already_ready(
    tmp_path,
    monkeypatch,
) -> None:
    root = tmp_path / "project"
    ledger = SpendLedger(tmp_path / "spend" / "ledger.sqlite3", tmp_path / "spend" / "install.json")
    executable = tmp_path / "litellm.exe"
    executable.write_bytes(b"fake")
    initialize_install(
        root,
        litellm_executable=executable,
        opening_micro_eur=0,
        opening_evidence="test dashboard, observed 2026-07-16",
        ledger=ledger,
        front_token_path=tmp_path / "secrets" / "front.txt",
        internal_token_path=tmp_path / "secrets" / "internal.txt",
    )
    commands = FakeCommands()
    install_task(root, commands=commands)
    monkeypatch.setattr(
        service,
        "gateway_status",
        lambda *_args, **_kwargs: {"ready": True, "errors": []},
    )

    result = start_task(root, commands=commands, ledger=ledger)

    assert result == {
        "started": False,
        "ready": True,
        "task_name": project_task_name(root),
    }
    assert commands.started == []


def test_forced_stop_removes_only_stale_marker_after_both_sockets_are_free(tmp_path) -> None:
    commands = FakeCommands()
    identity = expected_task_identity(tmp_path)
    commands.tasks[identity.task_name] = render_task_xml(identity)
    runtime_marker_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    runtime_marker_path(tmp_path).write_text("stale", encoding="utf-8")

    result = stop_task(tmp_path, commands=commands, timeout_seconds=0)

    assert result["stopped"] is True
    assert result["forced"] is True
    assert not runtime_marker_path(tmp_path).exists()


def test_stop_refuses_unknown_listener_when_task_is_absent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service, "_both_sockets_free", lambda: False)

    with pytest.raises(GatewayConfigError, match="task is absent"):
        stop_task(tmp_path, commands=FakeCommands(), timeout_seconds=0)


def _install_for_reconfigure(tmp_path, ledger):
    root = tmp_path / "project"
    executable = tmp_path / "litellm.exe"
    executable.write_bytes(b"fake")
    front_token = tmp_path / "secrets" / "front.txt"
    internal_token = tmp_path / "secrets" / "internal.txt"
    initialize_install(
        root,
        litellm_executable=executable,
        opening_micro_eur=580_000,
        opening_evidence="test dashboard, observed 2026-07-16",
        ledger=ledger,
        front_token_path=front_token,
        internal_token_path=internal_token,
    )
    return root, front_token, internal_token


def _file_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _install_for_runtime_rebind(tmp_path):
    root = tmp_path / "project"
    old_runtime = tmp_path / "old-runtime" / "litellm.exe"
    old_runtime.parent.mkdir(parents=True)
    old_runtime.write_bytes(b"old runtime")
    install_json = tmp_path / "spend" / "install.json"
    ledger = SpendLedger(tmp_path / "spend" / "ledger.sqlite3", install_json)
    front_token = tmp_path / "secrets" / "front.txt"
    internal_token = tmp_path / "secrets" / "internal.txt"
    initialize_install(
        root,
        litellm_executable=old_runtime,
        opening_micro_eur=580_000,
        opening_evidence="test dashboard, observed 2026-07-16",
        ledger=ledger,
        front_token_path=front_token,
        internal_token_path=internal_token,
    )
    return root, old_runtime, ledger, front_token, internal_token


def test_runtime_rebind_changes_only_manifest_executable_and_preserves_install_authorities(
    tmp_path,
    monkeypatch,
) -> None:
    root, old_runtime, _, _, _ = _install_for_runtime_rebind(tmp_path)
    candidate = tmp_path / "unusual runtime" / "launcher.shim"
    candidate.parent.mkdir()
    candidate.write_bytes(b"capable shim")
    execute = tmp_path / "agenttalk-python.exe"
    execute.write_bytes(b"python")
    commands = FakeCommands()
    install_task(root, commands=commands, execute=execute, principal="D\\u")
    identity = expected_task_identity(root, execute=execute, principal="D\\u")
    registered_before = commands.tasks[identity.task_name]
    before_files = _file_snapshot(tmp_path)
    manifest_path = service.install_manifest_path(root)
    manifest_before = json.loads(manifest_path.read_text(encoding="utf-8"))
    probe = FakeRuntimeProbeCommands()
    monkeypatch.setattr(service, "_both_sockets_free", lambda: True)

    result = service.rebind_runtime(
        root,
        litellm_executable=candidate,
        probe_commands=probe,
    )

    manifest_after = json.loads(manifest_path.read_text(encoding="utf-8"))
    changed_keys = {
        key
        for key in manifest_before.keys() | manifest_after.keys()
        if manifest_before.get(key) != manifest_after.get(key)
    }
    manifest_relative = manifest_path.relative_to(tmp_path).as_posix()
    after_files = _file_snapshot(tmp_path)
    assert changed_keys == {"litellm_executable"}
    assert {
        key: value for key, value in after_files.items() if key != manifest_relative
    } == {
        key: value for key, value in before_files.items() if key != manifest_relative
    }
    assert commands.tasks[identity.task_name] == registered_before
    assert manifest_after["litellm_executable"] == str(candidate.resolve())
    assert manifest_after["litellm_config_sha256"] == hashlib.sha256(
        litellm_config_path(root).read_bytes()
    ).hexdigest()
    assert manifest_after["price_policy_hash"] == price_policy_hash()
    assert service.load_install_manifest(root) == manifest_after
    assert result == {
        "runtime_rebound": True,
        "changed": True,
        "litellm_executable": str(candidate.resolve()),
        "previous_litellm_executable": str(old_runtime.resolve()),
        "config_sha256": manifest_after["litellm_config_sha256"],
        "price_policy_hash": manifest_after["price_policy_hash"],
    }
    assert probe.calls[0]["argv"] == [str(candidate.resolve()), "--version"]
    assert probe.calls[0]["cwd"] == str(root.resolve())
    probe_env = probe.calls[0]["env"]
    assert probe_env["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert "OVH_KEY" not in probe_env
    assert "LITELLM_MASTER_KEY" not in probe_env


def test_runtime_rebind_repairs_missing_outgoing_runtime_without_probing_it(
    tmp_path,
    monkeypatch,
) -> None:
    root, old_runtime, _, _, _ = _install_for_runtime_rebind(tmp_path)
    old_runtime.unlink()
    candidate = tmp_path / "new-runtime.exe"
    candidate.write_bytes(b"working")
    probe = FakeRuntimeProbeCommands()
    monkeypatch.setattr(service, "_both_sockets_free", lambda: True)

    result = service.rebind_runtime(
        root,
        litellm_executable=candidate,
        probe_commands=probe,
    )

    assert result["runtime_rebound"] is True
    assert probe.calls[0]["argv"] == [str(candidate.resolve()), "--version"]


def test_runtime_rebind_probes_only_candidate_not_unusable_outgoing_runtime(
    tmp_path,
    monkeypatch,
) -> None:
    root, old_runtime, _, _, _ = _install_for_runtime_rebind(tmp_path)
    candidate = tmp_path / "working-runtime.exe"
    candidate.write_bytes(b"working")
    probe = FakeRuntimeProbeCommands()
    monkeypatch.setattr(service, "_both_sockets_free", lambda: True)

    service.rebind_runtime(
        root,
        litellm_executable=candidate,
        probe_commands=probe,
    )

    assert old_runtime.is_file()
    assert [call["argv"] for call in probe.calls] == [
        [str(candidate.resolve()), "--version"]
    ]


def test_runtime_rebind_refuses_running_gateway_before_candidate_probe(
    tmp_path,
    monkeypatch,
) -> None:
    root, _, _, _, _ = _install_for_runtime_rebind(tmp_path)
    candidate = tmp_path / "missing-runtime.exe"
    probe = FakeRuntimeProbeCommands(error=AssertionError("probe must not run"))
    monkeypatch.setattr(service, "_both_sockets_free", lambda: False)
    before = _file_snapshot(tmp_path)

    with pytest.raises(GatewayConfigError, match="while the gateway is running; stop it first"):
        service.rebind_runtime(
            root,
            litellm_executable=candidate,
            probe_commands=probe,
        )

    assert probe.calls == []
    assert _file_snapshot(tmp_path) == before


def test_runtime_rebind_requires_existing_install_before_candidate_probe(
    tmp_path,
) -> None:
    probe = FakeRuntimeProbeCommands(error=AssertionError("probe must not run"))

    with pytest.raises(GatewayConfigError, match="requires an existing install"):
        service.rebind_runtime(
            tmp_path / "project",
            litellm_executable=tmp_path / "runtime.exe",
            probe_commands=probe,
        )

    assert probe.calls == []


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("missing", "litellm_runtime_missing"),
        ("nonzero", "litellm_runtime_probe_failed"),
        ("oserror", "litellm_runtime_probe_failed"),
        ("timeout", "litellm_runtime_probe_unknown"),
    ],
)
def test_runtime_rebind_refuses_candidate_without_mutating_install(
    tmp_path,
    monkeypatch,
    case,
    expected_reason,
) -> None:
    root, _, _, _, _ = _install_for_runtime_rebind(tmp_path)
    candidate = tmp_path / "candidate-runtime.exe"
    if case != "missing":
        candidate.write_bytes(b"candidate")
    error = None
    returncode = 0
    if case == "nonzero":
        returncode = 1
    elif case == "oserror":
        error = OSError("invalid executable")
    elif case == "timeout":
        error = subprocess.TimeoutExpired([str(candidate), "--version"], 30)
    probe = FakeRuntimeProbeCommands(returncode=returncode, error=error)
    monkeypatch.setattr(service, "_both_sockets_free", lambda: True)
    before = _file_snapshot(tmp_path)

    with pytest.raises(GatewayConfigError) as exc_info:
        service.rebind_runtime(
            root,
            litellm_executable=candidate,
            probe_commands=probe,
        )

    message = str(exc_info.value)
    outcome_reasons = {
        "litellm_runtime_missing",
        "litellm_runtime_probe_failed",
        "litellm_runtime_probe_unknown",
    }
    assert {reason for reason in outcome_reasons if reason in message} == {
        expected_reason
    }
    assert (
        f'agenttalk --root "{root.resolve()}" gateway runtime-rebind '
        "--litellm-executable"
    ) in message
    if case == "missing":
        assert probe.calls == []
    else:
        assert [call["argv"] for call in probe.calls] == [
            [str(candidate.resolve()), "--version"]
        ]
    if case == "timeout":
        assert "retry" in message
        assert "probe_failed" not in message
        assert type(exc_info.value) is not GatewayConfigError
        assert exc_info.value.retryable is True
        assert exc_info.value.reason_code == "litellm_runtime_probe_unknown"
    elif case in {"nonzero", "oserror"}:
        assert type(exc_info.value) is not GatewayConfigError
        assert exc_info.value.retryable is False
        assert exc_info.value.reason_code == "litellm_runtime_probe_failed"
    assert _file_snapshot(tmp_path) == before


@pytest.mark.parametrize("failure_at", ["construction", "start"])
def test_runtime_rebind_reader_thread_failure_is_retryable_unknown(
    tmp_path,
    monkeypatch,
    failure_at,
) -> None:
    root, _, _, _, _ = _install_for_runtime_rebind(tmp_path)
    candidate = tmp_path / "candidate-runtime"
    candidate.write_bytes(b"candidate")
    candidate_started = tmp_path / "candidate.started"
    real_popen = subprocess.Popen
    spawned: list[subprocess.Popen] = []
    reader_calls: list[str] = []

    def capturing_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        spawned.append(process)
        return process

    class StartFailureThread:
        ident = None

        def start(self) -> None:
            reader_calls.append("start")
            raise RuntimeError("reader thread could not start")

        def join(self, timeout=None) -> None:
            _ = timeout
            reader_calls.append("join")
            raise AssertionError("a never-started reader must not be joined")

        def is_alive(self) -> bool:
            reader_calls.append("is_alive")
            raise AssertionError("a never-started reader has no liveness state")

    def failing_thread(*_args, **_kwargs):
        if failure_at == "construction":
            raise RuntimeError("reader thread could not be constructed")
        return StartFailureThread()

    monkeypatch.setattr(service.threading, "Thread", failing_thread)
    monkeypatch.setattr(service.subprocess, "Popen", capturing_popen)
    monkeypatch.setattr(
        service,
        "_RUNTIME_PROBE_BOOTSTRAP",
        (
            "import sys\n"
            "from pathlib import Path\n"
            "if sys.stdin.buffer.read(1) == b'1':\n"
            f"    Path({str(candidate_started)!r}).write_text('started', encoding='ascii')\n"
        ),
    )
    monkeypatch.setattr(service, "_both_sockets_free", lambda: True)
    before = _file_snapshot(tmp_path)

    with pytest.raises(service.LiteLLMRuntimeProbeUnknown) as exc_info:
        service.rebind_runtime(root, litellm_executable=candidate)

    assert exc_info.value.reason_code == "litellm_runtime_probe_unknown"
    assert exc_info.value.retryable is True
    expected_detail = (
        "reader thread could not be constructed"
        if failure_at == "construction"
        else "reader thread could not start"
    )
    assert expected_detail in str(exc_info.value.__cause__)
    assert "never-started reader" not in str(exc_info.value.__cause__)
    assert reader_calls == ([] if failure_at == "construction" else ["start"])
    assert len(spawned) == 1
    assert spawned[0].poll() is not None
    assert not candidate_started.exists()
    assert _file_snapshot(tmp_path) == before


@pytest.mark.parametrize("corruption", ["config_hash", "price_policy"])
def test_runtime_rebind_refuses_invalid_bound_manifest_before_probe(
    tmp_path,
    monkeypatch,
    corruption,
) -> None:
    root, _, _, _, _ = _install_for_runtime_rebind(tmp_path)
    candidate = tmp_path / "candidate-runtime.exe"
    candidate.write_bytes(b"candidate")
    manifest_path = service.install_manifest_path(root)
    if corruption == "config_hash":
        litellm_config_path(root).write_bytes(b"changed outside the manifest")
        expected = "LiteLLM config hash mismatch"
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["price_policy_hash"] = "stale"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        expected = "price policy mismatch"
    probe = FakeRuntimeProbeCommands(error=AssertionError("probe must not run"))
    monkeypatch.setattr(service, "_both_sockets_free", lambda: True)
    before = _file_snapshot(tmp_path)

    with pytest.raises(GatewayConfigError, match=expected):
        service.rebind_runtime(
            root,
            litellm_executable=candidate,
            probe_commands=probe,
        )

    assert probe.calls == []
    assert _file_snapshot(tmp_path) == before


def test_runtime_rebind_same_path_reprobes_and_is_byte_idempotent(
    tmp_path,
    monkeypatch,
) -> None:
    root, old_runtime, _, _, _ = _install_for_runtime_rebind(tmp_path)
    probe = FakeRuntimeProbeCommands()
    monkeypatch.setattr(service, "_both_sockets_free", lambda: True)
    before = _file_snapshot(tmp_path)

    result = service.rebind_runtime(
        root,
        litellm_executable=old_runtime,
        probe_commands=probe,
    )

    assert result["changed"] is False
    assert probe.calls[0]["argv"] == [str(old_runtime.resolve()), "--version"]
    assert _file_snapshot(tmp_path) == before


def test_runtime_rebind_accepts_valid_identity_without_path_shape_rules(
    tmp_path,
    monkeypatch,
) -> None:
    root, _, _, _, _ = _install_for_runtime_rebind(tmp_path)
    candidate = tmp_path / "odd path" / "launcher.with-unfamiliar-suffix"
    candidate.parent.mkdir()
    candidate.write_bytes(b"shim")
    probe = FakeRuntimeProbeCommands(returncode=0)
    monkeypatch.setattr(service, "_both_sockets_free", lambda: True)

    result = service.rebind_runtime(
        root,
        litellm_executable=candidate,
        probe_commands=probe,
    )

    assert result["litellm_executable"] == str(candidate.resolve())


def test_runtime_rebind_accepts_symlinked_launcher_when_host_supports_it(
    tmp_path,
    monkeypatch,
) -> None:
    root, _, _, _, _ = _install_for_runtime_rebind(tmp_path)
    target = tmp_path / "runtime-target.exe"
    target.write_bytes(b"working")
    candidate = tmp_path / "runtime-link.exe"
    try:
        candidate.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"host cannot create a test symlink: {exc}")
    probe = FakeRuntimeProbeCommands()
    monkeypatch.setattr(service, "_both_sockets_free", lambda: True)

    result = service.rebind_runtime(
        root,
        litellm_executable=candidate,
        probe_commands=probe,
    )

    assert result["litellm_executable"] == str(target.resolve())
    assert probe.calls[0]["argv"] == [str(target.resolve()), "--version"]


def test_runtime_rebind_keeps_registered_task_bytes_but_run_uses_new_manifest_runtime(
    tmp_path,
    monkeypatch,
) -> None:
    root, old_runtime, ledger, front_token, internal_token = _install_for_runtime_rebind(
        tmp_path
    )
    candidate = tmp_path / "new-runtime.exe"
    candidate.write_bytes(b"working")
    execute = tmp_path / "agenttalk-python.exe"
    execute.write_bytes(b"python")
    commands = FakeCommands()
    identity = expected_task_identity(root, execute=execute, principal="D\\u")
    install_task(root, commands=commands, execute=execute, principal="D\\u")
    registered_before = commands.tasks[identity.task_name]
    task_xml = service.gateway_state_dir(root) / "task.xml"
    task_xml_before = task_xml.read_bytes()
    task_identity_before = task_identity_path(root).read_bytes()
    monkeypatch.setattr(service, "_both_sockets_free", lambda: True)
    service.rebind_runtime(
        root,
        litellm_executable=candidate,
        probe_commands=FakeRuntimeProbeCommands(),
    )
    provider_key = tmp_path / "secrets" / "provider.txt"
    provider_key.write_text("provider-secret\n", encoding="utf-8")
    monkeypatch.setattr(service, "exclusive_bind_probe", lambda _host, _port: None)
    captured: dict[str, object] = {}

    class SpawnObserved(Exception):
        pass

    def capture_spawn(argv, **_kwargs):
        captured["argv"] = list(argv)
        raise SpawnObserved

    with pytest.raises(SpawnObserved):
        run_service(
            root,
            ledger=ledger,
            key_path=provider_key,
            front_token_path=front_token,
            internal_token_path=internal_token,
            child_log_path=tmp_path / "litellm.log",
            popen=capture_spawn,
        )

    assert captured["argv"][0] == str(candidate.resolve())
    assert commands.tasks[identity.task_name] == registered_before
    assert task_xml.read_bytes() == task_xml_before
    assert task_identity_path(root).read_bytes() == task_identity_before
    assert "gateway run" in registered_before
    assert str(old_runtime.resolve()) not in registered_before
    assert str(candidate.resolve()) not in registered_before
    assert commands.started == []
    assert commands.stopped == []


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher identity boundary")
def test_runtime_rebind_real_probe_rejects_zero_exit_non_litellm_executable(
    tmp_path,
    monkeypatch,
) -> None:
    root, _, _, _, _ = _install_for_runtime_rebind(tmp_path)
    candidate = Path(os.environ["COMSPEC"])
    monkeypatch.setattr(service, "_both_sockets_free", lambda: True)
    before = _file_snapshot(tmp_path)

    with pytest.raises(GatewayConfigError, match="litellm_runtime_probe_failed"):
        service.rebind_runtime(root, litellm_executable=candidate)

    assert _file_snapshot(tmp_path) == before


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher identity boundary")
@pytest.mark.parametrize("suffix", [".cmd", ".bat"])
def test_runtime_rebind_real_probe_accepts_relative_forwarding_shim_with_odd_path(
    tmp_path,
    monkeypatch,
    suffix,
) -> None:
    root, _, _, _, _ = _install_for_runtime_rebind(tmp_path)
    candidate = _write_windows_litellm_identity_shim(
        tmp_path / "runtime path & Unicode Ω",
        suffix=suffix,
    )
    monkeypatch.chdir(tmp_path)
    relative_candidate = candidate.relative_to(tmp_path)
    monkeypatch.setattr(service, "_both_sockets_free", lambda: True)

    result = service.rebind_runtime(
        root,
        litellm_executable=relative_candidate,
    )

    assert result["litellm_executable"] == str(candidate.resolve())


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree boundary")
def test_runtime_rebind_real_probe_timeout_tears_down_candidate_descendants(
    tmp_path,
    monkeypatch,
) -> None:
    root, _, _, _, _ = _install_for_runtime_rebind(tmp_path)
    runtime_dir = tmp_path / "timeout runtime"
    runtime_dir.mkdir()
    ready = runtime_dir / "child.ready"
    release = runtime_dir / "child.release"
    sentinel = runtime_dir / "child.survived"
    child = runtime_dir / "child.py"
    child.write_text(
        """import os
import sys
import time
from pathlib import Path

ready, release, sentinel = map(Path, sys.argv[1:])
ready.write_text(str(os.getpid()), encoding=\"ascii\")
while not release.exists():
    time.sleep(0.02)
sentinel.write_text(\"survived\", encoding=\"ascii\")
""",
        encoding="utf-8",
    )
    candidate = runtime_dir / "litellm.cmd"
    candidate.write_text(
        (
            "@echo off\r\n"
            ">nul ping -n 2 -w 1000 127.0.0.1\r\n"
            f'start "" /b "{sys.executable}" "{child}" "{ready}" "{release}" "{sentinel}"\r\n'
            ":wait_ready\r\n"
            f'if not exist "{ready}" (\r\n'
            "  >nul ping -n 2 -w 100 127.0.0.1\r\n"
            "  goto wait_ready\r\n"
            ")\r\n"
            ":hang\r\n"
            ">nul ping -n 2 -w 1000 127.0.0.1\r\n"
            "goto hang\r\n"
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(service, "LITELLM_RUNTIME_PROBE_TIMEOUT_SECONDS", 3.0)
    monkeypatch.setattr(service, "_both_sockets_free", lambda: True)
    manifest_before = service.install_manifest_path(root).read_bytes()

    with pytest.raises(GatewayConfigError, match="litellm_runtime_probe_unknown"):
        service.rebind_runtime(root, litellm_executable=candidate)

    assert ready.is_file(), "fixture never proved that the descendant started"
    release.write_text("release", encoding="ascii")
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not sentinel.exists():
        time.sleep(0.02)
    assert not sentinel.exists()
    assert service.install_manifest_path(root).read_bytes() == manifest_before


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher identity boundary")
def test_runtime_rebind_real_probe_rejects_oversized_untrusted_output(
    tmp_path,
    monkeypatch,
) -> None:
    root, _, _, _, _ = _install_for_runtime_rebind(tmp_path)
    runtime_dir = tmp_path / "noisy runtime"
    runtime_dir.mkdir()
    implementation = runtime_dir / "noisy.py"
    implementation.write_text(
        "print('LiteLLM: Current Version = 1.91.3')\nprint('x' * 4096)\n",
        encoding="utf-8",
    )
    candidate = runtime_dir / "litellm.cmd"
    _write_windows_batch_forwarder(candidate, implementation)
    monkeypatch.setattr(service, "_both_sockets_free", lambda: True)

    with pytest.raises(GatewayConfigError, match="litellm_runtime_probe_failed"):
        service.rebind_runtime(root, litellm_executable=candidate)


def test_runtime_rebind_compare_and_swap_refuses_candidate_manifest_mutation(
    tmp_path,
    monkeypatch,
) -> None:
    root, _, _, _, _ = _install_for_runtime_rebind(tmp_path)
    candidate = tmp_path / "candidate-runtime.exe"
    candidate.write_bytes(b"candidate")
    manifest_path = service.install_manifest_path(root)
    mutated: dict[str, bytes] = {}

    def mutate_manifest() -> None:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        value["candidate_touch"] = True
        manifest_path.write_text(json.dumps(value), encoding="utf-8")
        mutated["bytes"] = manifest_path.read_bytes()

    monkeypatch.setattr(service, "_both_sockets_free", lambda: True)

    with pytest.raises(GatewayConfigError, match="gateway_runtime_manifest_changed"):
        service.rebind_runtime(
            root,
            litellm_executable=candidate,
            probe_commands=FakeRuntimeProbeCommands(side_effect=mutate_manifest),
        )

    assert manifest_path.read_bytes() == mutated["bytes"]


@pytest.mark.skipif(os.name != "nt", reason="Windows subprocess race boundary")
def test_runtime_rebind_serializes_supported_reconfigure_before_manifest_write(
    tmp_path,
    monkeypatch,
) -> None:
    root, _, _, _, _ = _install_for_runtime_rebind(tmp_path)
    candidate = _write_windows_litellm_identity_shim(tmp_path / "race runtime")
    manifest_path = service.install_manifest_path(root)
    before_candidate_write = threading.Event()
    release_candidate_write = threading.Event()
    reconfigure_done = threading.Event()
    errors: dict[str, BaseException] = {}
    real_write_json = service._durable_write_json

    def blocking_write(path, value) -> None:
        if (
            Path(path) == manifest_path
            and isinstance(value, dict)
            and value.get("litellm_executable") == str(candidate.resolve())
        ):
            before_candidate_write.set()
            if not release_candidate_write.wait(timeout=10.0):
                raise AssertionError("test did not release the rebind manifest write")
        real_write_json(path, value)

    def do_rebind() -> None:
        try:
            service.rebind_runtime(root, litellm_executable=candidate)
        except BaseException as exc:  # noqa: BLE001 - asserted across the thread boundary
            errors["rebind"] = exc

    def do_reconfigure() -> None:
        try:
            service.reconfigure_endpoint(root)
        except BaseException as exc:  # noqa: BLE001 - asserted across the thread boundary
            errors["reconfigure"] = exc
        finally:
            reconfigure_done.set()

    monkeypatch.setattr(service, "_both_sockets_free", lambda: True)
    monkeypatch.setattr(service, "_durable_write_json", blocking_write)
    monkeypatch.setattr(service, "DEFAULT_API_BASE", "https://fresh.example/v1")
    rebind_thread = threading.Thread(target=do_rebind, daemon=True)
    rebind_thread.start()
    assert before_candidate_write.wait(timeout=10.0)
    reconfigure_thread = threading.Thread(target=do_reconfigure, daemon=True)
    reconfigure_thread.start()
    interleaved = reconfigure_done.wait(timeout=1.0)
    release_candidate_write.set()
    rebind_thread.join(timeout=10.0)
    reconfigure_thread.join(timeout=10.0)

    assert not rebind_thread.is_alive()
    assert not reconfigure_thread.is_alive()
    assert interleaved is False
    assert errors == {}
    manifest = service.load_install_manifest(root)
    assert manifest["litellm_executable"] == str(candidate.resolve())
    assert manifest["litellm_config_sha256"] == hashlib.sha256(
        litellm_config_path(root).read_bytes()
    ).hexdigest()


def test_runtime_rebind_waits_for_service_startup_then_refuses_owned_sockets(
    tmp_path,
    monkeypatch,
) -> None:
    root, old_runtime, ledger, front_token, internal_token = _install_for_runtime_rebind(
        tmp_path
    )
    candidate = tmp_path / "candidate-runtime.exe"
    candidate.write_bytes(b"candidate")
    provider_key = tmp_path / "secrets" / "provider.txt"
    provider_key.write_text("provider-secret\n", encoding="utf-8")
    startup_spawned = threading.Event()
    allow_bind = threading.Event()
    sockets_owned = threading.Event()
    serving = threading.Event()
    stop_serving = threading.Event()
    rebind_checked_sockets = threading.Event()
    results: dict[str, object] = {}
    errors: dict[str, BaseException] = {}

    class FakeProcess:
        pid = os.getpid()

        def __init__(self) -> None:
            self.stdout = io.StringIO("")
            self.alive = True

        def poll(self):
            return None if self.alive else 0

        def terminate(self) -> None:
            self.alive = False

        def kill(self) -> None:
            self.alive = False

        def wait(self, timeout=None):
            self.alive = False
            return 0

    class FakeServer:
        def serve_forever(self, poll_interval=None) -> None:
            serving.set()
            if not stop_serving.wait(timeout=10.0):
                raise AssertionError("test did not stop the fake server")

        def shutdown(self) -> None:
            stop_serving.set()

        def server_close(self) -> None:
            return None

    class FakeFront:
        def __init__(self, _config, _ledger) -> None:
            pass

        def make_server(self):
            sockets_owned.set()
            return FakeServer()

        def drain(self, _timeout) -> None:
            return None

    def fake_popen(_argv, **_kwargs):
        startup_spawned.set()
        return FakeProcess()

    def wait_until_allowed(_front, _process) -> None:
        if not allow_bind.wait(timeout=10.0):
            raise AssertionError("test did not allow the service to bind")

    def observed_sockets_free() -> bool:
        rebind_checked_sockets.set()
        return not sockets_owned.is_set()

    def do_run() -> None:
        try:
            results["run"] = run_service(
                root,
                ledger=ledger,
                key_path=provider_key,
                front_token_path=front_token,
                internal_token_path=internal_token,
                child_log_path=tmp_path / "litellm.log",
                popen=fake_popen,
            )
        except BaseException as exc:  # noqa: BLE001 - asserted across the thread boundary
            errors["run"] = exc

    def do_rebind() -> None:
        try:
            results["rebind"] = service.rebind_runtime(
                root,
                litellm_executable=candidate,
                probe_commands=FakeRuntimeProbeCommands(),
            )
        except BaseException as exc:  # noqa: BLE001 - asserted across the thread boundary
            errors["rebind"] = exc

    monkeypatch.setattr(service, "exclusive_bind_probe", lambda _host, _port: None)
    monkeypatch.setattr(service, "_wait_liveliness", wait_until_allowed)
    monkeypatch.setattr(service, "GatewayFront", FakeFront)
    monkeypatch.setattr(service, "_process_start_token", lambda _pid: "start")
    monkeypatch.setattr(service, "_both_sockets_free", observed_sockets_free)
    run_thread = threading.Thread(target=do_run, daemon=True)
    run_thread.start()
    assert startup_spawned.wait(timeout=10.0)
    rebind_thread = threading.Thread(target=do_rebind, daemon=True)
    rebind_thread.start()
    checked_before_bind = rebind_checked_sockets.wait(timeout=1.0)
    allow_bind.set()
    assert serving.wait(timeout=10.0)
    rebind_thread.join(timeout=10.0)
    stop_serving.set()
    run_thread.join(timeout=10.0)

    assert checked_before_bind is False
    assert not rebind_thread.is_alive()
    assert not run_thread.is_alive()
    assert results == {"run": 0}
    assert isinstance(errors.get("rebind"), GatewayConfigError)
    assert "while the gateway is running" in str(errors["rebind"])
    assert "run" not in errors
    assert service.load_install_manifest(root)["litellm_executable"] == str(
        old_runtime.resolve()
    )


def test_reconfigure_rebinds_config_and_manifest_and_preserves_ledger_and_tokens(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(service, "_both_sockets_free", lambda: True)
    install_json = tmp_path / "spend" / "install.json"
    ledger = SpendLedger(tmp_path / "spend" / "ledger.sqlite3", install_json)
    root, front_token, internal_token = _install_for_reconfigure(tmp_path, ledger)

    # Simulate a pre-change install whose generated config points at a stale base.
    cfg = litellm_config_path(root)
    stale = service.render_litellm_config(api_base="https://stale.example/v1").encode("utf-8")
    cfg.write_bytes(stale)
    manifest_path = service.install_manifest_path(root)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["litellm_config_sha256"] = hashlib.sha256(stale).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    ledger_before = install_json.read_bytes()
    front_before = front_token.read_bytes()
    internal_before = internal_token.read_bytes()

    result = service.reconfigure_endpoint(root)

    assert result["reconfigured"] is True
    assert result["changed"] is True
    assert result["api_base"] == service.DEFAULT_API_BASE
    expected_cfg = service.render_litellm_config(
        api_base=service.DEFAULT_API_BASE
    ).encode("utf-8")
    assert cfg.read_bytes() == expected_cfg
    assert result["config_sha256"] == hashlib.sha256(expected_cfg).hexdigest()
    reloaded = service.load_install_manifest(root)
    assert reloaded["litellm_config_sha256"] == result["config_sha256"]
    # Ledger marker + both tokens are byte-for-byte untouched.
    assert install_json.read_bytes() == ledger_before
    assert front_token.read_bytes() == front_before
    assert internal_token.read_bytes() == internal_before


def test_reconfigure_refuses_while_gateway_running(tmp_path, monkeypatch) -> None:
    ledger = SpendLedger(
        tmp_path / "spend" / "ledger.sqlite3", tmp_path / "spend" / "install.json"
    )
    root, _, _ = _install_for_reconfigure(tmp_path, ledger)
    monkeypatch.setattr(service, "_both_sockets_free", lambda: False)
    with pytest.raises(GatewayConfigError, match="while the gateway is running"):
        service.reconfigure_endpoint(root)


def test_reconfigure_requires_existing_install(tmp_path) -> None:
    with pytest.raises(GatewayConfigError, match="requires an existing install"):
        service.reconfigure_endpoint(tmp_path / "project")


def test_reconfigure_is_idempotent_on_a_fresh_install(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service, "_both_sockets_free", lambda: True)
    ledger = SpendLedger(
        tmp_path / "spend" / "ledger.sqlite3", tmp_path / "spend" / "install.json"
    )
    root, _, _ = _install_for_reconfigure(tmp_path, ledger)
    # A fresh install already renders the pinned base, so reconfigure is a no-op.
    first = service.reconfigure_endpoint(root)
    assert first["changed"] is False
    second = service.reconfigure_endpoint(root)
    assert second["changed"] is False
    assert second["config_sha256"] == first["config_sha256"]
