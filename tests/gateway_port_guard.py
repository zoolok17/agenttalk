"""The opt-in guard for tests that touch the paid gateway's real, fixed ports
(127.0.0.1:4000/4001, ovh_gateway.PUBLIC_PORT/INTERNAL_PORT). Not a test module.

Such a test runs only when AGENTTALK_TEST_GATEWAY_PORTS=1 is set (CI sets it on every
dev-gate leg), and even then it is skipped when another process already holds either
port, as a running gateway does, so it can never reach a live gateway by accident.

One decision, taken once
------------------------
The question "may the gateway-port tests run now?" is answered ONCE per run, by the
process that owns the run, and everyone else is told the answer:

* a serial run answers at collection, when the first guarded test is collected, as before
  (no guarded test collected: no probe);
* under xdist the CONTROLLER answers once, in `pytest_configure`, before any worker starts, and
  hands the answer to every worker through `workerinput` (the `pytest_configure_node` hook of
  `GuardPlugin`, registered with `optionalhook=True`). The answer is cached, so a restarted worker
  gets the same one and workers never probe for the decision (the owned teardown check, below, is the only
  probing a worker does). A free-port probe is a momentary observation, not a
  reservation: it says the ports were free when the controller looked. It also cannot see every live listener (the
  bind probe is master's, unchanged): on Windows a bind to 127.0.0.1 succeeds beside a listener on the IPv4 wildcard or
  a dual-stack `::`, so "not held" is not proof that nothing listens, and the strict policy's refusal inherits that
  limit. Stop any other gateway before such a run.
* a nested pytest session (PYTEST_CURRENT_TEST is set, because the session was started from a
  running test) never probes the real ports: it is told they are not its to touch, so it cannot
  become a second owner. Its guarded tests are skipped. Known limit: a nested session is recognised only by an
  inherited PYTEST_CURRENT_TEST; one started with that variable cleared looks like an ordinary run (a future
  end-to-end test of the experiment entry point, P6, is meant to cover it).

Interface (the field and option names below are fixed; other code and tests bind to them)
----------------------------------------------------------------------------------------
The cached controller decision, `DECISION_KEY = "gateway_port_decision"` in `config.workerinput`,
is a dict with exactly these fields (`DECISION_FIELDS`, in this order):

  version   int   1
  policy    str   "skip" or "error" (the policy in force; see the selector below)
  opt_in    bool  AGENTTALK_TEST_GATEWAY_PORTS == "1" in the controller's environment
  nested    bool  the controller is itself inside a running test (PYTEST_CURRENT_TEST is set)
  held      list  [[host, port], ...] still held after PROBE_ATTEMPTS tries; [] when free, not
                  opted in, or nested
  probes    int   how many probe calls the controller made (0 without the opt-in or when nested);
                  the audit counts this: it is the "one probe round" of the plan
  reason    str|None  why the guarded tests must not run (the text of the skip), or None when they may

A worker that receives a missing or malformed decision stops the run (pytest.UsageError naming
the key). The decision is never recomputed by a worker.

The selector for the strict policy
----------------------------------
The policy is "error" exactly when pytest was started WITH the option `--experiment-record`
(its destination is `experiment_record`, added by tests/experiment_record.py, PR P3; the value is
not read, only whether it is set), and "skip" otherwise. The experiment entry point (P6) puts
`--experiment-record=<folder>` into the pytest argv of both arms and of every probe, and nothing
else does; the contract's `"gateway_policy": "error"` is what it validates and reports. Where the
option does not exist (P3 not in the tree) the policy is "skip".

Under "error", the process that owns the run decides at `pytest_configure` (serial and controller
alike), and a held port ENDS THE RUN there: `pytest.exit(..., returncode=2)` (2 is the status of a
collection error), before any test is collected and before any worker starts, with the held
host:port in the message. Nothing connects to the port. A missing opt-in or a nested session
still skips, under both policies. Ordinary runs and POSIX runs never have the option and keep the
skip.

The owned teardown check
------------------------
`GuardPlugin` watches every named test (`_GATEWAY_PORT_TEST_NAMES` in the conftest) that does not carry a skip
marker, in the process that runs it (a worker under xdist). It does not take the decision above, which is taken once;
this check probes in the workers, after each test. It has two parts, and a session-level rejection:

* for a test that ran (a test that then xfailed ran too): after its teardown, a child process it started (seen
  through `subprocess.Popen`, and `multiprocessing` children) that is still alive, or a listener still holding one
  of the two ports that is provably the test's own (see "Whose listener it is", below), fails that member's teardown.
  A surviving child process is ended; a listener held in this process is detected, not closed. A teardown failure
  alone does not promise that nothing overlaps later, because the next member can still start, and a marker such as
  xfail can turn the failure into another xfail;
* for a test pytest reported skipped (by a marker, by `skipif`, or by `pytest.skip()` in its body) or stopped before
  its body ran (a skip in setup, or `xfail(run=False)`, which pytest reports as skipped with a `wasxfail` mark): NO port
  probe, so another process holding the ports can never fail it. A test whose body never ran is not probed; an xfail
  whose body did run keeps the full check. But a child it started that is still alive is ended and counted as a
  survivor;
* the session records every survivor, found by either part (the guard records every survivor it finds, even when the
  member's own teardown already failed, and BEFORE it tries to end a child, so a cleanup that raises cannot lose the
  record; the record then says "not ended" and never claims the child was ended). At the end of the session, if any
  was found, the exit status becomes 1 (unless it is already worse) and the summary names each test and what it left,
  on a line that starts with "gateway port guard". Under xdist each worker hands its survivors to the controller
  through `workeroutput`, and the controller fails the run. So an xfail cannot hide a survivor, and the run is
  rejected in every case.

Whose listener it is
--------------------
A busy port is not proof that the test that just finished left it: another worker, or a program outside the run (a
gateway that starts late), can hold it for its own reasons. So a port is blamed on a test only when its owner is
found and is this process or a child the test started. The owner is looked up without a new dependency: on Windows
the TCP table (`GetExtendedTcpTable`), on Linux `/proc/net/tcp` and `/proc/<pid>/fd`, elsewhere `lsof` where it
exists. (On Linux each descriptor is read on its own, so one unreadable descriptor cannot hide a process.)

An owner counts only for the exact endpoint the guard probed, not for the port number alone: the same address family
and the same address, or that family's wildcard. An IPv4 wildcard (`0.0.0.0`) listener owns `127.0.0.1:P`; a listener on
another address (`127.0.0.2:P`) or on `::1` does not; an IPv6 `::` listener owns it only when it is dual-stack
(IPV6_V6ONLY off), which is known only for this process's own sockets, so for another process's `::` listener
(`/proc`, `lsof`) it does not count, and the Windows lookup reads only the IPv4 table. A listener that does not count
is not blamed, which only means "not provable".

A listener is the test's own only if it was born during that test, from that test. A listener started by a process the
test started whose parent has already exited (a launcher that spawns the provider and ends) is still the test's
leftover, although the launcher's tracked `Popen` is gone by teardown; a listener an EARLIER test started is not. To
tell, the guard asks the system to keep the test's family together, but only while a guarded test runs with the gateway
opt-in on (a "window"): nothing is arranged at configure or collection, and pytest itself never joins a job.
  - Windows: a job object around the test's own `Popen` children (each is assigned right after it starts; a child may
    still break away; no kill-on-close). A job member is the test's.
  - Linux: this process is a child subreaper for the length of the test (the old value is put back; a flag that was
    already set stays set), and the live processes are noted when the test begins. A new process whose chain of parent
    links reaches this process through new processes only is the test's; one that was alive at the start is not.
  - macOS: neither exists. A chain of parents that are all alive is followed, read before the guard ends a child.
Each owner gets one of three answers: the test's, another process's, or UNDECIDED (the window cannot say, or the owner
cannot be found out). The test's is blamed, named by process id, and ended. Another process's is named ("held by another
process") and never touched. UNDECIDED is never ended: it is named in an ordinary run ("the guard could not tell whose
listener it is; it may be this test's"), and refused in an evidence run (`--experiment-record`), where a false
green does more harm than a false stop. "ended" is printed only after the exit is confirmed (Windows: the wait on the
process handle; POSIX: the process is gone or only a zombie); otherwise the line says "not ended". The guard never
waits on a process it did not start. A killed process that this process adopted stays a zombie until the worker exits.
Stated limits: on macOS, or where the system refuses (a child that could not be put into the job), a listener whose
launcher has already exited cannot be tied to the test, so a real leftover can pass on those platforms in an ordinary
run; on Windows a child that is not started through `subprocess.Popen` (for example `multiprocessing`), one that breaks
away, or one that a fast launcher outruns is not a member and is called another process's; on Linux the flag is
process-wide for the length of the test, so an unrelated orphan adopted then stays a child of the worker until it exits
and is left alone.

The teardown check does not rely on the bind probe alone. After it, the owner lookup is also asked about the guarded
endpoints the probe called free, and a listener it proves is this process's own (or a child the test started) fails the
test as a leftover: the probe is blind to some listeners that accept connections. The lookup is never used the other
way, to call a port held by someone else busy where the probe said free.

On Windows the TCP table names the process that CREATED a socket and keeps naming it after a completed handoff
(`socket.share` / `socket.fromshare`) to another process, although the sender's own descriptor is closed. So the
table's word for this process is not proof: this process counts as the owner only when its own scan of its Python
sockets (below) finds a live listener on the endpoint. A table claim for this process that the scan cannot confirm is
called held by another process (named, not failed); what that no longer catches on Windows is a leftover listener held
in a raw
descriptor the scan cannot see. A tracked child or a descendant is still told by its own process id.

As a net for every platform, the open Python sockets of this process are also checked by the same rule. That walk
sees the socket objects the garbage collector lists: a raw or detached descriptor, or an object hidden from the
collector (`gc.freeze()`), is not seen, so a real leftover listener can be missed when the system lookup fails too. The
walk looks at each object by `type()`, never `isinstance`, which follows a dead weak proxy and raises; an object that
cannot be looked at, and any failure of the scan itself, counts as "no proof", never as a test error.
A port held by another process, or by an owner that cannot be found out (which can include a real leftover), is named
on its own line, "gateway port guard: ... is held by another process", and neither fails the test nor changes the
exit status.

A failure that only the guard finds (an xfail that absorbed the teardown failure, or a skipped test that left a child)
makes no failed report, and pytest has computed its own counts before the run is rejected. So the process exit status
is 1, and the experiment journal and the reducer's root process row record 1, but JUnit's failure counts, the terminal
summary count, the exit codes of xdist workers and `--last-failed` still show success. The exit status and the
"gateway port guard" lines are the record; the dev gate reads the exit status.

Not covered: a port held by another process is not a failure, even when it was an earlier leftover of a test in
another worker; a skipped test that left a listener on the port is not probed, so that listener is not detected; a
worker that crashes takes its list of survivors with it (its crash is reported on its own).
"""

