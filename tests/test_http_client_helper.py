"""#226: the opt-in client that sends headers and a small body together (see `_http_client`)."""

from __future__ import annotations

import http.client
import socket
import threading

import pytest
from _http_client import _STANDARD_SEND_OUTPUT, SMALL_BODY_LIMIT, small_body_with_headers  # noqa: F401 -- fixture by name


pytestmark = pytest.mark.usefixtures("small_body_with_headers")


def _sends(monkeypatch, *, body=None, encode_chunked=False, method="POST", headers=None):
    """What the client hands to `send`, one entry per call, for this request (nothing goes on the wire)."""
    sent: list[bytes] = []
    monkeypatch.setattr(
        http.client.HTTPConnection,
        "send",
        lambda self, data: sent.append(data.encode() if isinstance(data, str) else bytes(data)),
    )
    conn = http.client.HTTPConnection("127.0.0.1", 1)
    conn.putrequest(method, "/x", skip_host=True)
    for key, value in (headers or {"Host": "h:1", "Content-Length": str(len(body or b""))}).items():
        conn.putheader(key, value)
    conn.endheaders(body, encode_chunked=encode_chunked)
    return sent


def _standard(monkeypatch, **kwargs):
    """The same request sent the ordinary (split) way."""
    with monkeypatch.context() as ordinary:
        ordinary.setattr(http.client.HTTPConnection, "_send_output", _STANDARD_SEND_OUTPUT)
        return _sends(ordinary, **kwargs)


def test_headers_and_a_small_body_go_out_in_one_send_with_nothing_changed(monkeypatch):
    body = b'{"kind": "send"}'
    split = _standard(monkeypatch, body=body)
    assert len(split) == 2  # the ordinary client: headers, then the body

    together = _sends(monkeypatch, body=body)

    assert together == [split[0] + split[1]]  # the very same bytes: Host, Content-Length, separator and body
    assert together[0].endswith(b"\r\n\r\n" + body) and b"Content-Length: 16\r\n" in together[0]


_THREE = {"Host": "h:1", "Content-Length": "3"}


def _case(name):
    """The keyword arguments of one request, made afresh (a file or an iterator is used up by sending it)."""
    import io

    return {
        "no-body": {"method": "GET"},
        "empty-body": {"body": b""},
        "over-the-cap": {"body": b"x" * (SMALL_BODY_LIMIT + 1)},
        "chunked": {"body": b"abc", "encode_chunked": True, "headers": {"Host": "h:1", "Transfer-Encoding": "chunked"}},
        "file-like": {"body": io.BytesIO(b"abc"), "headers": _THREE},
        "iterable": {"body": iter([b"ab", b"c"]), "headers": _THREE},
        "text": {"body": "abc", "headers": _THREE},
    }[name]


@pytest.mark.parametrize("case", ["no-body", "empty-body", "over-the-cap", "chunked", "file-like", "iterable", "text"])
def test_anything_but_a_small_bytes_body_goes_out_exactly_as_the_standard_library_sends_it(monkeypatch, case):
    standard = _standard(monkeypatch, **_case(case))

    assert _sends(monkeypatch, **_case(case)) == standard


def test_a_body_at_the_cap_is_still_sent_with_its_headers(monkeypatch):
    assert len(_sends(monkeypatch, body=b"x" * SMALL_BODY_LIMIT)) == 1


def test_a_connection_that_fails_still_raises_through_the_helper():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # closed again below: nothing listens here
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    with pytest.raises(ConnectionRefusedError):
        conn.request("POST", "/x", body=b"x", headers={"Content-Length": "1"})
    conn.close()


def _refusing_server():
    """A server that reads the headers, answers 403 and closes at once, never reading the body. Returns (port, closed
    event, server socket, thread)."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    closed = threading.Event()

    def serve():
        connection, _ = listener.accept()
        with connection:
            seen = b""
            while b"\r\n\r\n" not in seen:
                seen += connection.recv(4096)
            connection.sendall(b"HTTP/1.0 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        closed.set()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return listener, thread, closed


def test_no_body_is_held_back_until_the_server_has_closed_an_early_refusal(monkeypatch):
    """The window of #226, made certain: any send that carries only the body waits (bounded) for the server to close
    its side before it goes out. With the headers and the body in one send there is no such send, and the refusal is
    read whatever the timing; a client that splits them again would be held and, on Windows, fail the read."""
    listener, thread, closed = _refusing_server()
    held: list[bytes] = []
    real_send = http.client.HTTPConnection.send

    def send(self, data):
        if not data.startswith(b"POST "):  # a body on its own
            held.append(bytes(data))
            closed.wait(5)
        return real_send(self, data)

    monkeypatch.setattr(http.client.HTTPConnection, "send", send)
    conn = http.client.HTTPConnection("127.0.0.1", listener.getsockname()[1], timeout=5)
    try:
        conn.request("POST", "/v2", body=b"x", headers={"Host": "h:1", "Content-Length": "1"})
        assert conn.getresponse().status == 403
    finally:
        conn.close()
        thread.join(5)
        listener.close()
    assert held == []
