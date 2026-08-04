from __future__ import annotations

import copy
import http.client
import io
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agenttalk import ovh_gateway as gateway
from agenttalk.ovh_gateway import (
    MAX_REQUEST_BYTES,
    MODEL_ALIAS,
    SpendLedger,
    settlement_cost_micro_eur,
)
from agenttalk.ovh_gateway_front import (
    CONFIG_ERROR_CODE,
    INFRA_ERROR_CODE,
    LEDGER_BLOCKED_CODE,
    PUBLIC_ROUTE,
    FrontConfig,
    GatewayFront,
    StreamUsage,
)
from _socket_harness import (
    CANCELLATION_JOIN_SECONDS,
    CompletionBudget,
    ServerActivity,
    SocketCancellation,
    TimeoutLatch,
    TrackedHTTPConnection,
    await_completed,
    cancellable_http_connection,
    cancellable_socket,
    run_cleanup_steps,
    track_server,
)


TEST_CHILD_CAP_ISSUER = "atgw-" + "i" * 43
# Hard cumulative test-runtime deadline. A correct hermetic loopback phase that
# takes longer than 30 seconds is intentionally failed; this cannot distinguish
# deadlock from extreme scheduling delay. The value is six times the five-second
# deadline that flaked on loaded Windows runners and matches #94's stall ceiling.
_FRONT_TIMEOUT_LATCH = TimeoutLatch()


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
        self.entered = entered
        self.release = release
        self.activity = ServerActivity()
        self._cancelling = threading.Event()
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
                        "headers": dict(self.headers.items()),
                        "body_bytes": body,
                        "body": json.loads(body),
                    }
                )
                if entered is not None:
                    entered.set()
                if release is not None:
                    release.wait()
                self.send_response(owner.status)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(owner.response)))
                self.end_headers()
                self.wfile.write(owner.response)

        class Server(ThreadingHTTPServer):
            def handle_error(self, request, client_address) -> None:
                if owner._cancelling.is_set():
                    return
                super().handle_error(request, client_address)

        self.server = Server(("127.0.0.1", 0), Handler)
        track_server(self.server, self.activity)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> FakeUpstream:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        deadline = time.monotonic() + CANCELLATION_JOIN_SECONDS

        def stop_listener() -> None:
            if self.thread.is_alive():
                self.server.shutdown()

        def join_listener() -> None:
            if self.thread.ident is not None:
                self.thread.join(timeout=max(0.0, deadline - time.monotonic()))
            assert not self.thread.is_alive(), "fake upstream listener survived teardown"

        run_cleanup_steps(
            lambda: self.cancel_pending(reject_new=True),
            stop_listener,
            self.server.server_close,
            join_listener,
            lambda: self.activity.wait_stopped(
                label="fake upstream",
                deadline=deadline,
            ),
        )

    def cancel_pending(self, *, reject_new: bool = False) -> None:
        self._cancelling.set()
        self.activity.cancel_active_sockets(reject_new=reject_new)
        if self.release is not None:
            self.release.set()

    def wait_idle(self, *, deadline: float | None = None) -> None:
        self.activity.wait_stopped(label="fake upstream", deadline=deadline)
        self._cancelling.clear()