from __future__ import annotations

import ipaddress
import multiprocessing
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping

import pytest

OPT_IN_VAR = "AGENTTALK_TEST_GATEWAY_PORTS"
# Under xdist two probes of the same free port can meet, so a port counts as held only if it is held
# on every attempt.
PROBE_ATTEMPTS = 3
PROBE_PAUSE_SECONDS = 0.1

NESTED_VAR = "PYTEST_CURRENT_TEST"
DECISION_KEY = "gateway_port_decision"
DECISION_VERSION = 1
DECISION_FIELDS = ("version", "policy", "opt_in", "nested", "held", "probes", "reason")
POLICY_SKIP = "skip"
POLICY_ERROR = "error"
STRICT_OPTION = "experiment_record"
PLUGIN_NAME = "agenttalk-gateway-port-guard"
SURVIVOR_WAIT_SECONDS = 5.0
SURVIVORS_KEY = "gateway_port_survivors"  # in a worker's `workeroutput`, for the controller
NOTES_KEY = "gateway_port_notes"  # ports found held by someone else, for the controller

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


def decide(
    environ: Mapping[str, str],
    *,
    policy: str = POLICY_SKIP,
    addresses: Iterable[Address] | None = None,
    probe: Callable[[str, int], bool] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict:
    """The decision (see the module docstring for its fields). The only function that probes."""
    probe = port_is_held if probe is None else probe
    sleep = time.sleep if sleep is None else sleep
    decision: dict = {
        "version": DECISION_VERSION,
        "policy": policy,
        "opt_in": environ.get(OPT_IN_VAR) == "1",
        "nested": NESTED_VAR in environ,
        "held": [],
        "probes": 0,
        "reason": None,
    }
    if not decision["opt_in"]:
        decision["reason"] = (
            "it binds or calls the paid gateway's real ports 127.0.0.1:4000/4001; "
            f"opt-in: set {OPT_IN_VAR}=1 to run it (CI does)"
        )
        return decision
    if decision["nested"]:
        decision["reason"] = (
            "this is a nested pytest session; a nested session never touches the paid gateway's real ports "
            "127.0.0.1:4000/4001 (the run that started it owns them)"
        )
        return decision

    def counted(host: str, port: int) -> bool:
        decision["probes"] += 1
        return probe(host, port)

    held = list(gateway_addresses() if addresses is None else addresses)
    for attempt in range(PROBE_ATTEMPTS):
        held = [address for address in held if counted(*address)]
        if not held:
            return decision
        if attempt + 1 < PROBE_ATTEMPTS:
            sleep(PROBE_PAUSE_SECONDS)
    decision["held"] = [list(address) for address in held]
    ports = ", ".join(f"{host}:{port}" for host, port in held)
    decision["reason"] = f"{ports} already held by another process (a running gateway?); not run, never contacted"
    return decision


def skip_reason(
    environ: Mapping[str, str],
    *,
    addresses: Iterable[Address] | None = None,
    probe: Callable[[str, int], bool] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> str | None:
    """Why a gateway-port test must not run now, or None when it may."""
    return decide(environ, addresses=addresses, probe=probe, sleep=sleep)["reason"]


def policy_for(config) -> str:
    """ "error" when pytest was started with --experiment-record (the selector), else "skip"."""
    return POLICY_ERROR if config.getoption(STRICT_OPTION, default=None) else POLICY_SKIP


def _checked(value) -> dict:
    """The controller's decision, or a UsageError when it is missing or malformed."""
    problem = f"the controller's {DECISION_KEY} is missing or malformed"
    if not isinstance(value, dict) or tuple(value) != DECISION_FIELDS:
        raise pytest.UsageError(problem)
    held_ok = isinstance(value["held"], list) and all(
        isinstance(a, list) and len(a) == 2 and isinstance(a[0], str) and type(a[1]) is int for a in value["held"]
    )
    if not (
        value["version"] == DECISION_VERSION
        and value["policy"] in (POLICY_SKIP, POLICY_ERROR)
        and isinstance(value["opt_in"], bool)
        and isinstance(value["nested"], bool)
        and held_ok
        and type(value["probes"]) is int
        and (value["reason"] is None or isinstance(value["reason"], str))
    ):
        raise pytest.UsageError(problem)
    return value


def decision_for(config) -> dict | None:
    """The cached decision for this process: a worker's from `workerinput`, the owner's from its plugin; None when
    this process has none (a serial run answers lazily at collection)."""
    if config is None:
        return None
    workerinput = getattr(config, "workerinput", None)
    if workerinput is not None:
        return _checked(workerinput.get(DECISION_KEY))
    manager = getattr(config, "pluginmanager", None)
    plugin = manager.get_plugin(PLUGIN_NAME) if manager is not None else None
    return plugin.decision if plugin is not None else None


# -- who owns a busy port ------------------------------------------------------------------------------------------


ANY_ADDRESS = "0.0.0.0"  # noqa: S104 - compared with, never bound
ANY_ADDRESS_V6 = "::"


def _reaches(host: str, bound: str, dual_stack: bool) -> bool:
    """Whether a listener bound to the local address `bound` accepts connections to the endpoint `host`. The same family
    and the same address, or that family's wildcard; an IPv6 listener reaches an IPv4 endpoint only through an
    IPv4-mapped address (which exists only on a dual-stack socket) or through `::` on a socket known to be dual-stack
    (`dual_stack`: IPV6_V6ONLY off). Anything that cannot be read as an address reaches nothing, which only means that
    the listener is not blamed on a test."""
    try:
        wanted = ipaddress.ip_address(host)
        have = ipaddress.ip_address(bound.split("%", 1)[0])
    except ValueError:
        return False
    if have.version == wanted.version:
        return have == wanted or have.is_unspecified
    if have.version == 6:  # an IPv6 listener and an IPv4 endpoint
        mapped = getattr(have, "ipv4_mapped", None)
        return mapped == wanted if mapped is not None else have.is_unspecified and dual_stack
    return False


def _proc_address(text: str, ipv6: bool) -> str | None:
    """The local address of a /proc/net/tcp (8 hex digits) or tcp6 (32) row. Each 32-bit word is in host byte order."""
    try:
        words = [bytes.fromhex(text[k : k + 8]) for k in range(0, len(text), 8)]
    except ValueError:
        return None
    packed = b"".join(word[::-1] if sys.byteorder == "little" else word for word in words)
    try:
        return str(ipaddress.IPv6Address(packed) if ipv6 else ipaddress.IPv4Address(packed))
    except ValueError:
        return None


def _listener_pids_from_proc(
    table_text: str, host: str, port: int, fd_links: Mapping[int, Iterable[str]], *, ipv6: bool = False
) -> set[int]:
    """Linux: the processes holding a listening socket that reaches host:port. `table_text` is /proc/net/tcp (or tcp6
    with `ipv6`); `fd_links` maps a process id to the targets of its /proc/<pid>/fd links ("socket:[inode]"). The table
    does not say whether an IPv6 wildcard socket is dual-stack, so `::` does not count here (only an IPv4-mapped
    address does)."""
    inodes = set()
    for line in table_text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 10 or fields[3] != "0A":  # 0A: LISTEN
            continue
        address_hex, _, port_hex = fields[1].rpartition(":")
        try:
            row_port = int(port_hex, 16)
        except ValueError:
            continue
        bound = _proc_address(address_hex, ipv6)
        if row_port == port and bound is not None and _reaches(host, bound, False):
            inodes.add(f"socket:[{fields[9]}]")
    return {pid for pid, links in fd_links.items() if inodes.intersection(links)}


def _fd_targets(fd_dir: str) -> list[str]:
    """What each descriptor in a /proc/<pid>/fd directory points to. A descriptor that cannot be read is skipped on its
    own: the listing of this very process includes the descriptor the listing used, which is closed again by the time
    it is read, and one unreadable descriptor must never hide the others."""
    try:
        names = os.listdir(fd_dir)
    except OSError:  # a process of another user, or one that has ended: not found, which is "not this process"
        return []
    targets = []
    for name in names:
        try:
            targets.append(os.readlink(os.path.join(fd_dir, name)))
        except OSError:
            continue
    return targets


def _owners_linux(host: str, port: int, proc: str = "/proc") -> set[int] | None:
    tables = []
    for name in ("tcp", "tcp6"):
        try:
            with open(os.path.join(proc, "net", name), encoding="utf-8") as handle:
                tables.append((handle.read(), name == "tcp6"))
        except OSError:
            if name == "tcp":
                return None
    fd_links = {
        int(entry): _fd_targets(os.path.join(proc, entry, "fd")) for entry in os.listdir(proc) if entry.isdigit()
    }
    return set().union(*(_listener_pids_from_proc(text, host, port, fd_links, ipv6=v6) for text, v6 in tables))


def _owners_windows(host: str, port: int) -> set[int] | None:
    """Windows: the owner process ids of the IPv4 listeners that reach host:port, from the IPv4 TCP table
    (GetExtendedTcpTable). The IPv6 table is not read, so an IPv6 listener (dual-stack included) is not found here."""
    import ctypes

    try:
        wanted = socket.inet_ntoa(socket.inet_aton(host))
    except OSError:
        return None  # not an IPv4 address: not looked up
    iphlpapi = ctypes.WinDLL("iphlpapi")
    af_inet, listener_table, insufficient = 2, 3, 122
    size = ctypes.c_ulong(0)
    iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, af_inet, listener_table, 0)
    for _ in range(5):  # the table can grow between the two calls
        buffer = ctypes.create_string_buffer(size.value)
        status = iphlpapi.GetExtendedTcpTable(buffer, ctypes.byref(size), False, af_inet, listener_table, 0)
        if status == 0:
            break
        if status != insufficient:
            return None
    else:
        return None
    (count,) = struct.unpack_from("<I", buffer, 0)
    owners = set()
    for index in range(count):
        _state, local, local_port, _remote, _remote_port, pid = struct.unpack_from("<6I", buffer, 4 + 24 * index)
        address = socket.inet_ntoa(struct.pack("<I", local))
        if socket.ntohs(local_port & 0xFFFF) == port and _reaches(wanted, address, False):
            owners.add(pid)
    return owners


def _lsof_owners(text: str, host: str, port: int) -> set[int]:
    """The owners of host:port in `lsof -F pftn` output: `p<pid>`, then per descriptor `f<fd>`, `t<type>` and `n<name>`
    (`127.0.0.1:4000`, `*:4000`, `[::1]:4000`). The type says the family; `*` is that family's wildcard, and lsof cannot
    tell a dual-stack IPv6 wildcard from an IPv6-only one, so it does not reach an IPv4 endpoint."""
    owners: set[int] = set()
    pid: int | None = None
    kind = name = None

    def flush() -> None:
        if pid is not None and kind in ("IPv4", "IPv6") and name:
            bound, _, number = name.rpartition(":")
            if bound == "*":
                bound = ANY_ADDRESS if kind == "IPv4" else ANY_ADDRESS_V6
            if number == str(port) and _reaches(host, bound.strip("[]"), False):
                owners.add(pid)

    for line in text.splitlines():
        field, value = line[:1], line[1:]
        if field in ("p", "f"):
            flush()
            kind = name = None
            if field == "p":
                pid = int(value) if value.isdigit() else None
        elif field == "t":
            kind = value
        elif field == "n":
            name = value
    flush()
    return owners


def _owners_lsof(host: str, port: int) -> set[int] | None:
    """Other POSIX systems (macOS): `lsof`, which macOS ships; None when it is not there or fails."""
    exe = shutil.which("lsof") or ("/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else None)
    if exe is None:
        return None
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [exe, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpftn"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode not in (0, 1):  # 1: nothing found
        return None
    return _lsof_owners(done.stdout, host, port)


def _listens(sock) -> bool:
    """Whether a socket is listening. macOS refuses `SO_ACCEPTCONN` (the kernel answers ENOPROTOOPT), so there the TCP
    state is read instead: `TCP_CONNECTION_INFO` (0x106), whose first byte is the state, and `TCPS_LISTEN` is 1.
    Anything that fails is "not shown to listen"."""
    if sys.platform == "darwin":
        return sock.getsockopt(socket.IPPROTO_TCP, 0x106, 1)[0] == 1
    return bool(sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN))


def _this_process_listens(host: str, port: int) -> bool:
    """Whether an open Python socket of this process listens on host:port, by the same rule as the system lookups (see
    `_reaches`; for an IPv6 socket, IPV6_V6ONLY is read from the socket itself). It walks the objects the garbage
    collector tracks, so it needs no system tool and works on every platform, but it sees only a Python `socket` object
    the collector lists: a raw or detached descriptor, or an object hidden from it (`gc.freeze()`), is not seen.
    Looking at an object can never become an error: it checks `type(obj)` (`isinstance` would follow a dead weak proxy
    and raise ReferenceError), and an object that cannot be looked at is simply not a listener."""
    import gc

    try:
        objects = gc.get_objects()
    except Exception:  # noqa: BLE001 - a scan that cannot run is "no proof", not a failure
        return False
    for obj in objects:
        try:
            if not issubclass(type(obj), socket.socket):
                continue
            if not _listens(obj):
                continue
            name = obj.getsockname()
            dual_stack = obj.family == socket.AF_INET6 and not obj.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY)
            if name[1] == port and _reaches(host, name[0], dual_stack):
                return True
        except Exception:  # noqa: BLE001, S112 - closed, expired, not an inet socket, or hostile: not shown to listen
            continue
    return False


def listener_owners(host: str, port: int) -> set[int] | None:
    """The process ids that listen on host:port, or None when that cannot be found out here (an owner lookup that
    fails, or a platform without one). An empty set means that no listener was found. No new dependency. An owner counts
    only for this exact endpoint: the same family and address (or that family's wildcard), never the port alone. A
    listener held by this process's own Python sockets on the endpoint is found whatever the system lookup says, as far
    as the collector lists the socket (see `_this_process_listens`)."""
    try:
        if sys.platform == "win32":
            found = _owners_windows(host, port)
        elif sys.platform.startswith("linux"):
            found = _owners_linux(host, port)
        else:
            found = _owners_lsof(host, port)
    except Exception:  # noqa: BLE001 - a lookup that fails is "not known", never a failure of the run
        found = None
    try:
        scanned = _this_process_listens(host, port)
    except Exception:  # noqa: BLE001 - whatever goes wrong in the scan counts as "no proof", never as an error
        scanned = False
    if sys.platform == "win32" and found:
        # The TCP table names the process that CREATED a socket, and keeps naming it after a completed handoff
        # (`socket.share` / `fromshare`) to another process, although its own descriptor is closed. So the table's word
        # for this process is not proof; only this process's own scan, below, can show that it still holds a listener.
        found = found - {os.getpid()}
    if scanned:
        return (found or set()) | {os.getpid()}
    return found


# -- whose listener it is: the test's window ----------------------------------------------------------------------
#
# A listener is the test's only if it was born during that test, from that test. The system is asked to keep that family
# together only while a guarded test runs with the gateway opt-in on (a "window"); nothing is arranged at configure or
# collection, and pytest itself never joins a job:
#   Windows  a job object around the test's own `Popen` children (each is assigned right after it starts; breakaway
#            stays allowed, no kill-on-close). The job lists its live members whatever became of their parents.
#   Linux    this process is a child subreaper for the length of the test (the old value is put back; a flag that was
#            already set stays set), so an orphan of the test is re-parented to it and its chain of parent links reaches
#            this process. A snapshot of the live processes at the start says what was already there.
#   macOS    neither exists; a chain of parents that are all alive is followed, read before the guard ends a child.
# Each owner gets one of three answers: MINE, OTHER or UNDECIDED (see `_classify`).

MINE = "mine"
OTHER = "other"
UNDECIDED = "undecided"
_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37
_CURRENT_WINDOW = None


def _kernel32():
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    k32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
    ]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


