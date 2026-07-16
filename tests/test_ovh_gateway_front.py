from __future__ import annotations

import http.client
import io
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agenttalk.ovh_gateway import MODEL_ALIAS, SpendLedger, settlement_cost_micro_eur
from agenttalk.ovh_gateway_front import (
    CONFIG_ERROR_CODE,
    INFRA_ERROR_CODE,
    LEDGER_BLOCKED_CODE,
    PUBLIC_ROUTE,
    FrontConfig,
    GatewayFront,
    StreamUsage,
)


def _listen_port() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        return int(server.server_address[1])
    finally:
        server.server_close()


class FakeUpstream:
    def __init__(
        self,
        *,
        status: int = 200,
        response: bytes | None = None,
        entered: threading.Event | None = None,
        release: threading.Event | None = None,
    ) -> None:
        self.status = status
        self.response = response or (
            b'event: message_start\n'
            b'data: {"type":"message_start","message":{"model":"'
            + MODEL_ALIAS.encode("ascii")
            + b'","usage":{"input_tokens":100}}}\n\n'
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}\n\n'
            b'event: message_delta\n'
            b'data: {"type":"message_delta","usage":{"output_tokens":10}}\n\n'
            b'event: message_stop\n'
            b'data: {"type":"message_stop"}\n\n'
        )
        self.requests: list[dict] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: object) -> None:
                return

            def do_GET(self) -> None:
                if self.path == "/health/liveliness":
                    self.send_response(200)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_error(404)

            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                body = self.rfile.read(length)
                owner.requests.append(
                    {
                        "method": "POST",
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "host": self.headers.get("Host"),
                        "body": json.loads(body),
                    }
                )
                if entered is not None:
                    entered.set()
                if release is not None:
                    release.wait(timeout=10)
                self.send_response(owner.status)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(owner.response)))
                self.end_headers()
                self.wfile.write(owner.response)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> FakeUpstream:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class RunningFront:
    def __init__(self, tmp_path, upstream: FakeUpstream) -> None:
        self.ledger = SpendLedger(tmp_path / "ledger.sqlite3", tmp_path / "install.json")
        self.ledger.initialize(generation="a" * 32)
        public_port = _listen_port()
        self.config = FrontConfig(
            public_token="front-secret",
            internal_token="internal-secret",
            public_port=public_port,
            internal_port=upstream.port,
            request_timeout_seconds=5,
        )
        self.front = GatewayFront(self.config, self.ledger)
        self.server = self.front.make_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> RunningFront:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(
        self,
        method: str = "POST",
        path: str = PUBLIC_ROUTE,
        *,
        body: dict | bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        if body is None:
            body = {
                "model": MODEL_ALIAS,
                "max_tokens": 256,
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            }
        encoded = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body
        request_headers = {
            "Host": self.config.public_host_header,
            "Authorization": "Bearer front-secret",
            "Content-Type": "application/json",
        }
        request_headers.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.config.public_port, timeout=5)
        try:
            conn.request(method, path, body=encoded, headers=request_headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()


def test_stream_usage_accepts_complete_sse_and_json() -> None:
    parser = StreamUsage()
    parser.feed(
        b'data: {"type":"message_start","message":{"model":"'
        + MODEL_ALIAS.encode()
        + b'","usage":{"input_tokens":5}}}\n'
        b'data: {"type":"message_delta","usage":{"output_tokens":2}}\n'
        b'data: {"type":"message_stop"}\n'
    )
    assert parser.finish() == (MODEL_ALIAS, 5, 2)

    parser = StreamUsage()
    parser.feed(
        json.dumps(
            {
                "type": "message",
                "model": MODEL_ALIAS,
                "usage": {"input_tokens": 8, "output_tokens": 3},
            }
        ).encode()
    )
    assert parser.finish() == (MODEL_ALIAS, 8, 3)

    parser = StreamUsage()
    parser.feed(
        b'data: {"type":"message_start","message":{"model":"'
        + MODEL_ALIAS.encode()
        + b'","usage":{"input_tokens":0}}}\n'
        b'data: {"type":"message_delta","usage":{"input_tokens":5,"output_tokens":2}}\n'
        b'data: {"type":"message_stop"}\n'
    )
    assert parser.finish() == (MODEL_ALIAS, 5, 2)


@pytest.mark.parametrize(
    ("method", "path", "headers", "expected"),
    [
        ("GET", PUBLIC_ROUTE, {}, 404),
        ("POST", "/", {}, 404),
        ("POST", "/v1/models", {}, 404),
        ("POST", "/v1/chat/completions", {}, 404),
        ("POST", "/health", {}, 404),
        ("POST", PUBLIC_ROUTE, {"Host": "localhost:4000"}, 400),
        ("POST", PUBLIC_ROUTE, {"Host": ""}, 400),
        ("POST", PUBLIC_ROUTE, {"Origin": "https://example.invalid"}, 403),
        ("POST", PUBLIC_ROUTE, {"Authorization": "Bearer wrong"}, 401),
    ],
)
def test_front_is_exact_default_deny(tmp_path, method, path, headers, expected) -> None:
    with FakeUpstream() as upstream, RunningFront(tmp_path, upstream) as front:
        status, response = front.request(method, path, headers=headers)
        assert status == expected
        assert CONFIG_ERROR_CODE.encode() in response
        assert upstream.requests == []
        assert front.ledger.status()["unresolved"] == []


def test_success_reserves_before_one_internal_attempt_then_settles(tmp_path) -> None:
    with FakeUpstream() as upstream, RunningFront(tmp_path, upstream) as front:
        assert front.front.internal_liveliness() is True
        status, body = front.request()
        assert status == 200
        assert b"message_stop" in body
        assert len(upstream.requests) == 1
        request = upstream.requests[0]
        assert request["path"] == PUBLIC_ROUTE
        assert request["authorization"] == "Bearer internal-secret"
        assert request["host"] == f"127.0.0.1:{upstream.port}"
        assert request["body"]["model"] == MODEL_ALIAS
        ledger = front.ledger.status()
        assert ledger["unresolved"] == []
        assert ledger["current_committed_micro_eur"] == settlement_cost_micro_eur(100, 10)


@pytest.mark.parametrize("upstream_status", [429, 500])
def test_internal_failure_never_echoes_raw_error_and_holds_next_call(
    tmp_path,
    upstream_status,
) -> None:
    raw = b'Authorization: Bearer internal-secret OVH_KEY=provider-secret raw-body'
    with FakeUpstream(
        status=upstream_status,
        response=raw,
    ) as upstream, RunningFront(tmp_path, upstream) as front:
        status, body = front.request()
        assert status == 502
        assert INFRA_ERROR_CODE.encode() in body
        assert b"internal-secret" not in body
        assert b"provider-secret" not in body
        assert len(upstream.requests) == 1
        assert front.ledger.status()["unresolved"][0]["state"] == "uncertain"

        status, body = front.request()
        assert status == 503
        assert LEDGER_BLOCKED_CODE.encode() in body
        assert len(upstream.requests) == 1


def test_request_policy_rejects_wrong_model_and_output_limit_before_reserve(tmp_path) -> None:
    with FakeUpstream() as upstream, RunningFront(tmp_path, upstream) as front:
        status, _ = front.request(body={"model": "other", "max_tokens": 10})
        assert status == 422
        status, _ = front.request(body={"model": MODEL_ALIAS, "max_tokens": 4_097})
        assert status == 422
        status, _ = front.request(body={
            "model": MODEL_ALIAS,
            "max_tokens": 10,
            "api_base": "http://127.0.0.1:1/v1",
            "max_retries": 9,
        })
        assert status == 422
        assert upstream.requests == []
        assert front.ledger.status()["unresolved"] == []


def test_single_permit_covers_transport_through_settlement(tmp_path) -> None:
    entered = threading.Event()
    release = threading.Event()
    with FakeUpstream(entered=entered, release=release) as upstream, RunningFront(
        tmp_path,
        upstream,
    ) as front:
        first: list[tuple[int, bytes]] = []
        worker = threading.Thread(target=lambda: first.append(front.request()))
        worker.start()
        assert entered.wait(timeout=5)

        status, body = front.request()

        assert status == 429
        assert b"ATGW_INFRA_BUSY" in body
        assert len(upstream.requests) == 1
        release.set()
        worker.join(timeout=10)
        assert first and first[0][0] == 200
        assert front.ledger.status()["ready"] is True


class _FakeResponse:
    status = 200

    def __init__(self, body: bytes) -> None:
        self.body = body

    def getheader(self, _name: str) -> str:
        return "text/event-stream"

    def read(self, _size: int = -1) -> bytes:
        body, self.body = self.body, b""
        return body


class _FakeConnection:
    def __init__(
        self,
        response: _FakeResponse | None = None,
        *,
        failure: BaseException | None = None,
    ) -> None:
        self.response = response
        self.failure = failure
        self.request_count = 0

    def request(self, *_args, **_kwargs) -> None:
        self.request_count += 1
        if self.failure is not None:
            raise self.failure

    def getresponse(self) -> _FakeResponse:
        assert self.response is not None
        return self.response

    def close(self) -> None:
        return


class _DisconnectingWriter:
    closed = False

    def write(self, _body: bytes) -> None:
        raise BrokenPipeError("fake client disconnect")

    def flush(self) -> None:
        return


class _FakeHandler:
    def __init__(self, *, disconnect: bool = False) -> None:
        self.wfile = _DisconnectingWriter() if disconnect else io.BytesIO()
        self.close_connection = False
        self.status: int | None = None
        self.error_code: str | None = None

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, _name: str, _value: str) -> None:
        return

    def end_headers(self) -> None:
        return

    def _stable_error(self, status: int, code: str) -> None:
        self.status = status
        self.error_code = code


