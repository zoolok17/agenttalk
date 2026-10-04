"""The opt-in guard for tests that touch the paid gateway's real, fixed ports
(127.0.0.1:4000/4001, ovh_gateway.PUBLIC_PORT/INTERNAL_PORT). Not a test module.

Such a test runs only when AGENTTALK_TEST_GATEWAY_PORTS=1 is set (CI sets it on every
dev-gate leg), and even then it is skipped when another process already holds either
port, as a running gateway does, so it can never reach a live gateway by accident.
The ports are checked once per session, at collection, before any test runs: a port a
test itself leaves held still fails the next test, as before."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable, Mapping

import pytest

OPT_IN_VAR = "AGENTTALK_TEST_GATEWAY_PORTS"
# Under xdist every worker probes at its own collection; two probes of the same free
# port can meet, so a port counts as held only if it is held on every attempt.
PROBE_ATTEMPTS = 3
PROBE_PAUSE_SECONDS = 0.1

Address = tuple[str, int]


def gateway_addresses() -> list[Address]:
    from agenttalk.ovh_gateway import INTERNAL_HOST, INTERNAL_PORT, PUBLIC_HOST, PUBLIC_PORT

    return [(PUBLIC_HOST, PUBLIC_PORT), (INTERNAL_HOST, INTERNAL_PORT)]


def port_is_held(host: str, port: int) -> bool:
    """Whether another socket already holds host:port, found the way the gateway checks
    its own ports (exclusive_bind_probe): a bind on a separate socket, closed at once.
    Nothing ever connects to the port and nothing is sent to it."""
    from agenttalk.ovh_gateway import GatewayConfigError
    from agenttalk.ovh_gateway_service import exclusive_bind_probe

    try:
        exclusive_bind_probe(host, port)
    except GatewayConfigError:
        return True
    return False


def skip_reason(
    environ: Mapping[str, str],
    *,
    addresses: Iterable[Address] | None = None,
    probe: Callable[[str, int], bool] = port_is_held,
    sleep: Callable[[float], None] = time.sleep,
) -> str | None:
    """Why a gateway-port test must not run now, or None when it may."""
    if environ.get(OPT_IN_VAR) != "1":
        return (
            "it binds or calls the paid gateway's real ports 127.0.0.1:4000/4001; "
            f"opt-in: set {OPT_IN_VAR}=1 to run it (CI does)"
        )
    held = list(gateway_addresses() if addresses is None else addresses)
    for attempt in range(PROBE_ATTEMPTS):
        held = [address for address in held if probe(*address)]
        if not held:
            return None
        if attempt + 1 < PROBE_ATTEMPTS:
            sleep(PROBE_PAUSE_SECONDS)
    ports = ", ".join(f"{host}:{port}" for host, port in held)
    return f"{ports} already held by another process (a running gateway?); not run, never contacted"


def base_test_name(item) -> str:
    return getattr(item, "originalname", None) or item.name.split("[", 1)[0]


def apply(
    items,
    names: frozenset[str],
    environ: Mapping[str, str] = os.environ,
    *,
    addresses: Iterable[Address] | None = None,
) -> None:
    """Skip every collected item named in `names` unless the guard lets it run. The
    ports are probed only when such an item is collected and the opt-in is set."""
    guarded = [item for item in items if base_test_name(item) in names]
    if not guarded:
        return
    reason = skip_reason(environ, addresses=addresses)
    if reason is None:
        return
    marker = pytest.mark.skip(reason=f"gateway-port test: {reason}")
    for item in guarded:
        item.add_marker(marker)