def _create_job():
    """A new job object that lets a child break away explicitly, or None. This process is NOT put into it."""
    import ctypes

    class Basic(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class Counters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("r", "w", "o", "rb", "wb", "ob")]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("Basic", Basic),
            ("Io", Counters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    k32 = _kernel32()
    job = k32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = Extended()
    info.Basic.LimitFlags = 0x800  # JOB_OBJECT_LIMIT_BREAKAWAY_OK: a child may still ask to break away
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # ExtendedLimitInformation
        k32.CloseHandle(job)
        return None
    return job


def _job_pids(job) -> set[int]:
    """The live members of a job object (Windows)."""
    import ctypes

    k32 = _kernel32()
    width = ctypes.sizeof(ctypes.c_size_t)
    capacity = 256
    for _ in range(6):
        buffer = ctypes.create_string_buffer(8 + width * capacity)
        if k32.QueryInformationJobObject(job, 3, buffer, len(buffer), None):  # BasicProcessIdList
            listed = struct.unpack_from("<II", buffer, 0)[1]
            return set(struct.unpack_from(f"<{listed}{'Q' if width == 8 else 'I'}", buffer, 8))
        if ctypes.get_last_error() != 234:  # ERROR_MORE_DATA: the list did not fit
            return set()
        capacity *= 4
    return set()


def _parent_links() -> dict[int, int]:
    """{pid: parent pid} for the live processes: /proc on Linux, `ps` elsewhere."""
    parents: dict[int, int] = {}
    if sys.platform.startswith("linux"):
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", "rb") as handle:
                    fields = handle.read().rpartition(b")")[2].split()  # after "(name)": state, parent, ...
            except OSError:
                continue
            if len(fields) > 1:
                parents[int(entry)] = int(fields[1])
        return parents
    exe = shutil.which("ps") or "/bin/ps"
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [exe, "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=10, check=False
    )
    for line in done.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and all(field.isdigit() for field in fields):
            parents[int(fields[0])] = int(fields[1])
    return parents


def _tie(pid: int, links, before, me: int, subreaper: bool) -> str:
    """Whose is this process, on a system with parent links? `links` is {pid: parent} read before the guard ended any
    child, `before` the set of processes alive when the test began, `subreaper` whether this process was a child
    subreaper during the test. A process (or one on its way up) that was alive when the test began cannot be this
    test's; a new process whose chain reaches this one through new processes only is. With the flag every descendant
    comes back here, so a chain that stops at an older process is an outsider's; without it that older parent may be
    an orphan of ours. Anything not known is UNDECIDED."""
    if links is None or before is None:
        return UNDECIDED
    seen: set[int] = set()
    current = pid
    while True:
        if current == me:
            return MINE
        if current in before:
            return OTHER if current == pid or subreaper else UNDECIDED
        if current not in links or current in seen:
            return UNDECIDED
        seen.add(current)
        current = links[current]


class Evidence:
    """What the window can say about a process, read before the guard ends any child."""

    def __init__(self, kind, members=None, refused=False, links=None, before=None, subreaper=False) -> None:
        self.kind = kind  # "job", "posix" or "none"
        self.members = members
        self.refused = refused
        self.links = links
        self.before = before
        self.subreaper = subreaper


def _classify(pid: int, evidence: Evidence) -> str:
    """MINE, OTHER or UNDECIDED for the owner `pid` of a listener."""
    if evidence.kind == "job":
        if evidence.members is None:
            return UNDECIDED
        if pid in evidence.members:
            return MINE
        return UNDECIDED if evidence.refused else OTHER
    if evidence.kind == "posix":
        return _tie(pid, evidence.links, evidence.before, os.getpid(), evidence.subreaper)
    return UNDECIDED


class Window:
    """The arrangement for one guarded test. Opened in setup, closed whatever the outcome."""

    def __init__(self) -> None:
        self.job = None  # Windows
        self.refused = False  # a child could not be put into the job
        self.before: set[int] | None = None  # POSIX: the processes alive when the test began
        self.subreaper = False  # Linux: this process was a child subreaper during the test
        self._subreaper_set = False
        self._subreaper_old = 0

    def open(self) -> None:
        global _CURRENT_WINDOW
        try:
            if sys.platform == "win32":
                self.job = _create_job()
            else:
                self.before = set(_parent_links())
                if sys.platform.startswith("linux"):
                    self._open_subreaper()
        except Exception:  # noqa: BLE001, S110 - what cannot be arranged leaves the answers UNDECIDED, never an error
            pass
        _CURRENT_WINDOW = self

    def _open_subreaper(self) -> None:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        value = ctypes.c_int(0)
        if libc.prctl(_PR_GET_CHILD_SUBREAPER, ctypes.byref(value), 0, 0, 0) != 0:
            return
        self._subreaper_old = value.value
        if self._subreaper_old == 1:
            self.subreaper = True  # already set by someone else: it stays set
        elif libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0:
            self._subreaper_set = True
            self.subreaper = True

    def adopt(self, popen) -> None:
        """Windows: put a `Popen` child into the job right after it started."""
        if self.job is None:
            return
        try:
            from ctypes import wintypes

            if not _kernel32().AssignProcessToJobObject(self.job, wintypes.HANDLE(int(popen._handle))):
                self.refused = True
        except Exception:  # noqa: BLE001
            self.refused = True

    def evidence(self) -> Evidence:
        try:
            if sys.platform == "win32":
                if self.job is None:
                    return Evidence("none")
                return Evidence("job", members=_job_pids(self.job), refused=self.refused)
            return Evidence("posix", links=_parent_links(), before=self.before, subreaper=self.subreaper)
        except Exception:  # noqa: BLE001
            return Evidence("none")

    def close(self) -> None:
        global _CURRENT_WINDOW
        _CURRENT_WINDOW = None
        try:
            if self._subreaper_set:
                import ctypes

                ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_CHILD_SUBREAPER, 0, 0, 0, 0)  # the old value was 0
            if self.job is not None:
                _kernel32().CloseHandle(self.job)  # no kill-on-close: what is in the job keeps running
        except Exception:  # noqa: BLE001, S110 - there is nothing more to undo
            pass


