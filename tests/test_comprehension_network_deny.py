"""#55 slice-1 PR-B item 10: the network-deny CI harness
(DESIGN-55-comprehension-plane.md, "Privacy and offline enforcement":
"Release evidence must additionally run the end-to-end scanner with
networking denied outside the process... The test places provider keys
and proxy variables in the parent environment and asserts both zero
connection attempts and their absence in the worker").

Two-sided, per the lead's dispatch on the approved PR-B plan (2026-08-27):
1. The worker's OWN view: run the REAL sanitized worker (the exact
   env-allowlist and subprocess invocation production uses) with fake
   provider/proxy environment variables injected into the PARENT
   environment, and confirm it completes normally.
2. The denial mechanism's OWN view, with a CANARY that proves the bound
   actually triggers: a network probe that succeeds OUTSIDE the denial
   boundary (proving this runner has real baseline connectivity, so a
   later "denied" result isn't just "no network hardware exists here")
   and is DEFINITELY blocked INSIDE it (proving the boundary, not merely
   the absence of an attempt, is what stops egress). A misconfigured rule
   that blocks nothing cannot mirror-pass as green - the canary would
   report REACHABLE in both cases and fail the assertion.

OPT-IN ONLY, gated on ``AGENTTALK_AUTHORIZE_NETWORK_DENY_TEST=1`` - these
tests mutate real OS-level state (a Linux network namespace via
``unshare``, or a Windows Firewall rule via ``netsh advfirewall``) and
must NEVER run as a side effect of an ordinary local ``pytest``/dev-gate
invocation on a developer's or CI runner's persistent machine. Only the
dedicated ``.github/workflows/comprehension-network-deny.yml`` job sets
that variable. Each platform's test also skips cleanly on every OTHER
platform.

NOT verified end-to-end in this development sandbox (documented here
rather than silently claimed): this repo's dev host has no Linux
environment to exercise ``unshare`` at all, and mutating the Windows
Firewall on a persistent developer machine to "test" the mechanism here
would itself be exactly the kind of un-cleaned-up, security-relevant
system change this project is careful never to risk outside a disposable
runner. First real execution is the dedicated CI job on a hosted,
disposable GitHub Actions runner.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from agenttalk.comprehension import worker as workermod

pytestmark = pytest.mark.skipif(
    not os.environ.get("AGENTTALK_AUTHORIZE_NETWORK_DENY_TEST"),
    reason="opt-in only - mutates real OS network-namespace/firewall state; set "
           "AGENTTALK_AUTHORIZE_NETWORK_DENY_TEST=1 (the dedicated CI job does this, "
           "never an ordinary local/dev-gate pytest run)",
)

_FAKE_CREDENTIAL_ENV = {
    "ANTHROPIC_API_KEY": "canary-should-never-reach-the-worker",
    "OPENAI_API_KEY": "canary-should-never-reach-the-worker",
    "HTTP_PROXY": "http://canary-proxy.invalid:8080",
    "HTTPS_PROXY": "http://canary-proxy.invalid:8080",
}

#: A real, stable, well-known TCP endpoint used ONLY by this opt-in canary
#: to prove baseline connectivity - never touched by production code (the
#: comprehension package's own no-socket/no-network-module import scan
#: covers that separately). A canary that cannot even reach this in the
#: "should still work" case would make the "denied" case meaningless.
_CANARY_HOST = "github.com"
_CANARY_PORT = 443
_CANARY_TIMEOUT_SECONDS = 5


def _tcp_probe_argv() -> list[str]:
    probe = (
        "import socket, sys\n"
        f"s = socket.create_connection(({_CANARY_HOST!r}, {_CANARY_PORT}), "
        f"timeout={_CANARY_TIMEOUT_SECONDS})\n"
        "s.close()\n"
        "print('REACHABLE')\n"
    )
    return [sys.executable, "-c", probe]


def _run_probe(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603  # nosec B603
        argv, capture_output=True, text=True,
        timeout=_CANARY_TIMEOUT_SECONDS + 15, check=False, **kwargs,
    )


def _worker_probe_argv(root: Path) -> tuple[list[str], str, dict[str, str]]:
    """This must build the EXACT same env and argv shape
    ``worker.run_sanitized_worker`` itself does. Cannot call
    ``run_sanitized_worker`` directly - it has no sudo/unshare wrapping
    point - so this replicates it instead. N6 (third cold read, fix round
    5): the argv half of that replica used to be a hand-copied literal
    (``[sys.executable, "-s", "-S", "-m", ...]``) that silently drifted
    out of sync with production's own list when round 4 added ``-s``/
    ``-S`` there - exactly the "test proves a replica, not production"
    gap a cold read had to catch by hand. Calling
    ``workermod._worker_subprocess_argv()`` here instead makes that class
    of drift structurally impossible: this is no longer A copy, it is
    THE list production itself launches with."""
    relative_paths = ["a.java"]
    (root / "a.java").write_text("package p;\nclass A {}\n", encoding="utf-8")
    payload = json.dumps({"root": str(root), "relative_paths": relative_paths})
    env = workermod.sanitized_worker_env({**os.environ, **_FAKE_CREDENTIAL_ENV})
    env["PYTHONPATH"] = workermod._derive_child_import_root()
    argv = workermod._worker_subprocess_argv()
    return argv, payload, env


def _linux_env_delivery_argv(env: dict[str, str], inner_argv: list[str]) -> list[str]:
    """N1 (cold-read, PR-B fix round 3): `sudo`'s default `env_reset`
    sudoers policy (the default on Debian/Ubuntu, including GitHub's
    hosted runners) discards whatever ``subprocess.run``'s own ``env=``
    parameter sets for the ``sudo`` invocation itself before it execs the
    target command - it does NOT propagate through to the worker
    regardless of what env dict this test process passed, so the prior
    version of this test almost certainly exercised only sudo's own
    reset environment, never the sanitized one. ``env -i KEY=value ...``
    (coreutils, present on every POSIX system) sets the child's
    environment EXPLICITLY at the argv level, evaluated by ``env`` itself
    AFTER sudo/unshare have already applied their own privilege/namespace
    changes - policy-independent, unlike relying on ``sudo -E`` or a
    sudoers ``env_keep`` entry that may or may not be configured on a
    given runner."""
    assignments = [f"{key}={value}" for key, value in sorted(env.items())]
    return ["env", "-i", *assignments, *inner_argv]


def _env_report_argv() -> list[str]:
    """N2 (cold-read, PR-B fix round 3): "the harness must assert the
    canary variables are ABSENT inside the worker" - the worker itself
    never reports its own environment (nor should it gain a test-only
    introspection mode), so this runs the SAME env-delivery mechanism
    (sudo/unshare/env -i) with a small script that self-reports instead
    of the real worker - positive proof of what this exact mechanism
    delivers to whatever it wraps, equivalent to the worker's own view."""
    probe = "import json, os\nprint(json.dumps(dict(os.environ)))\n"
    return [sys.executable, "-c", probe]


