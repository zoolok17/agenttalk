from __future__ import annotations

import socket

import pytest

import _socket_harness as socket_harness


_TIMEOUT_NOT_SET = object()


class _StalledConnectSocket:
    """Model a connect that can finish only through its configured timeout."""

    def __init__(self) -> None:
        self.timeout: object = _TIMEOUT_NOT_SET
        self.connect_timeout: object = _TIMEOUT_NOT_SET
        self.closed = False

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout

    def connect(self, _address: tuple[str, int]) -> None:
        self.connect_timeout = self.timeout
        if self.timeout is _TIMEOUT_NOT_SET:
            harness_timeout = socket_harness.COMPLETION_BUDGET_SECONDS
            raise AssertionError(
                f"{harness_timeout:g}s harness watchdog would own stalled connect; product timeout was not armed"
            )
        if self.timeout is None:
            return
        assert isinstance(self.timeout, (int, float))
        raise socket.timeout(f"{self.timeout:g}s product connect timeout")

    def shutdown(self, _how: int) -> None:
        return

    def close(self) -> None:
        self.closed = True


def _install_stalled_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> _StalledConnectSocket:
    stalled = _StalledConnectSocket()
    monkeypatch.setattr(
        socket_harness.socket,
        "socket",
        lambda *_args, **_kwargs: stalled,
    )
    return stalled


def test_tracked_http_connection_arms_product_timeout_before_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stalled = _install_stalled_socket(monkeypatch)
    connection = socket_harness.TrackedHTTPConnection(
        socket_harness.SocketCancellation(),
        "127.0.0.1",
        443,
        timeout=10.0,
    )

    with pytest.raises(socket.timeout, match=r"^10s product connect timeout$"):
        connection.connect()

    assert stalled.connect_timeout == 10.0
    assert stalled.closed


def test_cancellable_socket_selects_deadline_free_mode_before_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stalled = _install_stalled_socket(monkeypatch)

    with socket_harness.cancellable_socket(
        socket_harness.SocketCancellation(),
        "127.0.0.1",
        443,
    ):
        pass

    assert stalled.connect_timeout is None
    assert stalled.closed
