from __future__ import annotations

import copy
import dataclasses
import errno
import gc
import http.client
import io
import json
import socket
import socketserver
import sys
import threading
import time
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agenttalk import ovh_gateway as gateway
from agenttalk import ovh_gateway_front as front_module
from agenttalk.ovh_gateway import (
    MAX_OUTPUT_TOKENS,
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
import gateway_front_helpers as helpers
from gateway_front_helpers import (
    HeldFront,
    HeldSocket,
    assert_bind_refused,
    assert_hand_over,
    held_listener,
    start_held_front,
)


TEST_CHILD_CAP_ISSUER = "atgw-" + "i" * 43
# Hard cumulative test-runtime deadline. A correct hermetic loopback phase that
# takes longer than 30 seconds is intentionally failed; this cannot distinguish
# deadlock from extreme scheduling delay. The value is six times the five-second
# deadline that flaked on loaded Windows runners and matches #94's stall ceiling.
_FRONT_TIMEOUT_LATCH = TimeoutLatch()


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
        listener: HeldSocket | None = None,
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

        # The front's public port is held from bind to teardown: the fixture
        # binds port 0 itself and hands that listening socket to
        # `make_server(listener=...)`, so no other process (for example a second
        # xdist worker) can take the port in between. See gateway_front_helpers.
        held = start_held_front(
            self.ledger,
            upstream.port,
            fake_upstream_connection,
            listener=listener,
        )
        self.front, self.server = held.front, held.server
        self.config = self.front.config
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


def _new_ledger(tmp_path) -> SpendLedger:
    ledger = SpendLedger(tmp_path / "ledger.sqlite3", tmp_path / "install.json")
    ledger.initialize(
        opening_micro_eur=0,
        opening_evidence="held-port front test, observed 2026-10-30",
        generation="a" * 32,
        child_cap_issuer_token=TEST_CHILD_CAP_ISSUER,
    )
    return ledger


def _front_on(port: int, tmp_path, *, internal_port: int = 1) -> GatewayFront:
    config = FrontConfig(
        public_token="front-secret",
        internal_token="internal-secret",
        public_port=port,
        internal_port=internal_port,
        request_timeout_seconds=5,
    )
    return GatewayFront(config, _new_ledger(tmp_path), connection_factory=http.client.HTTPConnection)


def test_running_front_keeps_the_handed_over_port_from_bind_to_teardown(tmp_path) -> None:
    """The fixture used to take a port number from a socket it had closed and bind it again later (a retry loop
    covered the collisions). The listener is now held from bind to teardown: the server uses the very socket that
    was handed over, on the config's port; nothing closes it before the first request was served; a second socket
    cannot bind the port; and the one close is the server's own (the fixture does not close it again). The old
    test counted two port allocations for a retry that no longer exists."""
    listener, _port = held_listener()
    with FakeUpstream() as upstream:
        with RunningFront(tmp_path, upstream, listener=listener) as running:
            held = HeldFront(running.front, running.server, running.config.public_port)

            def serve_one_request() -> None:
                status, _body = running.request()
                assert status == 200

            assert_hand_over(held, listener, serve_one_request)
    assert listener.close_count == 1


def test_a_refused_hand_over_of_the_running_servers_own_socket_does_not_close_it(tmp_path) -> None:
    """`make_server(listener=<the socket the running server serves on>)` is refused ('already exists'), and the
    cleanup of that refusal must not close the live socket: the front keeps serving and the one close is its own."""
    listener, _port = held_listener()
    with FakeUpstream() as upstream:
        with RunningFront(tmp_path, upstream, listener=listener) as running:
            with pytest.raises(gateway.GatewayConfigError, match="already exists"):
                running.front.make_server(listener=running.server.socket)

            assert listener.close_count == 0
            assert listener.fileno() != -1
            assert running.front._server is running.server
            status, _body = running.request()
            assert status == 200
    assert listener.close_count == 1


def test_a_refused_hand_over_of_another_socket_while_a_server_exists_still_closes_that_socket(tmp_path) -> None:
    listener, _port = held_listener()
    other, _other_port = held_listener()
    with FakeUpstream() as upstream:
        with RunningFront(tmp_path, upstream, listener=listener) as running:
            with pytest.raises(gateway.GatewayConfigError, match="already exists"):
                running.front.make_server(listener=other)

            assert other.close_count == 1
            assert listener.close_count == 0
            status, _body = running.request()
            assert status == 200
    assert listener.close_count == 1
    assert other.close_count == 1


def test_start_held_front_holds_its_port_without_any_opt_in(tmp_path) -> None:
    """The proof of the repair that really runs: the helper the LiteLLM conformance test uses (that test skips
    without its opt-in) starts the front here against a fake upstream, and makes the same four checks."""
    listener, _port = held_listener()
    with FakeUpstream() as upstream:
        held = start_held_front(_new_ledger(tmp_path), upstream.port, http.client.HTTPConnection, listener=listener)
        thread = threading.Thread(target=held.server.serve_forever, daemon=True)
        thread.start()
        try:

            def serve_one_request() -> None:
                connection = http.client.HTTPConnection("127.0.0.1", held.port, timeout=10)
                try:
                    connection.request("GET", "/")
                    response = connection.getresponse()
                    response.read()
                    assert response.status == 404
                finally:
                    connection.close()

            assert_hand_over(held, listener, serve_one_request)
        finally:
            held.server.shutdown()
            held.server.server_close()
            thread.join(timeout=10)
    assert not thread.is_alive()
    assert listener.close_count == 1


def _reuse_control_applies() -> bool:
    """Whether a second bind with address reuse set succeeds on a bound, non-listening socket that set it too.

    It does on Windows and on Linux. On macOS (BSD rules) binding the same exact address twice needs SO_REUSEPORT, so
    that second bind is refused and the control below would prove the opposite; the macOS behaviour rests on Apple's
    documentation and kernel source, not on a local run, and the pull request's macOS leg is what confirms it."""
    return sys.platform == "win32" or sys.platform.startswith("linux")


def test_the_hand_over_checks_see_a_port_that_was_released(tmp_path) -> None:
    """Close the held socket before serving, and the check fails: the test can see a release."""
    listener, _port = held_listener()
    with FakeUpstream() as upstream:
        held = start_held_front(_new_ledger(tmp_path), upstream.port, http.client.HTTPConnection, listener=listener)
        listener.close()
        with pytest.raises(AssertionError, match="released before serving"):
            assert_hand_over(held, listener, lambda: None)
        held.server.server_close()

    # and, where the platform allows it (Windows and Linux), a port that is not held does not pass the second-bind
    # check: a bound socket with address reuse set lets a second reusing bind through there. The half above, which
    # detects a closed listener, runs on every platform.
    if not _reuse_control_applies():
        return
    loose = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        loose.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        loose.bind(("127.0.0.1", 0))
        with pytest.raises(AssertionError, match="not held"):
            assert_bind_refused(int(loose.getsockname()[1]))
    finally:
        loose.close()


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("win32", True), ("linux", True), ("linux2", True), ("darwin", False), ("freebsd14", False)],
)
def test_only_windows_and_linux_take_the_loose_socket_control(monkeypatch, platform, expected) -> None:
    # Simulated platforms: only the yes-or-no answer is computed, so nothing else reads the patched value.
    monkeypatch.setattr(sys, "platform", platform)
    assert _reuse_control_applies() is expected


