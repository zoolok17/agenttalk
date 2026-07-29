from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess  # nosec B404 - fixed local executable under an explicit test opt-in
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agenttalk.ovh_gateway import (
    MODEL_ALIAS,
    SpendLedger,
    render_litellm_config,
    settlement_cost_micro_eur,
)
from agenttalk.ovh_gateway_front import FrontConfig, GatewayFront


TEST_CHILD_CAP_ISSUER = "atgw-" + "i" * 43
# Public front exchanges use completion events rather than socket read
# deadlines. This matches the suite's existing synchronized-contention
# watchdog (#94) and only reports a stuck harness worker.
COMPLETION_WATCHDOG_SECONDS = 30


def _await_completed(operation, *, label: str):
    completed = threading.Event()
    results: list[object] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(operation())
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    assert completed.wait(timeout=COMPLETION_WATCHDOG_SECONDS), (
        f"{label} did not complete after its synchronization condition was armed"
    )
    worker.join()
    if errors:
        raise errors[0]
    assert len(results) == 1
    return results[0]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class _FakeOpenAI:
    def __init__(self, *, status: int = 200) -> None:
        self.requests: list[dict] = []
        self.status = status
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length))
                owner.requests.append({"path": self.path, "body": body})
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                if owner.status != 200:
                    response = b"fake-provider-error fake-provider-secret"
                    self.send_response(owner.status)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)
                    return
                if body.get("stream") is True:
                    chunks = [
                        {
                            "id": "chatcmpl-fake",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": MODEL_ALIAS,
                            "choices": [{
                                "index": 0,
                                "delta": {"role": "assistant", "content": "ok"},
                                "finish_reason": None,
                            }],
                        },
                        {
                            "id": "chatcmpl-fake",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": MODEL_ALIAS,
                            "choices": [{
                                "index": 0,
                                "delta": {},
                                "finish_reason": "stop",
                            }],
                            "usage": {
                                "prompt_tokens": 50,
                                "completion_tokens": 10,
                                "total_tokens": 60,
                            },
                        },
                    ]
                    response = b"".join(
                        b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n"
                        for chunk in chunks
                    ) + b"data: [DONE]\n\n"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)
                    return
                response = json.dumps({
                    "id": "chatcmpl-fake",
                    "object": "chat.completion",
                    "created": 1,
                    "model": MODEL_ALIAS,
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": "toolu_fake",
                                "type": "function",
                                "function": {
                                    "name": "Read",
                                    "arguments": json.dumps({"file_path": "README.md"}),
                                },
                            }],
                        },
                        "finish_reason": "tool_calls",
                    }],
                    "usage": {
                        "prompt_tokens": 50,
                        "completion_tokens": 10,
                        "total_tokens": 60,
                    },
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> _FakeOpenAI:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _wait_liveliness(port: int, process: subprocess.Popen, *, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"LiteLLM exited during startup with code {process.returncode}")
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            connection.request("GET", "/health/liveliness")
            response = connection.getresponse()
            response.read()
            if response.status == 200:
                return
        except OSError:
            pass
        finally:
            connection.close()
        time.sleep(0.1)
    pytest.fail("LiteLLM did not become live")


