"""Windows-first managed lifecycle for the watched OVH/Qwen gateway."""

from __future__ import annotations

import contextlib
import hashlib
import html
import http.client
import json
import os
import socket
import subprocess  # nosec B404 - fixed schtasks/LiteLLM argv lists; shell is never used
import sys
import threading
import time
import xml.etree.ElementTree as ET  # nosec B405 - bounded local Task Scheduler XML
from dataclasses import asdict, dataclass
from pathlib import Path

from .ovh_gateway import (
    INTERNAL_HOST,
    INTERNAL_PORT,
    PUBLIC_HOST,
    PUBLIC_PORT,
    GatewayConfigError,
    LedgerBlocked,
    LedgerHold,
    SpendLedger,
    _durable_write_bytes,
    _durable_write_json,
    default_front_token_path,
    default_internal_token_path,
    default_key_path,
    generate_token,
    price_policy_hash,
    read_secret_file,
    render_litellm_config,
    write_secret_file,
)
from .ovh_gateway_front import CONFIG_ERROR_CODE, PUBLIC_ROUTE, FrontConfig, GatewayFront


TASK_SCHEMA_VERSION = 1
RUNTIME_SCHEMA_VERSION = 1
DEFAULT_API_BASE = "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1"
STOP_TIMEOUT_SECONDS = 30.0
MAX_TASK_RESTARTS = 3
TASK_RESTART_INTERVAL = "PT1M"
TASK_PREFIX = "agenttalk-qwen-gateway"
_TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"


@dataclass(frozen=True)
class TaskIdentity:
    schema_version: int
    task_name: str
    project_root: str
    execute: str
    arguments: str
    working_directory: str
    principal: str
    public_host: str
    public_port: int
    internal_host: str
    internal_port: int
    price_policy_hash: str


def canonical_project_root(root: str | os.PathLike[str]) -> Path:
    return Path(root).resolve()


def project_task_name(root: str | os.PathLike[str]) -> str:
    canonical = str(canonical_project_root(root)).replace("\\", "/").casefold()
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{TASK_PREFIX}-{digest}"


def gateway_state_dir(root: str | os.PathLike[str]) -> Path:
    return canonical_project_root(root) / ".agenttalk" / "gateway"


def task_identity_path(root: str | os.PathLike[str]) -> Path:
    return gateway_state_dir(root) / "task-identity.json"


def runtime_marker_path(root: str | os.PathLike[str]) -> Path:
    return gateway_state_dir(root) / "runtime.json"


def kill_switch_path(root: str | os.PathLike[str]) -> Path:
    return gateway_state_dir(root) / "gateway.kill"


def litellm_config_path(root: str | os.PathLike[str]) -> Path:
    return gateway_state_dir(root) / "litellm.yaml"


def install_manifest_path(root: str | os.PathLike[str]) -> Path:
    return gateway_state_dir(root) / "install-manifest.json"


def _task_arguments(root: Path) -> str:
    return f'-m agenttalk --root "{root}" gateway run'


def expected_task_identity(
    root: str | os.PathLike[str],
    *,
    execute: str | os.PathLike[str] = sys.executable,
    principal: str | None = None,
) -> TaskIdentity:
    project = canonical_project_root(root)
    resolved_execute = str(Path(execute).resolve())
    principal = principal or f"{os.environ.get('USERDOMAIN', '.')}\\{os.environ.get('USERNAME', '')}"
    return TaskIdentity(
        schema_version=TASK_SCHEMA_VERSION,
        task_name=project_task_name(project),
        project_root=str(project),
        execute=resolved_execute,
        arguments=_task_arguments(project),
        working_directory=str(project),
        principal=principal,
        public_host=PUBLIC_HOST,
        public_port=PUBLIC_PORT,
        internal_host=INTERNAL_HOST,
        internal_port=INTERNAL_PORT,
        price_policy_hash=price_policy_hash(),
    )