class RunningFront:
    def __init__(
        self,
        tmp_path,
        upstream: FakeUpstream,
        *,
        ledger: SpendLedger | None = None,
        credential=None,
        completion_budget: CompletionBudget | None = None,
    ) -> None:
        self.upstream = upstream
        self.completion_budget = completion_budget or CompletionBudget(
            latch=_FRONT_TIMEOUT_LATCH
        )
        self.client_workers: list[threading.Thread] = []
        self.client_cancellations: list[SocketCancellation] = []
        self._client_lock = threading.Lock()
        self._clients_cancelled = False
        self._active_clients: set[SocketCancellation] = set()
        self._internal_cancellation = SocketCancellation()
        self.activity = ServerActivity()
        self.ledger = ledger or SpendLedger(
            tmp_path / "ledger.sqlite3",
            tmp_path / "install.json",
        )
        if ledger is None:
            self.ledger.initialize(
                opening_micro_eur=0,
                opening_evidence="fake front fixture, observed 2026-07-16",
                generation="a" * 32,
                child_cap_issuer_token=TEST_CHILD_CAP_ISSUER,
            )
        self.credential = credential or self.ledger.open_child_turn(
            agent="qwen-dev-1",
            message_id="front-fixture-message",
            request_id="q-front-fixture",
            issuer_token=TEST_CHILD_CAP_ISSUER,
        )
        public_port = _listen_port()
        self.config = FrontConfig(
            public_token="front-secret",
            internal_token="internal-secret",
            public_port=public_port,
            internal_port=upstream.port,
            request_timeout_seconds=5,
        )

        def fake_upstream_connection(*args, **kwargs) -> http.client.HTTPConnection:
            # GatewayFront still has to request its configured timeout. The
            # hermetic fake transport is governed by the cumulative test budget.
            assert kwargs.pop("timeout") == 5
            return TrackedHTTPConnection(
                self._internal_cancellation,
                *args,
                timeout=None,
                **kwargs,
            )

        self.front = GatewayFront(
            self.config,
            self.ledger,
            connection_factory=fake_upstream_connection,
        )
        self.server = self.front.make_server()
        track_server(self.server, self.activity)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> RunningFront:
        self.completion_budget.ensure_clear()
        self.thread.start()
        # Readiness barrier: serve_forever() starts accepting asynchronously, and
        # on a loaded runner (esp. Windows) a request can otherwise race a
        # not-yet-accepting / just-recycled socket -> ConnectionAbortedError. Block
        # until the listener actually accepts a connection before returning.
        deadline = time.monotonic() + 5.0
        while True:
            try:
                with socket.create_connection(
                    ("127.0.0.1", self.config.public_port), timeout=0.5
                ):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("front did not become ready within 5s") from None
                time.sleep(0.02)
        return self

    def __exit__(self, *_args: object) -> None:
        deadline = time.monotonic() + CANCELLATION_JOIN_SECONDS

        def stop_listener() -> None:
            if self.thread.is_alive():
                self.server.shutdown()

        def join_listener() -> None:
            if self.thread.ident is not None:
                self.thread.join(timeout=max(0.0, deadline - time.monotonic()))
            assert not self.thread.is_alive(), "gateway front listener survived teardown"

        def join_clients() -> None:
            for worker in self.client_workers:
                worker.join(timeout=max(0.0, deadline - time.monotonic()))
            assert all(not worker.is_alive() for worker in self.client_workers), (
                "gateway front retained client workers after cancellation"
            )

        run_cleanup_steps(
            self.cancel_active_requests,
            stop_listener,
            self.server.server_close,
            join_listener,
            lambda: self.activity.wait_stopped(
                label="gateway front",
                deadline=deadline,
            ),
            self.upstream.cancel_pending,
            lambda: self.upstream.wait_idle(deadline=deadline),
            join_clients,
        )

    def _register_client(self, cancellation: SocketCancellation) -> None:
        with self._client_lock:
            self.client_cancellations.append(cancellation)
            cancelled = self._clients_cancelled
            if not cancelled:
                self._active_clients.add(cancellation)
        if cancelled:
            cancellation.cancel()
            raise ConnectionAbortedError("gateway front fixture is shutting down")

    def _unregister_client(self, cancellation: SocketCancellation) -> None:
        with self._client_lock:
            self._active_clients.discard(cancellation)

    def _cancel_server_side(self) -> None:
        self._internal_cancellation.cancel()
        self.activity.cancel_active_sockets(reject_new=True)
        self.upstream.cancel_pending()

    def cancel_active_requests(self) -> None:
        with self._client_lock:
            self._clients_cancelled = True
            clients = tuple(self._active_clients)
        for cancellation in clients:
            cancellation.cancel()
        self._cancel_server_side()

    def _await(self, operation, *, label: str):
        cancellation = SocketCancellation()
        self._register_client(cancellation)
        try:
            return await_completed(
                operation,
                budget=self.completion_budget,
                label=label,
                cancellation=cancellation,
                on_cancel=self._cancel_server_side,
                workers=self.client_workers,
            )
        finally:
            self._unregister_client(cancellation)

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
            "Authorization": f"Bearer {self.credential.token}",
            "Content-Type": "application/json",
        }
        request_headers.update(headers or {})

        def exchange(cancellation: SocketCancellation) -> tuple[int, bytes]:
            # Retry ONLY connection-level aborts (transient localhost socket races
            # on loaded Windows runners) - never an HTTP response, so no status
            # assertion is ever masked.
            last_exc: OSError | None = None
            for _ in range(5):
                try:
                    with cancellable_http_connection(
                        cancellation,
                        "127.0.0.1",
                        self.config.public_port,
                    ) as conn:
                        conn.request(
                            method,
                            path,
                            body=encoded,
                            headers=request_headers,
                        )
                        response = conn.getresponse()
                        return response.status, response.read()
                except (
                    ConnectionAbortedError,
                    ConnectionResetError,
                    ConnectionRefusedError,
                    socket.timeout,
                ) as exc:
                    if cancellation.cancelled:
                        raise
                    last_exc = exc
                    time.sleep(0.05)
            raise last_exc if last_exc is not None else RuntimeError("request failed")

        return self._await(exchange, label="front request")

    def raw_request(self, request: bytes) -> tuple[int, bytes]:
        def exchange(cancellation: SocketCancellation) -> tuple[int, bytes]:
            with cancellable_socket(
                cancellation,
                "127.0.0.1",
                self.config.public_port,
            ) as connection:
                connection.sendall(request)
                connection.shutdown(socket.SHUT_WR)
                chunks: list[bytes] = []
                while chunk := connection.recv(16 * 1024):
                    chunks.append(chunk)
            response = b"".join(chunks)
            status = int(response.split(b" ", 2)[1])
            return status, response

        return self._await(exchange, label="raw front request")

    def internal_liveliness(self) -> bool:
        return self._await(
            lambda _cancellation: self.front.internal_liveliness(),
            label="front internal liveliness probe",
        )