# ----------------------------------------------------------- Linux: network namespace

@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only network-deny mechanism")
def test_network_denial_boundary_actually_blocks_egress_on_linux() -> None:
    """The canary: proves the bound triggers, not merely that nothing
    tried to connect. Uses passwordless sudo, as configured on GitHub's
    hosted Ubuntu runners; a runner without it fails fast (`sudo -n`),
    never hangs on a password prompt."""
    canary = _run_probe(_tcp_probe_argv())
    assert "REACHABLE" in canary.stdout, (
        f"baseline connectivity check failed - this runner has no real network "
        f"reachability, so a later 'denied' result would prove nothing: "
        f"stdout={canary.stdout!r} stderr={canary.stderr!r}")

    denied = _run_probe(["sudo", "-n", "unshare", "--net", "--", *_tcp_probe_argv()])
    assert "REACHABLE" not in denied.stdout, (
        f"the network namespace did NOT deny egress - the denial mechanism itself "
        f"is not working: stdout={denied.stdout!r} stderr={denied.stderr!r}")


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only network-deny mechanism")
def test_sanitized_worker_completes_normally_under_linux_network_denial(
    tmp_path: Path,
) -> None:
    """The worker's own view: fake provider credentials/proxy variables in
    the PARENT environment, the real sanitized_worker_env() allowlist, AND
    a real OS-level network namespace with no route out - the worker must
    still complete its (genuinely offline) job normally.

    N1 (cold-read, PR-B fix round 3): the env is now delivered via
    ``env -i KEY=value ...`` on the argv itself (see
    ``_linux_env_delivery_argv``), never via ``subprocess.run``'s own
    ``env=`` parameter on the ``sudo`` invocation - the latter is silently
    discarded by sudo's default env_reset policy before it ever reaches
    the worker."""
    argv, payload, env = _worker_probe_argv(tmp_path)
    delivery_argv = _linux_env_delivery_argv(env, argv)
    result = _run_probe(["sudo", "-n", "unshare", "--net", "--", *delivery_argv], input=payload)
    assert result.returncode == 0, f"worker failed under network denial: {result.stderr}"
    out = json.loads(result.stdout)
    assert out["schema_version"] == workermod.WORKER_SCHEMA_VERSION
    assert [c["relative_path"] for c in out["file_claims"]] == ["a.java"]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux-only network-deny mechanism")