@pytest.mark.parametrize("applies", [True, False, "native"])
def test_the_loose_socket_control_runs_only_where_it_applies(tmp_path, monkeypatch, applies) -> None:
    # The control is a call to assert_bind_refused on a loose socket. It is replaced here by a stand-in that records the
    # call and raises the 'not held' error, so the answer shown is the guard's, and no real bind is made on a platform
    # where the real one would answer differently. 'native' uses the real guard on this machine: on Windows and Linux
    # the control runs (both halves of the test); on macOS it does not, and the release half still does.
    calls = []

    def stand_in(port, host="127.0.0.1"):
        calls.append(port)
        raise AssertionError(f"a second bind to port {port} was accepted: the port is not held")

    expected = _reuse_control_applies() if applies == "native" else applies
    if applies != "native":
        monkeypatch.setattr(sys.modules[__name__], "_reuse_control_applies", lambda: applies)
    monkeypatch.setattr(sys.modules[__name__], "assert_bind_refused", stand_in)

    test_the_hand_over_checks_see_a_port_that_was_released(tmp_path)

    assert bool(calls) is expected


def test_held_listener_sets_the_exclusive_option_before_the_bind_and_never_address_reuse(monkeypatch) -> None:
    """The product sets the exclusive-address option before its bind, only where the platform has it, and never
    address reuse; the helper must do the same. (On a current Windows a held listening socket refuses a second
    bind even without the option, so only the calls themselves can show it.)"""
    calls: list[tuple] = []

    class Recording(HeldSocket):
        def setsockopt(self, *args) -> None:
            calls.append(("setsockopt", *args))
            super().setsockopt(*args)

        def bind(self, address) -> None:
            calls.append(("bind",))
            super().bind(address)

        def listen(self, backlog) -> None:
            calls.append(("listen", backlog))
            super().listen(backlog)

    monkeypatch.setattr(helpers, "HeldSocket", Recording)
    listener, _port = held_listener()
    listener.close()
    names = [call[0] for call in calls]
    assert names.index("bind") < names.index("listen")
    assert ("listen", 5) in calls
    options = [call for call in calls if call[0] == "setsockopt"]
    assert not any(call[2] == socket.SO_REUSEADDR for call in options), "address reuse must never be set"
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        assert ("setsockopt", socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1) in calls
        assert calls.index(("setsockopt", socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)) < calls.index(("bind",))
    else:
        assert options == []