def _rich_claude_code_body(*, stream: bool) -> dict:
    tools = [
        {
            "name": f"repository_tool_{index}",
            "description": "Inspect and modify repository state. " + ("x" * 5_000),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "instructions": {"type": "string"},
                },
                "required": ["path"],
            },
        }
        for index in range(31)
    ]
    return {
        "model": MODEL_ALIAS,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": "Inspect the repository."}],
            }
        ],
        "system": [
            {
                "type": "text",
                "text": "Follow the repository policy.\n" + ("s" * 16_384),
            }
        ],
        "tools": tools,
        "metadata": {"user_id": "agenttalk-qwen-dev-1"},
        "max_tokens": 4_096,
        "thinking": {"type": "enabled", "budget_tokens": 1_024},
        "context_management": {
            "edits": [{"type": "clear_tool_uses_20250919", "keep": "all"}],
        },
        "output_config": {"effort": "high"},
        "stream": stream,
    }


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
        ("POST", "/v1/models?beta=true", {}, 404),
        ("POST", "/v1/chat/completions", {}, 404),
        ("POST", "/v1/messages/?beta=true", {}, 404),
        ("POST", "/v1/./messages?beta=true", {}, 404),
        ("POST", "/v1/messages?beta=false", {}, 404),
        ("POST", "/v1/messages?beta=true&unexpected=1", {}, 404),
        ("POST", "/v1/messages?beta=true&num_retries=100", {}, 404),
        ("POST", "/v1/messages?beta=true&beta=true", {}, 404),
        ("POST", "/v1/messages?num_retries=100", {}, 404),
        ("POST", "/v1/messages?api_base=http%3A%2F%2F127.0.0.1%3A9", {}, 404),
        ("POST", "/v1/messages?unexpected=1", {}, 404),
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