def test_worker_env_delivery_excludes_the_canary_credentials_on_linux() -> None:
    """N2 (cold-read, PR-B fix round 3): positive proof that the canary
    provider-key/proxy variables are ABSENT from what this exact
    sudo/unshare/env delivery mechanism hands to a child process - not
    merely that the worker happened to complete successfully (which it
    would do regardless of whether the canary leaked, since the worker
    never reads those variables itself either way)."""
    env = workermod.sanitized_worker_env({**os.environ, **_FAKE_CREDENTIAL_ENV})
    delivery_argv = _linux_env_delivery_argv(env, _env_report_argv())
    result = _run_probe(["sudo", "-n", "unshare", "--net", "--", *delivery_argv])
    assert result.returncode == 0, f"env-report probe failed: {result.stderr}"
    observed_env = json.loads(result.stdout)
    for key in _FAKE_CREDENTIAL_ENV:
        assert key not in observed_env, (
            f"{key} leaked through the sudo/unshare/env delivery mechanism into the "
            f"child process - the canary must never reach it")
    assert set(observed_env) <= set(env), (
        f"the child process observed variables beyond what was explicitly delivered: "
        f"{set(observed_env) - set(env)}")


# ----------------------------------------------------------- Windows: firewall rule

def _add_windows_block_rule(*, program: str, rule_name: str) -> None:
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["netsh", "advfirewall", "firewall", "add", "rule",
         f"name={rule_name}", "dir=out", "action=block", f"program={program}", "enable=yes"],
        check=True, capture_output=True, text=True, timeout=30,
    )


def _remove_windows_block_rule(*, rule_name: str) -> None:
    subprocess.run(  # noqa: S603,S607  # nosec B603 B607
        ["netsh", "advfirewall", "firewall", "delete", "rule", f"name={rule_name}"],
        check=False, capture_output=True, text=True, timeout=30,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only network-deny mechanism")
def test_network_denial_boundary_actually_blocks_egress_on_windows() -> None:
    """Same canary discipline as the Linux leg: prove the rule genuinely
    blocks a real outbound TCP connection scoped to this exact program
    path, not merely that our own code never tries one. GitHub's hosted
    Windows runners execute workflow steps with administrative rights, so
    `netsh advfirewall` is self-serviceable inside the job with no
    operator-side machine or repo-settings change."""
    canary = _run_probe(_tcp_probe_argv())
    assert "REACHABLE" in canary.stdout, (
        f"baseline connectivity check failed - this runner has no real network "
        f"reachability, so a later 'denied' result would prove nothing: "
        f"stdout={canary.stdout!r} stderr={canary.stderr!r}")

    rule_name = f"agenttalk-network-deny-canary-{uuid.uuid4().hex[:8]}"
    _add_windows_block_rule(program=sys.executable, rule_name=rule_name)
    try:
        denied = _run_probe(_tcp_probe_argv())
        assert "REACHABLE" not in denied.stdout, (
            f"the firewall rule did NOT deny egress - the denial mechanism itself is "
            f"not working: stdout={denied.stdout!r} stderr={denied.stderr!r}")
    finally:
        _remove_windows_block_rule(rule_name=rule_name)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only network-deny mechanism")
def test_worker_env_delivery_excludes_the_canary_credentials_on_windows() -> None:
    """N2 (cold-read, PR-B fix round 3): the Windows leg has no sudo/
    env_reset concern (subprocess.run's own env= parameter sets the child
    process's environment directly, with no intermediate reset-happy
    layer), but the same positive proof is still owed here - the worker
    completing successfully does not by itself demonstrate the canary
    never reached it, since the worker never reads those variables
    either way."""
    env = workermod.sanitized_worker_env({**os.environ, **_FAKE_CREDENTIAL_ENV})
    result = _run_probe(_env_report_argv(), env=env)
    assert result.returncode == 0, f"env-report probe failed: {result.stderr}"
    observed_env = json.loads(result.stdout)
    for key in _FAKE_CREDENTIAL_ENV:
        assert key not in observed_env, (
            f"{key} leaked through to the child process - the canary must never reach it")
    assert set(observed_env) <= set(env), (
        f"the child process observed variables beyond what sanitized_worker_env allowed: "
        f"{set(observed_env) - set(env)}")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only network-deny mechanism")
def test_sanitized_worker_completes_normally_under_windows_network_denial(
    tmp_path: Path,
) -> None:
    """The worker's own view under the Windows mechanism: an outbound
    block rule scoped to sys.executable (the exact interpreter the worker
    runs as), fake provider credentials/proxy variables in the parent
    environment, and the real sanitized_worker_env() allowlist."""
    argv, payload, env = _worker_probe_argv(tmp_path)
    rule_name = f"agenttalk-network-deny-worker-{uuid.uuid4().hex[:8]}"
    _add_windows_block_rule(program=sys.executable, rule_name=rule_name)
    try:
        result = _run_probe(argv, input=payload, env=env)
        assert result.returncode == 0, f"worker failed under network denial: {result.stderr}"
        out = json.loads(result.stdout)
        assert out["schema_version"] == workermod.WORKER_SCHEMA_VERSION
        assert [c["relative_path"] for c in out["file_claims"]] == ["a.java"]
    finally:
        _remove_windows_block_rule(rule_name=rule_name)