def _ended(pid: int) -> bool:
    """Whether a process is gone (or only a zombie): /proc on Linux, `ps` elsewhere."""
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/stat", "rb") as handle:
                state = handle.read().rpartition(b")")[2].split()[0]
        except (OSError, IndexError):
            return True
        return state in (b"Z", b"X")
    exe = shutil.which("ps") or "/bin/ps"
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [exe, "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=10, check=False
    )
    state = done.stdout.strip()
    return not state or state.startswith("Z")


def _terminate_windows(pids, k32, last_error) -> set[int]:
    """End processes by number; return those whose exit is NOT confirmed. Only `WAIT_OBJECT_0` confirms an exit, and
    a refused open counts as gone only for "no such process" (error 87)."""
    left: set[int] = set()
    for pid in pids:
        handle = k32.OpenProcess(0x0001 | 0x00100000, False, pid)  # PROCESS_TERMINATE | SYNCHRONIZE
        if not handle:
            if last_error() != 87:
                left.add(pid)
            continue
        try:
            k32.TerminateProcess(handle, 1)
            if k32.WaitForSingleObject(handle, int(SURVIVOR_WAIT_SECONDS * 1000)) != 0:
                left.add(pid)
        finally:
            k32.CloseHandle(handle)
    return left


def _terminate_posix(pids, kill, ended, wait: float = SURVIVOR_WAIT_SECONDS) -> set[int]:
    """SIGKILL processes by number and look them up until they are gone; return those not confirmed gone. Nothing is
    ever waited for (`waitpid`): a process the guard did not start is not the guard's to reap."""
    left: set[int] = set()
    signalled = []
    for pid in pids:
        try:
            kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))  # (SIGKILL does not exist on Windows)
        except ProcessLookupError:
            continue
        except OSError:
            left.add(pid)
            continue
        signalled.append(pid)
    deadline = time.monotonic() + wait
    for pid in signalled:
        while not ended(pid):
            if time.monotonic() >= deadline:
                left.add(pid)
                break
            time.sleep(0.02)
    return left


