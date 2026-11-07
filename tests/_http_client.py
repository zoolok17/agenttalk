"""An opt-in test client for requests the server refuses early (#226).

The server answers a bad Host, a read-only route or a wrong-port target after the headers, closes the connection and
never reads the body. The standard library sends the headers and a small body in two separate calls, and a pause between
them lets the server close first: on Windows the later read of the response then fails with WinError 10053. A test that
expects such an early refusal asks for `small_body_with_headers`, and the client sends the headers and the small body
in one call. Nothing about the request changes (method, target, headers, Content-Length, body), no request is sent
twice, and a body that is not a small `bytes` (none, over the cap, chunked, a file or an iterable) goes out exactly as
the standard library sends it.
"""

from __future__ import annotations

import http.client

import pytest

SMALL_BODY_LIMIT = 8192  # bytes: ample for the existing payloads; anything larger is sent the ordinary way
_STANDARD_SEND_OUTPUT = http.client.HTTPConnection._send_output


def _send_small_body_with_headers(conn, message_body=None, encode_chunked=False):
    if isinstance(message_body, bytes) and not encode_chunked and len(message_body) <= SMALL_BODY_LIMIT:
        conn._buffer.extend((b"", b""))
        message = b"\r\n".join(conn._buffer) + message_body
        del conn._buffer[:]
        conn.send(message)
    else:
        _STANDARD_SEND_OUTPUT(conn, message_body, encode_chunked=encode_chunked)


@pytest.fixture
def small_body_with_headers(monkeypatch):
    """For the length of one test, `http.client` (and so `urllib`) sends headers and a small bytes body together."""
    monkeypatch.setattr(http.client.HTTPConnection, "_send_output", _send_small_body_with_headers)
