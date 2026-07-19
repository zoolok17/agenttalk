"""Default-deny loopback front for the watched OVH/Qwen trial gateway."""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .ovh_gateway import (
    INTERNAL_HOST,
    INTERNAL_PORT,
    MAX_CONTEXT_TOKENS,
    MAX_OUTPUT_TOKENS,
    MAX_REQUEST_BYTES,
    MODEL_ALIAS,
    PUBLIC_HOST,
    PUBLIC_PORT,
    ChildTurnCapBlocked,
    ChildTurnCapExceeded,
    GatewayConfigError,
    LedgerBlocked,
    LedgerHold,
    PolicyBlocked,
    SpendLedger,
    child_capability_from_header,
)


PUBLIC_ROUTE = "/v1/messages"
INTERNAL_LIVELINESS_ROUTE = "/health/liveliness"
POLICY_BLOCKED_CODE = "ATGW_POLICY_BLOCKED"
LEDGER_BLOCKED_CODE = "ATGW_LEDGER_BLOCKED"
CONFIG_ERROR_CODE = "ATGW_CONFIG_ERROR"
INFRA_ERROR_CODE = "ATGW_INFRA_UNAVAILABLE"
BUSY_CODE = "ATGW_INFRA_BUSY"
CHILD_TURN_CAP_EXCEEDED_CODE = "ATGW_CHILD_TURN_CAP_EXCEEDED"
FORBIDDEN_REQUEST_KEYS = frozenset({
    "api_base",
    "api_key",
    "base_url",
    "custom_llm_provider",
    "deployment_id",
    "extra_headers",
    "fallbacks",
    "headers",
    "max_retries",
    "model_list",
    "num_retries",
    "router_settings",
})


@dataclass(frozen=True)
class FrontConfig:
    public_token: str
    internal_token: str
    public_host: str = PUBLIC_HOST
    public_port: int = PUBLIC_PORT
    internal_host: str = INTERNAL_HOST
    internal_port: int = INTERNAL_PORT
    request_timeout_seconds: float = 120.0
    max_request_bytes: int = MAX_REQUEST_BYTES

    def validate(self) -> None:
        if self.public_host != PUBLIC_HOST or self.internal_host != INTERNAL_HOST:
            raise GatewayConfigError("both gateway listeners must use literal IPv4 loopback")
        if not (1 <= self.public_port <= 65535 and 1 <= self.internal_port <= 65535):
            raise GatewayConfigError("gateway ports must be in the TCP port range")
        if self.public_port == self.internal_port:
            raise GatewayConfigError("public and internal gateway ports must differ")
        if not self.public_token or not self.internal_token:
            raise GatewayConfigError("gateway tokens are required")
        if self.max_request_bytes <= 0 or self.max_request_bytes > MAX_REQUEST_BYTES:
            raise GatewayConfigError("request body limit exceeds the trial policy")
        if self.request_timeout_seconds <= 0:
            raise GatewayConfigError("request timeout must be positive")

    @property
    def public_host_header(self) -> str:
        return f"{self.public_host}:{self.public_port}"


class StreamUsage:
    """Extract authoritative usage from Anthropic JSON or SSE responses."""

    def __init__(self) -> None:
        self.model: str | None = None
        self.input_tokens: int | None = None
        self.output_tokens: int | None = None
        self.complete = False
        self._buffer = bytearray()
        self._body = bytearray()
        self._saw_sse = False

    @staticmethod
    def _valid_count(value: object) -> int | None:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return None

    def _consume_object(self, value: object) -> None:
        if not isinstance(value, dict):
            return
        value_type = value.get("type")
        if value_type == "message_start":
            message = value.get("message")
            if isinstance(message, dict):
                if isinstance(message.get("model"), str):
                    self.model = message["model"]
                usage = message.get("usage")
                if isinstance(usage, dict):
                    count = self._valid_count(usage.get("input_tokens"))
                    if count is not None:
                        self.input_tokens = count
        elif value_type == "message_delta":
            usage = value.get("usage")
            if isinstance(usage, dict):
                count = self._valid_count(usage.get("input_tokens"))
                if count is not None:
                    self.input_tokens = count
                count = self._valid_count(usage.get("output_tokens"))
                if count is not None:
                    self.output_tokens = count
        elif value_type == "message_stop":
            self.complete = True
        elif value_type == "message":
            if isinstance(value.get("model"), str):
                self.model = value["model"]
            usage = value.get("usage")
            if isinstance(usage, dict):
                self.input_tokens = self._valid_count(usage.get("input_tokens"))
                self.output_tokens = self._valid_count(usage.get("output_tokens"))
            self.complete = True

    def feed(self, chunk: bytes) -> None:
        self._body.extend(chunk)
        self._buffer.extend(chunk)
        while b"\n" in self._buffer:
            raw, _, remaining = self._buffer.partition(b"\n")
            self._buffer = bytearray(remaining)
            line = raw.rstrip(b"\r")
            if not line.startswith(b"data:"):
                continue
            self._saw_sse = True
            payload = line[5:].strip()
            if not payload or payload == b"[DONE]":
                continue
            try:
                self._consume_object(json.loads(payload.decode("utf-8")))
            except (UnicodeDecodeError, ValueError):
                continue

    def finish(self) -> tuple[str, int, int]:
        if not self._saw_sse:
            try:
                self._consume_object(json.loads(self._body.decode("utf-8")))
            except (UnicodeDecodeError, ValueError):
                pass
        if (
            not self.complete
            or self.model is None
            or self.input_tokens is None
            or self.output_tokens is None
            or self.input_tokens <= 0
            or self.output_tokens <= 0
        ):
            raise LedgerHold("completed response lacks authoritative usage")
        return self.model, self.input_tokens, self.output_tokens