def _terminate_processes(pids: Iterable[int]) -> set[int]:
    """End processes that are not `Popen` objects of ours; return the process ids whose exit is not confirmed."""
    pids = list(pids)
    if sys.platform == "win32":
        import ctypes

        return _terminate_windows(pids, _kernel32(), ctypes.get_last_error)
    return _terminate_posix(pids, os.kill, _ended)


# -- the owned teardown check --------------------------------------------------------------------------------------

_STARTED: list[subprocess.Popen] = []
_WATCHERS = 0
_ORIGINAL_INIT = None


def _install_tracker() -> int:
    """Start recording every Popen created in this process; return where this watcher's records begin."""
    global _WATCHERS, _ORIGINAL_INIT
    if _WATCHERS == 0:
        _ORIGINAL_INIT = subprocess.Popen.__init__

        def init(self, *args, **kwargs):
            _ORIGINAL_INIT(self, *args, **kwargs)
            _STARTED.append(self)
            if _CURRENT_WINDOW is not None:
                _CURRENT_WINDOW.adopt(self)

        subprocess.Popen.__init__ = init
    _WATCHERS += 1
    return len(_STARTED)


def _remove_tracker(start: int) -> list[subprocess.Popen]:
    """Stop recording for this watcher; return the Popen objects it saw."""
    global _WATCHERS
    seen = _STARTED[start:]
    del _STARTED[start:]
    _WATCHERS -= 1
    if _WATCHERS == 0:
        subprocess.Popen.__init__ = _ORIGINAL_INIT
    return seen


