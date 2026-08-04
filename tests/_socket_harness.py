from __future__ import annotations

import http.client
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TypeVar


COMPLETION_BUDGET_SECONDS = 30.0
CANCELLATION_JOIN_SECONDS = 5.0

_T = TypeVar("_T")
CompletionWait = Callable[[threading.Event, float], bool]


def _event_wait(event: threading.Event, timeout: float) -> bool:
    return event.wait(timeout=timeout)


def abort_socket(connection: socket.socket) -> None:
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        connection.close()
    except OSError:
        pass


def run_cleanup_steps(*steps: Callable[[], None]) -> None:
    """Run every teardown step before surfacing the first cleanup failure."""

    errors: list[BaseException] = []
    for step in steps:
        try:
            step()
        except BaseException as exc:
            errors.append(exc)
    if errors:
        raise errors[0]


class SocketCancellation:
    """Own sockets that must be interruptible from a supervising thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._sockets: set[socket.socket] = set()
        self.sockets: list[socket.socket] = []

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def register(self, connection: socket.socket) -> None:
        with self._lock:
            cancelled = self._cancelled
            self.sockets.append(connection)
            if not cancelled:
                self._sockets.add(connection)
        if cancelled:
            abort_socket(connection)
            raise ConnectionAbortedError("socket operation was cancelled")

    def unregister(self, connection: socket.socket) -> None:
        with self._lock:
            self._sockets.discard(connection)

    @contextmanager
    def track(self, connection: socket.socket) -> Iterator[socket.socket]:
        self.register(connection)
        try:
            yield connection
        finally:
            self.unregister(connection)

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            sockets = tuple(self._sockets)
        for connection in sockets:
            abort_socket(connection)


class TimeoutLatch:
    """Fail later socket cases promptly after the first cleaned-up deadline."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._first_timeout: str | None = None

    def ensure_clear(self) -> None:
        with self._lock:
            first_timeout = self._first_timeout
        if first_timeout is not None:
            raise AssertionError(
                f"socket harness already exceeded its deadline in {first_timeout}"
            )

    def trip(self, label: str) -> None:
        with self._lock:
            if self._first_timeout is None:
                self._first_timeout = label


class CompletionBudget:
    """One hard cumulative deadline for a collected test's socket phase."""

    def __init__(
        self,
        *,
        seconds: float = COMPLETION_BUDGET_SECONDS,
        latch: TimeoutLatch | None = None,
        wait_for_completion: CompletionWait = _event_wait,
    ) -> None:
        self.seconds = seconds
        self.latch = latch or TimeoutLatch()
        self._wait_for_completion = wait_for_completion
        self._deadline: float | None = None
        self._lock = threading.Lock()

    def ensure_clear(self) -> None:
        self.latch.ensure_clear()

    def wait(self, event: threading.Event) -> bool:
        self.ensure_clear()
        with self._lock:
            if self._deadline is None:
                self._deadline = time.monotonic() + self.seconds
            remaining = max(0.0, self._deadline - time.monotonic())
        return self._wait_for_completion(event, remaining)

    def timeout(self, label: str) -> AssertionError:
        self.latch.trip(label)
        return AssertionError(
            f"{label} exceeded the {self.seconds:g}s cumulative socket-phase "
            "deadline; a correct but slower operation is intentionally failed"
        )


def _join_worker(worker: threading.Thread, *, label: str) -> None:
    worker.join(timeout=CANCELLATION_JOIN_SECONDS)
    assert not worker.is_alive(), f"{label} client worker survived cancellation"


def _cancel_and_join(
    cancellation: SocketCancellation,
    worker: threading.Thread,
    *,
    label: str,
    on_cancel: Callable[[], None] | None,
) -> None:
    cancellation.cancel()
    try:
        if on_cancel is not None:
            on_cancel()
    finally:
        _join_worker(worker, label=label)


def await_completed(
    operation: Callable[[SocketCancellation], _T],
    *,
    budget: CompletionBudget,
    label: str,
    cancellation: SocketCancellation | None = None,
    on_cancel: Callable[[], None] | None = None,
    workers: list[threading.Thread] | None = None,
) -> _T:
    budget.ensure_clear()
    cancellation = cancellation or SocketCancellation()
    completed = threading.Event()
    results: list[_T] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(operation(cancellation))
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()

    worker = threading.Thread(
        target=run,
        name=f"{label} client",
        daemon=True,
    )
    if workers is not None:
        workers.append(worker)
    worker.start()
    try:
        completed_in_budget = budget.wait(completed)
    except BaseException:
        _cancel_and_join(
            cancellation,
            worker,
            label=label,
            on_cancel=on_cancel,
        )
        raise

    if not completed_in_budget:
        _cancel_and_join(
            cancellation,
            worker,
            label=label,
            on_cancel=on_cancel,
        )
        raise budget.timeout(label)

    # completed is set in the worker's final statement; no blocking operation
    # remains once the green-path condition is observed.
    worker.join()
    assert not worker.is_alive()
    if errors:
        raise errors[0]
    assert len(results) == 1
    return results[0]