def render_task_xml(identity: TaskIdentity) -> str:
    command = html.escape(identity.execute, quote=True)
    arguments = html.escape(identity.arguments, quote=True)
    working_directory = html.escape(identity.working_directory, quote=True)
    principal = html.escape(identity.principal, quote=True)
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="{_TASK_NS}">
  <RegistrationInfo><Description>agenttalk watched Qwen gateway</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{principal}</UserId></LogonTrigger></Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{principal}</UserId><LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>{TASK_RESTART_INTERVAL}</Interval><Count>{MAX_TASK_RESTARTS}</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command><Arguments>{arguments}</Arguments>
      <WorkingDirectory>{working_directory}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _task_xml_action(xml_text: str) -> tuple[str, str, str, str]:
    if len(xml_text) > 256 * 1024:
        raise GatewayConfigError("registered gateway task XML is too large")
    try:
        # ElementTree does not resolve external entities. Input is additionally
        # bounded and comes only from the local Task Scheduler query.
        root = ET.fromstring(xml_text)  # nosec B314  # noqa: S314
    except ET.ParseError as exc:
        raise GatewayConfigError("registered gateway task XML is malformed") from exc
    ns = {"t": _TASK_NS}
    actions = root.findall(".//t:Actions/t:Exec", ns)
    principals = root.findall(".//t:Principals/t:Principal/t:UserId", ns)
    if len(actions) != 1 or len(principals) != 1:
        raise GatewayConfigError("registered gateway task has an ambiguous action or principal")
    action = actions[0]

    def text(name: str) -> str:
        element = action.find(f"t:{name}", ns)
        if element is None or not isinstance(element.text, str):
            raise GatewayConfigError(f"registered gateway task lacks {name}")
        return element.text

    return text("Command"), text("Arguments"), text("WorkingDirectory"), principals[0].text or ""


def task_xml_matches(xml_text: str, identity: TaskIdentity) -> bool:
    try:
        action = _task_xml_action(xml_text)
    except GatewayConfigError:
        return False
    return action == (
        identity.execute,
        identity.arguments,
        identity.working_directory,
        identity.principal,
    )


def exclusive_bind_probe(host: str, port: int) -> None:
    if host != "127.0.0.1":
        raise GatewayConfigError("gateway bind probe requires literal IPv4 loopback")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind((host, port))
    except OSError as exc:
        raise GatewayConfigError(f"gateway port {host}:{port} is already occupied") from exc
    finally:
        sock.close()


