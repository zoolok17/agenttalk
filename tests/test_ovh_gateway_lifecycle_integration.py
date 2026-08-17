from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from agenttalk import ovh_gateway_service as service
from agenttalk.ovh_gateway import SpendLedger


pytestmark = [
    pytest.mark.skipif(os.name != "nt", reason="gateway lifecycle is Windows-first"),
    pytest.mark.subprocess,
]

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = (_REPO_ROOT / "src").resolve()

_REBIND_CHILD = r"""
import json
import os
from pathlib import Path
import sys

source = Path(sys.argv[1]).resolve()
root = Path(sys.argv[2])
candidate = Path(sys.argv[3])
result_path = Path(sys.argv[4])

from agenttalk import ovh_gateway_service as service

assert os.path.commonpath([str(Path(service.__file__).resolve()), str(source)]) == str(source)
try:
    value = service.rebind_runtime(root, litellm_executable=candidate)
    payload = {"ok": True, "value": value}
except BaseException as exc:
    payload = {"ok": False, "type": type(exc).__name__, "message": str(exc)}
result_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
"""

_RECONFIGURE_CHILD = r"""
import contextlib
import json
import os
from pathlib import Path
import sys

source = Path(sys.argv[1]).resolve()
root = Path(sys.argv[2])
attempted_path = Path(sys.argv[3])
result_path = Path(sys.argv[4])

from agenttalk import lifecycle_lock
from agenttalk import ovh_gateway_service as service

assert os.path.commonpath([str(Path(service.__file__).resolve()), str(source)]) == str(source)
original_hold = lifecycle_lock.CrossProcessLifecycleLock.hold

@contextlib.contextmanager
def observed_hold(self, operation):
    if operation == "reconfigure":
        attempted_path.write_text("attempted\n", encoding="ascii")
    with original_hold(self, operation) as claim:
        yield claim

lifecycle_lock.CrossProcessLifecycleLock.hold = observed_hold
try:
    value = service.reconfigure_endpoint(root)
    payload = {"ok": True, "value": value}
except BaseException as exc:
    payload = {"ok": False, "type": type(exc).__name__, "message": str(exc)}
result_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
"""

_RUN_SERVICE_CHILD = r"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

source = Path(sys.argv[1]).resolve()
root = Path(sys.argv[2])
ledger_db = Path(sys.argv[3])
ledger_install = Path(sys.argv[4])
provider_key = Path(sys.argv[5])
front_token = Path(sys.argv[6])
internal_token = Path(sys.argv[7])
child_log = Path(sys.argv[8])
startup_spawned = Path(sys.argv[9])
allow_bind = Path(sys.argv[10])
provider_bound = Path(sys.argv[11])
public_bound = Path(sys.argv[12])
stop_serving = Path(sys.argv[13])
result_path = Path(sys.argv[14])

from agenttalk import ovh_gateway_service as service
from agenttalk.ovh_gateway import SpendLedger

assert os.path.commonpath([str(Path(service.__file__).resolve()), str(source)]) == str(source)

provider_script = r'''import socket
from pathlib import Path
import sys
import time

host = sys.argv[1]
port = int(sys.argv[2])
allow = Path(sys.argv[3])
ready = Path(sys.argv[4])
while not allow.exists():
    time.sleep(0.01)
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.bind((host, port))
sock.listen(1)
ready.write_text("bound\n", encoding="ascii")
while True:
    time.sleep(1)
'''

def provider_popen(_argv, **kwargs):
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            provider_script,
            service.INTERNAL_HOST,
            str(service.INTERNAL_PORT),
            str(allow_bind),
            str(provider_bound),
        ],
        **kwargs,
    )
    startup_spawned.write_text("spawned\n", encoding="ascii")
    return process

class BoundFrontServer:
    def __init__(self):
        self._shutdown = False
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.bind((service.PUBLIC_HOST, service.PUBLIC_PORT))
        self._socket.listen(1)
        public_bound.write_text("bound\n", encoding="ascii")

    def serve_forever(self, poll_interval=None):
        while not self._shutdown and not stop_serving.exists():
            time.sleep(0.01)

    def shutdown(self):
        self._shutdown = True

    def server_close(self):
        self._socket.close()

class BoundFront:
    def __init__(self, _config, _ledger):
        pass

    def internal_liveliness(self):
        return provider_bound.exists()

    def make_server(self):
        return BoundFrontServer()

    def drain(self, _timeout):
        return None

service.GatewayFront = BoundFront
try:
    value = service.run_service(
        root,
        ledger=SpendLedger(ledger_db, ledger_install),
        key_path=provider_key,
        front_token_path=front_token,
        internal_token_path=internal_token,
        child_log_path=child_log,
        popen=provider_popen,
    )
    payload = {"ok": True, "value": value}
except BaseException as exc:
    payload = {"ok": False, "type": type(exc).__name__, "message": str(exc)}
result_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
"""

_OBSERVED_REBIND_CHILD = r"""
import contextlib
import json
import os
from pathlib import Path
import sys