@contextmanager
def cancellable_socket(
    cancellation: SocketCancellation,
    host: str,
    port: int,
) -> Iterator[socket.socket]:
    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with cancellation.track(connection):
        try:
            connection.settimeout(None)
            connection.connect((host, port))
            yield connection
        finally:
            abort_socket(connection)


@contextmanager
def cancellable_http_connection(
    cancellation: SocketCancellation,
    host: str,
    port: int,
) -> Iterator[http.client.HTTPConnection]:
    with cancellable_socket(cancellation, host, port) as raw:
        connection = http.client.HTTPConnection(host, port, timeout=None)
        connection.sock = raw
        try:
            yield connection
        finally:
            connection.close()


class TrackedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection whose raw socket remains cancellable through response read."""

    def __init__(
        self,
        cancellation: SocketCancellation,
        *args,
        **kwargs,
    ) -> None:
        self._cancellation = cancellation
        self._tracked_socket: socket.socket | None = None
        super().__init__(*args, **kwargs)

    def connect(self) -> None:
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._cancellation.register(raw)
        self._tracked_socket = raw
        try:
            raw.settimeout(self.timeout)
            raw.connect((self.host, self.port))
            self.sock = raw
        except BaseException:
            self._cancellation.unregister(raw)
            self._tracked_socket = None
            abort_socket(raw)
            raise

    def close(self) -> None:
        tracked_socket = self._tracked_socket
        try:
            super().close()
        finally:
            if tracked_socket is not None:
                self._cancellation.unregister(tracked_socket)
                self._tracked_socket = None


class ServerActivity:
    """Retain exact accepted sockets and handler threads for teardown proof."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._cancelled = False
        self.sockets: list[socket.socket] = []
        self.threads: list[threading.Thread] = []
        self._active_sockets: set[socket.socket] = set()

    def accepted(self, connection: socket.socket) -> None:
        with self._condition:
            self.sockets.append(connection)
            self._active_sockets.add(connection)
            cancelled = self._cancelled
            self._condition.notify_all()
        if cancelled:
            abort_socket(connection)

    def handler_started(self, thread: threading.Thread) -> None:
        with self._condition:
            self.threads.append(thread)
            self._condition.notify_all()

    def socket_closed(self, connection: socket.socket) -> None:
        with self._condition:
            self._active_sockets.discard(connection)
            self._condition.notify_all()

    def cancel_active_sockets(self, *, reject_new: bool = False) -> None:
        with self._condition:
            if reject_new:
                self._cancelled = True
            sockets = tuple(self._active_sockets)
        for connection in sockets:
            abort_socket(connection)

    def wait_stopped(self, *, label: str, deadline: float | None = None) -> None:
        if deadline is None:
            deadline = time.monotonic() + CANCELLATION_JOIN_SECONDS
        with self._condition:
            sockets_closed = self._condition.wait_for(
                lambda: not self._active_sockets,
                timeout=max(0.0, deadline - time.monotonic()),
            )
            threads = tuple(self.threads)
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        assert sockets_closed, f"{label} retained accepted sockets after cancellation"
        assert all(not thread.is_alive() for thread in threads), (
            f"{label} retained handler threads after cancellation"
        )
        assert all(connection.fileno() == -1 for connection in self.sockets), (
            f"{label} retained open accepted sockets after cancellation"
        )


def track_server(server, activity: ServerActivity) -> None:
    process_request = server.process_request
    process_request_thread = server.process_request_thread
    shutdown_request = server.shutdown_request

    def tracked_process_request(connection, client_address) -> None:
        activity.accepted(connection)
        process_request(connection, client_address)

    def tracked_process_request_thread(connection, client_address) -> None:
        activity.handler_started(threading.current_thread())
        process_request_thread(connection, client_address)

    def tracked_shutdown_request(connection) -> None:
        try:
            shutdown_request(connection)
        finally:
            activity.socket_closed(connection)

    server.process_request = tracked_process_request
    server.process_request_thread = tracked_process_request_thread
    server.shutdown_request = tracked_shutdown_request