class TaskCommands:
    """Narrow injectable wrapper around the Windows Task Scheduler CLI."""

    def run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(  # nosec B603  # noqa: S603
                argv,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GatewayConfigError("gateway Task Scheduler command failed") from exc

    def query_xml(self, task_name: str) -> str | None:
        result = self.run(["schtasks.exe", "/Query", "/TN", task_name, "/XML"])
        if result.returncode == 1:
            return None
        if result.returncode != 0:
            raise GatewayConfigError("gateway task query failed")
        return result.stdout

    def install(self, task_name: str, xml_path: Path) -> None:
        result = self.run(
            ["schtasks.exe", "/Create", "/TN", task_name, "/XML", str(xml_path)]
        )
        if result.returncode != 0:
            raise GatewayConfigError("gateway task registration failed")

    def start(self, task_name: str) -> None:
        result = self.run(["schtasks.exe", "/Run", "/TN", task_name])
        if result.returncode != 0:
            raise GatewayConfigError("gateway task start failed")

    def stop(self, task_name: str) -> None:
        result = self.run(["schtasks.exe", "/End", "/TN", task_name])
        if result.returncode not in {0, 1}:
            raise GatewayConfigError("gateway task stop failed")


def install_task(
    root: str | os.PathLike[str],
    *,
    commands: TaskCommands | None = None,
    execute: str | os.PathLike[str] = sys.executable,
    principal: str | None = None,
) -> dict:
    commands = commands or TaskCommands()
    identity = expected_task_identity(root, execute=execute, principal=principal)
    existing = commands.query_xml(identity.task_name)
    if existing is not None:
        if not task_xml_matches(existing, identity):
            raise GatewayConfigError("refusing to replace a foreign or mismatched gateway task")
        _durable_write_json(task_identity_path(root), asdict(identity))
        return {"installed": True, "changed": False, **asdict(identity)}
    state_dir = gateway_state_dir(root)
    state_dir.mkdir(parents=True, exist_ok=True)
    xml_path = state_dir / "task.xml"
    task_xml = render_task_xml(identity).replace("\n", "\r\n").encode("utf-16")
    _durable_write_bytes(xml_path, task_xml)
    try:
        commands.install(identity.task_name, xml_path)
    except GatewayConfigError:
        # A concurrent exact installer may win between query and create. Never
        # overwrite: accept only the now-registered byte-equivalent authority.
        raced = commands.query_xml(identity.task_name)
        if raced is None or not task_xml_matches(raced, identity):
            raise
        _durable_write_json(task_identity_path(root), asdict(identity))
        return {"installed": True, "changed": False, **asdict(identity)}
    registered = commands.query_xml(identity.task_name)
    if registered is None or not task_xml_matches(registered, identity):
        raise GatewayConfigError("registered gateway task failed identity verification")
    _durable_write_json(task_identity_path(root), asdict(identity))
    return {"installed": True, "changed": True, **asdict(identity)}


def initialize_install(
    root: str | os.PathLike[str],
    *,
    litellm_executable: str | os.PathLike[str],
    opening_micro_eur: int,
    opening_evidence: str,
    api_base: str = DEFAULT_API_BASE,
    ledger: SpendLedger | None = None,
    front_token_path: Path | None = None,
    internal_token_path: Path | None = None,
) -> dict:
    """One-time state setup. It intentionally does not activate a task or key."""
    root = canonical_project_root(root)
    if api_base != DEFAULT_API_BASE:
        raise GatewayConfigError("gateway install requires the pinned OVH API base")
    config_path = litellm_config_path(root)
    executable = Path(litellm_executable).resolve()
    if not executable.is_file():
        raise GatewayConfigError("LiteLLM executable is missing")
    front_path = front_token_path or default_front_token_path()
    internal_path = internal_token_path or default_internal_token_path()
    existing = [
        path.name
        for path in (config_path, install_manifest_path(root), front_path, internal_path)
        if path.exists()
    ]
    if existing:
        raise GatewayConfigError(
            "refusing partial or replacement gateway initialization: " + ", ".join(existing)
        )
    ledger = ledger or SpendLedger()
    marker = ledger.initialize(
        opening_micro_eur=opening_micro_eur,
        opening_evidence=opening_evidence,
    )
    _durable_write_bytes(
        config_path,
        render_litellm_config(api_base=api_base).encode("utf-8"),
    )
    config_hash = hashlib.sha256(config_path.read_bytes()).hexdigest()
    _durable_write_json(
        install_manifest_path(root),
        {
            "schema_version": 1,
            "litellm_executable": str(executable),
            "litellm_config": str(config_path),
            "litellm_config_sha256": config_hash,
            "price_policy_hash": price_policy_hash(),
            "public_bind": f"{PUBLIC_HOST}:{PUBLIC_PORT}",
            "internal_bind": f"{INTERNAL_HOST}:{INTERNAL_PORT}",
        },
    )
    write_secret_file(front_path, generate_token())
    write_secret_file(internal_path, generate_token())
    return {
        "initialized": True,
        "ledger_generation": marker["generation"],
        "price_policy_hash": marker["price_policy_hash"],
        "opening_micro_eur": marker["opening_micro_eur"],
        "opening_observed_at": marker["opening_observed_at"],
        "config_path": str(config_path),
        "config_sha256": config_hash,
        "litellm_executable": str(executable),
        "front_token_path": str(front_path),
        "internal_token_path": str(internal_path),
    }


def _safe_gateway_environment(ovh_key: str, internal_token: str) -> dict[str, str]:
    allowed = (
        "PATH",
        "SystemRoot",
        "windir",
        "TEMP",
        "TMP",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
    )
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["OVH_KEY"] = ovh_key
    env["LITELLM_MASTER_KEY"] = internal_token
    return env


def load_install_manifest(root: str | os.PathLike[str]) -> dict:
    path = install_manifest_path(root)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GatewayConfigError("gateway install manifest is unreadable") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise GatewayConfigError("gateway install manifest schema mismatch")
    if value.get("price_policy_hash") != price_policy_hash():
        raise GatewayConfigError("gateway install manifest price policy mismatch")
    expected_binds = {
        "public_bind": f"{PUBLIC_HOST}:{PUBLIC_PORT}",
        "internal_bind": f"{INTERNAL_HOST}:{INTERNAL_PORT}",
    }
    for key, expected in expected_binds.items():
        if value.get(key) != expected:
            raise GatewayConfigError(f"gateway install manifest {key} mismatch")
    executable = Path(str(value.get("litellm_executable") or "")).resolve()
    config = Path(str(value.get("litellm_config") or "")).resolve()
    if config != litellm_config_path(root).resolve():
        raise GatewayConfigError("gateway install manifest config path mismatch")
    if not executable.is_file() or not config.is_file():
        raise GatewayConfigError("gateway install manifest references a missing artifact")
    digest = hashlib.sha256(config.read_bytes()).hexdigest()
    if digest != value.get("litellm_config_sha256"):
        raise GatewayConfigError("LiteLLM config hash mismatch")
    return value


def _process_start_token(pid: int) -> str:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError as exc:
            raise GatewayConfigError("gateway process is not running") from exc
        return str(pid)
    import ctypes
    from ctypes import wintypes

    query_limited = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(query_limited, False, pid)
    if not handle:
        raise GatewayConfigError("cannot query gateway process identity")
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise GatewayConfigError("cannot query gateway process creation time")
        return f"{creation.dwHighDateTime:08x}{creation.dwLowDateTime:08x}"
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def actions_enabled(root: str | os.PathLike[str]) -> bool:
    return not kill_switch_path(root).exists()


def _both_sockets_free() -> bool:
    try:
        exclusive_bind_probe(PUBLIC_HOST, PUBLIC_PORT)
        exclusive_bind_probe(INTERNAL_HOST, INTERNAL_PORT)
    except GatewayConfigError:
        return False
    return True


def _service_absent(root: str | os.PathLike[str]) -> bool:
    root = canonical_project_root(root)
    sockets_free = _both_sockets_free()
    marker = runtime_marker_path(root)
    if sockets_free and marker.exists() and not actions_enabled(root):
        # Task Scheduler can terminate the runner without executing Python's
        # finally block. With actions disabled and both exact sockets free, the
        # marker is stale state, not process authority.
        with contextlib.suppress(OSError):
            marker.unlink()
    return sockets_free and not marker.exists()


def _runtime_projection(root: Path, manifest: dict, *, front_token_sha256: str) -> dict:
    path = runtime_marker_path(root)
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GatewayConfigError("gateway runtime marker is unreadable") from exc
    if not isinstance(marker, dict):
        raise GatewayConfigError("gateway runtime marker must be an object")
    expected = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "task_name": project_task_name(root),
        "price_policy_hash": price_policy_hash(),
        "config_sha256": manifest["litellm_config_sha256"],
        "public_bind": f"{PUBLIC_HOST}:{PUBLIC_PORT}",
        "internal_bind": f"{INTERNAL_HOST}:{INTERNAL_PORT}",
        "front_token_sha256": front_token_sha256,
    }
    for key, value in expected.items():
        if marker.get(key) != value:
            raise GatewayConfigError(f"gateway runtime marker {key} mismatch")
    for prefix in ("runner", "litellm"):
        pid = marker.get(f"{prefix}_pid")
        start = marker.get(f"{prefix}_start")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise GatewayConfigError(f"gateway runtime marker {prefix}_pid is invalid")
        if not isinstance(start, str) or _process_start_token(pid) != start:
            raise GatewayConfigError(f"gateway runtime marker {prefix} identity mismatch")
    return {
        key: marker[key]
        for key in (
            "schema_version",
            "runner_pid",
            "runner_start",
            "litellm_pid",
            "litellm_start",
            "task_name",
            "price_policy_hash",
            "config_sha256",
            "public_bind",
            "internal_bind",
        )
    }