source = Path(sys.argv[1]).resolve()
root = Path(sys.argv[2])
candidate = Path(sys.argv[3])
attempted_path = Path(sys.argv[4])
socket_checked_path = Path(sys.argv[5])
result_path = Path(sys.argv[6])

from agenttalk import lifecycle_lock
from agenttalk import ovh_gateway_service as service

assert os.path.commonpath([str(Path(service.__file__).resolve()), str(source)]) == str(source)
original_hold = lifecycle_lock.CrossProcessLifecycleLock.hold
original_sockets_free = service._both_sockets_free

@contextlib.contextmanager
def observed_hold(self, operation):
    if operation == "runtime-rebind":
        attempted_path.write_text("attempted\n", encoding="ascii")
    with original_hold(self, operation) as claim:
        yield claim

def observed_sockets_free():
    socket_checked_path.write_text("checked\n", encoding="ascii")
    return original_sockets_free()

lifecycle_lock.CrossProcessLifecycleLock.hold = observed_hold
service._both_sockets_free = observed_sockets_free
try:
    value = service.rebind_runtime(root, litellm_executable=candidate)
    payload = {"ok": True, "value": value}
except BaseException as exc:
    payload = {"ok": False, "type": type(exc).__name__, "message": str(exc)}
result_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
"""


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_SOURCE_ROOT)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _spawn(script: str, *args: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603 - exact local interpreter and test script
        [sys.executable, "-c", script, str(_SOURCE_ROOT), *(str(arg) for arg in args)],
        env=_child_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )


def _wait_for_path(
    path: Path,
    processes: tuple[subprocess.Popen[str], ...],
    *,
    timeout: float = 10.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        exited = [process for process in processes if process.poll() is not None]
        if exited:
            details = []
            for process in exited:
                stdout, stderr = process.communicate()
                details.append(
                    f"pid={process.pid}, rc={process.returncode}, "
                    f"stdout={stdout!r}, stderr={stderr!r}"
                )
            raise AssertionError(
                f"process exited before publishing {path}: " + "; ".join(details)
            )
        time.sleep(0.01)
    raise AssertionError(f"process did not publish {path} within {timeout:g}s")


def _wait_process(process: subprocess.Popen[str], timeout: float = 15.0) -> None:
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        stdout, stderr = process.communicate(timeout=5)
        raise AssertionError(
            f"child pid {process.pid} did not exit: stdout={stdout!r}, stderr={stderr!r}"
        ) from None
    assert process.returncode == 0, (stdout, stderr)


def _kill_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    subprocess.run(  # noqa: S603,S607 - bounded cleanup of the exact test process tree
        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )


def _read_result(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write_runtime_shim(path: Path, script_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'@echo off\r\n"{sys.executable}" "{script_path}" %*\r\n',
        encoding="utf-8",
    )


def _initialize_gateway(tmp_path: Path):
    root = tmp_path / "project"
    old_runtime = tmp_path / "old-runtime" / "litellm.exe"
    old_runtime.parent.mkdir(parents=True)
    old_runtime.write_bytes(b"old runtime")
    ledger_db = tmp_path / "spend" / "ledger.sqlite3"
    ledger_install = tmp_path / "spend" / "install.json"
    ledger = SpendLedger(ledger_db, ledger_install)
    front_token = tmp_path / "secrets" / "front.txt"
    internal_token = tmp_path / "secrets" / "internal.txt"
    service.initialize_install(
        root,
        litellm_executable=old_runtime,
        opening_micro_eur=580_000,
        opening_evidence="integration test observation",
        ledger=ledger,
        front_token_path=front_token,
        internal_token_path=internal_token,
    )
    return (
        root,
        old_runtime,
        ledger_db,
        ledger_install,
        front_token,
        internal_token,
    )


def _assert_ports_free() -> None:
    service.exclusive_bind_probe(service.PUBLIC_HOST, service.PUBLIC_PORT)
    service.exclusive_bind_probe(service.INTERNAL_HOST, service.INTERNAL_PORT)


def test_real_process_rebind_serializes_reconfigure_without_stale_manifest_write(
    tmp_path: Path,
) -> None:
    _assert_ports_free()
    root, _, _, _, _, _ = _initialize_gateway(tmp_path)
    config_path = service.litellm_config_path(root)
    manifest_path = service.install_manifest_path(root)
    stale_config = service.render_litellm_config(
        api_base="https://stale.example/v1"
    ).encode("utf-8")
    config_path.write_bytes(stale_config)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["litellm_config_sha256"] = hashlib.sha256(stale_config).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    probe_ready = tmp_path / "probe.ready"
    probe_release = tmp_path / "probe.release"
    candidate_script = tmp_path / "candidate.py"
    candidate_script.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "import time\n"
        f"ready = Path({str(probe_ready)!r})\n"
        f"release = Path({str(probe_release)!r})\n"
        "assert sys.argv[1:] == ['--version']\n"
        "ready.write_text('ready\\n', encoding='ascii')\n"
        "while not release.exists():\n"
        "    time.sleep(0.01)\n"
        "print('LiteLLM: Current Version = 1.91.3')\n",
        encoding="utf-8",
    )
    candidate = tmp_path / "candidate" / "litellm.cmd"
    _write_runtime_shim(candidate, candidate_script)
    rebind_result = tmp_path / "rebind-result.json"
    reconfigure_attempted = tmp_path / "reconfigure.attempted"
    reconfigure_result = tmp_path / "reconfigure-result.json"
    rebind = _spawn(_REBIND_CHILD, root, candidate, rebind_result)
    reconfigure = None
    try:
        _wait_for_path(probe_ready, (rebind,))
        reconfigure = _spawn(
            _RECONFIGURE_CHILD,
            root,
            reconfigure_attempted,
            reconfigure_result,
        )
        _wait_for_path(reconfigure_attempted, (rebind, reconfigure))
        assert reconfigure.poll() is None
        assert not reconfigure_result.exists()
        assert service.load_install_manifest(root)["litellm_executable"] != str(
            candidate.resolve()
        )

        probe_release.write_text("release\n", encoding="ascii")
        _wait_for_path(rebind_result, (rebind, reconfigure))
        _wait_for_path(reconfigure_result, (reconfigure,))
        _wait_process(rebind)
        _wait_process(reconfigure)

        assert _read_result(rebind_result)["ok"] is True
        assert _read_result(reconfigure_result)["ok"] is True
        final_manifest = service.load_install_manifest(root)
        expected_config = service.render_litellm_config(
            api_base=service.DEFAULT_API_BASE
        ).encode("utf-8")
        assert config_path.read_bytes() == expected_config
        assert final_manifest["litellm_executable"] == str(candidate.resolve())
        assert final_manifest["litellm_config_sha256"] == hashlib.sha256(
            expected_config
        ).hexdigest()
    finally:
        probe_release.write_text("release\n", encoding="ascii")
        _kill_tree(rebind)
        if reconfigure is not None:
            _kill_tree(reconfigure)


def test_real_process_service_startup_excludes_rebind_until_sockets_are_owned(
    tmp_path: Path,
) -> None:
    _assert_ports_free()
    (
        root,
        old_runtime,
        ledger_db,
        ledger_install,
        front_token,
        internal_token,
    ) = _initialize_gateway(tmp_path)
    provider_key = tmp_path / "secrets" / "provider.txt"
    provider_key.write_text("provider-secret\n", encoding="utf-8")
    candidate_script = tmp_path / "candidate.py"
    candidate_script.write_text(
        "import sys\n"
        "assert sys.argv[1:] == ['--version']\n"
        "print('LiteLLM: Current Version = 1.91.3')\n",
        encoding="utf-8",
    )
    candidate = tmp_path / "candidate" / "litellm.cmd"
    _write_runtime_shim(candidate, candidate_script)

    startup_spawned = tmp_path / "startup.spawned"
    allow_bind = tmp_path / "startup.allow-bind"
    provider_bound = tmp_path / "provider.bound"
    public_bound = tmp_path / "public.bound"
    stop_serving = tmp_path / "service.stop"
    run_result = tmp_path / "run-result.json"
    rebind_attempted = tmp_path / "rebind.attempted"
    socket_checked = tmp_path / "rebind.socket-checked"
    rebind_result = tmp_path / "rebind-result.json"

    runner = _spawn(
        _RUN_SERVICE_CHILD,
        root,
        ledger_db,
        ledger_install,
        provider_key,
        front_token,
        internal_token,
        tmp_path / "litellm.log",
        startup_spawned,
        allow_bind,
        provider_bound,
        public_bound,
        stop_serving,
        run_result,
    )
    rebind = None
    try:
        _wait_for_path(startup_spawned, (runner,))
        rebind = _spawn(
            _OBSERVED_REBIND_CHILD,
            root,
            candidate,
            rebind_attempted,
            socket_checked,
            rebind_result,
        )
        _wait_for_path(rebind_attempted, (runner, rebind))
        assert rebind.poll() is None
        assert not socket_checked.exists()
        assert not rebind_result.exists()

        allow_bind.write_text("bind\n", encoding="ascii")
        _wait_for_path(provider_bound, (runner, rebind))
        _wait_for_path(public_bound, (runner, rebind))
        _wait_for_path(service.runtime_marker_path(root), (runner, rebind))
        _wait_for_path(socket_checked, (runner, rebind))
        _wait_for_path(rebind_result, (runner, rebind))
        _wait_process(rebind)

        refusal = _read_result(rebind_result)
        assert refusal["ok"] is False
        assert refusal["type"] == "GatewayConfigError"
        assert "while the gateway is running" in refusal["message"]
        assert service.load_install_manifest(root)["litellm_executable"] == str(
            old_runtime.resolve()
        )

        stop_serving.write_text("stop\n", encoding="ascii")
        _wait_for_path(run_result, (runner,))
        _wait_process(runner)
        assert _read_result(run_result) == {"ok": True, "value": 0}
        assert not service.runtime_marker_path(root).exists()
        _assert_ports_free()
    finally:
        allow_bind.write_text("bind\n", encoding="ascii")
        stop_serving.write_text("stop\n", encoding="ascii")
        _kill_tree(runner)
        if rebind is not None:
            _kill_tree(rebind)