@pytest.mark.parametrize(
    "raw_request",
    [
        (
            b"POST /v1/messages HTTP/1.1\r\n"
            b"Host: {HOST}\r\nHost: {HOST}\r\n"
            b"Authorization: Bearer front-secret\r\nContent-Length: 2\r\n\r\n{}"
        ),
        (
            b"POST /v1/messages HTTP/1.1\r\nHost: {HOST}\r\n"
            b"Authorization: Bearer front-secret\r\n"
            b"Authorization: Bearer front-secret\r\nContent-Length: 2\r\n\r\n{}"
        ),
        (
            b"POST /v1/messages HTTP/1.1\r\nHost: {HOST}\r\n"
            b"Authorization: Bearer front-secret\r\n"
            b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}"
        ),
        (
            b"POST /v1/messages HTTP/1.1\r\nHost: {HOST}\r\n"
            b"Authorization: Bearer front-secret\r\n"
            b"Content-Type: application/json\r\nContent-Type: application/json\r\n"
            b"Content-Length: 2\r\n\r\n{}"
        ),
        (
            b"POST /v1/messages HTTP/1.1\r\nHost: {HOST}\r\n"
            b"Authorization: Bearer front-secret\r\nTransfer-Encoding: chunked\r\n"
            b"Content-Length: 2\r\n\r\n{}"
        ),
    ],
)
def test_front_rejects_duplicate_or_ambiguous_request_framing(
    tmp_path, raw_request
) -> None:
    with FakeUpstream() as upstream, RunningFront(tmp_path, upstream) as front:
        raw_request = raw_request.replace(
            b"{HOST}", front.config.public_host_header.encode("ascii")
        )
        raw_request = raw_request.replace(
            b"front-secret", front.credential.token.encode("ascii")
        )
        status, response = front.raw_request(raw_request)

        assert status == 400
        assert CONFIG_ERROR_CODE.encode() in response
        assert upstream.requests == []
        assert front.ledger.status()["unresolved"] == []


def test_front_rejects_absolute_form_request_target(tmp_path) -> None:
    with FakeUpstream() as upstream, RunningFront(tmp_path, upstream) as front:
        host = front.config.public_host_header.encode("ascii")
        request = (
            b"POST http://" + host + b"/v1/messages HTTP/1.1\r\n"
            b"Host: " + host + b"\r\nAuthorization: Bearer front-secret\r\n"
            b"Content-Length: 2\r\n\r\n{}"
        )
        status, response = front.raw_request(request)

        assert status == 404
        assert CONFIG_ERROR_CODE.encode() in response
        assert upstream.requests == []
        assert front.ledger.status()["unresolved"] == []


def test_front_rejects_body_above_bounded_cap_before_reserve(tmp_path) -> None:
    with FakeUpstream() as upstream, RunningFront(tmp_path, upstream) as front:
        request = (
            b"POST /v1/messages?beta=true HTTP/1.1\r\nHost: "
            + front.config.public_host_header.encode("ascii")
            + b"\r\nAuthorization: Bearer "
            + front.credential.token.encode("ascii")
            + b"\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {MAX_REQUEST_BYTES + 1}\r\n\r\n".encode("ascii")
        )

        status, response = front.raw_request(request)

        assert status == 413
        assert CONFIG_ERROR_CODE.encode() in response
        assert upstream.requests == []
        rejected = front.ledger.status()
        assert rejected["unresolved"] == []
        assert rejected["active_child_turns"][0]["attempt_count"] == 0

        status, _ = front.request(path="/v1/messages?beta=true")
        assert status == 200
        accepted = front.ledger.status()
        assert accepted["active_child_turns"][0]["attempt_count"] == 1