def _end(survivors: list) -> None:
    """End the survivors: terminate, wait a bounded time, then kill."""
    for process in survivors:
        process.terminate()
    deadline = time.monotonic() + SURVIVOR_WAIT_SECONDS
    for process in survivors:
        if isinstance(process, subprocess.Popen):
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=SURVIVOR_WAIT_SECONDS)
        else:
            process.join(timeout=max(0.0, deadline - time.monotonic()))
            if process.is_alive():
                process.kill()
                process.join(timeout=SURVIVOR_WAIT_SECONDS)
                if process.is_alive():
                    raise RuntimeError(f"process {process.pid} is still alive after kill")


def base_test_name(item) -> str:
    return getattr(item, "originalname", None) or item.name.split("[", 1)[0]


class GuardPlugin:
    """Holds the decision (owner), hands it to workers, and runs the owned teardown check."""

    def __init__(self, names, environ, addresses, probe, sleep, teardown_probe=None, owners=None, strict=False) -> None:
        self.strict = strict  # an evidence run (`--experiment-record`): undecided ownership is refused, not just named
        self.names = frozenset(names)
        self.environ = environ
        self.addresses = None if addresses is None else list(addresses)
        self.probe = probe
        self.teardown_probe = teardown_probe
        self.owners = owners or listener_owners
        self.sleep = sleep
        self.decision: dict | None = None
        self._watching: dict[str, tuple[int, set[int], Window]] = {}
        self._skipped: set[str] = set()  # node ids pytest reported as skipped (xfail excepted): they ran nothing
        self.survivors: list[str] = []  # the session flag: what each test left, for the end of the session
        self.notes: list[str] = []  # ports found held by someone else: named, never counted against a test

    @pytest.hookimpl(optionalhook=True)
    def pytest_configure_node(self, node) -> None:
        node.workerinput[DECISION_KEY] = dict(self.decision) if self.decision is not None else None

    def _covers(self, item) -> bool:
        return base_test_name(item) in self.names and item.get_closest_marker("skip") is None

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_setup(self, item) -> None:
        if self._covers(item) and self.environ.get(OPT_IN_VAR) == "1":
            window = Window()
            window.open()
            try:
                before = {p.pid for p in multiprocessing.active_children()}
                self._watching[item.nodeid] = (_install_tracker(), before, window)
            except BaseException:
                window.close()
                raise

    @pytest.hookimpl(trylast=True)
    def pytest_runtest_logreport(self, report) -> None:
        # A test whose body never ran is not probed: skipped in setup (a marker, `skipif`, or `xfail(run=False)`,
        # which pytest reports as skipped with `wasxfail`), or skipped by `pytest.skip()` in its body. A body that
        # ran and then xfailed is reported from the call phase with `wasxfail`; that one keeps the full check.
        if report.skipped and (report.when == "setup" or (report.when == "call" and not hasattr(report, "wasxfail"))):
            self._skipped.add(report.nodeid)

    def _children(self, watch: tuple[int, set[int], Window]) -> list:
        """Stop watching and return the child processes the test started that are still alive."""
        start, before, _window = watch
        started = _remove_tracker(start)
        children: list = [p for p in started if p.poll() is None]
        return children + [p for p in multiprocessing.active_children() if p.pid not in before]

    def _judge(self, address: Address, mine_pids: set, evidence: Evidence, *, free: bool = False):
        """(answer, pids to end, who) for the listener on `address`. MINE: this process, a child the test started, or a
        process that the window ties to this test. OTHER: provably someone else's. UNDECIDED: the window cannot say (or
        the lookup could not find the owner). For an endpoint the bind probe called free (`free`), an owner that cannot
        be found out is ignored: nothing is known, and nothing is made busy."""
        owners = self.owners(*address)
        if owners is None:
            if free:
                return OTHER, set(), ""
            return UNDECIDED, set(), "its owner could not be established on this platform"
        if owners & mine_pids:
            return MINE, set(), ""
        who = ", ".join(f"pid {pid}" for pid in sorted(owners)) or "no listener of ours was found"
        answers = {pid: _classify(pid, evidence) for pid in owners}
        mine = {pid for pid, answer in answers.items() if answer == MINE}
        if mine:
            return MINE, mine, who
        if any(answer == UNDECIDED for answer in answers.values()):
            return UNDECIDED, set(), who
        return OTHER, set(), who

    def _guarded_addresses(self) -> list[Address]:
        return list(gateway_addresses() if self.addresses is None else self.addresses)

    def _held(self) -> list[Address]:
        probe = self.teardown_probe or self.probe or port_is_held
        sleep = time.sleep if self.sleep is None else self.sleep
        held = self._guarded_addresses()
        for attempt in range(PROBE_ATTEMPTS):
            held = [address for address in held if probe(*address)]
            if not held:
                break
            if attempt + 1 < PROBE_ATTEMPTS:
                sleep(PROBE_PAUSE_SECONDS)
        return held

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_teardown(self, item, nextitem):
        # A wrapper, so the watch is always closed, even when a fixture's own teardown failed first. The member's own
        # failure is never replaced: the survivors are still ended, but only a clean teardown is failed by this check.
        failed_already = False
        try:
            return (yield)
        except BaseException:
            failed_already = True
            raise
        finally:
            self._close_watch(item, failed_already)

    def _close_watch(self, item, failed_already: bool) -> None:
        watch = self._watching.pop(item.nodeid, None)
        skipped = item.nodeid in self._skipped
        self._skipped.discard(item.nodeid)
        if watch is None:
            return
        try:
            self._check(item, watch, skipped, failed_already)
        finally:
            watch[2].close()  # the window closes whatever happened

    def _check(self, item, watch, skipped: bool, failed_already: bool) -> None:
        children = self._children(watch)
        evidence = watch[2].evidence()  # read BEFORE any child is ended: ending a parent would orphan its child
        label = f"{item.nodeid}{' (skipped)' if skipped else ''} left: "
        problems = []
        slot = -1
        if children:
            pids = ", ".join(str(getattr(p, "pid", "?")) for p in children)
            survived = f"{len(children)} child process(es) it started survived its teardown (pid {pids})"
            problems.append(survived)
            # Recorded BEFORE the cleanup, which can raise: the record then exists whatever the cleanup does, and it
            # does not claim the child was ended until it was.
            self.survivors.append(label + survived)
            slot = len(self.survivors) - 1
            try:
                _end(children)
            except Exception as error:
                problems[0] = f"{survived}; not ended: {type(error).__name__}: {str(error)[:200]}"
            else:
                problems[0] = f"{survived}; ended"
            self.survivors[slot] = label + problems[0]
        held = [] if skipped else self._held()  # a skipped test ran nothing: its ports are not probed
        mine_pids = {os.getpid(), *{getattr(p, "pid", None) for p in children}}
        mine: list[Address] = []
        to_end: set[int] = set()
        refused: list[tuple[Address, str]] = []
        sources = [(address, False) for address in held]
        if not skipped:  # the bind probe is blind to some listeners: the owner lookup is asked about the free ones too
            sources += [(address, True) for address in self._guarded_addresses() if address not in held]
        for address, free in sources:
            answer, pids, who = self._judge(address, mine_pids, evidence, free=free)
            if answer == MINE:
                mine.append(address)
                to_end |= pids
            elif answer == UNDECIDED and self.strict:
                refused.append((address, who))
            elif answer == UNDECIDED:
                self.notes.append(
                    f"{item.nodeid}: {address[0]}:{address[1]} is held; the guard could not tell whose listener it is "
                    f"({who}); it may be this test's; not counted against it"
                )
            elif not free:  # busy, but not provably this test's: named, never counted against it
                self.notes.append(
                    f"{item.nodeid}: {address[0]}:{address[1]} is held by another process ({who}), not by "
                    "this test; not counted against it"
                )
        if mine:
            ports = ", ".join(f"{host}:{port}" for host, port in mine)
            problems.append(f"a listener still holds {ports}")
            if to_end:
                pids = ", ".join(str(pid) for pid in sorted(to_end))
                problems[-1] += f" (held by process {pids}, started by this test)"
        if refused:
            ports = ", ".join(f"{host}:{port}" for (host, port), _who in refused)
            who = "; ".join(who for _address, who in refused)
            problems.append(
                f"a listener holds {ports} and the guard could not tell whose it is ({who}); refused in an evidence run"
            )
        if not problems:
            return
        what = "; ".join(problems)
        if slot >= 0:
            self.survivors[slot] = label + what
        else:
            self.survivors.append(label + what)
            slot = len(self.survivors) - 1
        if to_end:  # recorded first, as for the children: the record does not claim "ended" until it was confirmed
            try:
                left = _terminate_processes(to_end)
            except Exception as error:
                what += f"; not ended: {type(error).__name__}: {str(error)[:200]}"
            else:
                what += "; ended" if not left else f"; not ended (pid {', '.join(str(pid) for pid in sorted(left))})"
            self.survivors[slot] = label + what
        if failed_already or skipped:
            return  # the member's own failure is never replaced, and a skipped test is not turned into an error
        pytest.fail(f"{item.name}: {what}", pytrace=False)

    # -- the session flag -------------------------------------------------------------------------------------------

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node, error) -> None:
        """The controller: take over the survivors a worker found."""
        output = getattr(node, "workeroutput", None) or {}
        self.survivors.extend(output.get(SURVIVORS_KEY, []))
        self.notes.extend(output.get(NOTES_KEY, []))

    def pytest_sessionfinish(self, session) -> None:
        output = getattr(session.config, "workeroutput", None)
        if output is not None:  # a worker: the controller owns the exit status
            output[SURVIVORS_KEY] = list(self.survivors)
            output[NOTES_KEY] = list(self.notes)
            return
        if self.survivors:
            if int(session.exitstatus) == int(pytest.ExitCode.OK):
                session.exitstatus = pytest.ExitCode.TESTS_FAILED  # a worse status (interrupted, internal) is kept
            self._name_the_survivors(session.config)
        if self.notes:  # named, and no reason to change the exit status
            notes = list(dict.fromkeys(self.notes))
            self._report(session.config, "gateway port guard: ports held by another process", notes, red=False)

    def _name_the_survivors(self, config) -> None:
        """Say which test left what. Written here, not in the terminal summary, which pytest skips for some statuses."""
        self._report(config, "gateway port guard: survivors", self.survivors, red=True)

    @staticmethod
    def _report(config, title: str, lines: list[str], *, red: bool) -> None:
        reporter = config.pluginmanager.get_plugin("terminalreporter")
        lines = [f"gateway port guard: {line}" for line in lines]
        if reporter is None:
            print("\n".join(lines), file=sys.stderr)
            return
        reporter.write_sep("=", title, red=red, yellow=not red)
        for line in lines:
            reporter.write_line(line)