def test_make_server_takes_over_the_listener_and_sets_the_server_names(tmp_path) -> None:
    listener, port = held_listener()
    front = _front_on(port, tmp_path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        server = front.make_server(listener=listener)
        gc.collect()
    assert [w for w in caught if issubclass(w.category, ResourceWarning)] == [], "the server's own socket leaked"
    try:
        assert server.socket is listener
        assert front._server is server
        assert server.server_address == ("127.0.0.1", port)
        assert server.server_port == port
        assert server.server_name == socket.getfqdn("127.0.0.1")
    finally:
        server.server_close()
    assert listener.close_count == 1


def test_make_server_without_a_listener_still_binds_the_config_address_itself(tmp_path, monkeypatch) -> None:
    seen: list[dict] = []

    class Recording(ThreadingHTTPServer):
        def __init__(self, address, handler, **kwargs) -> None:
            seen.append({"address": address, **kwargs})
            raise RuntimeError("stop before binding")

    monkeypatch.setattr(front_module, "ThreadingHTTPServer", Recording)
    front = _front_on(4999, tmp_path)
    with pytest.raises(RuntimeError, match="stop before binding"):
        front.make_server()
    assert seen == [{"address": ("127.0.0.1", 4999)}]


class _SocketModuleWith:
    """What `socketserver` sees as the `socket` module: the real one, but `socket.socket` is `recording`."""

    def __init__(self, recording: type) -> None:
        self.socket = recording

    def __getattr__(self, name: str):
        return getattr(socket, name)


@pytest.mark.parametrize("platform_option", ["native", "absent"])
def test_make_server_without_a_listener_runs_its_own_bind_with_the_exclusive_option_first(
    tmp_path, monkeypatch, platform_option
) -> None:
    """The path every service caller uses: the product's own server class makes, binds and activates its socket.
    A recording socket (nothing is really bound, so no port is taken and no gap is opened) shows the configured
    loopback address, the exclusive-address option set before the bind where the platform has it, and that address
    reuse is not set. `absent` takes the option away, as on Linux and macOS."""
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if platform_option == "absent":
        monkeypatch.delattr(socket, "SO_EXCLUSIVEADDRUSE", raising=False)
        exclusive = None
    calls: list[tuple] = []

    class Recording(socket.socket):
        def setsockopt(self, *args) -> None:
            calls.append(("setsockopt", *args))
            super().setsockopt(*args)

        def bind(self, address) -> None:
            calls.append(("bind", address))
            self.bound = address

        def getsockname(self):
            return self.bound

        def listen(self, backlog) -> None:
            calls.append(("listen", backlog))

    monkeypatch.setattr(socketserver, "socket", _SocketModuleWith(Recording))
    front = _front_on(4999, tmp_path)

    server = front.make_server()

    try:
        assert isinstance(server.socket, Recording)
        assert front._server is server
        assert server.server_address == ("127.0.0.1", 4999)
        names = [call[0] for call in calls]
        assert names.index("bind") < names.index("listen"), calls
        assert ("bind", ("127.0.0.1", 4999)) in calls
        options = [call for call in calls if call[0] == "setsockopt"]
        # Exactly the exclusive option or nothing: no address reuse, no port reuse.
        assert options == ([] if exclusive is None else [("setsockopt", socket.SOL_SOCKET, exclusive, 1)]), calls
        if exclusive is not None:
            assert names.index("setsockopt") < names.index("bind"), calls
    finally:
        server.server_close()


def test_a_listener_on_another_address_is_refused_and_closed(tmp_path) -> None:
    other, _other_port = held_listener()
    wanted, wanted_port = held_listener()
    wanted.close()
    front = _front_on(wanted_port, tmp_path)
    with pytest.raises(gateway.GatewayConfigError, match="address"):
        front.make_server(listener=other)
    assert other.close_count == 1
    assert front._server is None


def _bound_listener_that_cannot_say_if_it_listens(
    monkeypatch, *, ask_error: OSError | None = None, listen_error: OSError | None = None
):
    """A socket bound to a loopback port that is not listening yet, as macOS shows it: asking for SO_ACCEPTCONN
    raises `ask_error` (macOS: ENOPROTOOPT). `listen` is recorded, and raises `listen_error` if given.
    Returns (socket, calls)."""
    monkeypatch.setattr(socket, "SO_ACCEPTCONN", getattr(socket, "SO_ACCEPTCONN", 2), raising=False)
    calls: list[tuple] = []

    class Unreporting(HeldSocket):
        def getsockopt(self, level, option, *args):
            if option == socket.SO_ACCEPTCONN and ask_error is not None:
                raise ask_error
            return super().getsockopt(level, option, *args)

        def listen(self, backlog) -> None:
            calls.append(("listen", backlog))
            if listen_error is not None:
                raise listen_error
            super().listen(backlog)

    bound = Unreporting(socket.AF_INET, socket.SOCK_STREAM)
    bound.bind(("127.0.0.1", 0))
    return bound, calls


def test_a_platform_that_cannot_report_listening_gets_a_listen_call_and_the_same_socket(tmp_path, monkeypatch) -> None:
    """macOS defines SO_ACCEPTCONN but answers ENOPROTOOPT. The hand-over then makes sure the socket listens (a listen
    on a listening BSD socket only sets the backlog) and the server uses that very socket."""
    bound, calls = _bound_listener_that_cannot_say_if_it_listens(
        monkeypatch, ask_error=OSError(errno.ENOPROTOOPT, "Protocol not available")
    )
    front = _front_on(int(bound.getsockname()[1]), tmp_path)

    server = front.make_server(listener=bound)

    try:
        assert server.socket is bound
        assert front._server is server
        assert calls == [("listen", ThreadingHTTPServer.request_queue_size)]
        assert server.server_address == bound.getsockname()
        assert bound.close_count == 0
    finally:
        server.server_close()
    assert bound.close_count == 1


@pytest.mark.parametrize(
    ("ask_error", "listen_error"),
    [
        (OSError(errno.EBADF, "Bad file descriptor"), None),  # any other error from the question is not hidden
        (OSError(errno.ENOPROTOOPT, "Protocol not available"), OSError(errno.EINVAL, "Invalid argument")),
    ],
)
def test_any_other_error_while_checking_that_the_listener_listens_propagates_and_closes_it(
    tmp_path, monkeypatch, ask_error, listen_error
) -> None:
    bound, calls = _bound_listener_that_cannot_say_if_it_listens(
        monkeypatch, ask_error=ask_error, listen_error=listen_error
    )
    front = _front_on(int(bound.getsockname()[1]), tmp_path)

    with pytest.raises(OSError) as raised:
        front.make_server(listener=bound)

    assert raised.value.errno == (listen_error or ask_error).errno
    assert bound.close_count == 1
    assert front._server is None
    assert calls == ([] if listen_error is None else [("listen", ThreadingHTTPServer.request_queue_size)])


def test_a_wrong_socket_is_still_refused_before_any_listen_call_when_the_question_is_unanswerable(
    tmp_path, monkeypatch
) -> None:
    bound, calls = _bound_listener_that_cannot_say_if_it_listens(
        monkeypatch, ask_error=OSError(errno.ENOPROTOOPT, "Protocol not available")
    )
    front = _front_on(int(bound.getsockname()[1]) + 1, tmp_path)  # another port than the socket's

    with pytest.raises(gateway.GatewayConfigError, match="address"):
        front.make_server(listener=bound)

    assert calls == []
    assert bound.close_count == 1


def test_a_listener_that_is_not_listening_is_refused_and_closed(tmp_path) -> None:
    if not hasattr(socket, "SO_ACCEPTCONN"):
        pytest.skip("this platform cannot ask a socket whether it is listening")
    bound = HeldSocket(socket.AF_INET, socket.SOCK_STREAM)
    bound.bind(("127.0.0.1", 0))
    try:
        bound.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
    except OSError as error:
        bound.close()
        if error.errno == errno.ENOPROTOOPT:
            pytest.skip("this platform defines SO_ACCEPTCONN but cannot report it (macOS)")
        raise
    front = _front_on(int(bound.getsockname()[1]), tmp_path)
    with pytest.raises(gateway.GatewayConfigError, match="listening"):
        front.make_server(listener=bound)
    assert bound.close_count == 1
    assert front._server is None


def test_a_datagram_socket_is_refused_and_closed(tmp_path) -> None:
    datagram = HeldSocket(socket.AF_INET, socket.SOCK_DGRAM)
    datagram.bind(("127.0.0.1", 0))
    front = _front_on(int(datagram.getsockname()[1]), tmp_path)
    with pytest.raises(gateway.GatewayConfigError, match="stream"):
        front.make_server(listener=datagram)
    assert datagram.close_count == 1


def test_a_socket_of_another_family_is_refused_and_closed(tmp_path) -> None:
    try:
        wrong = HeldSocket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        pytest.skip("this system cannot make an IPv6 socket")
    front = _front_on(4998, tmp_path)
    with pytest.raises(gateway.GatewayConfigError, match="IPv4"):
        front.make_server(listener=wrong)
    assert wrong.close_count == 1


def test_a_second_server_is_refused_and_its_listener_closed(tmp_path) -> None:
    first, port = held_listener()
    front = _front_on(port, tmp_path)
    server = front.make_server(listener=first)
    second, _second_port = held_listener()
    try:
        with pytest.raises(gateway.GatewayConfigError, match="already exists"):
            front.make_server(listener=second)
        assert second.close_count == 1
        assert first.close_count == 0
    finally:
        server.server_close()


def test_a_failure_while_building_the_server_closes_the_listener(tmp_path, monkeypatch) -> None:
    listener, port = held_listener()
    front = _front_on(port, tmp_path)

    def boom():
        raise RuntimeError("handler construction failed")

    monkeypatch.setattr(front, "_handler_type", boom)
    with pytest.raises(RuntimeError, match="handler construction failed"):
        front.make_server(listener=listener)
    assert listener.close_count == 1
    assert front._server is None


def test_port_zero_in_the_config_is_still_refused(tmp_path) -> None:
    with pytest.raises(gateway.GatewayConfigError, match="TCP port range"):
        _front_on(0, tmp_path)


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


def test_beta_rich_body_and_child_cap_exhaustion_survive_front_restart(
    tmp_path, monkeypatch
) -> None:
    # CHILD_TURN_MAX_CALLS is 100_000 under the envelope-only caps (item 8) -
    # looping that many real front HTTP round trips would make this test
    # impractically slow. Patch it down before either RunningFront/ledger is
    # created, so the child_cap_policy_hash committed to disk here and the
    # one verified on restart stay consistent throughout.
    monkeypatch.setattr(gateway, "CHILD_TURN_MAX_CALLS", 3)
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


def test_front_refuses_call_past_the_max_calls_cap_without_provider_transport(
    tmp_path, monkeypatch
) -> None:
    assert gateway.CHILD_TURN_MAX_CALLS == 100_000
    # 100_000 real front HTTP round trips is impractical for a unit test and
    # exercises no different code path than a small cap; patch it down
    # before the ledger initializes so the committed child_cap_policy_hash
    # stays consistent for the whole test.
    monkeypatch.setattr(gateway, "CHILD_TURN_MAX_CALLS", 3)
    with FakeUpstream() as upstream, RunningFront(tmp_path, upstream) as front:
        for _ in range(gateway.CHILD_TURN_MAX_CALLS):
            status, _ = front.request(path="/v1/messages?beta=true")
            assert status == 200

        status, body = front.request(path="/v1/messages?beta=true")

        assert status == 403
        assert b"ATGW_CHILD_TURN_CAP_EXCEEDED" in body
        assert len(upstream.requests) == gateway.CHILD_TURN_MAX_CALLS
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
        status, _ = front.request(
            body={"model": MODEL_ALIAS, "max_tokens": MAX_OUTPUT_TOKENS + 1}
        )
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


# ------------------------------------------------------ reasoning stripping


class _ChunkedResponse(_FakeResponse):
    def __init__(
        self,
        body: bytes,
        *,
        content_type: str = "text/event-stream",
        size: int = 23,
    ) -> None:
        super().__init__(body)
        self.content_type = content_type
        self.size = size

    def getheader(self, _name: str) -> str:
        return self.content_type

    def read(self, _size: int = -1) -> bytes:
        chunk, self.body = self.body[: self.size], self.body[self.size :]
        return chunk


def _reasoning_stream() -> bytes:
    from test_ovh_gateway_reasoning import TOOL_STREAM_EVENTS, _sse

    return _sse(*TOOL_STREAM_EVENTS)


def test_front_strips_reasoning_but_settles_on_the_provider_usage(tmp_path) -> None:
    front = _direct_front(tmp_path, _FakeConnection(_ChunkedResponse(_reasoning_stream())))
    handler = _FakeHandler()
    observed: list[tuple[str, int, int]] = []
    front.reasoning_observer = lambda attempt, stats: observed.append(
        (attempt, stats.blocks, stats.chars)
    )

    front._proxy(handler, b"{}", capability=_direct_capability(front))

    sent = handler.wfile.getvalue()
    assert handler.status == 200
    assert b"thinking" not in sent and b"I should read" not in sent
    assert b'"Read"' in sent and b"file_path" in sent  # the tool call is intact
    status = front.ledger.status()
    assert status["unresolved"] == []
    assert status["current_committed_micro_eur"] == settlement_cost_micro_eur(50, 30)
    assert len(observed) == 1 and len(observed[0][0]) == 32
    assert observed[0][1:] == (1, len("I should read the file. "))


def test_front_forwards_reasoning_untouched_when_stripping_is_disabled(tmp_path) -> None:
    raw = _reasoning_stream()
    front = _direct_front(tmp_path, _FakeConnection(_ChunkedResponse(raw)))
    front.config = dataclasses.replace(front.config, strip_reasoning=False)
    handler = _FakeHandler()

    front._proxy(handler, b"{}", capability=_direct_capability(front))

    assert handler.wfile.getvalue() == raw
    assert front.ledger.status()["current_committed_micro_eur"] == settlement_cost_micro_eur(50, 30)


def test_front_leaves_a_reasoning_free_stream_byte_identical_and_reports_nothing(tmp_path) -> None:
    from test_ovh_gateway_reasoning import (
        MESSAGE_DELTA,
        MESSAGE_START,
        MESSAGE_STOP,
        _delta,
        _sse,
        _start,
        _stop,
    )

    raw = _sse(
        MESSAGE_START,
        _start(0), _delta(0, "text_delta", text="plain"), _stop(0),
        {**MESSAGE_DELTA, "delta": {"stop_reason": "end_turn"}}, MESSAGE_STOP,
    )
    front = _direct_front(tmp_path, _FakeConnection(_ChunkedResponse(raw, size=5)))
    handler = _FakeHandler()
    observed: list[object] = []
    front.reasoning_observer = lambda *args: observed.append(args)

    front._proxy(handler, b"{}", capability=_direct_capability(front))

    assert handler.wfile.getvalue() == raw
    assert observed == []


def test_front_strips_reasoning_from_a_non_streaming_reply(tmp_path) -> None:
    body = json.dumps({
        "type": "message", "role": "assistant", "model": MODEL_ALIAS,
        "content": [
            {"type": "thinking", "thinking": "private", "signature": ""},
            {"type": "text", "text": "answer"},
        ],
        "usage": {"input_tokens": 50, "output_tokens": 30},
    }).encode()
    front = _direct_front(
        tmp_path, _FakeConnection(_ChunkedResponse(body, content_type="application/json", size=17))
    )
    handler = _FakeHandler()

    front._proxy(handler, b"{}", capability=_direct_capability(front))

    sent = json.loads(handler.wfile.getvalue())
    assert [block["type"] for block in sent["content"]] == ["text"]
    assert front.ledger.status()["current_committed_micro_eur"] == settlement_cost_micro_eur(50, 30)


def test_a_failing_reasoning_observer_never_affects_settlement_or_the_reply(tmp_path) -> None:
    front = _direct_front(tmp_path, _FakeConnection(_ChunkedResponse(_reasoning_stream())))
    handler = _FakeHandler()

    def broken(_attempt: str, _stats: object) -> None:
        raise RuntimeError("disk full")

    front.reasoning_observer = broken

    front._proxy(handler, b"{}", capability=_direct_capability(front))

    assert handler.status == 200 and b"thinking" not in handler.wfile.getvalue()
    status = front.ledger.status()
    assert status["unresolved"] == []
    assert status["current_committed_micro_eur"] == settlement_cost_micro_eur(50, 30)


# --------------------------------------- the strip can never leave a bare 'reserved'


def _assert_settled_and_open(front: GatewayFront) -> None:
    status = front.ledger.status()
    assert status["unresolved"] == []
    assert status["current_committed_micro_eur"] == settlement_cost_micro_eur(50, 30)


def test_a_lone_surrogate_after_reasoning_still_settles_and_forwards_the_whole_reply(
    tmp_path,
) -> None:
    from test_ovh_gateway_reasoning import _thinking_then_text

    raw = _thinking_then_text("ok \ud83d")
    front = _direct_front(tmp_path, _FakeConnection(_ChunkedResponse(raw)))
    handler = _FakeHandler()

    front._proxy(handler, b"{}", capability=_direct_capability(front))

    assert handler.status == 200
    sent = handler.wfile.getvalue()
    assert sent.endswith(b'"type": "message_stop"}\n\n')
    assert b"ud83d" in sent
    _assert_settled_and_open(front)
    # The next call is not blocked by an unresolved attempt.
    assert front.ledger.open_child_turn(
        agent="qwen-dev-2", message_id="next", request_id="q-next",
        issuer_token=TEST_CHILD_CAP_ISSUER,
    ).token


def test_an_internal_stripper_error_fails_open_and_settles_exactly(tmp_path, monkeypatch) -> None:
    raw = _reasoning_stream()
    front = _direct_front(tmp_path, _FakeConnection(_ChunkedResponse(raw)))
    handler = _FakeHandler()
    observed: list[object] = []
    front.reasoning_observer = lambda _attempt, stats: observed.append(stats)

    def boom(self, block, terminator):
        raise RuntimeError("stripper bug")

    monkeypatch.setattr(front_module.SseReasoningStripper, "_process", boom)

    front._proxy(handler, b"{}", capability=_direct_capability(front))

    assert handler.wfile.getvalue() == raw  # unstripped, but complete
    _assert_settled_and_open(front)
    assert len(observed) == 1 and observed[0].failed is True


def test_an_exception_escaping_the_stripper_call_fails_open_and_settles_exactly(
    tmp_path, monkeypatch
) -> None:
    raw = _reasoning_stream()
    front = _direct_front(tmp_path, _FakeConnection(_ChunkedResponse(raw)))
    handler = _FakeHandler()
    observed: list[object] = []
    front.reasoning_observer = lambda _attempt, stats: observed.append(stats)

    def boom(self, chunk):
        raise RuntimeError("stripper bug")

    monkeypatch.setattr(front_module.SseReasoningStripper, "feed", boom)

    front._proxy(handler, b"{}", capability=_direct_capability(front))

    assert handler.wfile.getvalue() == raw
    _assert_settled_and_open(front)
    assert len(observed) == 1 and observed[0].failed is True


def test_a_json_strip_exception_fails_open_and_settles_exactly(tmp_path, monkeypatch) -> None:
    body = json.dumps({
        "type": "message", "role": "assistant", "model": MODEL_ALIAS,
        "content": [
            {"type": "thinking", "thinking": "private", "signature": ""},
            {"type": "text", "text": "answer"},
        ],
        "usage": {"input_tokens": 50, "output_tokens": 30},
    }).encode()
    front = _direct_front(
        tmp_path, _FakeConnection(_ChunkedResponse(body, content_type="application/json"))
    )
    handler = _FakeHandler()

    def boom(_body: bytes):
        raise RuntimeError("strip bug")

    monkeypatch.setattr(front_module, "strip_reasoning_json", boom)

    front._proxy(handler, b"{}", capability=_direct_capability(front))

    assert handler.wfile.getvalue() == body
    _assert_settled_and_open(front)


def test_any_unexpected_response_path_error_marks_the_attempt_uncertain_never_reserved(
    tmp_path, monkeypatch
) -> None:
    front = _direct_front(tmp_path, _FakeConnection(_ChunkedResponse(_reasoning_stream())))
    handler = _FakeHandler()

    def boom(*_args, **_kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(front.ledger, "settle", boom)

    front._proxy(handler, b"{}", capability=_direct_capability(front))

    unresolved = front.ledger.status()["unresolved"]
    assert [row["state"] for row in unresolved] == ["uncertain"]


def test_a_non_streaming_reply_past_the_buffer_bound_is_forwarded_whole(
    tmp_path, monkeypatch
) -> None:
    body = json.dumps({
        "type": "message", "role": "assistant", "model": MODEL_ALIAS,
        "content": [
            {"type": "thinking", "thinking": "private " * 20, "signature": ""},
            {"type": "text", "text": "answer"},
        ],
        "usage": {"input_tokens": 50, "output_tokens": 30},
    }).encode()
    monkeypatch.setattr(front_module, "_MAX_BUFFERED_JSON_BYTES", 40)
    front = _direct_front(
        tmp_path, _FakeConnection(_ChunkedResponse(body, content_type="application/json", size=16))
    )
    handler = _FakeHandler()

    front._proxy(handler, b"{}", capability=_direct_capability(front))

    assert handler.wfile.getvalue() == body  # held prefix and the rest, unstripped, in order
    _assert_settled_and_open(front)


# ------------------------------------------------ the stream idle limit (#424)


_STREAM_BODY = (
    b'data: {"type":"message_start","message":{"model":"'
    + MODEL_ALIAS.encode("ascii")
    + b'","usage":{"input_tokens":5}}}\n\n'
    b'data: {"type":"message_delta","usage":{"output_tokens":2}}\n\n'
    b'data: {"type":"message_stop"}\n\n'
)
_STREAM_CUT = _STREAM_BODY.index(b'data: {"type":"message_delta"')


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _FakeSocket:
    """Stands in for the socket to LiteLLM: remembers the timeouts it was given."""

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        self.history = [timeout]

    def settimeout(self, value: float) -> None:
        self.timeout = value
        self.history.append(value)


class _SilentThenDataResponse(_FakeResponse):
    """Sends the stream in two parts with `silence` seconds of fake time between them.

    A read that would wait longer than the socket's timeout raises socket.timeout,
    after the fake clock has moved by exactly that timeout, as a real socket does.
    """

    def __init__(self, sock: _FakeSocket, clock: _FakeClock, silence: float) -> None:
        super().__init__(b"")
        self.sock = sock
        self.clock = clock
        self.silence = silence
        self.parts = [_STREAM_BODY[:_STREAM_CUT], _STREAM_BODY[_STREAM_CUT:]]
        self.reads = 0

    def read(self, _size: int = -1) -> bytes:
        self.reads += 1
        if self.reads == 2:
            if self.silence > self.sock.timeout:
                self.clock.now += self.sock.timeout
                raise socket.timeout("fake silent stream")
            self.clock.now += self.silence
        return self.parts.pop(0) if self.parts else b""


class _FakeSocketConnection(_FakeConnection):
    def __init__(self, sock: _FakeSocket, response: _FakeResponse) -> None:
        super().__init__(response)
        self.sock = sock
        self.timeout_when_reply_arrived: float | None = None

    def getresponse(self) -> _FakeResponse:
        self.timeout_when_reply_arrived = self.sock.timeout
        return super().getresponse()


def _silent_stream_front(tmp_path, silence: float):
    clock = _FakeClock()
    sock = _FakeSocket(FrontConfig("a", "b").request_timeout_seconds)
    connection = _FakeSocketConnection(sock, _SilentThenDataResponse(sock, clock, silence))
    front = _direct_front(tmp_path, connection)
    front._clock = clock
    waits: list[dict] = []
    front.stream_wait_observer = waits.append
    return front, connection, sock, waits


def test_the_stream_limit_is_longer_than_the_old_limit_and_named_once() -> None:
    config = FrontConfig(public_token="a", internal_token="b")
    assert config.request_timeout_seconds == 120.0
    assert config.stream_idle_timeout_seconds == front_module.STREAM_IDLE_TIMEOUT_SECONDS == 600.0
    with pytest.raises(gateway.GatewayConfigError, match="stream idle timeout"):
        dataclasses.replace(config, stream_idle_timeout_seconds=0).validate()


def test_a_stream_silent_past_the_old_limit_but_within_the_new_one_settles(tmp_path) -> None:
    front, connection, sock, waits = _silent_stream_front(tmp_path, silence=300.0)
    handler = _FakeHandler()

    front._proxy(handler, b"{}", capability=_direct_capability(front))

    assert handler.status == 200
    assert connection.timeout_when_reply_arrived == 120.0  # connecting and starting: today's limit
    assert sock.history == [120.0, 600.0]  # only the streaming wait is longer
    status = front.ledger.status()
    assert status["unresolved"] == []
    assert status["current_committed_micro_eur"] == settlement_cost_micro_eur(5, 2)
    assert len(waits) == 1
    assert waits[0]["outcome"] == "settled"
    assert waits[0]["max_wait_seconds"] == 300.0
    assert waits[0]["total_seconds"] == 300.0
    assert waits[0]["idle_limit_seconds"] == 600.0


def test_a_stream_silent_past_the_new_limit_is_still_uncertain(tmp_path) -> None:
    front, _connection, _sock, waits = _silent_stream_front(tmp_path, silence=700.0)

    front._proxy(_FakeHandler(), b"{}", capability=_direct_capability(front))

    (attempt,) = front.ledger.status()["unresolved"]
    assert attempt["state"] == "uncertain"
    assert attempt["reason"] == "stream disconnect or timeout"
    assert len(waits) == 1
    assert waits[0]["outcome"] == "uncertain: stream disconnect or timeout"
    assert waits[0]["max_wait_seconds"] == 600.0


def test_a_failing_wait_observer_never_affects_settlement(tmp_path) -> None:
    front, _connection, _sock, _waits = _silent_stream_front(tmp_path, silence=1.0)

    def broken(_entry: dict) -> None:
        raise RuntimeError("observer failed")

    front.stream_wait_observer = broken
    front._proxy(_FakeHandler(), b"{}", capability=_direct_capability(front))

    assert front.ledger.status()["unresolved"] == []


def test_a_reply_that_never_starts_writes_no_wait_record(tmp_path) -> None:
    front = _direct_front(tmp_path, _FakeConnection(failure=socket.timeout("fake timeout")))
    waits: list[dict] = []
    front.stream_wait_observer = waits.append

    front._proxy(_FakeHandler(), b"{}", capability=_direct_capability(front))

    assert waits == []


def test_the_wait_log_line_carries_the_measured_wait(tmp_path) -> None:
    path = tmp_path / "gateway" / "stream-waits.jsonl"
    front, _connection, _sock, _waits = _silent_stream_front(tmp_path, silence=250.0)
    front.stream_wait_observer = front_module.StreamWaitLog(path).record

    front._proxy(_FakeHandler(), b"{}", capability=_direct_capability(front))

    (line,) = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(line)
    assert entry["max_wait_seconds"] == 250.0 and entry["total_seconds"] == 250.0
    assert entry["outcome"] == "settled" and entry["idle_limit_seconds"] == 600.0
    assert len(entry["attempt_id"]) == 32 and entry["ts"].endswith("Z")


def test_a_real_socket_stall_past_the_start_limit_survives_on_the_stream_limit(tmp_path) -> None:
    """The longer limit must reach a real connection whose reply says "Connection: close"."""

    class StallingUpstream(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server API
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()  # an HTTP/1.0 reply: no length, closes at the end
            self.wfile.write(_STREAM_BODY[:_STREAM_CUT])
            self.wfile.flush()
            time.sleep(0.8)  # longer than the 0.3 s start limit used below
            self.wfile.write(_STREAM_BODY[_STREAM_CUT:])

        def log_message(self, *_args: object) -> None:
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), StallingUpstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    try:
        ledger = SpendLedger(tmp_path / "ledger.sqlite3", tmp_path / "install.json")
        ledger.initialize(
            opening_micro_eur=0,
            opening_evidence="fake front fixture, observed 2026-07-16",
            generation="a" * 32,
            child_cap_issuer_token=TEST_CHILD_CAP_ISSUER,
        )
        front = GatewayFront(
            FrontConfig(
                public_token="front",
                internal_token="internal",
                internal_port=int(upstream.server_address[1]),
                request_timeout_seconds=0.3,
                stream_idle_timeout_seconds=10.0,
            ),
            ledger,
        )
        waits: list[dict] = []
        front.stream_wait_observer = waits.append
        handler = _FakeHandler()

        front._proxy(handler, b"{}", capability=_direct_capability(front))

        assert handler.status == 200
        assert ledger.status()["unresolved"] == []
        assert waits[0]["outcome"] == "settled"
        assert 0.6 <= waits[0]["max_wait_seconds"] < 5.0
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)


def test_the_recorded_wait_is_the_gap_between_bytes_not_the_whole_stream(tmp_path) -> None:
    """A chunked reply that trickles in must not look like one long silence (#424 review)."""

    class TricklingUpstream(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _chunk(self, data: bytes) -> None:
            self.wfile.write(f"{len(data):x}\r\n".encode("ascii") + data + b"\r\n")
            self.wfile.flush()

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            pieces = [_STREAM_BODY[:_STREAM_CUT]] + [b": ping\n\n"] * 4 + [_STREAM_BODY[_STREAM_CUT:]]
            for piece in pieces:
                self._chunk(piece)
                time.sleep(0.25)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        def log_message(self, *_args: object) -> None:
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), TricklingUpstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    try:
        ledger = SpendLedger(tmp_path / "ledger.sqlite3", tmp_path / "install.json")
        ledger.initialize(
            opening_micro_eur=0,
            opening_evidence="fake front fixture, observed 2026-07-16",
            generation="a" * 32,
            child_cap_issuer_token=TEST_CHILD_CAP_ISSUER,
        )
        front = GatewayFront(
            FrontConfig(
                public_token="front",
                internal_token="internal",
                internal_port=int(upstream.server_address[1]),
            ),
            ledger,
        )
        waits: list[dict] = []
        front.stream_wait_observer = waits.append

        front._proxy(_FakeHandler(), b"{}", capability=_direct_capability(front))

        assert ledger.status()["unresolved"] == []
        assert waits[0]["total_seconds"] >= 1.2  # six pieces, 0.25 s apart
        assert waits[0]["max_wait_seconds"] < 0.9  # never the whole stream
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)