@pytest.mark.parametrize("upstream_status", [200, 429, 500])
def test_anthropic_messages_routes_once_to_chat_completions_with_representative_tools(
    tmp_path: Path,
    upstream_status: int,
) -> None:
    executable_value = os.environ.get("AGENTTALK_TEST_LITELLM_EXE")
    if not executable_value:
        pytest.skip("set AGENTTALK_TEST_LITELLM_EXE for the pinned local conformance test")
    executable = Path(executable_value).resolve()
    if not executable.is_file():
        pytest.fail("AGENTTALK_TEST_LITELLM_EXE is not a file")

    port = _free_port()
    public_port = _free_port()
    with _FakeOpenAI(status=upstream_status) as upstream:
        config = tmp_path / "litellm.yaml"
        config.write_text(
            render_litellm_config(api_base=f"http://127.0.0.1:{upstream.port}/v1"),
            encoding="utf-8",
        )
        env = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
        }
        env.update({
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "OVH_KEY": "fake-upstream-key",
            "LITELLM_MASTER_KEY": "sk-fake-internal",
        })
        process = subprocess.Popen(  # nosec B603 - argv is fixed and executable is opt-in
            [
                str(executable),
                "--config",
                str(config),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--num_workers",
                "1",
            ],
            cwd=tmp_path,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        ledger = SpendLedger(tmp_path / "ledger.sqlite3", tmp_path / "install.json")
        ledger.initialize(
            opening_micro_eur=0,
            opening_evidence="fake LiteLLM fixture, observed 2026-07-16",
            generation="a" * 32,
            child_cap_issuer_token=TEST_CHILD_CAP_ISSUER,
        )
        credential = ledger.open_child_turn(
            agent="qwen-dev-1",
            message_id="litellm-conformance-message",
            request_id="q-litellm-conformance",
            issuer_token=TEST_CHILD_CAP_ISSUER,
        )
        front = GatewayFront(
            FrontConfig(
                public_token="sk-fake-front",
                internal_token="sk-fake-internal",
                public_port=public_port,
                internal_port=port,
                request_timeout_seconds=10,
            ),
            ledger,
        )
        front_server = front.make_server()
        front_thread = threading.Thread(target=front_server.serve_forever, daemon=True)
        try:
            _wait_liveliness(port, process)
            front_thread.start()
            def front_ready() -> None:
                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    public_port,
                    timeout=None,
                )
                try:
                    connection.request("GET", "/")
                    response = connection.getresponse()
                    response.read()
                    assert response.status == 404
                finally:
                    connection.close()

            _await_completed(front_ready, label="gateway front readiness probe")
            tools = [
                {
                    "name": name,
                    "description": f"Representative Claude Code {name} tool",
                    "input_schema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                }
                for name in ("Read", "Edit", "Grep")
            ]
            request = json.dumps({
                "model": MODEL_ALIAS,
                "max_tokens": 64,
                "stream": True,
                "store": True,
                "messages": [{"role": "user", "content": "Read README.md"}],
                "tools": tools,
            }).encode("utf-8")
            def exchange() -> tuple[int, bytes]:
                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    public_port,
                    timeout=None,
                )
                try:
                    connection.request(
                        "POST",
                        "/v1/messages",
                        body=request,
                        headers={
                            "Authorization": f"Bearer {credential.token}",
                            "Content-Type": "application/json",
                            "Host": f"127.0.0.1:{public_port}",
                        },
                    )
                    response = connection.getresponse()
                    return response.status, response.read()
                finally:
                    connection.close()

            response_status, response_body = _await_completed(
                exchange,
                label="gateway front conformance request",
            )
        finally:
            try:
                if front_thread.is_alive():
                    front_server.shutdown()
                front_server.server_close()
                if front_thread.ident is not None:
                    front_thread.join(timeout=5)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    assert len(upstream.requests) == 1
    translated = upstream.requests[0]
    assert translated["path"] == "/v1/chat/completions"
    assert translated["body"]["store"] is False
    assert translated["body"]["model"] == MODEL_ALIAS
    assert [tool["function"]["name"] for tool in translated["body"]["tools"]] == [
        "Read",
        "Edit",
        "Grep",
    ]
    assert all(tool["type"] == "function" for tool in translated["body"]["tools"])
    assert all("parameters" in tool["function"] for tool in translated["body"]["tools"])
    assert "input" not in translated["body"]
    assert "max_output_tokens" not in translated["body"]

    ledger_status = ledger.status()
    if upstream_status == 200:
        assert response_status == 200, response_body.decode("utf-8", "replace")
        assert b"message_stop" in response_body
        assert ledger_status["current_committed_micro_eur"] == settlement_cost_micro_eur(
            50,
            10,
        )
        assert ledger_status["unresolved"] == []
    else:
        assert response_status == 502, response_body.decode("utf-8", "replace")
        assert b"fake-provider-secret" not in response_body
        assert ledger_status["current_committed_micro_eur"] == 0
        assert len(ledger_status["unresolved"]) == 1
        assert ledger_status["unresolved"][0]["state"] == "uncertain"