def _wait_liveliness(front: GatewayFront, process: subprocess.Popen, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise GatewayConfigError("LiteLLM exited before readiness")
        if front.internal_liveliness():
            return
        time.sleep(0.25)
    raise GatewayConfigError("LiteLLM readiness timed out")


def _public_front_attested(timeout_seconds: float = 5.0) -> bool:
    """Recognize the public front without disclosing its real bearer token."""
    conn: http.client.HTTPConnection | None = None
    try:
        conn = http.client.HTTPConnection(PUBLIC_HOST, PUBLIC_PORT, timeout=timeout_seconds)
        body = b"{}"
        conn.request(
            "POST",
            PUBLIC_ROUTE,
            body=body,
            headers={
                "Authorization": "Bearer atgw-status-probe-not-a-secret",
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
                "Host": f"{PUBLIC_HOST}:{PUBLIC_PORT}",
            },
        )
        response = conn.getresponse()
        payload = response.read(4097)
        if response.status != 401 or len(payload) > 4096:
            return False
        value = json.loads(payload.decode("ascii"))
        return (
            isinstance(value, dict)
            and isinstance(value.get("error"), dict)
            and value["error"].get("type") == CONFIG_ERROR_CODE
            and value["error"].get("message") == CONFIG_ERROR_CODE
        )
    except (OSError, ValueError, UnicodeDecodeError, http.client.HTTPException):
        return False
    finally:
        if conn is not None:
            conn.close()


def run_service(
    root: str | os.PathLike[str],
    *,
    litellm_executable: str | os.PathLike[str] | None = None,
    ledger: SpendLedger | None = None,
    key_path: Path | None = None,
    front_token_path: Path | None = None,
    internal_token_path: Path | None = None,
    popen=subprocess.Popen,
) -> int:
    """Run LiteLLM plus the public front. Called only by the managed task."""
    root = canonical_project_root(root)
    if not actions_enabled(root):
        raise GatewayConfigError("gateway.kill is present; actions are disabled")
    ledger = ledger or SpendLedger()
    if not ledger.status()["ready"]:
        raise LedgerBlocked("ledger has an unresolved reservation or service hold")
    exclusive_bind_probe(PUBLIC_HOST, PUBLIC_PORT)
    exclusive_bind_probe(INTERNAL_HOST, INTERNAL_PORT)
    ovh_key = read_secret_file(key_path or default_key_path())
    front_token = read_secret_file(front_token_path or default_front_token_path())
    internal_token = read_secret_file(internal_token_path or default_internal_token_path())
    manifest = load_install_manifest(root)
    configured_executable = str(Path(manifest["litellm_executable"]).resolve())
    if (
        litellm_executable is not None
        and str(Path(litellm_executable).resolve()) != configured_executable
    ):
        raise GatewayConfigError("requested LiteLLM executable differs from installed manifest")
    executable = configured_executable
    config_path = litellm_config_path(root)
    if not config_path.is_file():
        raise GatewayConfigError("LiteLLM config is missing")
    argv = [
        executable,
        "--config",
        str(config_path),
        "--host",
        INTERNAL_HOST,
        "--port",
        str(INTERNAL_PORT),
        "--num_workers",
        "1",
    ]
    process = popen(
        argv,
        cwd=str(root),
        env=_safe_gateway_environment(ovh_key, internal_token),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    front = GatewayFront(
        FrontConfig(public_token=front_token, internal_token=internal_token),
        ledger,
    )
    server = None
    monitor_stop = threading.Event()
    try:
        _wait_liveliness(front, process)
        server = front.make_server()
        _durable_write_json(
            runtime_marker_path(root),
            {
                "schema_version": RUNTIME_SCHEMA_VERSION,
                "runner_pid": os.getpid(),
                "runner_start": _process_start_token(os.getpid()),
                "litellm_pid": int(process.pid),
                "litellm_start": _process_start_token(int(process.pid)),
                "task_name": project_task_name(root),
                "price_policy_hash": price_policy_hash(),
                "config_sha256": manifest["litellm_config_sha256"],
                "public_bind": f"{PUBLIC_HOST}:{PUBLIC_PORT}",
                "internal_bind": f"{INTERNAL_HOST}:{INTERNAL_PORT}",
                "front_token_sha256": hashlib.sha256(front_token.encode("utf-8")).hexdigest(),
            },
        )

        def monitor() -> None:
            while not monitor_stop.wait(0.25):
                if not actions_enabled(root) or process.poll() is not None:
                    server.shutdown()
                    return

        monitor_thread = threading.Thread(target=monitor, daemon=True)
        monitor_thread.start()
        server.serve_forever(poll_interval=0.25)
        front.drain(STOP_TIMEOUT_SECONDS)
        monitor_stop.set()
        monitor_thread.join(timeout=2)
        return 0 if process.poll() is None or not actions_enabled(root) else 1
    finally:
        monitor_stop.set()
        if server is not None:
            server.server_close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        with contextlib.suppress(FileNotFoundError):
            runtime_marker_path(root).unlink()


def stop_task(
    root: str | os.PathLike[str],
    *,
    commands: TaskCommands | None = None,
    timeout_seconds: float = STOP_TIMEOUT_SECONDS,
) -> dict:
    commands = commands or TaskCommands()
    root = canonical_project_root(root)
    identity = expected_task_identity(root)
    existing = commands.query_xml(identity.task_name)
    if existing is None:
        if _service_absent(root):
            return {"stopped": True, "task_present": False}
        raise GatewayConfigError(
            "gateway task is absent but runtime state or a loopback port remains occupied"
        )
    if not task_xml_matches(existing, identity):
        raise GatewayConfigError("refusing to stop a foreign or mismatched gateway task")
    kill = kill_switch_path(root)
    kill.parent.mkdir(parents=True, exist_ok=True)
    kill.write_text("operator-stop\n", encoding="ascii")
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() < deadline:
        if _service_absent(root):
            return {"stopped": True, "forced": False, "task_present": True}
        time.sleep(0.25)
    commands.stop(identity.task_name)
    verify_deadline = time.monotonic() + 5.0
    while time.monotonic() < verify_deadline:
        if _service_absent(root):
            return {"stopped": True, "forced": True, "task_present": True}
        time.sleep(0.25)
    raise GatewayConfigError("gateway task ended but its loopback sockets remain occupied")


def start_task(
    root: str | os.PathLike[str],
    *,
    commands: TaskCommands | None = None,
    ledger: SpendLedger | None = None,
    readiness_timeout_seconds: float = 30.0,
) -> dict:
    commands = commands or TaskCommands()
    root = canonical_project_root(root)
    load_install_manifest(root)
    ledger = ledger or SpendLedger()
    if not ledger.status()["ready"]:
        raise LedgerBlocked("gateway ledger is not ready")
    identity = expected_task_identity(root)
    existing = commands.query_xml(identity.task_name)
    if existing is None:
        raise GatewayConfigError("gateway task is not installed")
    if not task_xml_matches(existing, identity):
        raise GatewayConfigError("refusing to start a foreign or mismatched gateway task")
    current = gateway_status(root, commands=commands, ledger=ledger)
    if current["ready"]:
        return {"started": False, "ready": True, "task_name": identity.task_name}
    kill_switch_path(root).unlink(missing_ok=True)
    exclusive_bind_probe(PUBLIC_HOST, PUBLIC_PORT)
    exclusive_bind_probe(INTERNAL_HOST, INTERNAL_PORT)
    commands.start(identity.task_name)
    deadline = time.monotonic() + max(0.0, readiness_timeout_seconds)
    last_errors: list[str] = []
    while time.monotonic() < deadline:
        status = gateway_status(root, commands=commands, ledger=ledger)
        if status["ready"]:
            return {"started": True, "ready": True, "task_name": identity.task_name}
        last_errors = list(status["errors"])
        time.sleep(0.25)
    detail = ",".join(last_errors) or "readiness_timeout"
    raise GatewayConfigError(f"gateway task did not become ready: {detail}")


def gateway_status(
    root: str | os.PathLike[str],
    *,
    commands: TaskCommands | None = None,
    ledger: SpendLedger | None = None,
    front_token_path: Path | None = None,
    internal_token_path: Path | None = None,
) -> dict:
    """Return an allowlisted, non-secret service projection."""
    commands = commands or TaskCommands()
    root = canonical_project_root(root)
    ledger = ledger or SpendLedger()
    result = {
        "schema_version": 1,
        "project_root": str(root),
        "task_name": project_task_name(root),
        "price_policy_hash": price_policy_hash(),
        "ambient_provider_keys_absent": not any(
            os.environ.get(key) for key in ("OVH_KEY", "ANTHROPIC_API_KEY")
        ),
        "install_manifest_ok": False,
        "ledger_ok": False,
        "task_identity_ok": False,
        "runtime_marker_present": False,
        "runtime": None,
        "worker_spend_ready": False,
        "worker_spend_errors": ["ledger_unavailable"],
        "errors": [],
    }
    manifest: dict | None = None
    try:
        manifest = load_install_manifest(root)
        result["install_manifest_ok"] = True
        result["config_sha256"] = manifest["litellm_config_sha256"]
    except GatewayConfigError:
        result["errors"].append("install_manifest_invalid")
    try:
        result["ledger"] = ledger.status()
        result["ledger_ok"] = True
        result["worker_spend_ready"] = result["ledger"]["worker_spend_ready"]
        result["worker_spend_errors"] = list(
            result["ledger"]["worker_spend_errors"]
        )
    except (LedgerBlocked, LedgerHold):
        result["errors"].append("ledger_blocked")
    try:
        identity = expected_task_identity(root)
        task_xml = commands.query_xml(identity.task_name)
        try:
            stored_identity = json.loads(task_identity_path(root).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored_identity = None
        result["task_identity_ok"] = (
            task_xml is not None
            and task_xml_matches(task_xml, identity)
            and stored_identity == asdict(identity)
        )
        if not result["task_identity_ok"]:
            result["errors"].append("task_identity_invalid")
    except (GatewayConfigError, OSError):
        result["errors"].append("task_query_failed")
    front_token_hash: str | None = None
    try:
        front_token = read_secret_file(front_token_path or default_front_token_path())
        front_token_hash = hashlib.sha256(front_token.encode("utf-8")).hexdigest()
    except GatewayConfigError:
        result["errors"].append("front_token_unavailable")
    if (
        runtime_marker_path(root).is_file()
        and manifest is not None
        and front_token_hash is not None
    ):
        try:
            result["runtime"] = _runtime_projection(
                root,
                manifest,
                front_token_sha256=front_token_hash,
            )
            result["runtime_marker_present"] = True
        except GatewayConfigError:
            result["errors"].append("runtime_marker_invalid")
    else:
        result["errors"].append("runtime_marker_missing")
    try:
        exclusive_bind_probe(PUBLIC_HOST, PUBLIC_PORT)
    except GatewayConfigError:
        result["public_listener_present"] = True
    else:
        result["public_listener_present"] = False
        result["errors"].append("public_listener_missing")
    result["public_front_attested"] = _public_front_attested()
    if not result["public_front_attested"]:
        result["errors"].append("public_front_attestation_failed")
    try:
        token = read_secret_file(internal_token_path or default_internal_token_path())
        front = GatewayFront(FrontConfig(public_token=token, internal_token=token), ledger)
        result["internal_liveliness"] = front.internal_liveliness()
    except GatewayConfigError:
        result["internal_liveliness"] = False
    if not result["internal_liveliness"]:
        result["errors"].append("internal_liveliness_failed")
    if not result["ambient_provider_keys_absent"]:
        result["errors"].append("supervisor_ambient_provider_key")
    result["errors"] = sorted(set(result["errors"]))
    result["operational_ready"] = not result["errors"]
    result["ready"] = result["operational_ready"]
    return result
