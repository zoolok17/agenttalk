"""Helpers for tests that run a `GatewayFront` on a port they hold from bind to teardown (audit step RP).

The old way took a port number from a socket that was closed at once and bound it again later, so another test
process could take the port in between.  Here the test binds port 0 itself, keeps listening, and hands that very
socket to `GatewayFront.make_server(listener=...)`.  There is no moment at which the port is free.

Ownership: `make_server` owns the handed socket from the moment it is called, and `server_close()` closes it.  The
helpers here never close it again, so a close counted on the socket (`HeldSocket.close_count`) is exact.
"""

from __future__ import annotations

import errno
import socket
from collections.abc import Callable
from http.server import ThreadingHTTPServer
from typing import NamedTuple

from agenttalk.ovh_gateway import SpendLedger
from agenttalk.ovh_gateway_front import FrontConfig, GatewayFront

# socketserver's own default request queue; the product never changes it.
BACKLOG = 5
_REFUSED_ERRNOS = {errno.EADDRINUSE, errno.EACCES}
PUBLIC_TOKEN = "front-secret"
INTERNAL_TOKEN = "internal-secret"


class HeldSocket(socket.socket):
    """A listening socket that counts how often it is closed."""

    close_count = 0

    def close(self) -> None:
        self.close_count += 1
        super().close()


class HeldFront(NamedTuple):
    front: GatewayFront
    server: ThreadingHTTPServer
    port: int


def held_listener(host: str = "127.0.0.1") -> tuple[HeldSocket, int]:
    """Bind `host` port 0 and listen; return the socket and its port.

    The exclusive-address option is set before the bind only where the platform has it, as the product does
    (`ovh_gateway_front.py`, `make_server`).  Address reuse is never set, so on Linux and macOS a plain held
    bind already refuses a second bind."""
    listener = HeldSocket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind((host, 0))
        listener.listen(BACKLOG)
    except BaseException:
        listener.close()
        raise
    return listener, int(listener.getsockname()[1])


def start_held_front(
    ledger: SpendLedger,
    internal_port: int,
    connection_factory: Callable[..., object],
    *,
    public_token: str = PUBLIC_TOKEN,
    internal_token: str = INTERNAL_TOKEN,
    request_timeout_seconds: float = 5,
    listener: HeldSocket | None = None,
) -> HeldFront:
    """A front whose public socket is a held listener (made here unless `listener` is given).

    Returns the front, its server and the held port.  The caller starts `server.serve_forever`; `server_close()`
    closes the listener."""
    if listener is None:
        listener, port = held_listener()
    else:
        port = int(listener.getsockname()[1])
    try:
        front = GatewayFront(
            FrontConfig(
                public_token=public_token,
                internal_token=internal_token,
                public_port=port,
                internal_port=internal_port,
                request_timeout_seconds=request_timeout_seconds,
            ),
            ledger,
            connection_factory=connection_factory,
        )
    except BaseException:
        listener.close()
        raise
    return HeldFront(front, front.make_server(listener=listener), port)


def assert_bind_refused(port: int, host: str = "127.0.0.1") -> None:
    """A second bind to `host:port` must fail.  It is made with address reuse set, so a refusal shows that the port
    is held, and that it stays held. It does not show the exclusive option: where a plain held listener already
    refuses the bind (a current Windows does), the refusal is the same with or without it. The option is proven by
    the observer test of the socket calls (`test_held_listener_sets_the_exclusive_option_before_the_bind...`)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError as error:
            if error.errno in _REFUSED_ERRNOS:
                return
            raise
        raise AssertionError(f"a second bind to port {port} was accepted: the port is not held")
    finally:
        probe.close()


def assert_hand_over(held: HeldFront, listener: HeldSocket, serve_one_request: Callable[[], None]) -> None:
    """The four checks of a real hand-over, while the front is running:

    1. the server's socket is the very socket that was handed over;
    2. its port is the config's public port;
    3. the socket was not closed before the server had served one request;
    4. a second socket cannot bind that port."""
    assert held.server.socket is listener, "the server does not use the handed-over socket"
    assert held.port == held.front.config.public_port == held.server.server_address[1], "the port differs"
    assert listener.close_count == 0, "the port was released before serving"
    serve_one_request()
    assert listener.close_count == 0, "the port was released before the first request was served"
    assert_bind_refused(held.port)
