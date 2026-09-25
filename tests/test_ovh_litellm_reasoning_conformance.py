from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agenttalk.ovh_gateway import MODEL_ALIAS, render_litellm_config
from agenttalk.ovh_gateway_reasoning import SseReasoningStripper

pytestmark = pytest.mark.subprocess

MASTER = "sk-fake-internal"


def _chunk(delta: dict, finish: str | None = None, usage: dict | None = None) -> str:
    data = {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": MODEL_ALIAS,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage:
        data["usage"] = usage
    return "data: " + json.dumps(data) + "\n\n"


def _reasoning_then_tool_stream() -> list[str]:
    return [
        _chunk({"role": "assistant", "content": ""}),
        _chunk({"reasoning_content": "I should read the file. "}),
        _chunk({"content": "Reading. "}),
        _chunk({"reasoning_content": "more thought "}),
        _chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                "function": {"name": "Read", "arguments": ""}}]}),
        _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"file_path": "a.txt"}'}}]}),
        _chunk({}, "tool_calls"),
        _chunk({}, None, {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80}),
        "data: [DONE]\n\n",
    ]


class _Upstream:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                owner.requests.append(json.loads(self.rfile.read(length)))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for piece in _reasoning_then_tool_stream():
                    self.wfile.write(piece.encode())
                    self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> _Upstream:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _litellm(tmp_path: Path, config: str):
    executable_value = os.environ.get("AGENTTALK_TEST_LITELLM_EXE")
    if not executable_value:
        pytest.skip("set AGENTTALK_TEST_LITELLM_EXE for the pinned local conformance test")
    executable = Path(executable_value)
    if not executable.is_file():
        pytest.fail("AGENTTALK_TEST_LITELLM_EXE is not a file")
    config_path = tmp_path / "litellm.yaml"
    config_path.write_text(config, encoding="utf-8")
    port = _free_port()
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT"}
    }
    env.update({
        "OVH_KEY": "fake-upstream-key",
        "LITELLM_MASTER_KEY": MASTER,
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
    })
    process = subprocess.Popen(  # nosec B603 - fixed argv, executable is opt-in
        [str(executable), "--config", str(config_path), "--host", "127.0.0.1",
         "--port", str(port), "--num_workers", "1"],
        cwd=tmp_path, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"LiteLLM exited during startup with code {process.returncode}")
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("GET", "/health/liveliness")
            response = connection.getresponse()
            response.read()
            if response.status == 200:
                return process, port
        except OSError:
            pass
        finally:
            connection.close()
        time.sleep(0.25)
    process.kill()
    pytest.fail("LiteLLM did not become live")


def _post(port: int, extra: dict) -> bytes:
    body = {
        "model": MODEL_ALIAS,
        "max_tokens": 1024,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "Read", "description": "read",
                   "input_schema": {"type": "object",
                                    "properties": {"file_path": {"type": "string"}},
                                    "required": ["file_path"]}}],
        **extra,
    }
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        connection.request(
            "POST", "/v1/messages?beta=true", body=json.dumps(body),
            headers={"Authorization": f"Bearer {MASTER}", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == 200
        return response.read()
    finally:
        connection.close()


def _stop(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _events(raw: bytes) -> list[dict]:
    out = []
    for block in raw.decode("utf-8").split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                out.append(json.loads(line[5:]))
    return out


# CLI 2.1.282 sends this on every request, even with MAX_THINKING_TOKENS=0
# (output_config.effort) or unset (thinking adaptive).
CLI_LIKE_REQUEST = {
    "output_config": {"effort": "high"},
    "thinking": {"type": "adaptive", "display": "omitted"},
}


def test_real_litellm_reasoning_is_stripped_before_the_cli_and_tool_call_survives(
    tmp_path,
) -> None:
    with _Upstream() as upstream:
        process, port = _litellm(tmp_path, render_litellm_config(
            api_base=f"http://127.0.0.1:{upstream.port}/v1"
        ))
        try:
            raw = _post(port, CLI_LIKE_REQUEST)
        finally:
            _stop(process)
    stripper = SseReasoningStripper()
    seen = stripper.feed(raw) + stripper.finish()
    events = _events(seen)
    kinds = [e["content_block"]["type"] for e in events if e["type"] == "content_block_start"]
    assert "thinking" not in kinds
    assert "tool_use" in kinds
    assert b'"thinking"' not in seen and b"I should read" not in seen
    indices = [e["index"] for e in events if e["type"] == "content_block_start"]
    assert indices == list(range(len(indices)))
    delta = next(e for e in events if e["type"] == "message_delta")
    assert delta["delta"]["stop_reason"] == "tool_use"
    assert delta["usage"]["output_tokens"] == 30  # provider usage is untouched


def test_real_litellm_drops_cli_effort_and_thinking_but_passes_extra_body_verbatim(
    tmp_path,
) -> None:
    def upstream_body(config_params: dict | None) -> dict:
        with _Upstream() as upstream:
            process, port = _litellm(tmp_path, render_litellm_config(
                api_base=f"http://127.0.0.1:{upstream.port}/v1",
                reasoning_params=config_params,
            ))
            try:
                _post(port, CLI_LIKE_REQUEST)
            finally:
                _stop(process)
            assert len(upstream.requests) == 1
            return upstream.requests[0]

    default = upstream_body(None)
    # What the CLI sends about effort and thinking never reaches OVH...
    for key in ("reasoning_effort", "thinking", "output_config", "chat_template_kwargs"):
        assert key not in default
    assert default["max_tokens"] == 1024

    # ...so the fixed route parameter is the only lever, and it arrives intact.
    configured = upstream_body({
        "reasoning_effort": "low",
        "chat_template_kwargs.enable_thinking": False,
    })
    assert configured["reasoning_effort"] == "low"
    assert configured["chat_template_kwargs"] == {"enable_thinking": False}