def _direct_front(tmp_path, connection: _FakeConnection) -> GatewayFront:
    ledger = SpendLedger(tmp_path / "ledger.sqlite3", tmp_path / "install.json")
    ledger.initialize(generation="a" * 32)
    return GatewayFront(
        FrontConfig(public_token="front", internal_token="internal"),
        ledger,
        connection_factory=lambda *_args, **_kwargs: connection,
    )


@pytest.mark.parametrize(
    "failure",
    [socket.timeout("fake timeout"), ConnectionResetError("fake reset")],
)
def test_internal_transport_failure_retains_reservation_and_returns_stable_infra(
    tmp_path,
    failure,
) -> None:
    connection = _FakeConnection(failure=failure)
    front = _direct_front(tmp_path, connection)
    handler = _FakeHandler()

    front._proxy(handler, b"{}")

    assert connection.request_count == 1
    assert handler.status == 502
    assert handler.error_code == INFRA_ERROR_CODE
    assert front.ledger.status()["unresolved"][0]["state"] == "uncertain"


def test_client_disconnect_retains_reservation_after_one_internal_attempt(tmp_path) -> None:
    body = (
        b'data: {"type":"message_start","message":{"model":"'
        + MODEL_ALIAS.encode("ascii")
        + b'","usage":{"input_tokens":5}}}\n\n'
        b'data: {"type":"message_delta","usage":{"output_tokens":2}}\n\n'
        b'data: {"type":"message_stop"}\n\n'
    )
    connection = _FakeConnection(_FakeResponse(body))
    front = _direct_front(tmp_path, connection)
    handler = _FakeHandler(disconnect=True)

    front._proxy(handler, b"{}")

    assert connection.request_count == 1
    assert front.ledger.status()["unresolved"][0]["state"] == "uncertain"
