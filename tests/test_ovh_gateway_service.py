from __future__ import annotations

import hashlib
import os
import socket
from pathlib import Path

import pytest

from agenttalk import ovh_gateway_service as service
from agenttalk.ovh_gateway import GatewayConfigError, SpendLedger, price_policy_hash
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
        ledger=ledger,
        front_token_path=front_token,
        internal_token_path=internal_token,
    )
    assert result["price_policy_hash"] == price_policy_hash()
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


def test_runner_uses_env_only_secrets_and_pinned_single_worker_argv(
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
        ledger=ledger,
        front_token_path=front_token,
        internal_token_path=internal_token,
    )
    key.write_text("provider-secret\n", encoding="utf-8")
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = os.getpid()

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
        return FakeProcess()

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

    rc = run_service(
        root,
        ledger=ledger,
        key_path=key,
        front_token_path=front_token,
        internal_token_path=internal_token,
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
    assert not runtime_marker_path(root).exists()


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
        ledger=ledger,
        front_token_path=front_token,
        internal_token_path=internal_token,
    )
    commands = FakeCommands()
    install_task(root, commands=commands)
    manifest = service.load_install_manifest(root)
    start = service._process_start_token(os.getpid())
    service._durable_write_json(
        runtime_marker_path(root),
        {
            "schema_version": 1,
            "runner_pid": os.getpid(),
            "runner_start": start,
            "litellm_pid": os.getpid(),
            "litellm_start": start,
            "task_name": project_task_name(root),
            "price_policy_hash": price_policy_hash(),
            "config_sha256": manifest["litellm_config_sha256"],
            "public_bind": "127.0.0.1:4000",
            "internal_bind": "127.0.0.1:4001",
            "front_token_sha256": hashlib.sha256(
                front_token.read_text(encoding="utf-8").strip().encode("utf-8")
            ).hexdigest(),
        },
    )
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
    encoded = str(status)
    assert "atgw-" not in encoded
    assert "provider-key" not in encoded

    front_token.write_text("replacement-token\n", encoding="utf-8")
    replaced = gateway_status(
        root,
        commands=commands,
        ledger=ledger,
        front_token_path=front_token,
        internal_token_path=internal_token,
    )
    assert replaced["ready"] is False
    assert "runtime_marker_invalid" in replaced["errors"]


def test_start_waits_for_attested_readiness(tmp_path, monkeypatch) -> None:
    root = tmp_path / "project"
    ledger = SpendLedger(tmp_path / "spend" / "ledger.sqlite3", tmp_path / "spend" / "install.json")
    executable = tmp_path / "litellm.exe"
    executable.write_bytes(b"fake")
    initialize_install(
        root,
        litellm_executable=executable,
        ledger=ledger,
        front_token_path=tmp_path / "secrets" / "front.txt",
        internal_token_path=tmp_path / "secrets" / "internal.txt",
    )
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