class GatewayFront:
    """Own one public listener and one conservative provider-attempt permit."""

    def __init__(
        self,
        config: FrontConfig,
        ledger: SpendLedger,
        *,
        connection_factory: Callable[..., http.client.HTTPConnection] = http.client.HTTPConnection,
    ) -> None:
        config.validate()
        self.config = config
        self.ledger = ledger
        self.connection_factory = connection_factory
        self._permit = threading.BoundedSemaphore(1)
        self._server: ThreadingHTTPServer | None = None

    def _stable_body(self, code: str) -> bytes:
        return json.dumps(
            {"type": "error", "error": {"type": code, "message": code}},
            separators=(",", ":"),
        ).encode("ascii")

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        front = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "agenttalk-qwen-front"
            sys_version = ""

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _stable_error(self, status: int, code: str) -> None:
                body = front._stable_body(code)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True

            def _reject_default(self) -> None:
                self._stable_error(404, CONFIG_ERROR_CODE)

            do_GET = _reject_default
            do_PUT = _reject_default
            do_DELETE = _reject_default
            do_PATCH = _reject_default
            do_OPTIONS = _reject_default
            do_HEAD = _reject_default

            def do_POST(self) -> None:
                request_line = self.requestline.split(" ")
                if (
                    len(request_line) != 3
                    or request_line[0] != "POST"
                    or request_line[1] != PUBLIC_ROUTE
                    or self.path != PUBLIC_ROUTE
                ):
                    self._reject_default()
                    return
                host_values = self.headers.get_all("Host") or []
                if len(host_values) != 1 or host_values[0] != front.config.public_host_header:
                    self._stable_error(400, CONFIG_ERROR_CODE)
                    return
                if self.headers.get_all("Origin"):
                    self._stable_error(403, CONFIG_ERROR_CODE)
                    return
                if self.headers.get_all("Transfer-Encoding"):
                    self._stable_error(400, CONFIG_ERROR_CODE)
                    return
                authorization_values = self.headers.get_all("Authorization") or []
                if len(authorization_values) != 1:
                    self._stable_error(400, CONFIG_ERROR_CODE)
                    return
                capability = child_capability_from_header(authorization_values[0])
                if capability is None:
                    self._stable_error(401, CONFIG_ERROR_CODE)
                    return
                content_type_values = self.headers.get_all("Content-Type") or []
                if len(content_type_values) > 1:
                    self._stable_error(400, CONFIG_ERROR_CODE)
                    return
                length_values = self.headers.get_all("Content-Length") or []
                if not length_values:
                    self._stable_error(411, CONFIG_ERROR_CODE)
                    return
                if len(length_values) != 1:
                    self._stable_error(400, CONFIG_ERROR_CODE)
                    return
                raw_length = length_values[0]
                if not raw_length or any(char < "0" or char > "9" for char in raw_length):
                    self._stable_error(411, CONFIG_ERROR_CODE)
                    return
                length = int(raw_length)
                if length <= 0 or length > front.config.max_request_bytes:
                    self._stable_error(413, CONFIG_ERROR_CODE)
                    return
                try:
                    body = self.rfile.read(length)
                except OSError:
                    self._stable_error(400, CONFIG_ERROR_CODE)
                    return
                if len(body) != length:
                    self._stable_error(400, CONFIG_ERROR_CODE)
                    return
                try:
                    request = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    self._stable_error(400, CONFIG_ERROR_CODE)
                    return
                if not isinstance(request, dict) or request.get("model") != MODEL_ALIAS:
                    self._stable_error(422, CONFIG_ERROR_CODE)
                    return
                if any(str(key).casefold() in FORBIDDEN_REQUEST_KEYS for key in request):
                    self._stable_error(422, CONFIG_ERROR_CODE)
                    return
                max_tokens = request.get("max_tokens")
                if (
                    not isinstance(max_tokens, int)
                    or isinstance(max_tokens, bool)
                    or max_tokens < 1
                    or max_tokens > MAX_OUTPUT_TOKENS
                    or length > MAX_CONTEXT_TOKENS
                ):
                    self._stable_error(422, CONFIG_ERROR_CODE)
                    return
                if not front._permit.acquire(blocking=False):
                    self._stable_error(429, BUSY_CODE)
                    return
                try:
                    front._proxy(self, body, capability=capability)
                finally:
                    front._permit.release()

        return Handler

    def _mark_uncertain(self, attempt_id: str, reason: str) -> None:
        try:
            self.ledger.mark_uncertain(attempt_id, reason=reason)
        except Exception:
            # The original durable reservation remains unresolved even when the
            # explanatory update cannot be committed.
            return

    def _proxy(
        self,
        handler: BaseHTTPRequestHandler,
        body: bytes,
        *,
        capability: str,
    ) -> None:
        attempt_id = uuid.uuid4().hex
        public_response_started = False
        try:
            self.ledger.reserve_for_child(attempt_id, capability=capability)
        except ChildTurnCapExceeded:
            handler._stable_error(403, CHILD_TURN_CAP_EXCEEDED_CODE)  # type: ignore[attr-defined]
            return
        except ChildTurnCapBlocked:
            handler._stable_error(403, CHILD_TURN_CAP_EXCEEDED_CODE)  # type: ignore[attr-defined]
            return
        except PolicyBlocked:
            handler._stable_error(429, POLICY_BLOCKED_CODE)  # type: ignore[attr-defined]
            return
        except (LedgerBlocked, LedgerHold):
            handler._stable_error(503, LEDGER_BLOCKED_CODE)  # type: ignore[attr-defined]
            return

        conn: http.client.HTTPConnection | None = None
        try:
            conn = self.connection_factory(
                self.config.internal_host,
                self.config.internal_port,
                timeout=self.config.request_timeout_seconds,
            )
            conn.request(
                "POST",
                PUBLIC_ROUTE,
                body=body,
                headers={
                    "Authorization": f"Bearer {self.config.internal_token}",
                    "Content-Type": "application/json",
                    "Host": f"{self.config.internal_host}:{self.config.internal_port}",
                },
            )
            response = conn.getresponse()
            if response.status != 200:
                response.read(MAX_REQUEST_BYTES)
                self._mark_uncertain(attempt_id, f"internal status {response.status}")
                handler._stable_error(502, INFRA_ERROR_CODE)  # type: ignore[attr-defined]
                return
            content_type = response.getheader("Content-Type") or "text/event-stream"
            handler.send_response(200)
            handler.send_header("Content-Type", content_type)
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Connection", "close")
            handler.end_headers()
            public_response_started = True
            usage = StreamUsage()
            while True:
                chunk = response.read(16 * 1024)
                if not chunk:
                    break
                usage.feed(chunk)
                handler.wfile.write(chunk)
                handler.wfile.flush()
            model, input_tokens, output_tokens = usage.finish()
            self.ledger.settle(
                attempt_id,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
            self._mark_uncertain(attempt_id, "stream disconnect or timeout")
            if not public_response_started and not handler.wfile.closed:
                try:
                    handler._stable_error(502, INFRA_ERROR_CODE)  # type: ignore[attr-defined]
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
        except (OSError, http.client.HTTPException, LedgerBlocked, LedgerHold):
            self._mark_uncertain(attempt_id, "transport or settlement failure")
            if not public_response_started and not handler.wfile.closed:
                try:
                    handler._stable_error(502, INFRA_ERROR_CODE)  # type: ignore[attr-defined]
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
        finally:
            if conn is not None:
                conn.close()
            handler.close_connection = True

    def make_server(self) -> ThreadingHTTPServer:
        if self._server is not None:
            raise GatewayConfigError("front server already exists")

        class Server(ThreadingHTTPServer):
            allow_reuse_address = False
            daemon_threads = True

            def server_bind(self) -> None:
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                super().server_bind()

        self._server = Server(
            (self.config.public_host, self.config.public_port),
            self._handler_type(),
        )
        return self._server

    def internal_liveliness(self) -> bool:
        conn: http.client.HTTPConnection | None = None
        try:
            conn = self.connection_factory(
                self.config.internal_host,
                self.config.internal_port,
                timeout=min(5.0, self.config.request_timeout_seconds),
            )
            conn.request(
                "GET",
                INTERNAL_LIVELINESS_ROUTE,
                headers={
                    "Authorization": f"Bearer {self.config.internal_token}",
                    "Host": f"{self.config.internal_host}:{self.config.internal_port}",
                },
            )
            response = conn.getresponse()
            response.read(MAX_REQUEST_BYTES)
            return response.status == 200
        except (OSError, http.client.HTTPException):
            return False
        finally:
            if conn is not None:
                conn.close()

    def drain(self, timeout_seconds: float) -> bool:
        """Wait until the sole request permit is idle without starting work."""
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while time.monotonic() < deadline:
            if self._permit.acquire(blocking=False):
                self._permit.release()
                return True
            time.sleep(0.05)
        return False