def test_front_accepts_body_at_exact_bounded_cap(tmp_path) -> None:
    empty = json.dumps(
        {"model": MODEL_ALIAS, "max_tokens": 1, "padding": ""},
        separators=(",", ":"),
    ).encode("utf-8")
    body = json.dumps(
        {
            "model": MODEL_ALIAS,
            "max_tokens": 1,
            "padding": "x" * (MAX_REQUEST_BYTES - len(empty)),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(body) == MAX_REQUEST_BYTES

    with FakeUpstream() as upstream, RunningFront(tmp_path, upstream) as front:
        status, _ = front.request(path="/v1/messages?beta=true", body=body)

        assert status == 200
        assert upstream.requests[0]["body_bytes"] == body
        assert front.ledger.status()["active_child_turns"][0]["attempt_count"] == 1


@pytest.mark.parametrize("stream", [True, False])
def test_front_forwards_capped_claude_beta_query_and_rich_body_intact(
    tmp_path,
    stream,
) -> None:
    request_body = _rich_claude_code_body(stream=stream)
    encoded = json.dumps(request_body).encode("utf-8")
    assert MAX_REQUEST_BYTES == 512 * 1024
    assert len(encoded) > 128 * 1024

    with FakeUpstream() as upstream, RunningFront(tmp_path, upstream) as front:
        status, response = front.request(
            path="/v1/messages?beta=true",
            body=encoded,
        )

        assert status == 200
        assert len(encoded) < MAX_REQUEST_BYTES
        assert b"message_stop" in response
        assert len(upstream.requests) == 1
        forwarded = upstream.requests[0]
        assert forwarded["path"] == "/v1/messages?beta=true"
        assert forwarded["body_bytes"] == encoded
        assert forwarded["body"] == request_body
        assert len(forwarded["body"]["tools"]) == 31
        assert set(forwarded["body"]) >= {
            "context_management",
            "messages",
            "metadata",
            "output_config",
            "stream",
            "system",
            "thinking",
            "tools",
        }


def test_beta_rich_body_and_child_cap_exhaustion_survive_front_restart(tmp_path) -> None:
    completion_budget = CompletionBudget(latch=_FRONT_TIMEOUT_LATCH)
    with FakeUpstream() as upstream:
        with RunningFront(
            tmp_path,
            upstream,
            completion_budget=completion_budget,
        ) as first:
            status, _ = first.request(path="/v1/messages?beta=true")
            assert status == 200

        restarted = SpendLedger(
            tmp_path / "ledger.sqlite3",
            tmp_path / "install.json",
        )
        replay = restarted.open_child_turn(
            agent="qwen-dev-1",
            message_id="front-fixture-message",
            request_id="q-front-fixture",
            issuer_token=TEST_CHILD_CAP_ISSUER,
        )
        with RunningFront(
            tmp_path,
            upstream,
            ledger=restarted,
            credential=replay,
            completion_budget=completion_budget,
        ) as second:
            denied, _ = second.request(
                path="/v1/messages?beta=true",
                headers={"Authorization": "Bearer front-secret"},
            )
            status, _ = second.request(
                path="/v1/messages?beta=true",
                body=_rich_claude_code_body(stream=True),
            )
            for _ in range(gateway.CHILD_TURN_MAX_CALLS - 2):
                repeated, _ = second.request(path="/v1/messages?beta=true")
                assert repeated == 200
            capped, capped_body = second.request(path="/v1/messages?beta=true")

            assert denied == 401
            assert status == 200
            assert capped == 403
            assert b"ATGW_CHILD_TURN_CAP_EXCEEDED" in capped_body
            ledger_status = restarted.status()
            assert ledger_status["unresolved"] == []
            assert (
                ledger_status["active_child_turns"][0]["attempt_count"]
                == gateway.CHILD_TURN_MAX_CALLS
            )
            assert len(upstream.requests) == gateway.CHILD_TURN_MAX_CALLS
            assert all(
                request["path"] == "/v1/messages?beta=true"
                for request in upstream.requests
            )


def test_success_reserves_before_one_internal_attempt_then_settles(tmp_path) -> None:
    with FakeUpstream() as upstream, RunningFront(tmp_path, upstream) as front:
        assert front.internal_liveliness() is True
        status, body = front.request(headers={"X-Front-Only": "must-not-forward"})
        assert status == 200
        assert b"message_stop" in body
        assert len(upstream.requests) == 1
        request = upstream.requests[0]
        assert request["path"] == PUBLIC_ROUTE
        assert request["authorization"] == "Bearer internal-secret"
        assert request["host"] == f"127.0.0.1:{upstream.port}"
        assert request["body"]["model"] == MODEL_ALIAS
        downstream_headers = {name.casefold() for name in request["headers"]}
        assert "x-front-only" not in downstream_headers
        assert downstream_headers <= {
            "accept-encoding",
            "authorization",
            "content-length",
            "content-type",
            "host",
        }
        ledger = front.ledger.status()
        assert ledger["unresolved"] == []
        assert ledger["current_committed_micro_eur"] == settlement_cost_micro_eur(100, 10)


def test_static_front_token_is_not_a_paid_transport_bypass(tmp_path) -> None:
    with FakeUpstream() as upstream, RunningFront(tmp_path, upstream) as front:
        status, body = front.request(
            path="/v1/messages?beta=true",
            headers={"Authorization": "Bearer front-secret"},
        )

        assert status == 401
        assert CONFIG_ERROR_CODE.encode() in body
        assert upstream.requests == []
        assert front.ledger.status()["unresolved"] == []


def test_forged_well_formed_child_capability_never_reaches_provider(tmp_path) -> None:
    with FakeUpstream() as upstream, RunningFront(tmp_path, upstream) as front:
        status, body = front.request(
            path="/v1/messages?beta=true",
            headers={"Authorization": "Bearer atgw-child-" + "z" * 43},
        )

        assert status == 403
        assert b"ATGW_CHILD_TURN_CAP_EXCEEDED" in body
        assert upstream.requests == []
        assert front.ledger.status()["unresolved"] == []


def test_front_refuses_ninth_child_call_without_provider_transport(tmp_path) -> None:
    assert gateway.CHILD_TURN_MAX_CALLS == 8
    with FakeUpstream() as upstream, RunningFront(tmp_path, upstream) as front:
        for _ in range(8):
            status, _ = front.request(path="/v1/messages?beta=true")
            assert status == 200

        status, body = front.request(path="/v1/messages?beta=true")

        assert status == 403
        assert b"ATGW_CHILD_TURN_CAP_EXCEEDED" in body
        assert len(upstream.requests) == 8
        assert front.ledger.status()["unresolved"] == []


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


def test_inflight_front_exchange_has_no_socket_deadlines(tmp_path) -> None:
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    results: list[tuple[int, bytes]] = []
    errors: list[BaseException] = []

    with FakeUpstream(entered=entered, release=release) as upstream, RunningFront(
        tmp_path,
        upstream,
    ) as front:
        def request() -> None:
            try:
                results.append(front.request())
            except BaseException as exc:
                errors.append(exc)
            finally:
                completed.set()

        worker = threading.Thread(target=request, daemon=True)
        worker.start()
        try:
            if not front.completion_budget.wait(entered):
                raise front.completion_budget.timeout("provider entry")

            client_sockets = [
                connection
                for cancellation in front.client_cancellations
                for connection in cancellation.sockets
                if connection.fileno() != -1
            ]
            internal_sockets = [
                connection
                for connection in front._internal_cancellation.sockets
                if connection.fileno() != -1
            ]
            assert client_sockets
            assert internal_sockets
            assert all(
                connection.gettimeout() is None
                for connection in client_sockets + internal_sockets
            )
            assert not completed.is_set()
        finally:
            release.set()
            if not completed.is_set():
                try:
                    finished = front.completion_budget.wait(completed)
                except AssertionError:
                    finished = False
                if not finished:
                    front.cancel_active_requests()
            worker.join(timeout=CANCELLATION_JOIN_SECONDS)
            if worker.is_alive():
                front.cancel_active_requests()
                worker.join(timeout=CANCELLATION_JOIN_SECONDS)
            assert not worker.is_alive()

        assert errors == []
        assert results and results[0][0] == 200


def test_single_permit_covers_transport_through_settlement(tmp_path) -> None:
    entered = threading.Event()
    release = threading.Event()
    with FakeUpstream(entered=entered, release=release) as upstream, RunningFront(
        tmp_path,
        upstream,
    ) as front:
        first: list[tuple[int, bytes]] = []
        errors: list[BaseException] = []
        completed = threading.Event()

        def request_first() -> None:
            try:
                first.append(front.request())
            except BaseException as exc:
                errors.append(exc)
            finally:
                completed.set()

        worker = threading.Thread(target=request_first, daemon=True)
        worker.start()
        try:
            if not front.completion_budget.wait(entered):
                raise front.completion_budget.timeout("permit upstream entry")
            status, body = front.request()
        finally:
            release.set()
            if not completed.is_set():
                try:
                    finished = front.completion_budget.wait(completed)
                except AssertionError:
                    finished = False
                if not finished:
                    front.cancel_active_requests()
            worker.join(timeout=CANCELLATION_JOIN_SECONDS)
            if worker.is_alive():
                front.cancel_active_requests()
                worker.join(timeout=CANCELLATION_JOIN_SECONDS)
            assert not worker.is_alive()

        assert errors == []
        assert first and first[0][0] == 200
        assert status == 429
        assert b"ATGW_INFRA_BUSY" in body
        assert len(upstream.requests) == 1
        assert front.ledger.status()["ready"] is True


def test_front_deadline_cancels_hung_exchange_before_teardown(tmp_path) -> None:
    entered = threading.Event()
    release = threading.Event()
    timeout_latch = TimeoutLatch()
    cleanup_steps: list[str] = []

    def expire_after_provider_entry(
        completed: threading.Event,
        remaining: float,
    ) -> bool:
        assert entered.wait(timeout=remaining)
        assert not completed.is_set()
        return False

    completion_budget = CompletionBudget(
        latch=timeout_latch,
        wait_for_completion=expire_after_provider_entry,
    )
    upstream = FakeUpstream(entered=entered, release=release)
    front = RunningFront(
        tmp_path,
        upstream,
        completion_budget=completion_budget,
    )

    original_server_close = front.server.server_close

    def fail_after_server_close() -> None:
        original_server_close()
        cleanup_steps.append("injected server-close failure")
        raise RuntimeError("injected teardown-stage failure")

    original_front_wait_stopped = front.activity.wait_stopped

    def observe_front_cleanup(*, label: str, deadline: float | None = None) -> None:
        cleanup_steps.append("front handlers")
        original_front_wait_stopped(label=label, deadline=deadline)

    original_upstream_wait_stopped = upstream.activity.wait_stopped

    def observe_upstream_cleanup(*, label: str, deadline: float | None = None) -> None:
        cleanup_steps.append("upstream handlers")
        original_upstream_wait_stopped(label=label, deadline=deadline)

    front.server.server_close = fail_after_server_close
    front.activity.wait_stopped = observe_front_cleanup
    upstream.activity.wait_stopped = observe_upstream_cleanup

    with pytest.raises(RuntimeError, match="injected teardown-stage failure"):
        with upstream, front:
            with pytest.raises(
                AssertionError,
                match="30s cumulative socket-phase deadline",
            ):
                front.request()

    assert entered.is_set()
    assert release.is_set()
    assert len(upstream.requests) == 1
    assert cleanup_steps.index("injected server-close failure") < cleanup_steps.index(
        "front handlers"
    )
    assert cleanup_steps.index("front handlers") < cleanup_steps.index(
        "upstream handlers"
    )
    assert front.client_workers
    assert all(not worker.is_alive() for worker in front.client_workers)
    assert front.activity.threads
    assert upstream.activity.threads
    assert all(not thread.is_alive() for thread in front.activity.threads)
    assert all(not thread.is_alive() for thread in upstream.activity.threads)
    assert front.activity.sockets
    assert upstream.activity.sockets
    assert all(connection.fileno() == -1 for connection in front.activity.sockets)
    assert all(connection.fileno() == -1 for connection in upstream.activity.sockets)
    client_sockets = [
        connection
        for cancellation in front.client_cancellations
        for connection in cancellation.sockets
    ]
    assert client_sockets
    assert front._internal_cancellation.sockets
    assert all(connection.fileno() == -1 for connection in client_sockets)
    assert all(
        connection.fileno() == -1
        for connection in front._internal_cancellation.sockets
    )

    ledger_after_teardown = copy.deepcopy(front.ledger.status())
    requests_after_teardown = copy.deepcopy(upstream.requests)
    release.set()
    assert front.ledger.status() == ledger_after_teardown
    assert upstream.requests == requests_after_teardown
    with pytest.raises(AssertionError, match="already exceeded its deadline"):
        timeout_latch.ensure_clear()


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
    ledger.initialize(
        opening_micro_eur=0,
        opening_evidence="fake front fixture, observed 2026-07-16",
        generation="a" * 32,
        child_cap_issuer_token=TEST_CHILD_CAP_ISSUER,
    )
    return GatewayFront(
        FrontConfig(public_token="front", internal_token="internal"),
        ledger,
        connection_factory=lambda *_args, **_kwargs: connection,
    )


def _direct_capability(front: GatewayFront) -> str:
    return front.ledger.open_child_turn(
        agent="qwen-dev-1",
        message_id="direct-front-message",
        request_id="q-direct-front",
        issuer_token=TEST_CHILD_CAP_ISSUER,
    ).token


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

    front._proxy(handler, b"{}", capability=_direct_capability(front))

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

    front._proxy(handler, b"{}", capability=_direct_capability(front))

    assert connection.request_count == 1
    assert front.ledger.status()["unresolved"][0]["state"] == "uncertain"