def configure(
    config,
    names: Iterable[str] = (),
    *,
    environ: Mapping[str, str] | None = None,
    addresses: Iterable[Address] | None = None,
    probe: Callable[[str, int], bool] | None = None,
    sleep: Callable[[float], None] | None = None,
    teardown_probe: Callable[[str, int], bool] | None = None,
    owners: Callable[[str, int], set[int] | None] | None = None,
) -> GuardPlugin:
    """Register the plugin; take the decision here when this process owns the run and must decide early (an xdist
    controller, or any run under the strict policy). A held port under the strict policy ends the run."""
    existing = config.pluginmanager.get_plugin(PLUGIN_NAME)
    if existing is not None:
        return existing
    environ = os.environ if environ is None else environ
    policy = policy_for(config)
    plugin = GuardPlugin(names, environ, addresses, probe, sleep, teardown_probe, owners, strict=policy == POLICY_ERROR)
    started_workers = getattr(config.option, "numprocesses", None) or getattr(config.option, "tx", None)
    controller = not hasattr(config, "workerinput") and bool(started_workers)
    if not hasattr(config, "workerinput") and (controller or policy == POLICY_ERROR):
        plugin.decision = decide(environ, policy=policy, addresses=addresses, probe=probe, sleep=sleep)
        if policy == POLICY_ERROR and plugin.decision["held"]:
            pytest.exit(
                f"gateway ports held: {plugin.decision['reason']}. The experiment's gateway policy is 'error': "
                "a held port stops the run instead of skipping the tests.",
                returncode=int(pytest.ExitCode.INTERRUPTED),
            )
    config.pluginmanager.register(plugin, PLUGIN_NAME)
    return plugin


def apply(
    items,
    names: frozenset[str],
    environ: Mapping[str, str] = os.environ,
    *,
    addresses: Iterable[Address] | None = None,
    config=None,
    probe: Callable[[str, int], bool] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    """Skip every collected item named in `names` unless the guard lets it run. With a `config` the cached decision
    is used (a worker never probes); without one, or in a serial ordinary run, the ports are probed here, only when
    such an item is collected and the opt-in is set."""
    guarded = [item for item in items if base_test_name(item) in names]
    if not guarded:
        return
    decision = decision_for(config)
    if decision is None:
        reason = skip_reason(environ, addresses=addresses, probe=probe, sleep=sleep)
    else:
        if decision["policy"] == POLICY_ERROR and decision["held"]:
            raise pytest.UsageError(f"gateway ports held under the 'error' policy: {decision['reason']}")
        reason = decision["reason"]
    if reason is None:
        return
    marker = pytest.mark.skip(reason=f"gateway-port test: {reason}")
    for item in guarded:
        item.add_marker(marker)
