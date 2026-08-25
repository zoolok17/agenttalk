"""agenttalk CLI: init, send, wait, recv, ack, transcript, end, status."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import io
import json
import math
import os
import re
import shutil
import stat
import subprocess  # nosec B404
import sys
import time
import traceback
import unicodedata
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException
from fractions import Fraction
from pathlib import Path

from agenttalk import __version__
from agenttalk import avatars as avatar_mod
from agenttalk import store as store_mod
from agenttalk.display import render
from agenttalk.store import (
    COMPOSING_INTENT_STALE_SECONDS,
    CONTROL_KINDS,
    LEAD_LOOP_CADENCE_FAIL_BACKOFF_BASE,
    LEAD_LOOP_CADENCE_FAIL_BACKOFF_MAX,
    LEAD_LOOP_CADENCE_HEALTH_THRESHOLD,
    LEAD_LOOP_LEASE_ENV,
    LEAD_LOOP_REMINDER_AFTER_DEFAULT,
    OPENER_KINDS,
    Store,
    _owner_identity_gone,
    find_root,
    find_stores_upward,
    validate_agent_name,
    validate_rescind,
)
from agenttalk import capacity as capmod
from agenttalk import checkpoint as checkpoint_mod
from agenttalk import deadman as deadman_mod
from agenttalk import ephemeral as eph
from agenttalk import domains as dom
from agenttalk import transcript as tx
from agenttalk import codex_config as cxc
from agenttalk import doctor as dr
from agenttalk import gates as gate_mod
from agenttalk import reply_transport
from agenttalk import lanes as lane_mod
from agenttalk import install_skills as iskl
from agenttalk import lead_loop_runtime
from agenttalk import launch_admission
from agenttalk import onboarding as ob
from agenttalk import signing as _signing
from agenttalk import threads as th
from agenttalk import supervisor as sup
from agenttalk import powershell_host as psh
from agenttalk import supervisor_lifecycle as supervisor_lifecycle
from agenttalk import wrapper_runtime as runtime_obs

# Hard ceiling on cumulative deadline extension from `composing` pings,
# regardless of how many arrive. Prevents a misbehaving (or stuck) peer
# from holding a waiter forever. 30 min was picked to comfortably cover
# long substantive review cycles without being effectively infinite.
_COMPOSING_MAX_EXTEND_SECONDS = 1800.0
_OPERATION_NONCE_RE = re.compile(r"[0-9a-f]{32}")


# --------------------------------------------------------------------- utils

def _get_store(args: argparse.Namespace, *, must_exist: bool = True) -> Store:
    root = Path(args.root).resolve() if getattr(args, "root", None) else find_root()
    store = Store(root)
    if must_exist and not store.initialized():
        sys.stderr.write(
            f"agenttalk: not initialized at {root}\n"
            f"Run `agenttalk init --here` from the project root.\n"
        )
        sys.exit(2)
    return store


def _read_body(args: argparse.Namespace) -> str:
    # `is not None` (not truthiness): an explicit `-m ""` is a deliberate
    # empty body, so it must short-circuit here rather than fall through to
    # --file / the stdin sniff (which could hang on an open pipe). Whether an
    # empty body is allowed is governed downstream by --allow-empty (review nit).
    if getattr(args, "message", None) is not None:
        return args.message
    f = getattr(args, "file", None)
    if f:
        # `--file -` reads the body from stdin — the reliable way to pass a
        # body on Windows (a here-string piped in), where inline `-m`
        # mangles backslashes / apostrophes / control chars.
        if f == "-":
            return sys.stdin.read()
        return Path(f).read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        data = sys.stdin.read()
        if data:
            return data
    return ""


def _operation_idempotency(
    store: Store,
    *,
    sender: str,
    recipient: str,
    body: str,
    kind: str,
    operation: str,
    meta: dict,
    nonce: str | None,
) -> tuple[object | None, str | None]:
    """Bind one wrapper operation nonce to one canonical semantic payload."""
    _ = store, sender
    if nonce is None:
        return None, None
    if _OPERATION_NONCE_RE.fullmatch(nonce) is None:
        return None, "operation nonce must be exactly 32 lowercase hexadecimal characters"
    # One digest producer for CLI and wrapper (#201) — divergence forks dedupe.
    digest = reply_transport.operation_digest_for(
        meta, operation=operation, body=body, kind=kind, recipient=recipient,
    )
    meta["operation_nonce"] = nonce
    meta["operation_digest"] = digest
    return None, None


def _json_scrub_nonfinite(value):
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list):
        return [_json_scrub_nonfinite(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_scrub_nonfinite(item) for key, item in value.items()}
    return value


def _finite_float_arg(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a finite number") from exc
    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("must be a finite number")
    return parsed


def _positive_int_arg(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _micro_eur_arg(value: str) -> int:
    if len(value) > 64:
        raise argparse.ArgumentTypeError("must be a non-negative EUR amount")
    try:
        parsed = Decimal(value)
        micro_eur = parsed * Decimal(1_000_000)
    except (DecimalException, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a non-negative EUR amount") from exc
    if not parsed.is_finite() or parsed < 0 or micro_eur != micro_eur.to_integral_value():
        raise argparse.ArgumentTypeError(
            "must be a non-negative EUR amount with at most six decimal places"
        )
    return int(micro_eur)


def _parse_meta(items: list[str] | None) -> dict:
    out: dict = {}
    for item in items or []:
        if "=" not in item:
            # Raise ValueError so main() converts to exit 2 (usage error).
            # Earlier code used SystemExit(str) which exits 1 — that
            # collides with `agenttalk wait`'s timeout signal.
            raise ValueError(f"--meta expects key=value, got {item!r}")
        k, v = item.split("=", 1)
        out[k.strip()] = v.strip()
    return out


# Kinds that get a request_id auto-minted if the caller didn't pass one, so a
# reply can echo it. Distinct prefixes make the id self-describing in
# logs/transcripts. Most of these also OPEN a trackable thread (they are in
# store.OPENER_KINDS, so `agenttalk threads` correlates the reply) — but `wake`
# is deliberately NOT a thread opener: it mints a `wk-` id purely so a reply can
# correlate, while staying FYI-class for thread derivation (0.24.0, feedback
# 3.3). Minting and thread-opening are separate concerns kept in separate
# constants (this map vs. store.OPENER_KINDS).
_AUTOGEN_REQUEST_ID_PREFIX = {
    "review-request": "rq-",
    "question": "q-",
    "proposal": "pp-",
    "wake": "wk-",
}

# A response kind -> the opener kind it is meant to correlate with. Used
# only for the soft missing-request_id warning.
_RESPONSE_TO_OPENER = {
    "review-result": "review-request",
    "proposal-response": "proposal",
}


def _maybe_autogen_request_id(kind: str, meta: dict, *, quiet: bool) -> None:
    """Mint a `request_id` into ``meta`` for thread-opening kinds if absent.

    Originally closed the review-request correlation gap (issue #5); as
    of 0.10.0 it also covers `question` and `proposal` so every thread
    `agenttalk threads` should track is correlatable. Explicit
    ``--meta request_id=...`` always wins (we only fill a missing one).
    Prints the generated id in non-quiet mode so the sender knows what
    to expect echoed back.
    """
    prefix = _AUTOGEN_REQUEST_ID_PREFIX.get(kind)
    if prefix is None or "request_id" in meta:
        return
    meta["request_id"] = prefix + uuid.uuid4().hex[:12]
    if not quiet:
        label = {"proposal": "proposal id", "wake": "wake id"}.get(
            kind, "auto request_id")
        print(f"({label}: {meta['request_id']})")


def _warn_missing_request_id(kind: str, meta: dict) -> None:
    """Soft stderr warning when a response carries no request_id.

    Stays a warning (exit code unchanged) on purpose: a hard error
    would break mixed-version peers and any response answering a
    pre-correlation request that never had an id to echo. Covers both
    `review-result` and `proposal-response`.
    """
    opener = _RESPONSE_TO_OPENER.get(kind)
    if opener is not None and "request_id" not in meta:
        sys.stderr.write(
            f"agenttalk: warning: {kind} has no request_id to correlate "
            f"with an open {opener}.\n"
            f"  Pass --meta request_id=<id>, or use `agenttalk reply` which "
            f"auto-echoes the original request_id.\n"
        )


def _resolve_self(value: str | None, *, roster: list[str] | None = None) -> str:
    """Pick agent identity: explicit flag wins, else $AGENTTALK_SELF.

    Exits 2 (usage error) on failure — NOT 1, since 1 collides with
    `agenttalk wait`'s timeout signal and would confuse loop skills.
    If `roster` is provided, the resolved name must be in it; typos
    like AGENTTALK_SELF=clude exit 2 rather than silently operating on
    a phantom mailbox. Names are also shape-validated (no path
    separators, no `..`, etc.) so a malicious env var can't smuggle
    a phantom mailbox at a different filesystem location.
    """
    name = value or os.environ.get("AGENTTALK_SELF")
    if not name:
        sys.stderr.write(
            "agenttalk: no agent identity: pass --from/--for or set AGENTTALK_SELF in this terminal\n"
            "  example (PowerShell): $env:AGENTTALK_SELF = 'claude'\n"
            "  example (bash):       export AGENTTALK_SELF=claude\n"
        )
        sys.exit(2)
    try:
        validate_agent_name(name)
    except ValueError as e:
        sys.stderr.write(f"agenttalk: {e}\n")
        sys.exit(2)
    _ensure_in_roster(name, roster, label="self")
    return name


def _guard_lead_loop_consumer(store: Store, agent: str, *, verb: str) -> int | None:
    """Reject a cursor-CONSUMING verb (wait/recv/drain/ack) for an agent whose team
    mailbox is owned by a LIVE managed lead-loop lease, UNLESS the caller is the lease
    owner (presents the live lease_id via AGENTTALK_LEAD_LOOP_LEASE). Returns exit 7
    when blocked, else None. Read-only verbs (sync/threads/status/check) are never
    guarded. CLI-layer only: the controller consumes the bus via recv_api in-process
    and is never routed through here, so it is never self-blocked; this guard stops an
    EXTERNAL consumer (a stray window, the model subprocess) from racing/losing the
    controller's records (closes the cursor-loss hole --refuse-stacked-wait misses)."""
    try:
        if not store.is_managed_lead_loop(agent):
            return None  # only a CONFIGURED managed identity is guarded (a cleared /
            # never-managed agent is never blocked, even if a stray lease file exists)
        lease = store.lead_loop_active_owner(agent)
    except Exception:  # noqa: BLE001 - a guard must never crash the verb on a torn lease
        return None
    if not lease:
        return None  # no live owner -> not guarded
    presented = os.environ.get(_LEAD_LOOP_LEASE_ENV)
    if presented and presented == lease.get("lease_id"):
        return None  # the lease owner -> allowed
    owner_pid = lease.get("owner_pid")
    sys.stderr.write(
        f"agenttalk {verb}: refusing to consume {agent!r}'s mailbox - it is owned by a "
        f"managed lead-loop controller (PID {owner_pid}) that consumes the bus in-process; "
        f"an external {verb} would race/lose records. Use read-only sync/threads/status/check "
        f"to inspect, or set {_LEAD_LOOP_LEASE_ENV}=<lease_id> if you ARE the owner.\n")
    return _LEAD_LOOP_GUARD_EXIT


def _resolve_peer(value: str | None, store_cfg: dict, self_name: str) -> str:
    """Pick peer identity: explicit flag wins, else $AGENTTALK_PEER, else
    the single other agent in the roster (if exactly one). Exits 2 if
    none of those resolve, or if the resolved value is not in the roster
    or equals `self_name`.
    """
    roster = store_cfg.get("agents", []) or None
    name = value or os.environ.get("AGENTTALK_PEER")
    if not name:
        others = [a for a in (roster or []) if a != self_name]
        if len(others) == 1:
            return others[0]
        sys.stderr.write(
            "agenttalk: no peer identity: pass --to or set AGENTTALK_PEER in this terminal\n"
            f"  roster: {', '.join(roster or [])}\n"
            "  example (PowerShell): $env:AGENTTALK_PEER = 'codex'\n"
            "  example (bash):       export AGENTTALK_PEER=codex\n"
        )
        sys.exit(2)
    try:
        validate_agent_name(name)
    except ValueError as e:
        sys.stderr.write(f"agenttalk: {e}\n")
        sys.exit(2)
    _ensure_in_roster(name, roster, label="peer")
    if name == self_name:
        sys.stderr.write(
            f"agenttalk: peer '{name}' is the same as self — refusing to self-message.\n"
            "  Pass --to <other-agent> or set AGENTTALK_PEER to a different name.\n"
        )
        sys.exit(2)
    return name


def _ensure_in_roster(name: str, roster: list[str] | None, *, label: str) -> None:
    if not roster:
        return
    if name not in roster:
        sys.stderr.write(
            f"agenttalk: {label} agent '{name}' is not in the project roster {sorted(roster)}.\n"
            "  Check --from/--to/--for or AGENTTALK_SELF/AGENTTALK_PEER for a typo,\n"
            "  or re-init with `agenttalk init --here --agents ...` to add this agent.\n"
        )
        sys.exit(2)


def _thread_row_for(store: Store, agent: str, rid: str):
    """The derived thread row for one rid from ``agent``'s perspective,
    computed ACK-INDEPENDENTLY (empty ``closed_rids``).

    The supersession barrier (`check`, the scoped-wait rescind wake) must
    answer from the validated log alone — a local `ack` masks the thread
    *view*, never the fact that a request was rescinded. Returns ``None``
    when the rid is unknown or not ``agent``'s thread. Read-only: thread
    derivation is a pure function; no cursor/threadstate writes.
    """
    rows = th.derive_threads(
        store.valid_messages(), agent=agent, cursor="", closed_rids=set(),
    )
    for t in rows:
        if t.request_id == rid:
            return t
    return None


def _reply_in_flight(store: Store, t) -> bool:
    """True iff ``t.peer`` has a FRESH composing-intent entry for this thread.

    Freshness = the marker entry's ``at`` is younger than
    COMPOSING_INTENT_STALE_SECONDS (the composing-extension cap — one
    number, one meaning). Deliberately NOT gated on peer heartbeat: only
    wait loops stamp heartbeats, and a peer that is *drafting* is by
    definition not waiting, so a heartbeat condition would invalidate
    every real marker. Corrupt/missing markers read as "not drafting"
    (observational, C-004). Broadcast rows have no single peer — False.
    """
    if getattr(t, "is_broadcast", False):
        return False
    try:
        validate_agent_name(t.peer)
    except ValueError:
        return False
    threads_map = store.read_composing_intent(t.peer).get("threads")
    entry = threads_map.get(t.request_id) if isinstance(threads_map, dict) else None
    if not isinstance(entry, dict):
        return False
    at = entry.get("at")
    if not isinstance(at, str) or not at:
        return False
    normalized = at[:-1] + "+00:00" if at.endswith("Z") else at
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    if dt.tzinfo is None:
        return False
    age = (datetime.now(timezone.utc) - dt).total_seconds()
    return 0 <= age <= COMPOSING_INTENT_STALE_SECONDS


# ------------------------------------------------------------------- handlers

def cmd_init(args: argparse.Namespace) -> int:
    # Resolution precedence mirrors the documented global --root contract so
    # init can't silently diverge from every other command (review H2):
    #   init's own --path/--here  >  global --root  >  $AGENTTALK_ROOT  >  CWD.
    # Before this, init used --path/CWD only, so `agenttalk --root X init`
    # created a SECOND store under CWD — the exact split-brain the #13
    # up-tree guard exists to prevent.
    if args.path:
        root = Path(args.path).resolve()
    elif getattr(args, "root", None):
        root = Path(args.root).resolve()
    elif os.environ.get("AGENTTALK_ROOT"):
        root = Path(os.environ["AGENTTALK_ROOT"]).resolve()
    else:
        root = Path.cwd().resolve()
    store = Store(root)
    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    if not agents:
        sys.stderr.write("agenttalk init: need at least one agent (e.g. --agents claude)\n")
        return 2
    # Up-tree guard (#13): two valid stores at different depths are the
    # real split-brain mechanism behind the production "--root gotcha" —
    # both resolve, neither errors, and two windows silently talk past
    # each other. Refuse to create a nested store unless --force says the
    # nesting is deliberate. A store at the target itself keeps the
    # existing re-init behavior (idempotent without --force).
    if not store.initialized() and not args.force:
        found = find_stores_upward(root.parent)
        if found:
            stores = "\n".join(f"    {p / '.agenttalk'}" for p in found)
            sys.stderr.write(
                f"agenttalk init: refusing to create a nested store at {root} — "
                f"an existing store was found up-tree:\n{stores}\n"
                f"  To JOIN it:   pass --root {found[0]} (or set AGENTTALK_ROOT)\n"
                f"  To NEST anyway (deliberate, e.g. a sandbox): re-run with --force\n"
            )
            return 2
    # store.init() validates the roster (safe names + uniqueness) and
    # raises ValueError on bad input; main() converts that to exit 2.
    cfg = store.init(agents, force=args.force)
    print(f"agenttalk initialized at {store.dir}")
    print(f"  agents:     {', '.join(cfg['agents'])}")
    print(f"  session_id: {cfg['session_id']}")
    # Identity hint: tell the user how to point each terminal at the
    # right agent name. Concrete examples for one- and two-agent rosters.
    print()
    if len(agents) == 1:
        (agent,) = agents
        print("Tip: set this terminal's agent identity before invoking skills:")
        print(f"  PowerShell: $env:AGENTTALK_SELF='{agent}'")
        print(f"  Bash:       export AGENTTALK_SELF={agent}")
    elif len(agents) == 2:
        a, b = agents
        print("Tip: in each terminal, set its agent identity before invoking skills:")
        print(f"  PowerShell (Terminal A): $env:AGENTTALK_SELF='{a}'; $env:AGENTTALK_PEER='{b}'")
        print(f"  PowerShell (Terminal B): $env:AGENTTALK_SELF='{b}'; $env:AGENTTALK_PEER='{a}'")
        print(f"  Bash (Terminal A):       export AGENTTALK_SELF={a} AGENTTALK_PEER={b}")
        print(f"  Bash (Terminal B):       export AGENTTALK_SELF={b} AGENTTALK_PEER={a}")
    else:
        print("Tip: in each terminal, set AGENTTALK_SELF to that terminal's agent name.")
        print("  PowerShell: $env:AGENTTALK_SELF='<name>'")
        print("  Bash:       export AGENTTALK_SELF=<name>")
    print("Commands also accept explicit --from/--to/--for flags as overrides.")
    return 0


STALE_THRESHOLD_SECONDS = 60.0

# Soft cap on concurrent live waiters in one store (fix #4c). When `wait`
# arms and finds more than this many fresh+live `.waiting` markers, it prints
# a non-blocking warning — leftover poll loops from old sessions are the
# accumulation half of the multi-day slowdown. Advisory only; never blocks.
WAITER_SOFTCAP = 8

# An outbound request still unanswered after this long is surfaced as a
# status warning — the peer may have missed it, or you forgot to wait.
OPEN_OUTBOUND_STALE_SECONDS = 600.0

# Managed lead-loop single-consumer guard (lead-loop Slice 1). A live managed
# lead-loop lease (the in-process controller) OWNS its team mailbox; an external
# cursor-CONSUMING CLI call (wait/recv/drain/ack) would race/lose records. The
# owner proves itself by presenting the live lease_id via this env var; exit 7
# is the dedicated "rejected: mailbox owned by a managed controller" code.
_LEAD_LOOP_LEASE_ENV = LEAD_LOOP_LEASE_ENV  # single source: store.LEAD_LOOP_LEASE_ENV
_LEAD_LOOP_GUARD_EXIT = 7
# Dedicated NON-CRASH exit codes for the wrapped lead-loop controller (WP2). They are
# DIAGNOSTIC (operator/logs); the supervisor acts on the matching exit MARKER (read via
# build_report), not the captured exit code. Distinct from a crash (the supervisor
# RELAUNCHES a crash) so a deliberate stand-down / blocked-acquire does not relaunch.
_LEAD_LOOP_BLOCKED_EXIT = 8       # acquire blocked: another live owner holds the lease
_LEAD_LOOP_STOOD_DOWN_EXIT = 9    # clean valid human release/end: stand-down sticks
_LEAD_LOOP_LEASE_LOST_EXIT = 10   # lost the lease mid-run (stolen/torn/force-released)


class _LeadLoopLeaseLost(Exception):
    """Raised when the lead-loop controller has LOST its mailbox lease mid-run (renew
    returned None: stolen / torn / force-released). A HARD loss-of-ownership signal
    that STOPS the loop immediately (no record is consumed without the lease) - the
    controller exits with NO exit marker so the supervisor relaunches it (which
    re-acquires, or HOLDS if another owner is now live). Codex WP2 blocker: a controller
    that lost the lease must not keep consuming the mailbox unguarded until it goes
    stale."""


def _gather_status(store: Store) -> dict:
    """Build the structured status payload shared by both output modes."""
    cfg = store.load_config()
    roles = cfg.get("roles", {}) or {}
    liaison = store.operator_facing()
    msgs = store.all_messages()
    now = datetime.now(timezone.utc)
    # Resolve the lead-loop heartbeat window from supervisor.json (if present) so the
    # status view uses the SAME threshold as the steal path - never the 120s default
    # for a wrapped agent (WP1 contract; avoids armed/heartbeat_stale skew).
    sup_cfg = _load_supervisor_config(store)
    from agenttalk import supervisor as _sup
    sup_agents = (
        sup_cfg.get("agents") if isinstance(sup_cfg.get("agents"), dict) else {}
    )
    supervisor_rows, supervisor_warnings = _status_supervisor_summaries(
        store, now.timestamp(), sup_cfg)
    agents = []
    for a in cfg.get("agents", []):
        hb = store.read_heartbeat(a)
        if hb is None:
            heartbeat_iso: str | None = None
            last_seen_s: float | None = None
            stale: bool | None = None
        else:
            heartbeat_iso = hb.isoformat().replace("+00:00", "Z")
            last_seen_s = (now - hb).total_seconds()
            stale = last_seen_s > STALE_THRESHOLD_SECONDS
        waiting = store.read_waiting(a)
        # Decide whether a waiting marker reflects a LIVE wait or an
        # orphan from a crashed shell. Fresh heartbeat ⇒ live. Stale
        # heartbeat ⇒ orphan. No heartbeat (e.g. --heartbeat-interval 0)
        # ⇒ fall back to the recorded epoch deadline: a bounded wait
        # can't outlive its own deadline (+ a stale-threshold margin).
        if waiting is None:
            waiting_stale: bool | None = None
        elif stale is False:
            waiting_stale = False
        elif stale is True:
            waiting_stale = True
        else:
            dl = waiting.get("deadline_epoch")
            waiting_stale = bool(
                isinstance(dl, (int, float))
                and time.time() > dl + STALE_THRESHOLD_SECONDS
            )
        cfg_agent = sup_agents.get(a) if isinstance(sup_agents.get(a), dict) else {}
        health_timing = _sup.resolve_health_timing(sup_cfg or {}, cfg_agent)
        health = store.read_health(
            a,
            now_epoch=now.timestamp(),
            heartbeat=hb,
            ttl_seconds=health_timing["ttl_seconds"],
            heartbeat_skew_seconds=health_timing["heartbeat_skew_seconds"],
        )
        row = {
            "name": a,
            "role": roles.get(a),
            "cursor": store.cursor(a) or None,
            "unread": len(store.unread_for(a)),
            "heartbeat": heartbeat_iso,
            "last_seen_seconds": (round(last_seen_s, 3)
                                  if last_seen_s is not None else None),
            "stale": stale,
            "health": health,
            "waiting": waiting,
            "waiting_stale": waiting_stale,
        }
        sup_row = supervisor_rows.get(a)
        if isinstance(sup_row, dict):
            decision = sup_row.get("decision")
            if isinstance(decision, dict):
                row["supervisor"] = {"decision": decision}
            if isinstance(sup_row.get("lead_liveness"), dict):
                row["lead_liveness"] = sup_row["lead_liveness"]
            effective = sup_row.get("health_effective_state")
            if isinstance(effective, str):
                row["health_display"] = {
                    "state": effective,
                    "source": "bus_last_seen" if effective != health.get("state") else "health",
                }
        if a == liaison:
            row["operator_facing"] = True  # additive: absent unless set
        # Managed lead-loop visibility (additive: present only when the agent is
        # configured managed OR a lease file exists). The doctor check is the
        # gating signal (managed-unarmed=ERROR); this row is the inspectable state.
        if store.is_managed_lead_loop(a) or store.read_lead_loop_lease(a) is not None:
            hsa = lead_loop_runtime.resolve_timing(
                store, a, supervisor_config=sup_cfg or None)["heartbeat_stale_after"]
            row["lead_loop"] = store.lead_loop_state(a, heartbeat_stale_after=hsa)
        agents.append(row)
    invalid = store.list_invalid_messages()
    quarantined = store.quarantined_count()
    dead_lettered = _unresolved_dead_letter_count(store)
    signing_enforced = store.signing_enforced()
    # project_id is path-derived; surfaces here for diagnostics
    project_id = store.project_id()
    lead_chat = _lead_chat_status(store)
    try:
        from agenttalk import coordination_stall as _coordination_stall

        coordination = _coordination_stall.build_snapshot(
            store,
            supervisor_config=sup_cfg,
        )
    except Exception:  # noqa: BLE001 - status stays fail-safe
        coordination = {"items": [], "diagnostics": []}
    coordination_items = coordination.get("items") or []
    warnings = (
        _status_warnings(agents) + _thread_warnings(store, cfg) + supervisor_warnings
    )
    for item in coordination_items:
        reason = item.get("reason") if isinstance(item, dict) else None
        if isinstance(reason, str) and reason and reason not in warnings:
            warnings.append(reason)
    payload = {
        "root": str(store.root),
        "session_id": cfg.get("session_id"),
        "project_id": project_id,
        "signing_enforced": signing_enforced,
        "message_count": len(msgs),
        "invalid_messages": [{"id": mid, "reason": reason} for mid, reason in invalid],
        "agents": agents,
        "stale_threshold_seconds": STALE_THRESHOLD_SECONDS,
        "warnings": warnings,
    }
    if os.name == "nt" and (
        (store.dir / psh.SELECTION_FILENAME).exists()
        or (store.dir / "supervisor.ps1").exists()
    ):
        try:
            host = supervisor_lifecycle.read_selected_host(store)
            payload["powershell_host"] = {
                **psh.selection_public_view(host),
                "status": "warn" if host.get("_warning") else "ok",
                "warning": host.get("_warning"),
                "age_seconds": host.get("_age_seconds"),
            }
        except (OSError, supervisor_lifecycle.SupervisorLifecycleError) as exc:
            payload["powershell_host"] = {
                "status": "error",
                "warning": str(exc),
            }
    if coordination_items:
        payload["coordination_stalls"] = coordination_items
    if quarantined:
        payload["quarantined"] = quarantined  # additive: absent when zero
    if dead_lettered:
        payload["dead_lettered_count"] = dead_lettered  # additive: absent when zero
    if lead_chat.get("request_id"):
        payload["lead_chat"] = lead_chat
    if signing_enforced:
        health = _signing.inspect_key(project_id, store.root)
        payload["hmac_key"] = health.to_dict()
    return payload


def _lead_chat_status(store: Store) -> dict:
    """CLI-visible lead-chat identity/rid view using the single store deriver."""
    try:
        operator, lead = store.lead_chat_identities()
        request_id = store.lead_chat_request_id(operator=operator, lead=lead)
        liveness = store.lead_chat_liveness(lead=lead)
    except ValueError as e:
        return {
            "available": False,
            "status": "unavailable",
            "error": "lead_chat_identity_denied",
            "detail": str(e),
        }
    return {
        "available": bool(liveness.get("available")),
        "status": liveness.get("status") or "unavailable",
        "operator_identity": operator,
        "lead": lead,
        "request_id": request_id,
    }


def _status_warnings(agents: list[dict]) -> list[str]:
    """Actionable diagnostics derived from the per-agent status rows.

    An agent with unread but a never-set cursor has been reading with plain
    ``recv`` (or not at all), so its read state is a lie. Coordination stalls
    come only from the explicit-edge detector; generic concurrent waiters are
    healthy idle and never imply deadlock.
    """
    warnings: list[str] = []
    for a in agents:
        if a["unread"] > 0 and not a["cursor"]:
            n = a["unread"]
            warnings.append(
                f"{a['name']}: {n} unread but cursor=(none) — never acked. "
                f"It is likely peeking with `recv`; run "
                f"`agenttalk drain --for {a['name']}` to consume + advance."
            )
    return warnings


def _read_supervisor_state(store: Store, path_value: str | None = None) -> dict:
    path = Path(path_value) if path_value else store.dir / "supervisor-state.json"
    return sup.load_supervisor_state(path)


def _read_supervisor_snapshot(store: Store, path_value: str | None = None) -> list[dict] | None:
    path = Path(path_value) if path_value else store.dir / "supervisor-snapshot.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return None
    return raw if isinstance(raw, list) else None


def _status_supervisor_summaries(store: Store, now_epoch: float,
                                 sup_cfg: dict) -> tuple[dict[str, dict], list[str]]:
    try:
        obs = sup.build_supervisor_observation(
            store,
            now_epoch=now_epoch,
            state=_read_supervisor_state(store),
            supervisor_config=sup_cfg,
            snapshot=_read_supervisor_snapshot(store),
            event_limit=0,
            lead_liveness_stale_after_seconds=STALE_THRESHOLD_SECONDS,
        )
    except Exception as exc:
        return {}, [f"supervisor_assessment_unavailable:{type(exc).__name__}"]
    rows: dict[str, dict] = {}
    for item in obs.get("agents") or []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        decision = item.get("decision") if isinstance(item.get("decision"), dict) else None
        health = item.get("health") if isinstance(item.get("health"), dict) else {}
        lead_liveness = item.get("lead_liveness") if isinstance(item.get("lead_liveness"), dict) else None
        effective = health.get("effective_state")
        raw_state = health.get("state")
        if decision is None and lead_liveness is None and effective == raw_state:
            continue
        row: dict[str, object] = {}
        if decision is not None:
            row["decision"] = decision
        if isinstance(effective, str) and effective != raw_state:
            row["health_effective_state"] = effective
        if lead_liveness is not None:
            row["lead_liveness"] = lead_liveness
        rows[item["name"]] = row
    warnings = []
    ring = obs.get("event_ring") if isinstance(obs.get("event_ring"), dict) else {}
    for warning in ring.get("warnings") or []:
        if isinstance(warning, str):
            warnings.append(warning)
    return rows, warnings


def _closed_rids(store: Store, agent: str) -> set[str]:
    """request_ids ``agent`` has explicitly closed via `ack --to-request`."""
    return {
        rid for rid, e in store.read_threadstate(agent).items()
        if isinstance(e, dict) and e.get("closed") is True
    }


# ----------------------------------------------------- compaction (#2)
#
# `compact` archives a safe contiguous PREFIX of valid messages (id <
# keep_floor) to the COLD archived/compacted/ dir. The keep_floor policy is the
# whole safety story; it lives here (not in Store) because it reuses thread
# derivation, and threads.py imports Store. Store provides only the safe mover.

COMPACT_DEFAULTS = {
    "enabled": False,          # the automatic opportunistic trigger (OFF for v1)
    "keep_count": 1000,        # always keep at least this many newest messages
    "keep_age_days": 30.0,     # always keep everything younger than this
    # Auto check fires only above this live count. Kept ABOVE keep_count so the
    # auto path never wakes in a dead band where archiving is impossible
    # (live <= keep_count => count_floor "" => nothing to do but stamp).
    "trigger_threshold": 1200,
    "min_interval_seconds": 3600.0,  # auto check throttle
}

# Thread states whose entire request group must stay LIVE (never compacted):
# the ball is on someone, or it was rescinded and the supersession must remain
# derivable. Everything else (plain "closed") is archivable.
_COMPACT_PROTECTED_STATES = frozenset(
    {"owed-inbound", "reply-waiting", "open-outbound", "closed-superseded"})


def _compact_config(cfg: dict) -> dict:
    """Resolve compact knobs from config over the defaults (missing key =
    default; an older store with no ``compact`` block just gets defaults)."""
    raw = cfg.get("compact")
    raw = raw if isinstance(raw, dict) else {}
    out = dict(COMPACT_DEFAULTS)
    for k in out:
        if k in raw:
            out[k] = raw[k]
    return out


def _parse_ts(ts: str) -> datetime | None:
    """Parse a message ``ts`` to an aware datetime, or None if unparseable."""
    if not isinstance(ts, str) or not ts:
        return None
    norm = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    try:
        dt = datetime.fromisoformat(norm)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _min_floor(values: list[str | None]) -> str | None:
    """Combine keep-floor candidates. None = "this dimension imposes no
    restriction" (skipped). "" = a fail-safe ("keep everything") and WINS.
    Otherwise the lexical MIN — the most conservative floor keeps the most."""
    present = [v for v in values if v is not None]
    if not present:
        return None
    if any(v == "" for v in present):
        return ""
    return min(present)


def _compute_keep_floor(store: Store, cfg: dict, *, keep_count: int,
                        keep_age_days: float, now: datetime | None = None):
    """Return ``(keep_floor, capped_by, components)``.

    Archive valid messages with ``id < keep_floor``. ``""`` means a fail-safe
    fired — archive NOTHING. Pure read; reuses the SAME validated message set
    and per-agent cursor / closed_rids / retired that ``threads``/``sync`` use,
    so an ``ack --to-request`` thread stops pinning compaction.
    """
    now = now or datetime.now(timezone.utc)
    components: dict[str, str | None] = {}

    # cursor: never archive a message unread by an active recipient. id <
    # min(active cursors) => id < every active cursor => already read by
    # whichever single agent it is addressed to. An active agent that never
    # consumed (cursor "") fails safe to "keep everything".
    active = cfg.get("agents") or []
    if not active:
        components["cursor"] = ""
    else:
        cursors = [store.cursor(a) for a in active]
        components["cursor"] = "" if any(c == "" for c in cursors) else min(cursors)

    # Need the validated log for epoch + thread + keeptail; if it can't be
    # read, fail safe.
    try:
        msgs = store.valid_messages()
        epoch = store.current_epoch()
    except (ValueError, OSError, FileNotFoundError):
        return "", "fail-safe", components

    components["epoch"] = epoch  # None => no barrier => no restriction

    # thread: keep the WHOLE request group of any protected row. Earliest group
    # id (opener) so opener+replies+rescind provenance all stay live.
    try:
        retired = set(store.retired_agents())
        protected: set[str] = set()
        for a in active:
            for t in th.derive_threads(msgs, agent=a, cursor=store.cursor(a),
                                       now=now, closed_rids=_closed_rids(store, a),
                                       retired=retired):
                if t.state in _COMPACT_PROTECTED_STATES:
                    protected.add(t.request_id)
        if protected:
            earliest: dict[str, str] = {}
            for m in msgs:
                rid = (m.meta or {}).get("request_id")
                if isinstance(rid, str) and rid in protected:
                    if rid not in earliest or m.id < earliest[rid]:
                        earliest[rid] = m.id
            components["thread"] = min(earliest.values()) if earliest else None
        else:
            components["thread"] = None
    except Exception:  # noqa: BLE001 — a derivation error must archive NOTHING
        components["thread"] = ""

    # keeptail: keep newest keep_count AND everything younger than keep_age
    # (the UNION => the lower of the two boundaries).
    ids = sorted(m.id for m in msgs)
    count_floor = "" if len(ids) <= keep_count else ids[len(ids) - keep_count]
    cutoff = now - timedelta(days=keep_age_days)
    young = [m.id for m in msgs
             if (_parse_ts(m.ts) is not None and _parse_ts(m.ts) >= cutoff)]
    age_floor = min(young) if young else None
    components["keeptail"] = _min_floor([count_floor, age_floor])

    keep_floor = _min_floor(list(components.values())) or ""
    capped = [k for k, v in components.items() if v is not None and v == keep_floor]
    capped_by = ",".join(capped) if capped else "none"
    return keep_floor, capped_by, components


def _run_compaction(store: Store, cfg: dict, *, keep_count: int,
                    keep_age_days: float, dry_run: bool,
                    now: datetime | None = None) -> dict:
    """Compute the floor and move (or plan) the prefix. Stamps the throttle
    record on a real run."""
    keep_floor, capped_by, components = _compute_keep_floor(
        store, cfg, keep_count=keep_count, keep_age_days=keep_age_days, now=now)
    records = store.archive_messages_below(keep_floor, dry_run=dry_run)
    if not dry_run:
        store.write_compact_stamp({
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "at_epoch": time.time(),
            "keep_floor": keep_floor, "capped_by": capped_by,
            "archived": len(records),
        })
    return {"dry_run": dry_run, "keep_floor": keep_floor, "capped_by": capped_by,
            "components": components, "archived": records}


def _maybe_auto_compact(store: Store, *, now_epoch: float) -> None:
    """Opportunistic compaction at wait-arm: OFF by default, only above the
    threshold, throttled, and totally fail-safe — it must NEVER raise into the
    wait loop."""
    try:
        cfg = store.load_config()
        cc = _compact_config(cfg)
        if not cc.get("enabled"):
            return
        if store.live_message_count() <= int(cc["trigger_threshold"]):
            return
        stamp = store.read_compact_stamp()
        if stamp is not None:
            last = stamp.get("at_epoch")
            if (isinstance(last, (int, float))
                    and now_epoch - last < float(cc["min_interval_seconds"])):
                return
        res = _run_compaction(store, cfg, keep_count=int(cc["keep_count"]),
                              keep_age_days=float(cc["keep_age_days"]),
                              dry_run=False)
        if res["archived"]:
            sys.stderr.write(
                f"agenttalk: auto-compacted {len(res['archived'])} message(s) "
                f"to archived/compacted/ (capped by {res['capped_by']}).\n")
    except Exception:  # noqa: BLE001 — compaction must never break a wait
        return


def cmd_compact(args: argparse.Namespace) -> int:
    store = _get_store(args)
    try:
        cfg = store.load_config()
    except (ValueError, OSError, FileNotFoundError) as e:
        sys.stderr.write(f"agenttalk: {e}\n")
        return 2
    cc = _compact_config(cfg)
    keep_count = (args.keep_count if args.keep_count is not None
                  else int(cc["keep_count"]))
    keep_age = (args.keep_age_days if args.keep_age_days is not None
                else float(cc["keep_age_days"]))
    res = _run_compaction(store, cfg, keep_count=keep_count,
                          keep_age_days=keep_age, dry_run=args.dry_run)
    n = len(res["archived"])
    if args.json:
        print(json.dumps({
            "dry_run": res["dry_run"],
            "keep_floor": res["keep_floor"],
            "capped_by": res["capped_by"],
            "archived_count": n,
            "archived_ids": [r["id"] for r in res["archived"]],
            "components": res["components"],
        }, ensure_ascii=False))
        return 0
    if not res["keep_floor"]:
        print(f"compact: nothing archived — a keep-everything fail-safe held "
              f"(capped by {res['capped_by']}).")
        return 0
    verb = "would archive" if res["dry_run"] else "archived"
    print(f"compact: {verb} {n} message(s) to archived/compacted/ "
          f"(keep_floor={res['keep_floor']}, capped by {res['capped_by']}).")
    return 0


def _thread_warnings(store: Store, cfg: dict) -> list[str]:
    """Thread-correlation warnings shared with `agenttalk threads`.

    Derived from the SAME validated message set + derivation as the
    `threads` command (one source of truth — no drift). Surfaces the two
    "forgot to check if the reviewer replied" footguns per agent:
      1. a correlated response is sitting unread in the inbox; and
      2. an outbound request has gone unanswered past the stale window.
    """
    roster = cfg.get("agents", []) or []
    try:
        msgs = store.valid_messages()
    except (ValueError, OSError, FileNotFoundError):
        return []
    now = datetime.now(timezone.utc)
    liaison = store.operator_facing()
    out: list[str] = []
    pending_escalations_exist = False
    for a in roster:
        rows = th.derive_threads(msgs, agent=a, cursor=store.cursor(a), now=now,
                                 closed_rids=_closed_rids(store, a),
                                 retired=set(store.retired_agents()))
        waiting = [t for t in rows if t.state == "reply-waiting"]
        if waiting:
            ids = ", ".join(t.request_id for t in waiting[:3])
            more = "" if len(waiting) <= 3 else f" (+{len(waiting) - 3} more)"
            out.append(
                f"{a}: {len(waiting)} unconsumed response(s) in inbox "
                f"[{ids}{more}] — run `agenttalk drain --for {a}` then act "
                f"(see `agenttalk threads --for {a}`)."
            )
        stale = [
            t for t in rows
            if t.state == "open-outbound"
            and t.age_seconds is not None
            and t.age_seconds > OPEN_OUTBOUND_STALE_SECONDS
            # #14: a fresh "reply in flight" marker from the peer means
            # the silence is explained — suppress the stale warning for
            # exactly that thread (and only that thread).
            and not _reply_in_flight(store, t)
        ]
        if stale:
            t0 = stale[0]
            if t0.is_broadcast:
                who = f"{len(t0.pending)}/{len(t0.audience)} ({', '.join(t0.pending)})"
            else:
                who = t0.peer
            out.append(
                f"{a}: outbound {t0.opener_kind} {t0.request_id} still "
                f"awaiting {who} after {_format_age(t0.age_seconds)} — "
                f"check `agenttalk threads --for {a}`."
            )
        # #18: stale pending escalations are operator questions nobody is
        # answering — the loudest possible diagnostic, addressed to the
        # liaison (only its perspective is checked, so each escalation
        # warns once, not once per roster member).
        if a == liaison:
            stale_esc = [
                t for t in rows
                if t.needs_operator and t.operator_state == "pending"
                and t.age_seconds is not None
                and t.age_seconds > OPEN_OUTBOUND_STALE_SECONDS
            ]
            if stale_esc:
                ids = ", ".join(t.request_id for t in stale_esc[:3])
                out.append(
                    f"{a}: {len(stale_esc)} operator escalation(s) pending "
                    f"past {_format_age(OPEN_OUTBOUND_STALE_SECONDS)} "
                    f"[{ids}] — surface them to the operator and reply "
                    f"(`agenttalk sync --for {a}`)."
                )
        if any(t.needs_operator and t.operator_state == "pending"
               and t.role == "opener" for t in rows):
            pending_escalations_exist = True
        # Incomplete fan-out (#16, 0.15.0): a broadcaster row whose frozen
        # batch_total exceeds the copies actually on disk means a partial
        # fan-out survived (crash/exit-5 without re-send). Warn ONCE per
        # batch (broadcaster perspective only), name the missed members
        # from the frozen audience_resolved, and suppress once the thread
        # is rescinded (closed-superseded) - rescind is a valid resolution.
        for trow in rows:
            if (trow.is_broadcast and trow.role == "opener"
                    and trow.batch_total is not None
                    and len(trow.audience) < trow.batch_total
                    and trow.state != "closed-superseded"):
                planned: list[str] = []
                for m in msgs:
                    if ((m.meta or {}).get("broadcast_id") == trow.request_id
                            and (m.meta or {}).get("audience_resolved")):
                        planned = [x for x in
                                   (m.meta or {})["audience_resolved"].split(",") if x]
                        break
                missed = [x for x in planned if x not in trow.audience]
                out.append(
                    f"{a}: incomplete fan-out {trow.request_id} - "
                    f"{len(trow.audience)}/{trow.batch_total} copies exist"
                    + (f", missed: {', '.join(missed)}" if missed else "")
                    + f". Recover with `agenttalk broadcast --from {a} "
                      f"--resume {trow.request_id}`, or rescind the thread."
                )
    if pending_escalations_exist and liaison is None:
        out.append(
            "operator escalations are pending but NO operator-facing agent "
            "is configured — they are routed to whoever was targeted, with "
            "no liaison contract. Run `agenttalk roster set-operator-facing "
            "<agent>`."
        )
    return out


def _inject_next(d: dict, t) -> None:
    """Add next_action / next_owner to a thread row dict when derivable (#19).

    These are derived onto the Thread (WP02) but deliberately NOT emitted by
    Thread.to_dict() — they appear on every open thread, so emitting them there
    would change the baseline JSON shape and trip the 0.15.0 additivity gates.
    Surfacing is done here, at the CLI layer, conditionally (terminal threads
    omit both)."""
    if getattr(t, "next_action", None) is not None:
        d["next_action"] = t.next_action
    if getattr(t, "next_owner", None) is not None:
        d["next_owner"] = t.next_owner


def cmd_threads(args: argparse.Namespace) -> int:
    """Show open request/reply threads from one agent's perspective.

    The "did the reviewer ever respond?" answer. Derives every
    correlated thread (review-request/review-result,
    proposal/proposal-response, question/answer) from validated
    messages and labels each with a single actionable state. Default
    view hides closed threads; `--all` includes them. `--json` emits the
    stable structured contract for skills to parse.
    """
    store = _get_store(args)
    cfg = store.load_config()
    agent = _resolve_self(args.agent, roster=cfg.get("agents") or [])
    all_rows = th.derive_threads(
        store.valid_messages(), agent=agent, cursor=store.cursor(agent),
        closed_rids=_closed_rids(store, agent),
        retired=set(store.retired_agents()),
    )
    cnts = th.counts(all_rows)
    shown = all_rows if args.all else [t for t in all_rows if t.state in th.ACTIONABLE_STATES]

    if args.json:
        out_rows = []
        for t in shown:
            d = t.to_dict()
            # reply-in-flight is display-layer (reads the peer's marker),
            # additive, and only meaningful while you're the one waiting.
            if t.state == "open-outbound" and _reply_in_flight(store, t):
                d["reply_in_flight"] = True
            # next-owner / next-action hint (#19 Phase A): derived onto the
            # Thread by WP02; surfaced HERE (not in to_dict, which stays
            # shape-stable). Conditional → terminal threads omit both.
            _inject_next(d, t)
            out_rows.append(d)
        print(json.dumps({
            "agent": agent,
            "threads": out_rows,
            "counts": cnts,
        }, indent=2))
        return 0

    summary = " ".join(f"{k}={v}" for k, v in cnts.items())
    print(f"threads for {agent}:  ({summary})")
    if not shown:
        scope = "threads" if args.all else "actionable threads"
        print(f"  (no {scope})")
        return 0
    label = {
        "reply-waiting": "REPLY-WAITING",
        "owed-inbound": "OWED-INBOUND ",
        "open-outbound": "OPEN-OUTBOUND",
        "closed": "closed       ",
        "closed-superseded": "SUPERSEDED   ",
    }
    for t in shown:
        age = _format_age(t.age_seconds) if t.age_seconds is not None else "?"
        flag = " (unread)" if t.unread else ""
        subj = f'  "{t.subject}"' if t.subject else ""
        bcast = ""
        if t.is_broadcast:
            bcast = f"  responded={len(t.responded)}/{len(t.audience)}"
            if t.pending:
                bcast += f" pending=[{', '.join(t.pending)}]"
        extra = ""
        if t.state == "closed-superseded":
            extra = f"  rescinded-by={t.rescinded_by}"
        if t.is_broadcast and t.responded_na:
            extra += f"  na=[{', '.join(t.responded_na)}]"
        if t.na_response:
            extra += "  (n/a)"
        if t.needs_operator:
            extra += f"  operator={t.operator_state}"
        if t.state == "open-outbound" and _reply_in_flight(store, t):
            extra += "  (reply in flight)"
        print(
            f"  [{label.get(t.state, t.state)}] {t.request_id:<16} "
            f"{t.opener_kind:<15} peer={t.peer:<10} age={age:<8}{subj}{flag}{bcast}{extra}"
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = _get_store(args)
    payload = _gather_status(store)
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    print(f"root:       {payload['root']}")
    print(f"session_id: {payload['session_id']}")
    print(f"agents:     {', '.join(a['name'] for a in payload['agents'])}")
    print(f"messages:   {payload['message_count']}")
    if "powershell_host" in payload:
        host = payload["powershell_host"]
        print(
            "powershell: "
            f"{host.get('status', 'unknown').upper()} "
            f"{host.get('path', 'unavailable')}"
        )
        if host.get("warning"):
            print(f"  warning:  {host['warning']}")
    if payload["signing_enforced"]:
        hk = payload.get("hmac_key") or {}
        status_label = "OK" if hk.get("exists") and hk.get("readable") and not hk.get("mode_warning") else "PROBLEM"
        print(f"hmac:       enforced · key {status_label} ({hk.get('path', '?')})")
        if hk.get("mode_warning"):
            print(f"  warning:  {hk['mode_warning']}")
        if hk.get("in_project_dir"):
            print("  warning:  key file is INSIDE the project — defeats the defense;")
            print("            move it under ~/.config/agenttalk/keys/")
    legacy_flag = store.legacy_require_signatures_flag()
    legacy_pid = store.legacy_config_project_id()
    if legacy_flag is not None or legacy_pid is not None:
        print("hmac:       NOTE: legacy fields in config.json are IGNORED")
        print("            (enforcement is anchored to the per-user key file at the")
        print("             PATH-DERIVED project_id, not to anything in config.json).")
        if legacy_flag is not None:
            print(f"            legacy require_signatures = {legacy_flag}")
        if legacy_pid is not None:
            print(f"            legacy project_id = {legacy_pid}")
    if payload["invalid_messages"]:
        n = len(payload["invalid_messages"])
        print(f"INVALID:    {n} message{'s' if n != 1 else ''} failed schema/roster validation "
              f"(see `agenttalk status --json` for details under invalid_messages[]; "
              f"`agenttalk prune --invalid --dry-run` to inspect)")
    if payload.get("quarantined"):
        print(f"quarantined: {payload['quarantined']} file(s) in .agenttalk/quarantine/ (recoverable)")
    if payload.get("dead_lettered_count"):
        print(f"dead-letter: {payload['dead_lettered_count']} poison message(s) "
              "(see `agenttalk dead-letter list`; recoverable via requeue)")
    for a in payload["agents"]:
        cursor = a["cursor"] or "(none)"
        if a["heartbeat"] is None:
            seen = "(no heartbeat)"
        else:
            seen = f"last_seen={_format_age(a['last_seen_seconds'])}"
            if a["stale"]:
                seen += " (stale)"
        if a.get("waiting"):
            seen += " waiting(stale)" if a.get("waiting_stale") else " waiting"
        h = a.get("health") if isinstance(a.get("health"), dict) else {}
        h_display = a.get("health_display") if isinstance(a.get("health_display"), dict) else {}
        h_state = h_display.get("state") if isinstance(h_display.get("state"), str) else (
            h.get("state") if isinstance(h.get("state"), str) else "unknown"
        )
        h_age = h.get("age_seconds")
        seen += f" health={h_state}"
        if isinstance(h_age, (int, float)):
            seen += f"/{_format_age(h_age)}"
        sv = a.get("supervisor") if isinstance(a.get("supervisor"), dict) else {}
        dec = sv.get("decision") if isinstance(sv.get("decision"), dict) else None
        if dec:
            seen += f" supervisor={dec.get('state', '?')}/{dec.get('action', '?')}"
        role = f" role={a['role']}" if a.get("role") else ""
        of = " [operator-facing]" if a.get("operator_facing") else ""
        print(f"  {a['name']:<10}{role}{of} cursor={cursor:<32} unread={a['unread']:<3} {seen}")
    for w in payload.get("warnings", []):
        print(f"WARN:       {w}")
    return 0


def cmd_supervisor(args: argparse.Namespace) -> int:
    """Lead-readable supervisor assessment. Read-only and advisory."""
    store = _get_store(args)
    now = args.now if args.now is not None else time.time()
    config = _load_supervisor_config(store)
    try:
        payload = sup.build_supervisor_observation(
            store,
            now_epoch=now,
            state=_read_supervisor_state(store, args.state_file),
            supervisor_config=config,
            snapshot=_read_supervisor_snapshot(store, args.snapshot_file),
            event_limit=max(0, int(args.events)),
            lead_liveness_stale_after_seconds=STALE_THRESHOLD_SECONDS,
        )
    except Exception as exc:
        payload = {
            "schema_version": 1,
            "root": str(store.root),
            "now_epoch": float(now),
            "now": datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z"),
            "agents": [],
            "report": None,
            "plan": None,
            "event_ring": {
                "path": str(sup.supervisor_events_path(store)),
                "cap": sup.SUPERVISOR_EVENT_RING_CAP,
                "events": [],
                "warnings": [f"supervisor_read_unavailable:{type(exc).__name__}"],
            },
        }
    if args.json:
        try:
            rendered = json.dumps(payload, indent=2, allow_nan=False)
        except ValueError:
            payload = _json_scrub_nonfinite(payload)
            if isinstance(payload.get("event_ring"), dict):
                warnings = payload["event_ring"].setdefault("warnings", [])
                if isinstance(warnings, list):
                    warnings.append("supervisor_json_nonfinite_sanitized")
            rendered = json.dumps(payload, indent=2, allow_nan=False)
        print(rendered)
        return 0
    print(f"root:       {payload['root']}")
    print(f"supervisor: events cap={payload.get('event_ring', {}).get('cap')}")
    for item in payload.get("agents") or []:
        if not isinstance(item, dict):
            continue
        decision = item.get("decision") if isinstance(item.get("decision"), dict) else None
        if decision:
            plan = f"{decision.get('state', '?')}/{decision.get('action', '?')}"
            reason = decision.get("reason") or ""
        else:
            plan = "UNMANAGED"
            reason = "not auto_restart in supervisor.json"
        health = item.get("health") if isinstance(item.get("health"), dict) else {}
        hb = "stale" if item.get("heartbeat_stale") else "fresh"
        age = item.get("heartbeat_age_seconds")
        hb_age = f"/{_format_age(age)}" if isinstance(age, (int, float)) else ""
        rr = item.get("restart_request") if isinstance(item.get("restart_request"), dict) else {}
        flags = []
        if rr.get("pending"):
            flags.append(f"restart_by={rr.get('requested_by') or '?'}")
        elif rr.get("blocked"):
            flags.append(
                "restart_blocked=process_tree_hold"
                f" requested_by={rr.get('requested_by') or '?'}"
            )
        hold = item.get("config_blocked_hold")
        if isinstance(hold, dict) and hold.get("present"):
            flags.append("config_blocked")
        plan_health = decision.get("health") if isinstance(decision, dict) else None
        plan_warnings = (
            plan_health.get("warnings")
            if isinstance(plan_health, dict) and isinstance(plan_health.get("warnings"), list)
            else []
        )
        for warning in plan_warnings:
            if isinstance(warning, str):
                flags.append(f"plan_health={warning}")
        print(
            f"  {item.get('name', '?'):<10} {plan:<32} "
            f"health={health.get('effective_state', health.get('state', 'unknown'))} "
            f"heartbeat={hb}{hb_age} {reason}"
        )
        if flags:
            print(f"    {' '.join(flags)}")
    ring = payload.get("event_ring") if isinstance(payload.get("event_ring"), dict) else {}
    for warning in ring.get("warnings") or []:
        print(f"WARN:       {warning}")
    events = ring.get("events") if isinstance(ring.get("events"), list) else []
    if events:
        print("recent supervisor events:")
        for event in events[-int(args.events):]:
            if not isinstance(event, dict):
                continue
            if event.get("kind") == "agent_decision":
                print(
                    f"  {event.get('at', '?')} {event.get('agent', '?')}: "
                    f"{event.get('state', '?')}/{event.get('action', '?')} "
                    f"{event.get('reason_code', '')}"
                )
            elif event.get("kind") == "poll_summary":
                print(
                    f"  {event.get('at', '?')} summary: "
                    f"{event.get('healthy_idle', 0)}/{event.get('planned_agents', 0)} healthy"
                )
    return 0


def _format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds / 60)}min ago"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"


def _warn_owed_decision_to_peer(store, sender: str, recipient: str,
                                outgoing_request_id: str | None) -> None:
    """Soft, best-effort pre-send nudge (0.24.0, feedback 3.2).

    If the sender currently owes the RECIPIENT an open *decision-request* — a
    `proposal` or an operator escalation (``needs_operator``) — warn before
    sending unrelated traffic, so a fresh message doesn't cross an open
    decision the peer is waiting on. Suppressed when this message is itself a
    reply on that same ``request_id``, and silent for non-decision traffic
    (plain question/review/note). NEVER blocks or fails the send: any
    thread-derivation error is swallowed (the warning is advisory only).
    """
    try:
        rows = th.derive_threads(
            store.valid_messages(), agent=sender, cursor=store.cursor(sender),
            closed_rids=_closed_rids(store, sender),
        )
        owed = [
            t for t in rows
            if t.state == "owed-inbound" and t.peer == recipient
            and (t.opener_kind == "proposal" or t.needs_operator)
            and t.request_id != outgoing_request_id
        ]
        if not owed:
            return
        labels = ", ".join(
            f"{'operator escalation' if t.needs_operator else 'proposal'} "
            f"{t.request_id}"
            for t in owed
        )
        sys.stderr.write(
            f"agenttalk send: warning: you owe {recipient} an open "
            f"decision-request ({labels}) — answer or rescind it before "
            f"unrelated traffic (this message was still sent).\n"
        )
    except Exception:
        return  # advisory only; a derivation failure must never disturb the send


_WRAPPER_GENERATION_ENV = "AGENTTALK_WRAPPER_GENERATION"
_INBOUND_REQUEST_ID_ENV = "AGENTTALK_INBOUND_REQUEST_ID"


def _prepare_await_reply(
    store: Store,
    *,
    sender: str,
    kind: str,
    meta: dict,
    source: str,
    enabled: bool,
) -> dict | None:
    """Validate and build a body-free across-turn wrapped wait record."""
    if not enabled:
        return None
    if kind not in OPENER_KINDS:
        raise ValueError(
            "--await-reply requires a thread-opening kind: question, "
            "review-request, or proposal"
        )
    generation = os.environ.get(_WRAPPER_GENERATION_ENV)
    if not (
        isinstance(generation, str)
        and generation
        and store.wrapper_wait_generation(sender) == generation
    ):
        raise ValueError(
            "--await-reply is only valid inside the current managed wrapper turn"
        )
    request_id = meta.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise ValueError("--await-reply requires a tracked request_id")
    parent = os.environ.get(_INBOUND_REQUEST_ID_ENV)
    if parent:
        existing = meta.get("parent_request")
        if existing is not None and existing != parent:
            raise ValueError(
                "--await-reply parent_request conflicts with the current inbound request"
            )
        meta["parent_request"] = parent
    return {
        "schema_version": 1,
        "agent": sender,
        "request_id": request_id,
        "wrapper_generation": generation,
        "wait_token": f"await-{uuid.uuid4().hex}",
        "started_at": _iso_now(),
        "source": source,
    }


def _register_await_reply(store: Store, record: dict | None, *, quiet: bool) -> None:
    if record is None:
        return
    if store.wrapper_wait_generation(record["agent"]) != record["wrapper_generation"]:
        sys.stderr.write(
            "agenttalk: warning: wrapper generation changed after send; "
            "reply-await marker was not recorded\n"
        )
        return
    try:
        store.write_awaiting(record["agent"], record)
    except (OSError, ValueError) as exc:
        sys.stderr.write(
            f"agenttalk: warning: reply-await marker was not recorded: {exc}\n"
        )
        return
    if not quiet:
        print(f"await_reply_token={record['wait_token']}")


def cmd_send(args: argparse.Namespace) -> int:
    # rescind/end have dedicated commands that handle multi-recipient fan-out
    # and anchoring; a hand-rolled `send --kind rescind` would only address one
    # recipient and could half-supersede a multi-party thread (review nit).
    if args.kind in ("rescind", "end"):
        sys.stderr.write(
            f"agenttalk send: --kind {args.kind} is not allowed via `send` — use "
            f"the dedicated `agenttalk {args.kind}` command, which handles "
            f"fan-out/anchoring correctly.\n")
        return 2
    store = _get_store(args)
    cfg = store.load_config()
    roster = cfg.get("agents") or []
    # #19 FR-004: give a tombstone-specific explanation before the generic
    # "not in roster" path. A retired identity is removed from the active
    # roster, so it would otherwise just look like an unknown agent.
    retired = set(store._retired_names(cfg))
    cand = args.sender or os.environ.get("AGENTTALK_SELF")
    if cand in retired:
        sys.stderr.write(
            f"agenttalk send: {cand!r} is retired (a tombstone) and cannot "
            f"send (#19). Its history stays valid; see `agenttalk roster`.\n")
        return 2
    sender = _resolve_self(args.sender, roster=roster)
    if args.recipient in retired:
        sys.stderr.write(
            f"agenttalk send: {args.recipient!r} is retired (a tombstone) and "
            f"cannot receive new messages (#19). See `agenttalk roster`.\n")
        return 2
    recipient = _resolve_peer(args.recipient, cfg, sender)
    body = _read_body(args)
    if not body and not args.allow_empty:
        sys.stderr.write("agenttalk send: empty body (use -m TEXT, --file PATH, pipe stdin, or --allow-empty)\n")
        return 2
    meta = _parse_meta(args.meta)
    _maybe_autogen_request_id(args.kind, meta, quiet=args.quiet)
    await_record = _prepare_await_reply(
        store,
        sender=sender,
        kind=args.kind,
        meta=meta,
        source="send",
        enabled=bool(getattr(args, "await_reply", False)),
    )
    _warn_missing_request_id(args.kind, meta)
    gate_mod.validate_response_status(args.kind, meta)
    gate_mod.validate_review_result_evidence(args.kind, meta)
    _warn_owed_decision_to_peer(store, sender, recipient, meta.get("request_id"))
    msg = store.send(
        sender=sender,
        recipient=recipient,
        body=body,
        kind=args.kind,
        subject=args.subject or "",
        meta=meta,
    )
    if not args.quiet:
        print(render(msg, header=f"AGENTTALK :: SENT  {msg.sender} -> {msg.recipient}"))
    _register_await_reply(store, await_record, quiet=args.quiet)
    if args.print_id:
        print(msg.id)
    return 0


def cmd_await_cancel(args: argparse.Namespace) -> int:
    """Conditionally cancel one wrapped reply wait by its opaque token."""
    store = _get_store(args)
    cfg = store.load_config()
    sender = _resolve_self(args.sender, roster=cfg.get("agents") or [])
    if not store.clear_awaiting_if_token(sender, args.token):
        sys.stderr.write(
            "agenttalk await-cancel: no matching active token; nothing cleared\n"
        )
        return 3
    if not args.quiet:
        print(f"cancelled reply wait {args.token}")
    return 0


def cmd_composing(args: argparse.Namespace) -> int:
    """Send a 'composing' control ping so the peer's `agenttalk wait` extends.

    Cheap one-liner the agent can run periodically while drafting a long
    reply. Same write path as ``send``: validated, optionally HMAC-signed,
    appears in the transcript and dashboard like any other message — but
    `wait` consumes it as a deadline-extension signal rather than as a
    reply, and `recv` hides it from the default inbox view.
    """
    store = _get_store(args)
    cfg = store.load_config()
    roster = cfg.get("agents") or []
    sender = _resolve_self(args.sender, roster=roster)
    body = args.message or "still drafting — please hold the line"
    meta = _parse_meta(args.meta)
    # --to-request sugar (#14): bind the ping to one thread so the peer's
    # scoped wait extends (the comp_rid gate) AND record the observational
    # reply-in-flight marker that threads/sync display. The rid alone
    # identifies the counterparty, so the peer is DERIVED from the thread
    # row — single-argument usage per FR-016 (WP02 review blocker 1);
    # explicit --to is only a consistency override. Drafting only makes
    # sense when you owe the thread's next move, i.e. owed-inbound
    # (review blocker 2). Deliberately state-based, not role-based: after
    # a review-result(needs-info) the ball bounces to the REQUESTER, who
    # then legitimately drafts on the same rid — a role==responder gate
    # would break that ping-pong.
    rid = getattr(args, "to_request", None)
    if rid:
        if "request_id" in meta and meta["request_id"] != rid:
            sys.stderr.write(
                "agenttalk composing: --to-request and --meta request_id "
                "disagree — pass one or the other.\n")
            return 2
        # VIEW-accurate row (real cursor + acks) — unlike the barrier
        # helper `_thread_row_for`, which is deliberately cursor-blind:
        # here we must see that a drained needs-info has moved the ball
        # to the sender (owed-inbound), not a stale reply-waiting.
        rows = th.derive_threads(
            store.valid_messages(), agent=sender, cursor=store.cursor(sender),
            closed_rids=_closed_rids(store, sender),
        )
        row = next((t for t in rows if t.request_id == rid), None)
        if row is None:
            sys.stderr.write(
                f"agenttalk composing: no thread {rid!r} is visible to "
                f"{sender} — check `agenttalk threads --for {sender} --all`.\n")
            return 2
        if row.state != "owed-inbound":
            sys.stderr.write(
                f"agenttalk composing: you do not owe a reply on thread "
                f"{rid!r} (state: {row.state}) — composing marks YOUR "
                f"in-flight reply, not the peer's.\n")
            return 2
        derived = row.peer
        if args.recipient and args.recipient != derived:
            sys.stderr.write(
                f"agenttalk composing: --to {args.recipient!r} disagrees "
                f"with thread {rid!r} (its counterparty is {derived!r}) — "
                f"drop --to or fix the request id.\n")
            return 2
        recipient = derived
        meta["request_id"] = rid
    else:
        recipient = _resolve_peer(args.recipient, cfg, sender)
    operation_nonce = getattr(args, "operation_nonce", None)
    existing, operation_error = _operation_idempotency(
        store,
        sender=sender,
        recipient=recipient,
        body=body,
        kind="composing",
        operation="composing",
        meta=meta,
        nonce=operation_nonce,
    )
    if operation_error is not None:
        sys.stderr.write(f"agenttalk composing: {operation_error}.\n")
        return 2
    if existing is not None:
        if not args.quiet:
            print(f"(composing operation already recorded: id={existing.id})")
        return 0
    try:
        if operation_nonce is not None:
            msg, published = store.send_operation(
                sender=sender,
                recipient=recipient,
                body=body,
                kind="composing",
                subject=args.subject or "composing",
                meta=meta,
                operation_nonce=operation_nonce,
                operation_digest=str(meta["operation_digest"]),
            )
        else:
            msg = store.send(
                sender=sender,
                recipient=recipient,
                body=body,
                kind="composing",
                subject=args.subject or "composing",
                meta=meta,
            )
            published = True
    except ValueError as exc:
        sys.stderr.write(f"agenttalk composing: {exc}.\n")
        return 2
    if rid:
        store.write_composing_intent(sender, rid, recipient)  # best-effort
    if not published and not args.quiet:
        print(f"(composing operation already recorded: id={msg.id})")
    elif not args.quiet:
        print(f"(composing ping sent: {sender} -> {recipient}; id={msg.id})")
    return 0


def cmd_rescind(args: argparse.Namespace) -> int:
    """Mark one of your own tracked requests as no-longer-current (#12).

    Writes a first-class, transcript-visible `rescind` message correlated
    to the thread. Thread derivation reports the thread as
    `closed-superseded`; a peer blocked in `wait --to-request` wakes with
    a RESCINDED outcome (exit 3); `check --to-request` reports
    superseded. Generic primitive only — HOLD/VOID/strike conventions
    live in skills, never in the transport (C-006). Requester-only:
    you can only rescind threads you opened.
    """
    store = _get_store(args)
    cfg = store.load_config()
    sender = _resolve_self(args.sender, roster=cfg.get("agents") or [])
    rid = args.to_request
    try:
        openers = validate_rescind(store, sender, rid, target_msg_id=args.to_id)
    except ValueError as e:
        sys.stderr.write(f"agenttalk rescind: {e}\n")
        return 2
    # Idempotent audit semantics: rescinding an already-superseded thread
    # still writes the message (the transcript is the provenance), but the
    # derived state cannot change — say so.
    row = _thread_row_for(store, sender, rid)
    already = row is not None and row.state == "closed-superseded"
    # Reason is OPTIONAL: read it only when explicitly provided (-m/--file).
    # No implicit stdin fallback — a bare `rescind` must not block on (or
    # sniff) stdin the way body-required commands may.
    reason = ""
    if getattr(args, "message", None) or getattr(args, "file", None):
        reason = _read_body(args)
    meta: dict = {"request_id": rid}
    if args.to_id:
        meta["target_msg_id"] = args.to_id
    # One rescind per distinct opener recipient (a broadcast fanned out
    # one opener copy per member; the rescind mirrors that so every
    # waiter wakes). NOT a broadcast itself — no broadcast_id.
    recipients = list(dict.fromkeys(m.recipient for m in openers))
    for r in recipients:
        msg = store.send(
            sender=sender,
            recipient=r,
            body=reason,
            kind="rescind",
            subject=args.subject or f"rescind: {rid}",
            meta=dict(meta),
        )
        if not args.quiet:
            print(render(msg, header=f"AGENTTALK :: RESCIND  {sender} -> {r}"))
    if already:
        sys.stderr.write(
            f"agenttalk rescind: note — thread {rid} was already superseded; "
            f"state unchanged (this rescind is recorded for audit only).\n"
        )
    return 0


_EPOCH_ABSENT = object()  # sentinel: opener has NO epoch_at_send key (pre-0.16)


def _epoch_verdict(store: Store, rid: str) -> tuple[str, str | None, int]:
    """The epoch dimension of `check --epoch` (#19 Phase A, B1).

    Returns ``(epoch_state, current_epoch_id, exit_code)``:
      - no barrier exists at all            -> ("current", None, 0)
      - epoch_at_send == current epoch      -> ("current", <id>, 0)
      - epoch_at_send older / null          -> ("previous-epoch", <id>, 3)
      - epoch_at_send ABSENT + barrier exists-> ("unknown-pre-epoch", <id>, 3)
    `null`/None sorts older than any real id. An absent stamp (a pre-0.16 opener)
    with a live barrier fails CLOSED (exit 3) — automation gates on the exit code
    and a pre-epoch opener must be re-asked for irreversible actions.
    """
    current = store.current_epoch()
    if current is None:
        return "current", None, 0
    ea: object = _EPOCH_ABSENT
    for m in store.valid_messages():
        if m.kind in OPENER_KINDS and (m.meta or {}).get("request_id") == rid:
            ea = (m.meta or {}).get("epoch_at_send", _EPOCH_ABSENT)
            break
    if ea is _EPOCH_ABSENT:
        return "unknown-pre-epoch", current, 3
    if ea == current:
        return "current", current, 0
    return "previous-epoch", current, 3


def cmd_check(args: argparse.Namespace) -> int:
    """The pre-action currentness gate (#12): current | superseded | unknown.

    THE contract for irreversible actions: run this immediately before
    acting on a request you drained earlier — a rescind that landed
    after you read the request is invisible to you otherwise (the
    executor-already-drained race that no inbox primitive can close).
    Read-only: no cursor, heartbeat, or threadstate writes. Computed
    ack-independently from the validated log (a local ack never masks
    a rescind). Exit codes: 0 current, 3 superseded, 4 unknown rid.
    """
    store = _get_store(args)
    agent = _resolve_self(args.agent, roster=store.load_config().get("agents") or [])
    rid = args.to_request
    row = _thread_row_for(store, agent, rid)
    if row is None:
        if args.json:
            print(json.dumps({"request_id": rid, "state": "unknown",
                              "rescind": None}, indent=2))
        else:
            print(f"unknown     {rid}")
            sys.stderr.write(
                f"agenttalk check: no thread {rid!r} is visible to {agent} — "
                f"check the id (`agenttalk threads --for {agent} --all`) and "
                f"the root (`agenttalk whoami`).\n"
            )
        return 4
    if row.state == "closed-superseded":
        rescind = {
            "id": row.rescind_msg_id,
            "by": row.rescinded_by,
            "at": row.rescind_at,
            "reason": row.rescind_reason,
        }
        if args.json:
            print(json.dumps({"request_id": rid, "state": "superseded",
                              "rescind": rescind}, indent=2))
        else:
            print(f"superseded  {rid}")
            print(f"  rescinded by {row.rescinded_by} at {row.rescind_at}  "
                  f"(msg {row.rescind_msg_id})")
            if row.rescind_reason:
                print(f"  reason: {row.rescind_reason}")
            print("  do NOT act on this request — a fresh exchange needs a new request_id.")
        return 3
    # Rescind dimension is current. With --epoch, ALSO check the global epoch
    # (#19). The top-level "state" keeps its rescind meaning (current); the
    # epoch dimension is reported in the additive "epoch" object and can drive
    # exit 3 on its own (data-model §4).
    epoch_obj = None
    exit_code = 0
    if getattr(args, "epoch", False):
        epoch_state, cur_epoch, exit_code = _epoch_verdict(store, rid)
        epoch_obj = {"state": epoch_state, "current_epoch": cur_epoch}
    gate_obj = None
    if getattr(args, "gates", False):
        gate_obj = gate_mod.check_gates(store.root)
        if gate_obj["verdict"] == "HOLD" and exit_code == 0:
            exit_code = 3
    if args.json:
        out = {"request_id": rid, "state": "current", "rescind": None}
        if epoch_obj is not None:
            out["epoch"] = epoch_obj
        if gate_obj is not None:
            out["gates"] = gate_obj
        print(json.dumps(out, indent=2))
    else:
        if exit_code == 0:
            print(f"current     {rid}")
            if epoch_obj is not None:
                print(f"  epoch: current ({epoch_obj['current_epoch'] or 'no barrier'})")
        elif epoch_obj is not None and epoch_obj["state"] == "previous-epoch":
            print(f"previous-epoch  {rid}")
            print(f"  this request predates the current global epoch "
                  f"({epoch_obj['current_epoch']}) — do NOT act on it; re-ask "
                  f"under the current barrier for irreversible actions.")
        elif epoch_obj is not None:  # unknown-pre-epoch
            print(f"pre-epoch   {rid}")
            print(f"  this opener predates epochs (no epoch_at_send) and a "
                  f"barrier exists ({epoch_obj['current_epoch']}) — do NOT act; "
                  f"re-ask under the current barrier for irreversible actions.")
        else:
            print(f"hold        {rid}")
        if gate_obj is not None:
            print(f"  gates: {gate_obj['verdict']}")
            for blocker in gate_obj["blockers"]:
                why = f" - {blocker['reason']}" if blocker.get("reason") else ""
                print(f"    blocker {blocker['name']}: {blocker['status']}{why}")
    return exit_code


def cmd_gate(args: argparse.Namespace) -> int:
    """Manage lightweight assurance gates."""
    store = _get_store(args)
    action = getattr(args, "gate_cmd", None)
    if action == "list":
        state = gate_mod.load_gate_state(store.root)
        if getattr(args, "json", False):
            print(json.dumps(state, indent=2))
            return 0
        required = state.get("required_gates") or []
        if required:
            print("required gates: " + ", ".join(required))
        gates = state.get("gates") or {}
        if not gates:
            print("gates: none")
            return 0
        print(f"gates ({len(gates)}):")
        for name, gate in sorted(gates.items()):
            req = " required" if name in required else ""
            print(
                f"  {name}: {gate.get('status', 'unknown')} "
                f"{gate.get('severity', 'blocker')} scope={gate.get('scope', 'global')}{req}"
            )
        return 0
    if action == "check":
        scope = "release" if getattr(args, "release", False) else getattr(args, "scope", None)
        result = gate_mod.check_gates(store.root, scope=scope)
        if getattr(args, "json", False):
            print(json.dumps(result, indent=2))
        else:
            print(result["verdict"])
            if result["required_gates"]:
                print("required gates: " + ", ".join(result["required_gates"]))
            for blocker in result["blockers"]:
                why = f" - {blocker['reason']}" if blocker.get("reason") else ""
                print(f"  blocker {blocker['name']}: {blocker['status']}{why}")
        return 0 if result["verdict"] == "GO" else 3
    if action == "set":
        actor = _resolve_self(getattr(args, "actor", None), roster=store.load_config().get("agents") or [])
        required = True if args.required else False if args.optional else None
        # Serialize the whole load->mutate->write as ONE critical section so a
        # concurrent set/waive cannot read-modify-write over each other and drop
        # a gate. gates.py also refuses on load_error independently (direct
        # callers), so corrupt state is never silently overwritten either way.
        with store._config_lock():
            gate = gate_mod.set_gate(
                store.root,
                name=args.name,
                status=args.status,
                severity=args.severity,
                scope=args.scope,
                actor=actor,
                evidence_source=args.evidence_source,
                evidence=args.evidence,
                reason=args.reason,
                revision=args.revision,
                epoch=store.current_epoch(),
                required=required,
            )
        if getattr(args, "json", False):
            print(json.dumps(gate, indent=2))
        else:
            print(f"gate {gate['name']}: {gate['status']} {gate['severity']} scope={gate['scope']}")
        return 0
    if action == "waive":
        actor = None
        if getattr(args, "actor", None):
            actor = _resolve_self(args.actor, roster=store.load_config().get("agents") or [])
        with store._config_lock():
            gate = gate_mod.waive_gate(
                store.root,
                name=args.name,
                operator=args.operator,
                reason=args.reason,
                scope=args.scope,
                expires=args.expires,
                date=args.date,
                actor=actor,
            )
        if getattr(args, "json", False):
            print(json.dumps(gate, indent=2))
        else:
            print(f"gate {gate['name']}: waived until {gate['waiver']['expires']}")
        return 0
    sys.stderr.write("agenttalk gate: expected set, list, check, or waive.\n")
    return 2


# ----------------------------------------------------------------- close (P2)
#
# `close` aggregates the 0.32.0 assurance signals into ONE auditable release
# verdict for a frozen revision. The pure core (schema, verdict, transitions)
# lives in close.py; this is the thin I/O shell: git revision freeze, gate-check
# reuse, advisory roster authority, and the explicit release barrier bump.

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _git(root, argv: list[str]) -> tuple[int, str]:
    """Run a read-only git command in ``root``; (rc, stdout) or (-1, "") on any
    failure. argv is a fixed list (never shell); git is the only tool invoked."""
    import subprocess  # nosec B404  # local import keeps the dependency optional
    try:
        # Fixed git argv in the repo root; never shell, never operator input as a
        # program. "git" is resolved from PATH on purpose (cross-platform; no fixed
        # install path), so partial-path (B607) is intentional here.
        p = subprocess.run(  # noqa: S603,S607  # nosec B603 B607
            ["git", "-C", str(root), *argv],
            capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
        )
        return p.returncode, (p.stdout or "")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return -1, ""


class GitWriteError(RuntimeError):
    """Mutating git command refused or failed in the authority-critical path."""


def _allowed_git_write(argv: list[str]) -> bool:
    if len(argv) == 7 and argv[0:3] == ["worktree", "add", "-b"] and argv[4] == "--":
        branch = argv[3]
        base = argv[6]
        try:
            return branch == lane_mod.lane_branch(branch.removeprefix("lane/")) and \
                bool(lane_mod._FULL_SHA_RE.match(base))
        except lane_mod.LaneError:
            return False
    if len(argv) == 4 and argv[:3] == ["worktree", "remove", "--"]:
        return True
    if len(argv) == 3 and argv[:2] == ["update-ref", "-d"]:
        ref = argv[2]
        if not ref.startswith("refs/heads/lane/"):
            return False
        try:
            lane_mod.validate_lane_id(ref.removeprefix("refs/heads/lane/"))
            return True
        except lane_mod.LaneError:
            return False
    return False


def _git_write(root, argv: list[str], *, timeout: float = 30.0) -> tuple[int, str, str]:
    """Run a narrowly allowlisted mutating git command.

    This is intentionally separate from `_git`: callers must opt into mutating git,
    every positional path/ref remains an argv element, and timeouts kill+reap before
    returning so a lane is never persisted after an unknown write state.
    """
    if not _allowed_git_write(argv):
        raise GitWriteError(f"mutating git command shape is not allowlisted: {argv!r}")
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    for key in ("GIT_ASKPASS", "SSH_ASKPASS", "GIT_EDITOR"):
        env.pop(key, None)
    cmd = ["git", "-c", "core.editor=false", "-C", str(root), *argv]
    try:
        proc = subprocess.Popen(  # noqa: S603,S607  # nosec B603 B607
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", env=env)
    except OSError as e:
        raise GitWriteError(f"mutating git failed to start: {e}") from e
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as e:
        # kill() can itself raise (PermissionError on Windows,
        # ProcessLookupError if the child already exited) - guarded so it
        # can never pre-empt the GitWriteError raised below.
        with contextlib.suppress(OSError):
            proc.kill()
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired as reap:
            # A third exit from this function's timeout handling that used
            # to raise immediately without ever calling wait() - a killed
            # git could still go unreaped while this function returned
            # (via raising), contradicting the docstring's own kill+REAP
            # contract and leaving a lane mutation's lock state uncertain.
            # Same fallback as the BaseException branch below: wait() does
            # not touch the pipes, so it still completes once the process
            # is actually gone.
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=25)
            raise GitWriteError(
                "mutating git timed out and could not be reaped; git/config lock may be stranded"
            ) from reap
        raise GitWriteError(
            f"mutating git timed out after {timeout:g}s and was killed: {err or out or e}"
        ) from e
    except BaseException:
        if proc.poll() is None:
            # kill() can itself raise - unguarded, that secondary error
            # would replace the owner BaseException being handled here and
            # skip the reap fallback below entirely.
            with contextlib.suppress(OSError):
                proc.kill()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                # A hook or descendant holding the captured stdout/stderr
                # pipe handles open can keep communicate() blocked past
                # this retry even though the killed process is already
                # dead - wait() does not touch the pipes at all, so it
                # still completes once the process is actually gone.
                # This function's own contract (kill+REAP before
                # returning, so a lane is never persisted after an
                # unknown write state) applies on this path too, not
                # just the routine-timeout branch above; silently
                # swallowing a second TimeoutExpired here left the reap
                # unconfirmed.
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=25)
        raise
    return proc.returncode, out or "", err or ""


def _resolve_revision(root, ref: str) -> tuple[str, str]:
    """Freeze ``ref`` to a full 40-char SHA via git. Returns (sha, kind) where kind
    is 'sha' (caller passed a full SHA we verified or could not check) or 'ref'
    (a ref/short-sha git resolved). Fails closed when neither git nor a full SHA
    can produce a frozen revision."""
    from agenttalk import close as close_mod
    rc, out = _git(root, ["rev-parse", "--verify", f"{ref}^{{commit}}"])
    sha = out.strip()
    if rc == 0 and close_mod._FULL_SHA_RE.match(sha):
        return sha, ("sha" if sha == ref else "ref")
    if close_mod._FULL_SHA_RE.match(ref):
        return ref, "sha"   # git unavailable but the operator pinned a full SHA
    raise close_mod.CloseError(
        f"could not resolve revision {ref!r} to a full SHA (git rc={rc}); "
        "pass a full 40-char SHA or run inside the repo")


def _worktree_clean(root) -> bool | None:
    """True/False if git could report; None if git is unavailable (caller then
    relies on the operator's --dirty-artifact / explicit flags)."""
    rc, out = _git(root, ["status", "--porcelain"])
    if rc != 0:
        return None
    return out.strip() == ""


def _close_lead_set(store) -> set[str]:
    """Advisory close-lead authority: the sole role=lead UNION the operator-facing
    liaison. agenttalk does not enforce identity (role is advisory; the bus
    authenticates the sender), so the CLI WARNS on an unrecognized actor but still
    records the action — close is release-confidence + audit, never an enforced lock."""
    leads: set[str] = set()
    sole = store.sole_lead()
    if sole:
        leads.add(sole)
    liaison = store.operator_facing()
    if liaison:
        leads.add(liaison)
    return leads


def _check_close_authority(store, actor: str, action: str) -> bool:
    leads = _close_lead_set(store)
    authorized = (not leads) or (actor in leads)
    if leads and not authorized:
        sys.stderr.write(
            f"agenttalk close {action}: WARNING - {actor!r} is not a recognized "
            f"close lead {sorted(leads)}; recording anyway (close authority is "
            "advisory, not enforced). The action and actor are logged.\n")
    return authorized


_CLOSE_BARRIER_BINDING_VERSION = 1
_CLOSE_BARRIER_BINDING_KEYS = {
    "version", "close_id", "instance_id", "revision", "generation",
}


def _new_close_barrier_binding(record: dict) -> dict:
    """Bind one requested release barrier to the persisted publish generation."""
    from agenttalk import close as close_mod

    instance_id = close_mod.close_instance_id(record)
    revision = record.get("revision")
    if instance_id is None:
        raise close_mod.CloseError("release barrier requires a versioned close instance")
    if not isinstance(revision, str) or not revision:
        raise close_mod.CloseError("release barrier requires a frozen close revision")
    return {
        "version": _CLOSE_BARRIER_BINDING_VERSION,
        "close_id": close_mod.validate_close_id(record.get("close_id")),
        "instance_id": instance_id,
        "revision": revision,
        "generation": close_mod.close_generation(record) + 1,
    }


def _validated_close_barrier_binding(record: dict) -> dict:
    """Return the durable barrier intent, rejecting ambiguous or stale bindings."""
    from agenttalk import close as close_mod

    final = record.get("final")
    if not isinstance(final, dict):
        raise close_mod.CloseError("published close has no final barrier state")
    binding = final.get("barrier_binding")
    if not isinstance(binding, dict) or set(binding) != _CLOSE_BARRIER_BINDING_KEYS:
        raise close_mod.CloseError(
            "published close has no valid release-barrier binding; it is not resumable")
    generation = binding.get("generation")
    expected_identity = {
        "version": _CLOSE_BARRIER_BINDING_VERSION,
        "close_id": close_mod.validate_close_id(record.get("close_id")),
        "instance_id": close_mod.close_instance_id(record),
        "revision": record.get("revision"),
    }
    if any(binding.get(key) != value for key, value in expected_identity.items()):
        raise close_mod.CloseError("release-barrier binding does not match this close instance")
    if (not isinstance(generation, int) or isinstance(generation, bool)
            or generation < 1):
        raise close_mod.CloseError("release-barrier binding generation is invalid")
    barrier_epoch = final.get("barrier_epoch")
    if barrier_epoch is not None and (
        not isinstance(barrier_epoch, str) or not barrier_epoch
    ):
        raise close_mod.CloseError("published close barrier_epoch is invalid")
    expected_generation = generation + (1 if barrier_epoch is not None else 0)
    if close_mod.close_generation(record) != expected_generation:
        raise close_mod.CloseConflict(
            "published close generation no longer matches its release-barrier binding")
    return dict(binding)


def _bound_close_barriers(store, binding: dict) -> list:
    """Find validated global barriers carrying exactly one close binding."""
    matches = []
    for message in store.valid_messages():
        meta = message.meta or {}
        barrier = meta.get("barrier")
        if not (
            message.kind == "message"
            and isinstance(barrier, dict)
            and barrier.get("version") == 1
            and barrier.get("scope") == "global"
            and barrier.get("type") == "epoch-bump"
            and meta.get("close_id") == binding["close_id"]
            and meta.get("close_barrier") == binding
        ):
            continue
        matches.append(message)
    return matches


def _ensure_close_release_barrier(store, transaction, *, actor: str) -> str:
    """Send or resume one bound barrier, then stamp its validated message id.

    The caller holds the per-close lock for the whole scan/send/stamp sequence.
    A failed send leaves the durable binding pending; a failed stamp leaves the
    bound message discoverable by the next retry.
    """
    from agenttalk import close as close_mod

    record = transaction.record
    binding = _validated_close_barrier_binding(record)
    matches = _bound_close_barriers(store, binding)
    if len(matches) > 1:
        raise close_mod.CloseError(
            "multiple validated release barriers match this close; refusing an ambiguous stamp")
    final = record["final"]
    barrier_epoch = final.get("barrier_epoch")
    if barrier_epoch is not None:
        if len(matches) != 1 or matches[0].id != barrier_epoch:
            raise close_mod.CloseError(
                "stamped release barrier is missing or does not match its close binding")
        return barrier_epoch
    if not matches:
        store.send(
            sender=actor, recipient=actor, kind="message",
            subject=f"release barrier: close {binding['close_id']}",
            body=final.get("reason") or f"close {binding['close_id']} published GO",
            meta={
                "barrier": {"version": 1, "scope": "global", "type": "epoch-bump"},
                "close_id": binding["close_id"],
                "close_barrier": binding,
            },
        )
        matches = _bound_close_barriers(store, binding)
    if len(matches) != 1:
        raise close_mod.CloseError(
            "release barrier was not durably validated exactly once; retry the publish")
    barrier_epoch = matches[0].id
    final["barrier_epoch"] = barrier_epoch
    transaction.commit()
    return barrier_epoch


# ----- P3 signoff helpers (the impure resolution shell; the verdict stays pure) -

def _agent_groups(cfg: dict, agent: str) -> list[str]:
    """The groups an agent currently belongs to (for ack from_groups - keeps the
    pure refset-group authorization working without the core reading the roster)."""
    groups = cfg.get("groups", {}) or {}
    return [g for g, members in groups.items()
            if isinstance(members, list) and agent in members]


def _changed_paths_of(record: dict) -> list[str]:
    inv = record.get("risk_inventory") or []
    return sorted({p for e in inv if isinstance(e, dict)
                   for p in (e.get("affected_paths") or [])})


def _build_signoff_eval(store, record: dict):
    """Resolve everything P3 needs from config/roster/domains (IMPURE) into the
    bundle compute_verdict consumes (PURE). None when the close has no derived
    signoffs (P2-only). Fails closed via policy_error on a malformed policy."""
    from agenttalk import close as close_mod
    if not record.get("signoff_route"):   # P3 in play iff `apply` has run
        return None
    cfg = store.load_config()
    active = store.active_agents()
    policy, err = close_mod.load_signoff_policy(store)
    if err:
        return {"policy_present": True, "policy_error": err,
                "current_policy_hash": "", "current_risk_inventory_hash": "",
                "unmapped_risks": [], "resolved_candidates": {}, "active_agents": active}
    inv = record.get("risk_inventory") or []
    cur_policy_hash = close_mod.policy_hash(policy) if policy else ""
    cur_inv_hash = close_mod.risk_inventory_hash(inv)
    unmapped = (close_mod.derive_required_signoffs(policy, inv)["unmapped"]
                if policy else [])
    domain_refset = close_mod.signoff_domain_refset(
        store,
        cfg,
        _changed_paths_of(record),
    )
    default_reviewers = ((policy or {}).get("defaults", {}) or {}).get("reviewers") or {}
    resolved: dict[str, list[str]] = {}
    for s in record["required_signoffs"]:
        resolved[s["id"]] = close_mod.resolve_signoff_candidates(
            cfg,
            candidate_refset=s.get("candidate_refset") or {},
            default_reviewers=default_reviewers,
            use_default_reviewers=bool(s.get("use_default_reviewers")),
            domain_refset=domain_refset,
            include_domain_reviewers=bool(s.get("include_domain_reviewers")),
        )
    return {"policy_present": policy is not None, "policy_error": None,
            "current_policy_hash": cur_policy_hash,
            "current_risk_inventory_hash": cur_inv_hash,
            "unmapped_risks": unmapped, "resolved_candidates": resolved,
            "active_agents": active}


def _build_dod_eval(store, record: dict):
    """Resolve the #60 Definition-of-Done evidence (IMPURE) into the bundle
    :func:`close.evaluate_dod` consumes (PURE). ``None`` when the close's scope has no DoD
    requirements (byte-identical to pre-#60). Fails closed via ``policy_error`` on a malformed
    ``dod.json``. Live-derived at check time - there is no frozen DoD route to forget to apply."""
    from agenttalk import close as close_mod
    policy, err = close_mod.load_dod_policy(store)
    if err:
        return {"policy_present": True, "policy_error": err, "required_dimensions": {}}
    dims = close_mod.derive_required_dod(policy, record.get("scope"))["dimensions"]
    if not dims:
        return None
    bundle = {"policy_present": policy is not None, "policy_error": None,
              "required_dimensions": dims}
    if "assurance" in dims:
        bundle["assurance"] = _resolve_dod_assurance_gate(store, dims["assurance"], record)
    if "coverage" in dims:
        bundle["coverage"] = _resolve_dod_coverage_gate(store, dims["coverage"], record)
    if "knowledge" in dims:
        bundle["knowledge"] = _resolve_dod_knowledge(store, dims["knowledge"], record)
    return bundle


# Blank-glyph fillers that render empty/space but fall in an otherwise-"visible" general category
# (Lo/So), so the category + combining-mark filtering in _substantive_len misses them. This is a
# MAINTAINED blocklist, NOT an exhaustive oracle: Unicode has no stdlib "renders blank" predicate,
# and these blanks are deliberately categorized as letters/symbols (so category/isalnum can't
# separate them from real content) and are not all Default_Ignorable. We defend the known classes;
# an obscure blank not yet listed is an accepted, bounded residual (see _substantive_len).
_BLANK_GLYPH_FILLERS = frozenset({
    0x115F,    # HANGUL CHOSEONG FILLER (Lo)
    0x1160,    # HANGUL JUNGSEONG FILLER (Lo)
    0x3164,    # HANGUL FILLER (Lo)
    0xFFA0,    # HALFWIDTH HANGUL FILLER (Lo)
    0x2800,    # BRAILLE PATTERN BLANK (So)
    0x1D159,   # MUSICAL SYMBOL NULL NOTEHEAD (So) - no glyph in a supporting renderer
    0x13441,   # EGYPTIAN HIEROGLYPH FULL BLANK (Lo) - rendered as whitespace (Unicode ch.11)
    0x13442,   # EGYPTIAN HIEROGLYPH HALF BLANK (Lo) - rendered as whitespace
})


def _substantive_len(text: str) -> int:
    """Count VISIBLE, substantive characters in ``text`` for the knowledge ``min_body_chars``
    triviality floor, so padding cannot buy past it ANYWHERE in the body. A char contributes only
    if it is not whitespace AND not any of the invisible/blank classes below. General category
    alone is NOT a visibility predicate, and Python's stdlib exposes no "renders blank" oracle, so
    this is a BEST-EFFORT metric over the classes we can detect, NOT a proof of visibility:

    - whitespace (`str.isspace()`) and separators (category Z*);
    - other/control/format/surrogate/private-use/unassigned (category C*) - this alone covers
      U+200B, U+FEFF, U+2060, the bidi marks/overrides, tag chars, soft hyphen, etc.;
    - zero-width combining marks (categories Mn nonspacing / Me enclosing) - these have no advance
      width of their own and only modify a base, so a run of them is invisible: covers the
      COMBINING GRAPHEME JOINER (U+034F), all VARIATION SELECTORs (U+FE00-FE0F, U+E0100-E01EF),
      and the Mongolian free variation selectors. A base letter still counts; a standalone mark
      does not. (Spacing marks Mc, e.g. Indic vowel signs, DO carry width and are counted.);
    - a MAINTAINED set of blank-glyph fillers that render empty but sit in Lo/So (`_BLANK_GLYPH_
      FILLERS`): Hangul fillers, BRAILLE PATTERN BLANK, MUSICAL SYMBOL NULL NOTEHEAD, and the
      EGYPTIAN HIEROGLYPH FULL/HALF BLANK.

    BOUNDED RESIDUAL (honest, do not re-overclaim): the blank-filler set is NOT exhaustive - there
    is no stdlib visibility oracle and Unicode keeps adding blanks, so an obscure blank codepoint
    not yet listed could pass this floor. That is an ACCEPTED residual: this floor defends the
    "did you write anything visible" contract against the known invisible/blank classes; gaming
    one's OWN quality gate with an exotic unlisted blank is outside the core threat model. Add new
    blanks to `_BLANK_GLYPH_FILLERS` as they are found. (`str.strip()` alone removed only edge
    whitespace; Z*/C* alone missed the Mn variation selectors and the Lo/So blank fillers.)"""
    n = 0
    for ch in text:
        if ch.isspace():
            continue
        cat = unicodedata.category(ch)
        if cat[0] in ("Z", "C"):
            continue
        if cat in ("Mn", "Me"):
            continue
        if ord(ch) in _BLANK_GLYPH_FILLERS:
            continue
        n += 1
    return n


def _resolve_dod_knowledge(store, spec: dict, record: dict) -> dict:
    """Resolve the CURATED knowledge notes BOUND to this close's revision (never reads a note by
    id - #44; the addressable identity is (domain,key), and a note is bound by a sha anchor /
    verified_against_sha equal to the close revision). All I/O here; :func:`close._evaluate_dod_
    knowledge` only counts. Any malformed note/anchor is skipped, never raised."""
    from agenttalk import knowledge as kn

    revision = str(record.get("revision") or "")
    types = set(spec.get("types") or [])
    bound: list[dict] = []
    try:
        events, _problems = kn.read_events(store)
        views = kn.resolve_views(events)
    except Exception:  # noqa: BLE001 - a broken knowledge log must not crash `close check`
        events, views = [], {}
    for _key, slot in (views.items() if isinstance(views, dict) else []):
        note = slot.get("curated") if isinstance(slot, dict) else None
        if not isinstance(note, dict):
            continue
        try:
            if not kn.is_curated(note) or kn.is_retracted(note):
                continue
            if note.get("type") not in types:
                continue
            if _knowledge_note_bound_to(note, revision):
                # min_body_chars is a SUBSTANTIVE-content floor, so measure only visible content:
                # count characters that are not whitespace and not in a Unicode separator (Z*) or
                # other/control/format (C*) category. This defeats padding ANYWHERE in the body,
                # not just at the edges - trailing spaces, interior "x x x ..." runs, and invisible
                # fillers like U+200B ZERO WIDTH SPACE (Cf) or control chars all count as zero.
                bound.append({"type": note.get("type"),
                              "body_len": _substantive_len(str(note.get("body") or ""))})
        except Exception:  # noqa: BLE001, S112  # nosec - skip a malformed note, never crash
            continue
    return {
        "when": spec.get("when", "on_remediation"),
        "min_notes": spec.get("min_notes", 1),
        "min_body_chars": spec.get("min_body_chars", 0),
        "types": sorted(types),
        "has_remediation": bool(record.get("remediation_items")),
        "bound_notes": bound,
    }


def _knowledge_note_bound_to(note: dict, revision: str) -> bool:
    """A note is bound to ``revision`` iff its sha anchor (lessons nest it under ``lesson``) OR
    its ``verified_against_sha`` equals the close revision (both are full 40-char SHAs)."""
    if not revision:
        return False
    if note.get("verified_against_sha") == revision:
        return True
    if note.get("type") == "lesson":
        anchor = (note.get("lesson") or {}).get("anchor")
    else:
        anchor = note.get("anchor")
    return (isinstance(anchor, dict) and anchor.get("kind") == "sha"
            and anchor.get("sha") == revision)


def _resolve_dod_assurance_gate(store, spec: dict, record: dict) -> dict:
    """Read the named ``assurance:<scope>`` gate's OWN fields (never artifact.json - Q1 ruling)
    so the DoD assurance dimension is a binding/freshness check over the CI-attested gate. Carries
    the gate's own ``scope`` and the close's applicable scope so the evaluator can enforce that the
    gate actually applies to this close (a feature-scoped gate must not satisfy a release close)."""
    from datetime import datetime, timezone

    from agenttalk import gates as gate_mod
    gate_name = spec["gate"]
    state = gate_mod.load_gate_state(store.root)
    g = (state.get("gates") or {}).get(gate_name)
    close_scope = record.get("gate_scope")
    if not isinstance(g, dict):
        return {"gate": gate_name, "present": False, "close_gate_scope": close_scope}
    now = datetime.now(timezone.utc)
    waiver = g.get("waiver")
    waiver_active = False
    if isinstance(waiver, dict):
        try:
            waiver_active = gate_mod._waiver_active(waiver, now=now)
        except (ValueError, TypeError):
            waiver_active = False
    return {
        "gate": gate_name, "present": True,
        "status": g.get("status"), "severity": g.get("severity"),
        "evidence_source": g.get("evidence_source"), "revision": g.get("revision"),
        "waiver_active": waiver_active,
        "gate_scope": g.get("scope"), "close_gate_scope": close_scope,
        "age_days": _iso_age_days(g.get("updated_at"), now),
        "max_age_days": spec.get("max_age_days"),
    }


def _coverage_percent_from_gate(g: dict):
    """Extract the coverage percentage the producer stored on this gate. The producer writes it
    into the gate's latest EVIDENCE entry (``gates.set_gate(..., evidence_details={"coverage_
    percent": <float>})`` → ``gate["evidence"][-1]["coverage_percent"]``), NOT top-level. Read the
    most recent evidence entry that carries it; return ``None`` if absent (the pure evaluator then
    fails closed). This is the producer↔consumer contract seam for #60 inc-3."""
    entries = g.get("evidence")
    if not isinstance(entries, list) or not entries:
        return None
    # Read ONLY the latest evidence entry — do NOT backtrack. Backtracking could bind a stale
    # percentage from an older green to newer green metadata (e.g. green@A(95) -> red@B ->
    # green@B with no percent would wrongly surface 95 as B's coverage). If the current entry
    # lacks the field, return None and let the evaluator fail closed. (reviewer-1, #60 inc-3)
    latest = entries[-1]
    if not isinstance(latest, dict):
        return None
    return latest.get("coverage_percent")


def _resolve_dod_coverage_gate(store, spec: dict, record: dict) -> dict:
    """Read the policy-selected ``coverage:<profile>`` gate from the gate record itself.

    Close scope and assurance scan profile are deliberately independent. Policy selects one of
    the finite producer gates validated by ``close.validate_dod_policy``; the gate's own
    attestation fields, revision, timestamp, producer-profile scope, and numeric
    ``coverage_percent`` (from its latest evidence entry) form the entire binding.
    """
    from datetime import datetime, timezone

    from agenttalk import gates as gate_mod

    gate_name = spec["gate"]
    state = gate_mod.load_gate_state(store.root)
    g = (state.get("gates") or {}).get(gate_name)
    if not isinstance(g, dict):
        return {
            "gate": gate_name,
            "present": False,
            "min_percent": spec.get("min_percent"),
            "max_age_days": spec.get("max_age_days"),
        }
    now = datetime.now(timezone.utc)
    waiver = g.get("waiver")
    waiver_active = False
    if isinstance(waiver, dict):
        try:
            waiver_active = gate_mod._waiver_active(waiver, now=now)
        except (ValueError, TypeError):
            waiver_active = False
    return {
        "gate": gate_name,
        "present": True,
        "status": g.get("status"),
        "severity": g.get("severity"),
        "evidence_source": g.get("evidence_source"),
        "revision": g.get("revision"),
        "waiver_active": waiver_active,
        "gate_scope": g.get("scope"),
        "coverage_percent": _coverage_percent_from_gate(g),
        "min_percent": spec.get("min_percent"),
        "age_days": _iso_age_days(g.get("updated_at"), now),
        "max_age_days": spec.get("max_age_days"),
    }


def _iso_age_days(updated_at: object, now) -> float | None:
    """SIGNED age in days of an ISO-8601 ``updated_at`` vs ``now`` (NEGATIVE = future-dated);
    ``None`` if missing/unparseable. Never raises (a bad timestamp must not crash `close check`) -
    but the DoD evaluator FAILS CLOSED on a ``None``/future age when freshness is required, so this
    must NOT clamp a future timestamp to 0 (that would mask a future-dated attestation as fresh).
    The returned float is rounded away from the valid interval: positive ages round upward and
    negative ages downward, so conversion cannot make stale/future evidence look fresh. Reuses the
    existing :func:`_parse_ts` timestamp parser."""
    if not isinstance(updated_at, str):
        return None
    parsed = _parse_ts(updated_at)
    if parsed is None:
        return None
    delta = now - parsed
    total_microseconds = (
        (delta.days * 86400 + delta.seconds) * 1_000_000
        + delta.microseconds
    )
    exact_days = Fraction(total_microseconds, 86_400_000_000)
    age_days = float(exact_days)
    represented = Fraction.from_float(age_days)
    if exact_days > 0 and represented < exact_days:
        age_days = math.nextafter(age_days, math.inf)
    elif exact_days < 0 and represented > exact_days:
        age_days = math.nextafter(age_days, -math.inf)
    return age_days


def _signoff_risk_inventory(args, store, record: dict) -> list[dict]:
    """Build a risk inventory from CLI flags. Changed paths DEFAULT from the frozen
    revision's git diff (base..revision); manual --changed-path is an audited
    override. --risk-class X (repeatable) -> active risk; --risk-na CLASS=REASON ->
    dispositioned N/A."""
    revision = record.get("revision")
    paths = list(getattr(args, "changed_path", None) or [])
    path_source = "manual"
    if not paths:
        base = getattr(args, "base", None) or f"{revision}^"
        rc, out = _git(store.root, ["diff", "--name-only", f"{base}..{revision}"])
        if rc == 0:
            paths = [ln.strip() for ln in out.splitlines() if ln.strip()]
            path_source = f"git-diff {base}..{revision[:12]}"
    inv: list[dict] = []
    for rc_class in getattr(args, "risk_class", None) or []:
        inv.append({"risk_class": rc_class, "source": path_source,
                    "affected_paths": paths, "na_reason": None})
    for spec in getattr(args, "risk_na", None) or []:
        if "=" not in spec:
            raise close_na_error("--risk-na must be CLASS=REASON")
        cls, reason = spec.split("=", 1)
        inv.append({"risk_class": cls, "source": "cli-na",
                    "affected_paths": [], "na_reason": reason})
    return inv


def close_na_error(msg: str):
    from agenttalk import close as close_mod
    return close_mod.CloseError(msg)


def _close_conflict_result(action: str, error: Exception) -> int:
    sys.stderr.write(
        f"agenttalk close {action}: HOLD - concurrent close update conflict: {error}. "
        "Reload the close and retry.\n")
    return 3


def _signoff_set_for_lens(record: dict, lens_id: str) -> str | None:
    """The signoff set a generated lens belongs to, or None for a plain P2 lens."""
    for ln in record.get("required_lenses", []) or []:
        if isinstance(ln, dict) and ln.get("id") == lens_id and ln.get("signoff_set_id"):
            return ln["signoff_set_id"]
    return None


def _close_derive_signoffs(args, store, record: dict, actor: str) -> int:
    """Resolve policy + risk inventory + refsets and APPLY them onto ``record`` (in
    place). The single mutating derivation; the caller persists. Returns 0 or a CLI
    exit code (2 on invalid policy / bad input)."""
    from agenttalk import close as close_mod
    policy, err = close_mod.load_signoff_policy(store)
    if err:
        sys.stderr.write(f"agenttalk close signoffs: invalid policy - {err}\n")
        return 2
    if policy is None:
        sys.stderr.write(
            "agenttalk close signoffs: no .agenttalk/signoffs.json policy "
            "(opt-in) - nothing to derive.\n")
        return 2
    try:
        inv = _signoff_risk_inventory(args, store, record)
        audit = _resolve_signoff_audit(store, policy, inv, record)
        close_mod.apply_signoffs(record, policy=policy, risk_inventory=inv,
                                 derived_by=actor, at=_iso_now(),
                                 resolved_candidates_at_apply=audit)
    except close_mod.CloseError as e:
        sys.stderr.write(f"agenttalk close signoffs: {e}\n")
        return 2
    return 0


def _resolve_signoff_audit(store, policy, inv, record) -> dict:
    """AUDIT-ONLY snapshot of who currently resolves for each derived set (concrete
    agents at apply time). NOT the authority - check re-resolves the refsets."""
    from agenttalk import close as close_mod
    cfg = store.load_config()
    derived = close_mod.derive_required_signoffs(policy, inv)["signoffs"]
    changed = sorted({p for e in inv if isinstance(e, dict)
                      for p in (e.get("affected_paths") or [])})
    domain_refset = close_mod.signoff_domain_refset(store, cfg, changed)
    default_reviewers = ((policy or {}).get("defaults", {}) or {}).get("reviewers") or {}
    audit = {}
    for s in derived:
        audit[s["id"]] = close_mod.resolve_signoff_candidates(
            cfg,
            candidate_refset=s.get("candidate_refset") or {},
            default_reviewers=default_reviewers,
            use_default_reviewers=bool(s.get("use_default_reviewers")),
            domain_refset=domain_refset,
            include_domain_reviewers=bool(s.get("include_domain_reviewers")),
        )
    return audit


def _cmd_close_signoffs(args, store, roster) -> int:
    """`close signoffs {plan,apply,override}` - the impure derivation layer."""
    from agenttalk import close as close_mod
    sub = getattr(args, "signoffs_cmd", None)

    if sub == "plan":
        record = close_mod.load_close(store, args.id)
        policy, err = close_mod.load_signoff_policy(store)
        if err:
            sys.stderr.write(f"agenttalk close signoffs plan: invalid policy - {err}\n")
            return 2
        if policy is None:
            print("signoff policy: none (.agenttalk/signoffs.json absent) - "
                  "zero derived signoffs")
            return 0
        try:
            inv = _signoff_risk_inventory(args, store, record)
        except close_mod.CloseError as e:
            sys.stderr.write(f"agenttalk close signoffs plan: {e}\n")
            return 2
        derived = close_mod.derive_required_signoffs(policy, inv)
        audit = _resolve_signoff_audit(store, policy, inv, record)
        out = {"would_require": derived["signoffs"], "unmapped_risks": derived["unmapped"],
               "resolved_candidates": audit, "risk_inventory": inv}
        if getattr(args, "json", False):
            print(json.dumps(out, indent=2))
        else:
            if derived["unmapped"]:
                print("UNMAPPED risks (would HOLD unless allow_unmapped): "
                      + ", ".join(derived["unmapped"]))
            if not derived["signoffs"]:
                print("no signoffs derived for this risk inventory")
            for s in derived["signoffs"]:
                cands = ", ".join(audit.get(s["id"], [])) or "(none - would be unroutable)"
                print(f"  {s['id']}: need {s['required_count']} of [{cands}]")
        return 0

    if sub == "apply":
        actor = _resolve_self(getattr(args, "actor", None), roster=roster)
        _check_close_authority(store, actor, "signoffs apply")
        try:
            with close_mod.close_transaction(store, args.id) as transaction:
                record = transaction.record
                rc = _close_derive_signoffs(args, store, record, actor)
                if rc != 0:
                    return rc
                transaction.commit()
        except (close_mod.CloseConflict, TimeoutError) as e:
            return _close_conflict_result("signoffs apply", e)
        n = len(record.get("required_signoffs") or [])
        unmapped = (record.get("signoff_route") or {}).get("unmapped_risks") or []
        print(f"applied signoffs to {args.id}: {n} required set(s)"
              + (f"; UNMAPPED {', '.join(unmapped)}" if unmapped else ""))
        return 0

    if sub == "override":
        actor = _resolve_self(getattr(args, "actor", None), roster=roster)
        # ENFORCED (not advisory): a signoff override bypasses a REQUIRED specialist
        # set -> false GO if anyone could record it (reviewer-1 blocker). It is a
        # close-lead privilege; refuse a non-lead, and fail closed when no lead is
        # configured (no authority to record the bypass).
        leads = _close_lead_set(store)
        if not leads:
            sys.stderr.write(
                "agenttalk close signoffs override: no close lead is configured "
                "(role=lead or operator-facing) - cannot record a signoff override "
                "(it bypasses a required specialist set). Designate a lead first.\n")
            return 2
        if actor not in leads:
            sys.stderr.write(
                f"agenttalk close signoffs override: {actor!r} is not a close lead "
                f"{sorted(leads)}; the override escape is a close-lead privilege. "
                "Refusing.\n")
            return 2
        try:
            with close_mod.close_transaction(store, args.id) as transaction:
                record = transaction.record
                close_mod.signoff_override(record, set_id=args.set, by=actor,
                                           at=_iso_now(), reason=args.reason)
                transaction.commit()
        except (close_mod.CloseConflict, TimeoutError) as e:
            return _close_conflict_result("signoffs override", e)
        except close_mod.CloseError as e:
            sys.stderr.write(f"agenttalk close signoffs override: {e}\n")
            return 2
        print(f"signoff {args.set} overridden on {args.id} by {actor} (audited, not counted)")
        return 0

    sys.stderr.write("agenttalk close signoffs: expected plan, apply, or override.\n")
    return 2


def _close_lens_specs(args) -> list[dict]:
    from agenttalk import close as close_mod
    # --allow LENS:TOKEN  (TOKEN '@role' -> allowed_roles, else allowed_agents)
    agents: dict[str, list[str]] = {}
    roles: dict[str, list[str]] = {}
    for entry in getattr(args, "allow", None) or []:
        if ":" not in entry:
            raise close_mod.CloseError(f"--allow must be LENS:TOKEN (got {entry!r})")
        lens, token = entry.split(":", 1)
        if token.startswith("@"):
            roles.setdefault(lens, []).append(token[1:])
        else:
            agents.setdefault(lens, []).append(token)
    specs = []
    for lid in getattr(args, "lens", None) or []:
        specs.append(close_mod.validate_lens_spec({
            "id": lid, "allowed_agents": agents.get(lid, []),
            "allowed_roles": roles.get(lid, []), "required": True}))
    for lid in getattr(args, "optional_lens", None) or []:
        specs.append(close_mod.validate_lens_spec({
            "id": lid, "allowed_agents": agents.get(lid, []),
            "allowed_roles": roles.get(lid, []), "required": False}))
    return specs


def _print_verdict(close_id: str, result: dict) -> None:
    print(f"{result['verdict']}  ({close_id})")
    for h in result["holds"]:
        print(f"  HOLD[{h['code']}]: {h['detail']}")


def _close_worktree_eval(store, record: dict) -> dict | None:
    if not isinstance(record, dict):
        return {"status": "unverified", "reason": "close record is malformed"}
    if record.get("non_lane_isolation_not_asserted"):
        return {"status": "not_applicable", "reason": "non-lane close; isolation not asserted"}
    artifact_raw = record.get("lane_delivery_artifact")
    if not artifact_raw:
        return {"status": "unverified", "reason": "no lane delivery artifact recorded"}
    path = Path(artifact_raw)
    if not path.is_absolute():
        path = store.root / path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"status": "unverified", "reason": f"delivery artifact unreadable: {e}"}
    if not isinstance(data, dict):
        return {"status": "unverified", "reason": "delivery artifact is not an object"}
    lane_id = data.get("lane_id")
    head = data.get("delivered_head")
    if not isinstance(lane_id, str) or not isinstance(head, str):
        return {"status": "unverified", "reason": "delivery artifact lacks lane/head"}
    try:
        data = lane_mod.validate_delivery_artifact(
            path, lane_id=lane_id, head_sha=head, store=store,
            require_isolation=True, reject_pending_transaction=True,
        )
    except lane_mod.LaneError as e:
        return {"status": "unverified", "reason": str(e)}
    status = data.get("isolation_status")
    if (status == "verified"
            and data.get("integrity_version") != lane_mod.INTEGRITY_VERSION):
        # Current v3 evidence is HMAC-bound point-in-time proof. Its lane branch
        # may legitimately be deleted or reused after the completed transaction.
        rc, out = _git(store.root, ["rev-parse", "--verify", f"{lane_mod.lane_ref(lane_id)}^{{commit}}"])
        branch_tip = out.strip()
        if rc != 0 or branch_tip != head:
            return {"status": "unverified",
                    "reason": "lane branch tip is missing or differs from delivered head",
                    "lane_id": lane_id, "delivered_head": head}
    return {
        "status": "waived" if status == "waived" else "verified",
        "lane_id": lane_id,
        "delivered_head": head,
        "artifact": str(path),
    }


def cmd_close(args: argparse.Namespace) -> int:
    """Assurance P2 milestone/release close (advisory; see close.py)."""
    from agenttalk import close as close_mod
    store = _get_store(args)
    action = getattr(args, "close_cmd", None)
    roster = store.load_config().get("agents") or []

    if action == "open":
        close_id = close_mod.validate_close_id(args.id)
        try:
            revision, kind = _resolve_revision(store.root, args.revision)
        except close_mod.CloseError as e:
            sys.stderr.write(f"agenttalk close open: {e}\n")
            return 2
        clean = _worktree_clean(store.root)
        if clean is None:  # git could not report; trust the explicit flags
            clean = not bool(args.dirty_artifact) if not args.allow_dirty else False
        opener = _resolve_self(getattr(args, "actor", None), roster=roster)
        record = close_mod.empty_close(
            close_id, scope=args.scope, revision=revision, revision_kind=kind,
            gate_scope=args.gate_scope or args.scope, opened_by=opener,
            opened_at=_iso_now(), epoch_at_open=store.current_epoch(),
            required_lenses=_close_lens_specs(args),
            revision_clean=bool(clean), dirty_artifact=args.dirty_artifact,
            lane_delivery_artifact=getattr(args, "lane_artifact", None),
            non_lane_isolation_not_asserted=bool(
                getattr(args, "non_lane_isolation_not_asserted", False)))
        record["worktree_isolation"] = _close_worktree_eval(store, record)
        if not clean and not args.dirty_artifact:
            sys.stderr.write(
                "agenttalk close open: WARNING - worktree is dirty and no "
                "--dirty-artifact was recorded; close check will HOLD on revision "
                "until a clean SHA or a recorded diff artifact is provided.\n")
        if getattr(args, "derive_signoffs", False):
            rc = _close_derive_signoffs(args, store, record, opener)
            if rc != 0:
                return rc
        try:
            path = close_mod.close_path(store, close_id)
            if not args.force and path.exists():
                sys.stderr.write(
                    f"agenttalk close open: {close_id!r} already exists "
                    "(use --force to replace it, or `close reopen`).\n")
                return 2
            if args.force and path.exists():
                current = close_mod.load_close(store, close_id)
                if close_mod.close_instance_id(current) is None:
                    current = close_mod.upgrade_legacy_close(store, close_id)
                close_mod.replace_close(
                    store, record,
                    expected_generation=close_mod.close_generation(current),
                    expected_instance_id=close_mod.close_instance_id(current),
                )
            else:
                close_mod.create_close(store, record)
        except (close_mod.CloseConflict, TimeoutError) as e:
            return _close_conflict_result("open", e)
        except close_mod.CloseError as e:
            sys.stderr.write(f"agenttalk close open: {e}\n")
            return 2
        if getattr(args, "json", False):
            print(json.dumps(record, indent=2))
        else:
            print(f"opened close {close_id} @ {revision[:12]} ({kind}, "
                  f"{'clean' if clean else 'dirty'}); "
                  f"{len(record['required_lenses'])} lens(es), "
                  f"{len(record.get('required_signoffs') or [])} signoff(s)")
        return 0

    if action == "signoffs":
        return _cmd_close_signoffs(args, store, roster)

    if action == "ack":
        agent = _resolve_self(getattr(args, "actor", None), roster=roster)
        from_role = (store.load_config().get("roles") or {}).get(agent)
        evidence = None
        if args.status == close_mod.ACCEPT:
            # typed-evidence reuse expects scalar string fields (gates._has_value);
            # join repeatable --evidence into one pointer string (pointer-not-mirror).
            evidence_str = ", ".join(args.evidence) if args.evidence else None
            meta = {
                "risk_class": args.risk_class, "release_blocker": args.release_blocker,
                "tests_referenced": args.tests_referenced,
                "tests_executed": args.tests_executed,
                "residual_risk": args.residual_risk, "na_reason": args.na_reason,
                "evidence": evidence_str, "request_id": args.request_id,
            }
            meta = {k: v for k, v in meta.items() if v is not None}
            try:
                gate_mod.validate_review_result_evidence(
                    "review-result", {**meta, "status": "approved"})
            except ValueError as e:
                sys.stderr.write(f"agenttalk close ack: {e}\n")
                return 2
            evidence = meta
        elif args.status == close_mod.COUNTER:
            evidence = {"finding": args.finding, "request_id": args.request_id}
            evidence = {k: v for k, v in evidence.items() if v is not None}
        # An --override authorizes an otherwise-unauthorized ack, so it is a CLOSE
        # LEAD privilege - honor it ONLY from a recognized close lead (reviewer-1
        # blocker: any agent could self-authorize a required lens). Fail closed for
        # the privileged path: with no lead configured there is no one to record it.
        override = False
        if args.override:
            leads = _close_lead_set(store)
            if agent in leads:
                override = True
            else:
                sys.stderr.write(
                    f"agenttalk close ack: --override IGNORED - {agent!r} is not a "
                    f"recognized close lead {sorted(leads)}; the ack stays subject "
                    "to the lens authorization (override is a lead privilege).\n")
        from_groups = _agent_groups(store.load_config(), agent)
        try:
            with close_mod.close_transaction(store, args.id) as transaction:
                record = transaction.record
                counter_id = None
                if args.status == close_mod.COUNTER:
                    counter_id = (args.counter
                                  or f"ctr-{args.lens}-{record.get('revision', '')[:8]}")
                # Signoff candidacy is state-dependent, so resolve it from the
                # latest record while the same lock protects the eventual ack.
                sid = _signoff_set_for_lens(record, args.lens)
                if sid is not None and not override:
                    ev = _build_signoff_eval(store, record) or {}
                    candidates = set((ev.get("resolved_candidates") or {}).get(sid, []))
                    if agent not in candidates:
                        sys.stderr.write(
                            f"agenttalk close ack: {agent!r} is not a current candidate for "
                            f"signoff {sid!r} "
                            f"(candidates: {sorted(candidates) or 'none'}); refusing. "
                            "Use `close signoffs override` for the lead escape.\n")
                        return 2
                close_mod.apply_ack(
                    record, lens_id=args.lens, status=args.status, agent=agent,
                    from_role=from_role, at=_iso_now(), evidence=evidence,
                    reason=args.reason, counter_id=counter_id,
                    override=override, from_groups=from_groups)
                transaction.commit()
        except (close_mod.CloseConflict, TimeoutError) as e:
            return _close_conflict_result("ack", e)
        except close_mod.CloseError as e:
            sys.stderr.write(f"agenttalk close ack: {e}\n")
            return 2
        msg = f"ack {args.status} lens {args.lens} by {agent}"
        print(msg + (f" (counter {counter_id})" if counter_id else ""))
        return 0

    if action == "draft":
        actor = _resolve_self(getattr(args, "actor", None), roster=roster)
        _check_close_authority(store, actor, "draft")
        try:
            with close_mod.close_transaction(store, args.id) as transaction:
                record = transaction.record
                close_mod.set_draft(
                    record, body=args.message or "", by=actor, at=_iso_now())
                transaction.commit()
        except (close_mod.CloseConflict, TimeoutError) as e:
            return _close_conflict_result("draft", e)
        except close_mod.CloseError as e:
            sys.stderr.write(f"agenttalk close draft: {e}\n")
            return 2
        print(f"draft recorded on {args.id} by {actor}")
        return 0

    if action == "counter":
        if getattr(args, "counter_cmd", None) != "decide":
            sys.stderr.write("agenttalk close counter: the only action is `decide`.\n")
            return 2
        actor = _resolve_self(getattr(args, "actor", None), roster=roster)
        _check_close_authority(store, actor, "counter decide")
        remediation = None
        if args.decision == "accept":
            remediation = {
                "id": args.rem_id or f"rem-{args.counter}",
                "owner": args.rem_owner, "severity": args.rem_severity,
                "affected": args.affected or [], "blocker": bool(args.blocker),
                "gate": args.gate, "fix": args.rem_fix,
                "verification": args.rem_verification,
                "regression_test": args.regression_test, "target": args.target,
            }
        try:
            with close_mod.close_transaction(store, args.id) as transaction:
                record = transaction.record
                close_mod.decide_counter(
                    record, counter_id=args.counter, decision=args.decision, by=actor,
                    at=_iso_now(), reason=args.reason, remediation=remediation)
                transaction.commit()
        except (close_mod.CloseConflict, TimeoutError) as e:
            return _close_conflict_result("counter decide", e)
        except close_mod.CloseError as e:
            sys.stderr.write(f"agenttalk close counter decide: {e}\n")
            return 2
        print(f"counter {args.counter} {args.decision}ed on {args.id} by {actor}")
        return 0

    if action == "check":
        path = close_mod.close_path(store, args.id)
        if not path.exists():
            sys.stderr.write(f"agenttalk close check: no close {args.id!r}.\n")
            return 2
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            record = {"close_id": args.id}  # corrupt -> compute_verdict -> malformed
        gate_check = gate_mod.check_gates(
            store.root, scope=record.get("gate_scope") if isinstance(record, dict) else None)
        rec = record if isinstance(record, dict) else {}
        signoff_eval = _build_signoff_eval(store, rec) if isinstance(record, dict) else None
        worktree_eval = _close_worktree_eval(store, rec) if isinstance(record, dict) else None
        dod_eval = _build_dod_eval(store, rec) if isinstance(record, dict) else None
        result = close_mod.compute_verdict(rec, gate_check, signoff_eval, worktree_eval, dod_eval)
        if getattr(args, "json", False):
            print(json.dumps({**result, "gate_verdict": gate_check["verdict"],
                              "signoff_policy": (None if signoff_eval is None
                                                 else "present" if signoff_eval.get("policy_present")
                                                 else "none"),
                              "worktree_isolation": worktree_eval}, indent=2))
        else:
            _print_verdict(args.id, result)
        return 0 if result["verdict"] == close_mod.VERDICT_GO else 3

    if action == "publish":
        actor = _resolve_self(getattr(args, "actor", None), roster=roster)
        _check_close_authority(store, actor, "publish")
        verdict = close_mod.VERDICT_GO if args.verdict == "go" else close_mod.VERDICT_HOLD
        barrier_epoch = None
        try:
            # The per-close lock spans reload, all impure evaluations, verdict derivation,
            # persistence, and the optional barrier stamp - and evidence is resolved immediately
            # before the write (nothing but in-memory record mutation sits between the resolve at
            # `_build_dod_eval`/`compute_verdict` and `transaction.commit`), keeping the exposure
            # minimal. It is NOT airtight (#66): the per-close lock does NOT exclude EVIDENCE
            # mutators (`gate set`/`gate waive`, `knowledge retract`, signoff writes) - those live
            # under the config lock, a separate mutex - and `commit` itself does a cross-file
            # read+write (`load_close` -> `_write_close`). So an evidence mutation landing in that
            # window can leave a persisted GO whose evidence has since changed. This is a narrow
            # KNOWN, documented race tracked by #66/#31 (no enforced serialization invariant
            # excludes the evidence mutators today); full closure needs one enforced lock spanning
            # every evidence writer + this commit, and a stale-GO detector - both ride the #31
            # close-provenance envelope. Do NOT restore the
            # earlier "no ack/counter can invalidate a GO between check and write" claim; it was
            # false (proven by a real-CLI repro that persisted GO against a gate set red mid-publish).
            with close_mod.close_transaction(store, args.id) as transaction:
                record = transaction.record
                if record.get("status") == close_mod.PUBLISHED:
                    final = record.get("final") or {}
                    if not (
                        verdict == close_mod.VERDICT_GO
                        and args.bump_barrier
                        and final.get("verdict") == close_mod.VERDICT_GO
                        and final.get("barrier_binding") is not None
                    ):
                        sys.stderr.write(
                            f"agenttalk close publish: {args.id!r} is already published; "
                            "`close reopen` first.\n")
                        return 2
                    barrier_epoch = _ensure_close_release_barrier(
                        store, transaction, actor=actor)
                else:
                    gate_check = gate_mod.check_gates(
                        store.root, scope=record.get("gate_scope"))
                    signoff_eval = _build_signoff_eval(store, record)
                    worktree_eval = _close_worktree_eval(store, record)
                    dod_eval = _build_dod_eval(store, record)
                    record["worktree_isolation"] = worktree_eval
                    result = close_mod.compute_verdict(
                        record, gate_check, signoff_eval, worktree_eval, dod_eval)
                    if args.verdict == "go" and result["verdict"] != close_mod.VERDICT_GO:
                        sys.stderr.write(
                            "agenttalk close publish: refusing GO - close check is HOLD:\n")
                        _print_verdict(args.id, result)
                        return 3
                    # Persist the final verdict and a generation-bound barrier intent
                    # before attempting the message write. A retry can then resume
                    # either side of the send/stamp boundary without duplicating it.
                    close_mod.record_publish(
                        record, verdict=verdict, by=actor, at=_iso_now(),
                        reason=args.reason or "", gate_check=gate_check,
                        residual_risk=args.residual_risk, barrier_epoch=None)
                    if verdict == close_mod.VERDICT_GO and args.bump_barrier:
                        record["final"]["barrier_binding"] = (
                            _new_close_barrier_binding(record))
                    transaction.commit()
                    if verdict == close_mod.VERDICT_GO and args.bump_barrier:
                        barrier_epoch = _ensure_close_release_barrier(
                            store, transaction, actor=actor)
        except (close_mod.CloseConflict, TimeoutError) as e:
            return _close_conflict_result("publish", e)
        except close_mod.CloseError as e:
            sys.stderr.write(f"agenttalk close publish: {e}\n")
            return 2
        except (OSError, ValueError) as e:
            sys.stderr.write(
                f"agenttalk close publish: release barrier is incomplete but "
                f"recoverable: {e}. Retry the same publish command.\n")
            return 2
        print(f"published close {args.id}: {verdict} by {actor}"
              + (f"; release barrier {barrier_epoch}" if barrier_epoch else ""))
        return 0 if verdict == close_mod.VERDICT_GO else 3

    if action == "reopen":
        actor = _resolve_self(getattr(args, "actor", None), roster=roster)
        _check_close_authority(store, actor, "reopen")
        revision = None
        clean = None
        if getattr(args, "revision", None):
            try:
                revision, _kind = _resolve_revision(store.root, args.revision)
            except close_mod.CloseError as e:
                sys.stderr.write(f"agenttalk close reopen: {e}\n")
                return 2
            wc = _worktree_clean(store.root)
            clean = True if wc is None else wc
        try:
            with close_mod.close_transaction(store, args.id) as transaction:
                record = transaction.record
                close_mod.reopen(record, by=actor, at=_iso_now(),
                                 revision=revision, revision_clean=clean)
                transaction.commit()
        except (close_mod.CloseConflict, TimeoutError) as e:
            return _close_conflict_result("reopen", e)
        except close_mod.CloseError as e:
            sys.stderr.write(f"agenttalk close reopen: {e}\n")
            return 2
        print(f"reopened close {args.id} by {actor}"
              + (f" @ {revision[:12]} (prior lens acks now stale)" if revision else ""))
        return 0

    if action == "list":
        ids = close_mod.list_close_ids(store)
        if getattr(args, "json", False):
            print(json.dumps(ids, indent=2))
            return 0
        if not ids:
            print("closes: none")
            return 0
        print(f"closes ({len(ids)}):")
        for cid in ids:
            try:
                rec = close_mod.load_close(store, cid)
                final = rec.get("final") or {}
                tag = final.get("verdict") or rec.get("status")
            except close_mod.CloseError:
                tag = "malformed"
            print(f"  {cid}: {tag}")
        return 0

    if action == "show":
        record = close_mod.load_close(store, args.id)
        print(json.dumps(record, indent=2))
        return 0

    sys.stderr.write(
        "agenttalk close: expected open, ack, draft, counter, check, publish, "
        "reopen, list, or show.\n")
    return 2


# ----------------------------------------------------------------- lane (P1)
#
# Lane deliver-gate: the CLI/git adapter resolves all I/O (git diff, merge-tree,
# domain classification, gate check, epoch, registry hash, active-lane snapshot)
# and hands lane_mod.compute_verdict already-resolved data — the verdict stays pure.

def _git_read_one(root, argv: list[str], what: str) -> str:
    rc, out = _git(root, argv)
    if rc != 0 or not out.strip():
        raise lane_mod.LaneError(f"could not read {what} (git rc={rc})")
    return out.strip()


def _common_git_dir(root) -> str:
    rc, out = _git(root, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
    if rc == 0 and out.strip():
        return lane_mod.canonical_host_path(out.strip())
    raw = _git_read_one(root, ["rev-parse", "--git-common-dir"], "common git dir")
    return lane_mod.canonical_host_path(Path(root) / raw)


def _git_dir(root) -> str:
    rc, out = _git(root, ["rev-parse", "--path-format=absolute", "--git-dir"])
    if rc == 0 and out.strip():
        return lane_mod.canonical_host_path(out.strip())
    raw = _git_read_one(root, ["rev-parse", "--git-dir"], "git dir")
    return lane_mod.canonical_host_path(Path(root) / raw)


def _prepare_worktrees_root(store, configured: str | None) -> Path:
    root = lane_mod.worktrees_root(store, configured)
    root_c = lane_mod.canonical_host_path(root)
    repo_c = lane_mod.canonical_host_path(store.root)
    git_c = lane_mod.canonical_host_path(store.root / ".git")
    if root_c == repo_c or root_c == git_c or root_c.startswith(git_c + "/"):
        raise lane_mod.LaneError("worktrees root cannot be the repo root or inside .git")
    if not root_c.startswith(repo_c + "/"):
        raise lane_mod.LaneError("worktrees root must be inside the locked store root")
    marker = root / lane_mod.WORKTREE_MARKER_FILENAME
    if root.exists():
        if not marker.exists() and any(root.iterdir()):
            raise lane_mod.LaneError(
                "worktrees root exists and is non-empty but lacks the agenttalk marker")
    else:
        root.mkdir(parents=True, exist_ok=True)
    marker.write_text("agenttalk managed worktrees\n", encoding="utf-8")
    return root


def _branch_exists(root, branch: str) -> bool:
    rc, _out = _git(root, ["rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"])
    return rc == 0


def _mint_worktree_path(root: Path, lane_id: str, base_sha: str) -> Path:
    return root / f"{lane_id}-{base_sha[:12]}-{uuid.uuid4().hex[:8]}"


def _path_stat_token(path: Path) -> tuple[bool, int | None, int | None]:
    try:
        st = path.stat()
    except OSError:
        return (False, None, None)
    return (True, st.st_mtime_ns, st.st_size)


def _lane_assignment_fingerprint(store) -> tuple:
    """Cheap stale-work detector for assign's lock window.

    The expensive authority read is ``current_epoch()`` outside the lock. Inside
    the lock we only compare filesystem shape that would change if messages,
    config, or the domain registry changed while provisioning was being prepared.
    """
    try:
        names = sorted(p.name for p in store.messages_dir.glob("*.json") if p.is_file())
    except OSError:
        names = []
    return (
        _path_stat_token(store.dir / "config.json"),
        _path_stat_token(store.dir / dom.FILENAME),
        _path_stat_token(store.messages_dir),
        len(names),
        names[-1] if names else None,
    )


def _cleanup_failed_provision(store, *, lane_id: str, base_sha: str,
                              created_path: Path | None,
                              cleanup_branch: bool) -> str | None:
    notes: list[str] = []
    if created_path is not None:
        try:
            rc, _out, err = _git_write(store.root, ["worktree", "remove", "--", str(created_path)])
            if rc != 0:
                notes.append(f"worktree cleanup failed: {err.strip() or rc}")
        except GitWriteError as e:
            notes.append(f"worktree cleanup failed: {e}")
    if cleanup_branch:
        ref = lane_mod.lane_ref(lane_id)
        rc, out = _git(store.root, ["rev-parse", "--verify", f"{ref}^{{commit}}"])
        tip = out.strip()
        if rc == 0:
            if tip == base_sha:
                try:
                    rc2, _out, err = _git_write(store.root, ["update-ref", "-d", ref])
                    if rc2 != 0:
                        notes.append(f"branch cleanup failed: {err.strip() or rc2}")
                except GitWriteError as e:
                    notes.append(f"branch cleanup failed: {e}")
            else:
                notes.append("branch cleanup skipped: lane branch tip changed after failed provision")
    return "; ".join(notes) if notes else None


def _verify_lane_worktree(store, lane: dict, *, expected_base: str | None = None) -> dict:
    wt = lane.get("worktree_path")
    if not isinstance(wt, str) or not wt:
        raise lane_mod.LaneError("lane has no registered worktree")
    lane_id = lane_mod.validate_lane_id(str(lane.get("lane_id")))
    branch = lane_mod.lane_branch(lane_id)
    toplevel = _git_read_one(wt, ["rev-parse", "--show-toplevel"], "worktree toplevel")
    toplevel_c = lane_mod.canonical_host_path(toplevel)
    expected_c = lane_mod.canonical_host_path(wt)
    if toplevel_c != expected_c:
        raise lane_mod.LaneError(
            f"registered worktree path {expected_c!r} does not match git toplevel {toplevel_c!r}")
    stored_c = lane.get("worktree_toplevel_canonical")
    if stored_c and lane_mod.canonical_host_path(stored_c) != toplevel_c:
        raise lane_mod.LaneError("stored worktree canonical path no longer matches git")
    repo_common = _common_git_dir(store.root)
    wt_common = _common_git_dir(wt)
    if repo_common != wt_common:
        raise lane_mod.LaneError("worktree common git dir does not match the store repository")
    if _git_dir(wt) == wt_common:
        raise lane_mod.LaneError("registered worktree is the primary checkout")
    head = _git_read_one(wt, ["rev-parse", "--verify", "HEAD^{commit}"], "worktree HEAD")
    if expected_base is not None and head != expected_base:
        raise lane_mod.LaneError(
            f"new worktree HEAD {head[:12]} does not match base {expected_base[:12]}")
    rc, short_branch = _git(wt, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    detached_at_lane_tip = False
    if rc == 0:
        if short_branch.strip() != branch:
            raise lane_mod.LaneError(
                f"worktree branch {short_branch.strip()!r} does not match {branch!r}")
    else:
        branch_tip = _git_read_one(store.root, [
            "rev-parse", "--verify", f"{lane_mod.lane_ref(lane_id)}^{{commit}}"],
            "lane branch tip")
        if head != branch_tip:
            raise lane_mod.LaneError("detached worktree HEAD is not at the lane branch tip")
        detached_at_lane_tip = True
    rc, status = _git(wt, ["status", "--porcelain", "--untracked-files=no"])
    if rc != 0:
        raise lane_mod.LaneError(f"could not verify worktree tracked status (git rc={rc})")
    if status.strip():
        raise lane_mod.LaneError("worktree has staged/unstaged tracked changes")
    return {
        "head": head,
        "worktree_toplevel_canonical": toplevel_c,
        "common_git_dir_canonical": wt_common,
        "tracked_tree_clean": True,
        "detached_at_lane_tip": detached_at_lane_tip,
    }


def _release_class_lane(lane: dict) -> bool:
    return lane.get("release_class", True) is not False


def _lane_worktree_idle(store, lane: dict) -> bool:
    wt = lane.get("worktree_path")
    if not wt:
        return True
    wt_c = lane_mod.canonical_host_path(wt)
    lane_id = lane.get("lane_id")
    try:
        for marker in store.list_launch_requests():
            scope = marker.get("scope") if isinstance(marker, dict) else None
            if marker.get("lane_id") == lane_id or (isinstance(scope, dict) and scope.get("lane_id") == lane_id):
                state = marker.get("state")
                if state not in {"archived", "failed", "launched"}:
                    return False
    except Exception:  # noqa: BLE001
        return False
    try:
        state_path = store.dir / "state" / "supervisor-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8-sig")) if state_path.exists() else {}
    except (OSError, ValueError):
        return False
    for section in ("agents", "ephemeral_reviewers"):
        obj = state.get(section) if isinstance(state, dict) else None
        if not isinstance(obj, dict):
            continue
        active = obj.get("active") if section == "ephemeral_reviewers" else obj
        if not isinstance(active, dict):
            continue
        for rec in active.values():
            if not isinstance(rec, dict):
                continue
            cwd = rec.get("cwd") or rec.get("launch_cwd") or rec.get("workspace_path")
            if isinstance(cwd, str) and lane_mod.canonical_host_path(cwd) == wt_c:
                return False
    return True


def _worktree_list(root) -> list[dict]:
    rc, out = _git(root, ["worktree", "list", "--porcelain"])
    if rc != 0:
        return []
    records: list[dict] = []
    current: dict[str, str] = {}
    for line in out.splitlines():
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        if " " in line:
            key, value = line.split(" ", 1)
        else:
            key, value = line, ""
        current[key] = value
    if current:
        records.append(current)
    return records


def _lane_id_from_worktree_dir(name: str) -> str | None:
    parts = name.rsplit("-", 2)
    if len(parts) != 3:
        return None
    lane_id, base12, nonce = parts
    if not re.fullmatch(r"[0-9a-f]{12}", base12) or not re.fullmatch(r"[0-9a-f]{8}", nonce):
        return None
    try:
        return lane_mod.validate_lane_id(lane_id)
    except lane_mod.LaneError:
        return None


def _managed_worktree_paths(store) -> dict[str, str]:
    found: dict[str, str] = {}
    try:
        markers = list(store.root.rglob(lane_mod.WORKTREE_MARKER_FILENAME))
    except OSError:
        return found
    for marker in markers:
        root = marker.parent
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir():
                continue
            lane_id = _lane_id_from_worktree_dir(child.name)
            if lane_id:
                found.setdefault(lane_mod.lane_branch(lane_id), str(child))
    return found


def _lane_branch_delete_safe(root, lane_id: str, target: str) -> tuple[bool, str]:
    ref = lane_mod.lane_ref(lane_id)
    rc, _out = _git(root, ["merge-base", "--is-ancestor", ref, target])
    if rc != 0:
        return False, "branch is not proven ancestor of target"
    return True, "branch tip is ancestor of target"


def _lane_branch_gc_allowed(lane: dict | None) -> tuple[bool, str]:
    if lane is None:
        return False, "no lane record; branch deletion needs manual review"
    removable_states = {
        lane_mod.STATUS_DELIVERED,
        lane_mod.STATUS_ABANDONED,
        lane_mod.STATUS_CLEANUP_FAILED,
        lane_mod.STATUS_CLEANUP_PENDING,
    }
    if lane.get("status") in removable_states or lane.get("worktree_state") in removable_states:
        return True, "lane is retired or cleanup-pending"
    return False, "lane is still active or not in a cleanup state"


def _lane_worktree_remove_safe(store, lane_id: str, lane: dict | None,
                               worktree_path: str | None,
                               branch_delete_safe: bool) -> tuple[bool, str]:
    if not worktree_path:
        return False, "no worktree path discovered"
    if lane is None and not branch_delete_safe:
        return False, "no lane record and branch is not proven safe"
    probe = dict(lane or {"lane_id": lane_id})
    probe["lane_id"] = lane_id
    probe["worktree_path"] = worktree_path
    status = probe.get("status")
    wt_state = probe.get("worktree_state")
    removable_states = {
        lane_mod.STATUS_DELIVERED,
        lane_mod.STATUS_ABANDONED,
        lane_mod.STATUS_CLEANUP_FAILED,
        lane_mod.STATUS_CLEANUP_PENDING,
    }
    if lane is not None and status not in removable_states and wt_state not in removable_states:
        return False, "lane is not delivered, abandoned, or cleanup-pending"
    if not _lane_worktree_idle(store, probe):
        return False, "worktree has an active or pending launch"
    try:
        _verify_lane_worktree(store, probe)
    except lane_mod.LaneError as e:
        return False, f"worktree is not clean/verifiable: {e}"
    return True, "worktree is clean and idle"


def _lane_diff(root, base: str, head: str) -> dict:
    """`git diff --name-status -z -M -C base..head` parsed structurally. touched =
    paths a delivery WRITES (M/A/D/T, rename old+new, copy DEST); a copy SOURCE is
    evidence-only. Fails closed: unavailable / parse_error."""
    rc, out = _git(root, ["diff", "--name-status", "-z", "-M", "-C", f"{base}..{head}"])
    if rc != 0:
        return {"error": "unavailable", "paths": []}
    toks = out.split("\x00")
    paths: list[dict] = []
    i = 0
    try:
        while i < len(toks):
            st = toks[i]
            if not st:
                i += 1
                continue
            code = st[0]
            if code in ("R", "C"):
                old, new = toks[i + 1], toks[i + 2]
                i += 3
                if code == "R":   # rename: old removed + new added -> both touched
                    paths.append({"path": old, "status": st, "touched": True, "role": "rename-old"})
                    paths.append({"path": new, "old_path": old, "status": st,
                                  "touched": True, "role": "rename-new"})
                else:             # copy: dest touched, source evidence-only
                    paths.append({"path": old, "status": st, "touched": False, "role": "copy-source"})
                    paths.append({"path": new, "old_path": old, "status": st,
                                  "touched": True, "role": "copy-dest"})
            else:
                p = toks[i + 1]
                i += 2
                paths.append({"path": p, "status": st, "touched": True})
    except IndexError:
        return {"error": "parse_error", "paths": []}
    return {"error": None, "paths": paths}


def _lane_merge(root, target_head: str, head: str) -> dict:
    """`git merge-tree --write-tree <target_head> <head>` is the conflict authority.
    rc 0 = clean; rc 1 = conflict; anything else (incl. git <2.38 / unavailable) =
    honest-degraded unknown — NEVER inferred clean."""
    rc, out = _git(root, ["merge-tree", "--write-tree", target_head, head])
    if rc == 0:
        return {"status": "clean", "detail": (out.strip().splitlines() or [""])[0][:40]}
    if rc == 1:
        return {"status": "conflict", "detail": "merge-tree reported conflicts"}
    return {"status": "unknown", "detail": f"merge-tree rc={rc} (requires Git>=2.38?)"}


def _lane_resolve(store, ref: str):
    """Resolve a ref/SHA to a full SHA via the P2 git helper; raise LaneError."""
    try:
        sha, _kind = _resolve_revision(store.root, ref)
        return sha
    except Exception as e:  # noqa: BLE001 - normalize to LaneError for the CLI
        raise lane_mod.LaneError(f"could not resolve {ref!r} to a full SHA: {e}") from e


def _lane_eval(store, lane: dict, other_active: list[dict], head: str,
               gate_scope: str | None):
    """Resolve everything and run the pure verdict. Returns (verdict, ctx) where ctx
    carries changed/merge/gate_check/head/target_head_now for the artifact + output."""
    cfg_casefold = dom.default_casefold_paths()
    reg = _load_domain_registry(store)
    base = lane.get("base_sha")
    changed = _lane_diff(store.root, base, head)
    touched = [p["path"] for p in changed.get("paths", []) if p.get("touched")]
    # Resolve shared-path approver authority at eval time so compute_verdict can
    # REVALIDATE each recorded approval against the current registry (audit fix):
    # for every touched shared path, map each matching shared entry's glob to the
    # set of agents currently authorized for it (close leads + the entry's
    # default_approvers). The pure verdict reads cls['shared_entry_approvers'] /
    # cls['close_leads'] and never resolves refsets itself.
    cfg = store.load_config()
    leads = sorted(_close_lead_set(store))

    def _classify(p: str) -> dict:
        cls = dom.check_path(reg.data, p)
        if cls.get("shared_paths"):
            per_glob: dict[str, list[str]] = {}
            for entry in reg.data.get("shared_paths", []):
                if dom.glob_matches(entry["glob"], p, casefold=cfg_casefold):
                    per_glob[entry["glob"]] = sorted(
                        set(leads)
                        | set(dom.resolve_refset(entry.get("default_approvers") or {}, cfg)))
            cls["shared_entry_approvers"] = per_glob
            cls["close_leads"] = leads
        return cls

    classifications = {p: _classify(p) for p in touched}
    target_head_now = _lane_resolve(store, lane.get("target_ref"))
    merge = _lane_merge(store.root, target_head_now, head)
    scope = gate_scope or f"lane:{lane.get('lane_id')}"
    gate_check = gate_mod.check_gates(store.root, scope=scope)
    current_epoch = store.current_epoch()
    verdict = lane_mod.compute_verdict(
        lane, changed=changed, classifications=classifications,
        active_lanes=other_active, current_epoch=current_epoch,
        current_registry_hash=reg.registry_hash, merge=merge, gate_check=gate_check,
        casefold=cfg_casefold)
    ctx = {"changed": changed, "merge": merge, "gate_check": gate_check, "head": head,
           "target_head_now": target_head_now,
           "target_moved": target_head_now != lane.get("target_head_at_assign"),
           "classifications": classifications, "current_epoch": current_epoch,
           "current_registry_hash": reg.registry_hash, "gate_scope": scope,
           "config_snapshot": cfg}
    return verdict, ctx


def _print_lane_verdict(lane_id: str, verdict: dict, ctx: dict) -> None:
    print(f"{verdict['verdict']}  (lane {lane_id} @ {ctx['head'][:12]})")
    if ctx.get("target_moved"):
        print(f"  note: target moved since assign -> recomputed merge vs {ctx['target_head_now'][:12]}")
    for h in verdict["holds"]:
        print(f"  HOLD[{h['code']}]: {h['detail']}")


def _lane_candidate(store, lane: dict, requested_head: str | None, *, delivery: bool):
    provenance = None
    if lane.get("worktree_path") and (delivery or not requested_head):
        provenance = _verify_lane_worktree(store, lane)
        head = provenance["head"]
        if requested_head:
            requested = _lane_resolve(store, requested_head)
            if requested != head:
                raise lane_mod.LaneError(
                    "--head does not match the registered lane worktree HEAD; "
                    "deliver from the provisioned worktree or omit --head")
    else:
        head = _lane_resolve(store, requested_head) if requested_head else \
            _lane_resolve(store, "HEAD")
    return head, provenance


def _lane_transaction_matches(lane: dict, pending: dict) -> bool:
    return bool(
        lane.get("status") == lane_mod.STATUS_DELIVERED
        and lane.get("instance_id") == pending.get("lane_instance_id")
        and lane.get("generation") == pending.get("lane_generation")
        and lane.get("delivery_transaction_id") == pending.get("transaction_id")
    )


def _lane_transaction_lock_path(store, lane_id: str) -> Path:
    safe_id = lane_mod.validate_lane_id(lane_id)
    return store.dir / "locks" / f"lane-{safe_id}.transaction.lock"


class _LaneTerminalBoundaryChanged(lane_mod.LaneError):
    def __init__(self, error: str, conflicts: list[str]) -> None:
        super().__init__(error)
        self.error = error
        self.conflicts = conflicts


def _lane_reset_lock_path(store) -> Path:
    return store.dir / "locks" / "lane-reset.lock"


def _lane_publication_marker_complete(
        lane: object, pending: dict, artifact: Path) -> bool:
    if not isinstance(lane, dict) or not _lane_transaction_matches(lane, pending):
        raise lane_mod.LaneError(
            "lane instance changed before the committed-artifact marker update"
        )
    current_pending = lane.get("publish_pending")
    if current_pending is False:
        marked = lane.get("delivery_artifact")
        if (not isinstance(marked, str)
                or lane_mod.canonical_host_path(marked)
                != lane_mod.canonical_host_path(artifact)):
            raise lane_mod.LaneError("lane publication marker conflicts with transaction")
        return True
    if (not isinstance(current_pending, dict)
            or current_pending.get("transaction_id") != pending.get("transaction_id")):
        raise lane_mod.LaneError("lane publication marker no longer matches transaction")
    expected_nonce = pending.get("terminal_rebind_nonce")
    if (pending.get("terminal_rebound") is not True
            or current_pending.get("terminal_rebound") is not True
            or not re.fullmatch(r"[0-9a-f]{32}", str(expected_nonce or ""))
            or current_pending.get("terminal_rebind_nonce") != expected_nonce):
        raise lane_mod.LaneError(
            "lane publication marker no longer matches terminal binding"
        )
    return False


def _lane_checkpoint_publication(store, lane_id: str, pending: dict, artifact: Path) -> None:
    with store._config_lock():
        data = lane_mod.load_lanes(store)
        lane = (data.get("lanes") or {}).get(lane_id)
        if _lane_publication_marker_complete(lane, pending, artifact):
            return

    rebound, error, existing_final = _lane_rebind_pending_delivery(
        store, lane_id, checkpoint=False,
    )
    if error is not None:
        conflicts = _lane_abort_terminal_binding(
            store, lane_id, rebound, artifact=existing_final,
        )
        raise _LaneTerminalBoundaryChanged(error, conflicts)
    if (existing_final is None
            or lane_mod.canonical_host_path(existing_final)
            != lane_mod.canonical_host_path(artifact)):
        error = "committed final changed before completion checkpoint"
        conflicts = _lane_abort_terminal_binding(
            store, lane_id, rebound, artifact=existing_final,
        )
        raise _LaneTerminalBoundaryChanged(error, conflicts)
    if rebound.get("terminal_rebind_nonce") != pending.get("terminal_rebind_nonce"):
        raise lane_mod.LaneError(
            "lane publication marker changed during completion revalidation"
        )

    # The successful rebind is the point-in-time verdict boundary. This nonce-bound
    # state checkpoint keeps the final nonconsumable until that observation succeeds.
    with store._config_lock():
        data = lane_mod.load_lanes(store)
        lane = (data.get("lanes") or {}).get(lane_id)
        if _lane_publication_marker_complete(lane, rebound, artifact):
            return
        lane["publish_pending"] = False
        lane["delivery_artifact"] = str(artifact)
        lane["published_at"] = _iso_now()
        lane_mod.save_lanes(store, data)


def _lane_checkpoint_cleanup(store, lane_id: str, pending: dict, *,
                             success: bool, error: str | None = None) -> None:
    with store._config_lock():
        data = lane_mod.load_lanes(store)
        lane = (data.get("lanes") or {}).get(lane_id)
        if not isinstance(lane, dict) or not _lane_transaction_matches(lane, pending):
            raise lane_mod.LaneError("lane instance changed before cleanup checkpoint")
        lane["cleanup_pending"] = not success
        lane["worktree_state"] = (
            lane_mod.STATUS_DELIVERED if success else lane_mod.STATUS_CLEANUP_FAILED
        )
        if error:
            lane["worktree_cleanup_error"] = error[:500]
        else:
            lane.pop("worktree_cleanup_error", None)
        lane_mod.save_lanes(store, data)


def _lane_remove_prepared(pending: dict) -> None:
    raw = pending.get("prepared_artifact")
    if not isinstance(raw, str):
        return
    try:
        Path(raw).unlink(missing_ok=True)
    except OSError:
        pass


def _lane_rebind_pending_delivery(
        store, lane_id: str, *, checkpoint: bool = True,
) -> tuple[dict, str | None, Path | None]:
    """Re-evaluate a persisted, nonconsumable GO after its first state save.

    Transaction evidence is collected before the provisional delivery save. An
    equivalent live snapshot after that save brackets the transaction boundary.
    ``checkpoint=False`` performs the same observation without rotating the durable
    binding nonce, for the checks immediately around publication. Git and artifact
    I/O stay outside the global config lock.
    """
    with store._config_lock():
        data = lane_mod.load_lanes(store)
        lane = (data.get("lanes") or {}).get(lane_id)
        pending_raw = lane.get("publish_pending") if isinstance(lane, dict) else None
        if (isinstance(lane, dict) and lane.get("status") == lane_mod.STATUS_DELIVERED
                and pending_raw is False):
            return {}, None, None
        if (not isinstance(lane, dict) or not isinstance(pending_raw, dict)
                or not _lane_transaction_matches(lane, pending_raw)):
            raise lane_mod.LaneError("lane has no matching pending delivery to rebind")
        pending = dict(pending_raw)
        expected_active = pending.get("active_lane_fingerprints")
        if not isinstance(expected_active, list):
            raise lane_mod.LaneError("pending delivery active-lane snapshot is missing")
        active_changed = (
            lane_mod.active_lane_fingerprints(data, exclude=lane_id) != expected_active
        )
        others = [item for item in lane_mod.reservation_lanes(data)
                  if item.get("lane_id") != lane_id]

    lane_snapshot = pending.get("lane_snapshot")
    if not isinstance(lane_snapshot, dict):
        raise lane_mod.LaneError("pending delivery lane snapshot is missing")
    head = pending.get("head_sha")
    if not isinstance(head, str) or not lane_mod._FULL_SHA_RE.fullmatch(head):
        raise lane_mod.LaneError("pending delivery head is malformed")
    committed = lane_mod.existing_committed_delivery_artifact(store, pending)
    if committed is not None:
        committed = Path(committed)
        transaction_evidence = lane_mod.validate_committed_transaction_artifact(
            committed, store=store, pending=pending,
        )
    else:
        prepared_path = pending.get("prepared_artifact")
        if not isinstance(prepared_path, str):
            raise lane_mod.LaneError("pending delivery prepared artifact is missing")
        transaction_evidence = lane_mod.validate_prepared_delivery_artifact(
            Path(prepared_path), store=store, lane_id=lane_id, head_sha=head,
            transaction_id=str(pending.get("transaction_id")),
            lane_instance_id=str(pending.get("lane_instance_id")),
            lane_generation=pending.get("lane_generation"),
        )
    if active_changed:
        return pending, "active-lane set changed across the delivery state save", committed
    expected_snapshot = transaction_evidence.get("evaluation_snapshot")
    expected_evaluation = pending.get("evaluation_fingerprint")
    expected_input = pending.get("input_fingerprint")
    if (not isinstance(expected_snapshot, dict)
            or lane_mod.fingerprint(expected_snapshot) != expected_evaluation):
        raise lane_mod.LaneError(
            "pending delivery evaluation snapshot differs from transaction evidence"
        )
    if lane_mod.fingerprint(lane_snapshot) != expected_snapshot.get("lane_fingerprint"):
        raise lane_mod.LaneError(
            "pending delivery lane snapshot differs from transaction evidence"
        )
    if expected_snapshot.get("active_lane_fingerprints") != expected_active:
        raise lane_mod.LaneError(
            "pending delivery active lanes differ from transaction evidence"
        )
    if (not re.fullmatch(r"[0-9a-f]{64}", str(expected_input or ""))
            or expected_snapshot.get("cooperating_digest") != expected_input):
        raise lane_mod.LaneError("pending delivery cooperating-input digest is malformed")

    cooperating_before = lane_mod.cooperating_input_fingerprint(store)
    if cooperating_before != expected_input:
        return (
            pending,
            "cooperating config/domain/gate/epoch/message inputs changed",
            committed,
        )
    try:
        rebound_head, provenance = _lane_candidate(
            store, lane_snapshot, head, delivery=True,
        )
        verdict, ctx = _lane_eval(
            store, lane_snapshot, others, rebound_head,
            str(expected_snapshot.get("gate_scope")),
        )
        final_head, final_provenance = _lane_candidate(
            store, lane_snapshot, head, delivery=True,
        )
        final_target = _lane_resolve(store, str(lane_snapshot.get("target_ref")))
    except lane_mod.LaneError as exc:
        return pending, f"terminal Git/worktree recheck failed ({exc})", committed
    cooperating_after = lane_mod.cooperating_input_fingerprint(store)
    if verdict["verdict"] != lane_mod.VERDICT_GO:
        return pending, "terminal verdict is HOLD", committed
    if (final_head != rebound_head or final_target != ctx.get("target_head_now")
            or lane_mod.fingerprint(final_provenance or {})
            != lane_mod.fingerprint(provenance or {})):
        return pending, "target/head/worktree changed during terminal evaluation", committed
    if cooperating_before != cooperating_after:
        return pending, "cooperating inputs changed during terminal evaluation", committed
    ctx["cooperating_input_fingerprint"] = cooperating_after
    rebound_snapshot = lane_mod.build_evaluation_snapshot(
        lane=lane_snapshot, active_lanes=others, context=ctx,
        worktree_provenance=final_provenance,
    )
    if lane_mod.fingerprint(rebound_snapshot) != expected_evaluation:
        return pending, "terminal evaluation differs from the prepared GO snapshot", committed
    checkpoint_input = lane_mod.cooperating_input_fingerprint(store)
    if checkpoint_input != cooperating_after:
        return (
            pending,
            "cooperating inputs changed before terminal binding checkpoint",
            committed,
        )

    with store._config_lock():
        data = lane_mod.load_lanes(store)
        latest = (data.get("lanes") or {}).get(lane_id)
        current_pending = latest.get("publish_pending") if isinstance(latest, dict) else None
        if (isinstance(latest, dict) and current_pending is False
                and _lane_transaction_matches(latest, pending)):
            return pending, None, committed
        if (not isinstance(latest, dict) or not isinstance(current_pending, dict)
                or not _lane_transaction_matches(latest, current_pending)):
            raise lane_mod.LaneError("lane transaction changed before terminal binding checkpoint")
        if lane_mod.active_lane_fingerprints(data, exclude=lane_id) != expected_active:
            return (
                dict(current_pending),
                "active-lane set changed before terminal binding checkpoint",
                committed,
            )
        if not checkpoint:
            return dict(current_pending), None, committed
        current_pending = dict(current_pending)
        current_pending.pop("terminal_hold", None)
        current_pending["terminal_rebound"] = True
        current_pending["terminal_rebind_nonce"] = uuid.uuid4().hex
        current_pending["terminal_rebound_at"] = _iso_now()
        latest["publish_pending"] = current_pending
        latest.pop("terminal_hold", None)
        lane_mod.save_lanes(store, data)
        return current_pending, None, committed


def _lane_rollback_pending_delivery(
        store, lane_id: str, pending: dict) -> tuple[str, list[str]]:
    """Restore the active lane, or retain a recoverable HOLD if that would overlap."""
    lane_snapshot = pending.get("lane_snapshot")
    if not isinstance(lane_snapshot, dict) or lane_snapshot.get("status") != lane_mod.STATUS_ACTIVE:
        raise lane_mod.LaneError("pending delivery cannot restore its active lane snapshot")
    with store._config_lock():
        data = lane_mod.load_lanes(store)
        latest = (data.get("lanes") or {}).get(lane_id)
        current_pending = latest.get("publish_pending") if isinstance(latest, dict) else None
        if (not isinstance(latest, dict) or not isinstance(current_pending, dict)
                or not _lane_transaction_matches(latest, current_pending)):
            raise lane_mod.LaneError("lane transaction changed before terminal rollback")
        if (current_pending.get("terminal_rebind_nonce")
                != pending.get("terminal_rebind_nonce")):
            return "bound", []
        conflicts = lane_mod.reservation_conflicts(data, lane_snapshot, exclude=lane_id)
        if conflicts:
            previous_hold = current_pending.get("terminal_hold")
            held_at = previous_hold.get("held_at") \
                if isinstance(previous_hold, dict) else None
            hold = {
                "code": "terminal_rollback_overlap",
                "detail": (
                    "restoring the original ACTIVE lane would overlap lanes assigned "
                    "after provisional delivery"
                ),
                "conflicting_lane_ids": conflicts,
                "held_at": held_at if isinstance(held_at, str) else _iso_now(),
            }
            current_pending = dict(current_pending)
            current_pending["terminal_hold"] = hold
            latest["publish_pending"] = current_pending
            latest["terminal_hold"] = hold
            lane_mod.save_lanes(store, data)
            return "held", conflicts
        data["lanes"][lane_id] = dict(lane_snapshot)
        lane_mod.save_lanes(store, data)
    return "restored", []


def _lane_abort_terminal_binding(
        store, lane_id: str, pending: dict, *, artifact: Path | None = None) -> list[str]:
    if artifact is not None and artifact.exists():
        _lane_quarantine_rejected_final(store, artifact)
    rollback, conflicts = _lane_rollback_pending_delivery(store, lane_id, pending)
    if rollback == "restored":
        _lane_remove_prepared(pending)
    elif rollback == "bound":
        raise lane_mod.LaneError(
            "lane terminal binding changed before the stale transaction could be rolled back"
        )
    return conflicts


def _lane_prepare_publication(store, lane_id: str) -> tuple[str | None, list[str]]:
    with store._config_lock():
        data = lane_mod.load_lanes(store)
        lane = (data.get("lanes") or {}).get(lane_id)
        pending_raw = lane.get("publish_pending") if isinstance(lane, dict) else None
        if (isinstance(lane, dict) and lane.get("status") == lane_mod.STATUS_DELIVERED
                and pending_raw is False):
            return None, []
        if (not isinstance(lane, dict) or not isinstance(pending_raw, dict)
                or not _lane_transaction_matches(lane, pending_raw)):
            raise lane_mod.LaneError("lane has no matching pending delivery to publish")
        pending = dict(pending_raw)
    pending, error, existing_final = _lane_rebind_pending_delivery(store, lane_id)
    if error is None:
        return None, []
    conflicts = _lane_abort_terminal_binding(
        store, lane_id, pending, artifact=existing_final,
    )
    return error, conflicts


def _lane_terminal_boundary_error(error: str, conflicts: list[str]) -> str:
    if conflicts:
        conflict_ids = ", ".join(repr(item) for item in conflicts)
        return (
            "agenttalk lane deliver: terminal inputs changed at the delivery boundary "
            f"({error}); no consumable evidence was published. The lane remains delivered "
            "in a nonconsumable terminal HOLD because restoring it ACTIVE would overlap "
            f"lane(s) {conflict_ids}. Resolve or abandon the conflicting lane(s), then "
            "retry lane deliver with the same head.\n"
        )
    return (
        "agenttalk lane deliver: terminal inputs changed at the delivery boundary "
        f"({error}); no consumable evidence was published, and the lane was restored "
        "active.\n"
    )


def _lane_recovery_diagnosis(store, lane: dict) -> tuple[str, str]:
    if "publish_pending" not in lane:
        return "marker_missing", "delivered lane has no publication marker"
    pending = lane.get("publish_pending")
    if pending is False:
        cleanup = lane.get("cleanup_pending")
        if cleanup is True:
            return "cleanup_pending", "committed publication is waiting for worktree cleanup"
        if cleanup is not False:
            return "cleanup_marker_corrupt", "worktree cleanup marker is not explicit true/false"
        return "complete", "publication marker is complete"
    if not isinstance(pending, dict):
        return "marker_corrupt", "publication marker is not an object or explicit false"
    if not _lane_transaction_matches(lane, pending):
        return "transaction_invalid", "publication marker does not match the lane transaction"
    hold = pending.get("terminal_hold") or lane.get("terminal_hold")
    if isinstance(hold, dict):
        return "terminal_hold", str(hold.get("detail") or "terminal recovery is held")
    try:
        final = lane_mod.existing_committed_delivery_artifact(store, pending)
    except lane_mod.LaneError as exc:
        try:
            committed = lane_mod.delivery_transaction_final_path(store, pending)
        except lane_mod.LaneError:
            committed = None
        state = (
            "final_invalid"
            if committed is not None and committed.exists()
            else "transaction_invalid"
        )
        return state, str(exc)
    if final is not None:
        return "final_pending_marker", "committed final exists but marker save is pending"
    prepared_raw = pending.get("prepared_artifact")
    prepared = Path(prepared_raw) if isinstance(prepared_raw, str) else None
    if prepared is not None and not prepared.is_absolute():
        prepared = store.root / prepared
    if prepared is None or not prepared.exists():
        return "prepared_missing", "prepared delivery evidence is missing"
    try:
        lane_mod.validate_prepared_delivery_artifact(
            prepared, store=store, lane_id=str(pending.get("lane_id")),
            head_sha=str(pending.get("head_sha")),
            transaction_id=str(pending.get("transaction_id")),
            lane_instance_id=str(pending.get("lane_instance_id")),
            lane_generation=pending.get("lane_generation"),
        )
    except lane_mod.LaneError as exc:
        return "prepared_invalid", str(exc)
    return "publication_pending", "prepared evidence is waiting for terminal rebind/publication"


def _lane_recovery_active_snapshot(lane: dict) -> dict:
    restored = dict(lane)
    for key in (
        "cleanup_pending", "delivered_at", "delivered_by", "delivered_head",
        "delivery_artifact", "delivery_transaction_id", "prepared_artifact",
        "publish_pending", "published_at", "recovery_quarantined_final",
        "terminal_hold", "worktree_cleanup_error",
    ):
        restored.pop(key, None)
    restored["status"] = lane_mod.STATUS_ACTIVE
    if restored.get("worktree_path"):
        restored["worktree_state"] = lane_mod.STATUS_ACTIVE
    return restored


def _lane_recovery_final_candidate(store, lane_id: str, lane: dict,
                                   pending: dict | None) -> Path | None:
    transaction_id = lane.get("delivery_transaction_id")
    delivered_head = lane.get("delivered_head")
    if (re.fullmatch(r"[0-9a-f]{32}", str(transaction_id or ""))
            and lane_mod._FULL_SHA_RE.fullmatch(str(delivered_head or ""))):
        return lane_mod.delivery_artifact_path(
            store, lane_id, str(delivered_head), str(transaction_id),
        )
    if pending is not None:
        try:
            return Path(lane_mod.delivery_transaction_final_path(store, pending))
        except lane_mod.LaneError:
            pass
    marked = lane.get("delivery_artifact")
    if not isinstance(marked, str) or not marked:
        return None
    candidate = Path(marked)
    if not candidate.is_absolute():
        candidate = store.root / candidate
    if (lane_mod.canonical_host_path(candidate.parent)
            != lane_mod.canonical_host_path(lane_mod.deliveries_dir(store))):
        raise lane_mod.LaneError(
            "corrupt delivery marker points outside the committed artifact directory"
        )
    return candidate


def _lane_quarantine_rejected_final(store, final: Path) -> Path:
    if (lane_mod.canonical_host_path(final.parent)
            != lane_mod.canonical_host_path(lane_mod.deliveries_dir(store))):
        raise lane_mod.LaneError(
            "rejected delivery final is outside the committed artifact directory"
        )
    quarantine = lane_mod.deliveries_dir(store) / ".recovery-quarantine"
    quarantine.mkdir(parents=True, exist_ok=True)
    destination = quarantine / f"{final.name}.{uuid.uuid4().hex}.rejected"
    os.replace(final, destination)
    return destination


def _lane_recover_delivery(
        store, lane_id: str, *, reason: str) -> tuple[str, list[str]]:
    with store._config_lock():
        data = lane_mod.load_lanes(store)
        lane = (data.get("lanes") or {}).get(lane_id)
        if (not isinstance(lane, dict)
                or not lane_mod.delivery_recovery_required(lane)):
            raise lane_mod.LaneError("lane has no incomplete delivered transaction to recover")
        expected = lane_mod.fingerprint(lane)
        observed = dict(lane)
        pending = lane.get("publish_pending")
        pending_copy = dict(pending) if isinstance(pending, dict) else None

    quarantined_final = None
    final_candidate = _lane_recovery_final_candidate(
        store, lane_id, observed, pending_copy,
    )
    if pending_copy is not None and _lane_transaction_matches(observed, pending_copy):
        try:
            final = lane_mod.existing_committed_delivery_artifact(store, pending_copy)
        except lane_mod.LaneError:
            final = None
        if final is not None:
            return "committed", []
    if final_candidate is not None and final_candidate.exists():
        quarantined_final = _lane_quarantine_rejected_final(store, final_candidate)

    restored = _lane_recovery_active_snapshot(observed)
    with store._config_lock():
        data = lane_mod.load_lanes(store)
        current = (data.get("lanes") or {}).get(lane_id)
        if not isinstance(current, dict) or lane_mod.fingerprint(current) != expected:
            suffix = (
                f"; rejected final was quarantined at {quarantined_final}"
                if quarantined_final is not None else ""
            )
            raise lane_mod.LaneError(
                f"lane changed while preparing recovery; retry{suffix}"
            )
        conflicts = lane_mod.reservation_conflicts(data, restored, exclude=lane_id)
        if conflicts:
            hold = {
                "code": "recovery_overlap",
                "detail": "restoring ACTIVE would overlap a current path reservation",
                "conflicting_lane_ids": conflicts,
                "held_at": _iso_now(),
            }
            current["terminal_hold"] = hold
            if quarantined_final is not None:
                current["recovery_quarantined_final"] = str(quarantined_final)
            marker = current.get("publish_pending")
            if isinstance(marker, dict):
                marker = dict(marker)
                marker["terminal_hold"] = hold
                current["publish_pending"] = marker
            lane_mod.save_lanes(store, data)
            return "held", conflicts
        restored["delivery_recovery"] = {
            "at": _iso_now(),
            "reason": reason,
            "transaction_id": observed.get("delivery_transaction_id"),
        }
        quarantine_record = quarantined_final or observed.get("recovery_quarantined_final")
        if quarantine_record is not None:
            restored["delivery_recovery"]["quarantined_final"] = str(quarantine_record)
        data["lanes"][lane_id] = restored
        lane_mod.save_lanes(store, data)
    return "restored", []


def _lane_finalize_delivery(store, lane_id: str) -> tuple[Path, bool, str]:
    """Publish only while the persisted transaction remains a freshly bound GO."""
    with store._config_lock():
        data = lane_mod.load_lanes(store)
        lane = (data.get("lanes") or {}).get(lane_id)
        if not isinstance(lane, dict) or lane.get("status") != lane_mod.STATUS_DELIVERED:
            raise lane_mod.LaneError("lane has no delivered transaction to resume")
        pending_raw = lane.get("publish_pending")
        if isinstance(pending_raw, dict):
            pending = dict(pending_raw)
            publication_pending = True
            if pending.get("terminal_rebound") is not True:
                raise lane_mod.LaneError(
                    "delivery transaction terminal inputs have not been rebound"
                )
        elif pending_raw is False:
            publication_pending = False
            pending = {
                "transaction_id": lane.get("delivery_transaction_id"),
                "lane_id": lane_id,
                "lane_instance_id": lane.get("instance_id"),
                "lane_generation": lane.get("generation"),
                "head_sha": lane.get("delivered_head"),
                "committed_artifact": lane.get("delivery_artifact"),
                "prepared_artifact": lane.get("prepared_artifact"),
            }
        else:
            raise lane_mod.LaneError("delivered lane has no recoverable publication marker")
        lane_snapshot = dict(lane)

    if publication_pending:
        pending, error, existing_final = _lane_rebind_pending_delivery(
            store, lane_id, checkpoint=False,
        )
        if error is not None:
            conflicts = _lane_abort_terminal_binding(
                store, lane_id, pending, artifact=existing_final,
            )
            raise _LaneTerminalBoundaryChanged(error, conflicts)
        artifact = Path(lane_mod.publish_delivery_artifact(store, pending))
        _lane_checkpoint_publication(store, lane_id, pending, artifact)
        _lane_remove_prepared(pending)
    else:
        artifact_raw = pending.get("committed_artifact")
        if not isinstance(artifact_raw, str):
            raise lane_mod.LaneError("completed delivery artifact marker is missing")
        artifact = Path(artifact_raw)
    committed = lane_mod.validate_delivery_artifact(
        artifact, lane_id=lane_id, head_sha=str(pending.get("head_sha")), store=store,
        require_isolation=_release_class_lane(lane_snapshot),
        require_live_marker=True,
    )
    delivered_head = str(committed["delivered_head"])

    cleanup_lock = store.dir / "state" / f"lane-{lane_id}.cleanup.lock"
    with store._exclusive_lock(cleanup_lock, what=f"lane {lane_id} cleanup lock"):
        with store._config_lock():
            current = (lane_mod.load_lanes(store).get("lanes") or {}).get(lane_id)
            if (not isinstance(current, dict)
                    or not _lane_transaction_matches(current, pending)):
                raise lane_mod.LaneError("lane instance changed before teardown")
            cleanup_pending = bool(current.get("cleanup_pending"))
            cleanup_lane = dict(current)
        if not cleanup_pending:
            return artifact, False, delivered_head

        wt_raw = cleanup_lane.get("worktree_path")
        if not isinstance(wt_raw, str) or not wt_raw:
            _lane_checkpoint_cleanup(store, lane_id, pending, success=True)
            return artifact, False, delivered_head
        wt = Path(wt_raw)
        if not wt.exists():
            _lane_checkpoint_cleanup(store, lane_id, pending, success=True)
            return artifact, False, delivered_head
        if not _lane_worktree_idle(store, cleanup_lane):
            return artifact, True, delivered_head
        try:
            provenance = _verify_lane_worktree(store, cleanup_lane)
            if provenance.get("head") != pending.get("head_sha"):
                raise lane_mod.LaneError("worktree HEAD changed before teardown")
            rc, _out, err = _git_write(
                store.root, ["worktree", "remove", "--", wt_raw],
            )
            if rc != 0:
                _lane_checkpoint_cleanup(
                    store, lane_id, pending, success=False,
                    error=err.strip() or f"git worktree remove rc={rc}",
                )
                return artifact, True, delivered_head
        except (lane_mod.LaneError, GitWriteError) as exc:
            _lane_checkpoint_cleanup(store, lane_id, pending, success=False, error=str(exc))
            return artifact, True, delivered_head
        _lane_checkpoint_cleanup(store, lane_id, pending, success=True)
        return artifact, False, delivered_head


def cmd_lane(args: argparse.Namespace) -> int:
    """Lane deliver-gate (advisory, point-in-time coordination; see lanes.py)."""
    store = _get_store(args)
    action = getattr(args, "lane_cmd", None)
    roster = store.load_config().get("agents") or []

    if action == "assign":
        lane_id = lane_mod.validate_lane_id(args.id)
        actor = _resolve_self(getattr(args, "actor", None), roster=roster)
        _ensure_in_roster(args.assignee, roster, label="assignee")
        advisory = bool(getattr(args, "advisory", False))
        provision_worktree = not bool(getattr(args, "no_worktree", False))
        waiver_reason = getattr(args, "worktree_waiver_reason", None)
        if not provision_worktree and not (isinstance(waiver_reason, str) and waiver_reason.strip()):
            sys.stderr.write(
                "agenttalk lane assign: --no-worktree requires --worktree-waiver-reason.\n")
            return 2
        if not provision_worktree and not advisory:
            sys.stderr.write(
                "agenttalk lane assign: release-class lanes require a provisioned "
                "worktree; legacy --no-worktree release waivers are no longer accepted. "
                "Use --advisory only for non-release coordination.\n"
            )
            return 2
        reg = _load_domain_registry(store)
        if args.domain not in (reg.data.get("domains") or {}):
            sys.stderr.write(
                f"agenttalk lane assign: unknown domain {args.domain!r} "
                f"(known: {sorted((reg.data.get('domains') or {}))}).\n")
            return 2
        try:
            base = _lane_resolve(store, args.base)
            target_head = _lane_resolve(store, args.target)
            prefixes = lane_mod.normalize_prefixes(args.path, casefold=False)
            planned_epoch = store.current_epoch()
            assignment_fp = _lane_assignment_fingerprint(store)
            assigned_at = _iso_now()
            branch = lane_mod.lane_branch(lane_id)
            wt_root = _prepare_worktrees_root(store, getattr(args, "worktrees_root", None)) \
                if provision_worktree else None
            wt_path = _mint_worktree_path(wt_root, lane_id, base) if wt_root else None
        except lane_mod.LaneError as e:
            sys.stderr.write(f"agenttalk lane assign: {e}\n")
            return 2
        casefold = dom.default_casefold_paths()
        created_path: Path | None = None
        cleanup_branch = False
        provision_error: str | None = None
        try:
            with store._config_lock():
                if _lane_assignment_fingerprint(store) != assignment_fp:
                    raise lane_mod.LaneError(
                        "assignment inputs changed while preparing lane assignment; retry assign")
                data = lane_mod.load_lanes(store)
                previous = (data.get("lanes") or {}).get(lane_id)
                if previous is not None and not args.force:
                    sys.stderr.write(
                        f"agenttalk lane assign: lane {lane_id!r} already exists "
                        "(use --force).\n")
                    return 2
                if (args.force and isinstance(previous, dict)
                        and lane_mod.delivery_recovery_required(previous)):
                    sys.stderr.write(
                        "agenttalk lane assign: delivered lane publication marker is not "
                        "explicitly complete; retry lane deliver or repair the corrupt marker "
                        "before force reassignment.\n"
                    )
                    return 2
                for other in lane_mod.reservation_lanes(data):
                    if other.get("lane_id") == lane_id:
                        continue
                    # Disjointness is checked WITHIN a domain (an empty subset = the whole
                    # domain, so it conflicts with any same-domain lane). Different-domain
                    # lanes are allowed at assign; the per-path domain classification +
                    # active_lane_overlap recompute at deliver enforce any real overlap
                    # (which also covers the case of overlapping domain globs). Fail closed
                    # on same-domain overlap.
                    if other.get("domain_id") != args.domain:
                        continue
                    if not lane_mod.prefixes_disjoint(prefixes, other.get("path_subset") or [],
                                                      casefold=casefold):
                        sys.stderr.write(
                            f"agenttalk lane assign: path subset {prefixes or '[whole domain]'} "
                            f"overlaps active lane {other.get('lane_id')!r} "
                            f"{other.get('path_subset') or '[whole domain]'} in domain "
                            f"{args.domain!r} - refusing (fail closed on overlap).\n")
                        return 2
                worktree = None
                waiver = None
                if provision_worktree:
                    if wt_root is None or wt_path is None:
                        raise lane_mod.LaneError("worktree provisioning was not prepared")
                    if _branch_exists(store.root, branch):
                        raise lane_mod.LaneError(f"lane branch {branch!r} already exists")
                    if wt_path.exists():
                        raise lane_mod.LaneError(f"planned worktree path already exists: {wt_path}")
                    if not lane_mod._FULL_SHA_RE.match(base):
                        raise lane_mod.LaneError("resolved worktree base is not a full 40-char SHA")
                    cleanup_branch = True
                    rc, _out, err = _git_write(
                        store.root, ["worktree", "add", "-b", branch, "--", str(wt_path), base])
                    created_path = wt_path
                    if rc != 0:
                        raise lane_mod.LaneError(f"git worktree add failed rc={rc}: {err.strip()}")
                    worktree = {
                        "path": str(wt_path),
                        "branch": branch,
                        "base_sha": base,
                        "created_at": assigned_at,
                        "root": str(wt_root),
                        "state": lane_mod.STATUS_ACTIVE,
                    }
                else:
                    waiver = {
                        "reason": waiver_reason, "by": actor, "at": assigned_at,
                        "authority": "advisory_only",
                    }
                previous_generation = (
                    previous.get("generation") if isinstance(previous, dict) else None
                )
                generation = (
                    previous_generation + 1
                    if isinstance(previous_generation, int)
                    and not isinstance(previous_generation, bool)
                    else 2 if isinstance(previous, dict) else 1
                )
                lane = lane_mod.new_lane(
                    lane_id, assignee=args.assignee, assigned_by=actor, assigned_at=assigned_at,
                    domain_id=args.domain, path_subset=prefixes, base_sha=base,
                    target_ref=args.target, target_head_at_assign=target_head,
                    epoch_at_assign=planned_epoch,
                    registry_hash_at_assign=reg.registry_hash, notes=args.notes,
                    worktree=worktree, waiver=waiver,
                    release_class=not advisory, generation=generation)
                if provision_worktree:
                    prov = _verify_lane_worktree(store, lane, expected_base=base)
                    lane["worktree_toplevel_canonical"] = prov["worktree_toplevel_canonical"]
                    lane["worktree_common_git_dir_canonical"] = prov["common_git_dir_canonical"]
                data.setdefault("lanes", {})[lane_id] = lane
                lane_mod.save_lanes(store, data)
        except (lane_mod.LaneError, GitWriteError) as e:
            provision_error = str(e)
        if provision_error:
            cleanup_note = _cleanup_failed_provision(
                store, lane_id=lane_id, base_sha=base,
                created_path=created_path, cleanup_branch=cleanup_branch)
            if cleanup_note:
                sys.stderr.write(f"agenttalk lane assign: cleanup note: {cleanup_note}\n")
            sys.stderr.write(f"agenttalk lane assign: {provision_error}\n")
            return 2
        print(f"assigned lane {lane_id} to {args.assignee} @ domain {args.domain}; "
              f"base {base[:12]} -> {args.target} ({target_head[:12]}); "
              f"subset {prefixes or '[whole domain]'}"
              + (f"; worktree {wt_path} [{branch}]" if provision_worktree
                 else "; ADVISORY UNISOLATED (never release evidence)"))
        return 0

    if action == "approve-shared":
        actor = _resolve_self(getattr(args, "actor", None), roster=roster)
        leads = _close_lead_set(store)
        reg = _load_domain_registry(store)
        cfg = store.load_config()
        # ALL-MATCHING authority (lead decision D-11): every shared entry that matches
        # --path must eventually be approved by an authorized approver. This command
        # records the actor's approval against EACH matching entry the actor is
        # authorized for (a close lead is authorized for ALL -> clears the path in one
        # shot; a specific default_approver contributes only their entry). FAIL CLOSED:
        # no matching entry, or the actor authorized for NONE of them -> refuse.
        matched = [e for e in reg.data.get("shared_paths", [])
                   if dom.glob_matches(e["glob"], args.path, casefold=dom.default_casefold_paths())]
        if not matched:
            sys.stderr.write(
                f"agenttalk lane approve-shared: {args.path!r} matches no shared_path "
                "in domains.json - nothing to approve (fail closed).\n")
            return 2
        authorized: list[dict] = []
        unauthorized_globs: list[str] = []
        for e in matched:
            appr_set = set(leads) | set(
                dom.resolve_refset(e.get("default_approvers") or {}, cfg))
            if actor in appr_set:
                authorized.append(e)
            else:
                unauthorized_globs.append(e["glob"])
        if not authorized:
            globs = sorted(e["glob"] for e in matched)
            sys.stderr.write(
                f"agenttalk lane approve-shared: {actor!r} is not an authorized approver "
                f"for any shared entry matching {args.path!r} {globs} (close lead or the "
                "entry's default_approvers); refusing (fail closed).\n")
            return 2
        with store._config_lock():
            data = lane_mod.load_lanes(store)
            lane = (data.get("lanes") or {}).get(args.id)
            if not isinstance(lane, dict):
                sys.stderr.write(f"agenttalk lane approve-shared: no lane {args.id!r}.\n")
                return 2
            for e in authorized:
                # Persist each MATCHED ENTRY GLOB as the authority token (not the raw
                # --path): the verdict requires a valid approval per matching entry.
                lane_mod.add_shared_approval(
                    lane, path_or_glob=e["glob"], approved_by=actor, reason=args.reason,
                    at=_iso_now(), epoch=store.current_epoch(),
                    registry_hash=reg.registry_hash)
            lane_mod.save_lanes(store, data)
        recorded = sorted(e["glob"] for e in authorized)
        msg = (f"recorded shared-path approval(s) for {recorded} (via {args.path}) "
               f"on lane {args.id} by {actor}")
        if unauthorized_globs:
            # Report the ACTOR's authority limit, NOT lane-outstanding state: another
            # approver may already have cleared these entries, so claiming they are
            # "still needed" could contradict the gate (reviewer-1 P3). Run `lane check`
            # for the authoritative outstanding picture.
            msg += (f"; NOTE: you ({actor}) are not an authorized approver for "
                    f"{sorted(unauthorized_globs)} - those entries need approval by their "
                    "own authorized approvers (run `lane check` for the gate verdict)")
        print(msg)
        return 0

    if action == "check":
        data = lane_mod.load_lanes(store)
        lane = (data.get("lanes") or {}).get(args.id)
        if not isinstance(lane, dict):
            sys.stderr.write(f"agenttalk lane check: no lane {args.id!r}.\n")
            return 2
        try:
            head, _provenance = _lane_candidate(
                store, lane, getattr(args, "head", None), delivery=False)
        except lane_mod.LaneError as e:
            sys.stderr.write(f"agenttalk lane check: {e}\n")
            return 2
        others = [
            lane for lane in lane_mod.reservation_lanes(data)
            if lane.get("lane_id") != args.id
        ]
        verdict, ctx = _lane_eval(store, lane, others, head, getattr(args, "gate_scope", None))
        if getattr(args, "json", False):
            print(json.dumps({**verdict, "target_moved": ctx["target_moved"],
                              "merge": ctx["merge"]}, indent=2))
        else:
            _print_lane_verdict(args.id, verdict, ctx)
        return 0 if verdict["verdict"] == lane_mod.VERDICT_GO else 3

    if action == "deliver":
        actor = _resolve_self(getattr(args, "actor", None), roster=roster)
        try:
            with store._config_lock():
                data = lane_mod.load_lanes(store)
                current = (data.get("lanes") or {}).get(args.id)
                if not isinstance(current, dict):
                    sys.stderr.write(f"agenttalk lane deliver: no lane {args.id!r}.\n")
                    return 2
                if current.get("status") == lane_mod.STATUS_DELIVERED:
                    resume = True
                    lane_snapshot = dict(current)
                    expected_lane_fingerprint = None
                    expected_active = None
                elif current.get("status") != lane_mod.STATUS_ACTIVE:
                    sys.stderr.write(
                        f"agenttalk lane deliver: lane {args.id!r} is "
                        f"{current.get('status')!r}, not active.\n"
                    )
                    return 2
                else:
                    resume = False
                    migrated = False
                    if not re.fullmatch(r"[0-9a-f]{32}", str(current.get("instance_id") or "")):
                        current["instance_id"] = uuid.uuid4().hex
                        migrated = True
                    generation = current.get("generation")
                    if (isinstance(generation, bool) or not isinstance(generation, int)
                            or generation < 1):
                        current["generation"] = 1
                        migrated = True
                    if migrated:
                        lane_mod.save_lanes(store, data)
                    lane_snapshot = dict(current)
                    expected_lane_fingerprint = lane_mod.fingerprint(current)
                    expected_active = lane_mod.active_lane_fingerprints(
                        data, exclude=args.id,
                    )
        except (OSError, lane_mod.LaneError) as exc:
            sys.stderr.write(f"agenttalk lane deliver: state read failed ({exc}).\n")
            return 2

        if resume:
            requested_head = getattr(args, "head", None)
            if requested_head:
                try:
                    resolved_head = _lane_resolve(store, requested_head)
                except lane_mod.LaneError as exc:
                    sys.stderr.write(f"agenttalk lane deliver: {exc}\n")
                    return 2
                if resolved_head != lane_snapshot.get("delivered_head"):
                    winner_head = str(lane_snapshot.get("delivered_head") or "unknown")
                    sys.stderr.write(
                        "agenttalk lane deliver: --head differs from the persisted "
                        f"delivered transaction at winner head {winner_head[:12]}; "
                        "refusing retry.\n"
                    )
                    return 3
            try:
                with store._exclusive_lock(
                        _lane_transaction_lock_path(store, args.id),
                        what=f"lane {args.id} delivery transaction lock"):
                    if isinstance(lane_snapshot.get("publish_pending"), dict):
                        try:
                            terminal_error, terminal_conflicts = _lane_prepare_publication(
                                store, args.id,
                            )
                        except (OSError, lane_mod.LaneError) as exc:
                            sys.stderr.write(
                                "agenttalk lane deliver: terminal input recovery failed "
                                f"({exc}).\n"
                            )
                            return 2
                        if terminal_error:
                            sys.stderr.write(_lane_terminal_boundary_error(
                                terminal_error, terminal_conflicts,
                            ))
                            return 3
                    artifact, cleanup_pending, delivered_head = _lane_finalize_delivery(
                        store, args.id,
                    )
            except _LaneTerminalBoundaryChanged as exc:
                sys.stderr.write(_lane_terminal_boundary_error(
                    exc.error, exc.conflicts,
                ))
                return 3
            except (OSError, TimeoutError, lane_mod.LaneError, GitWriteError) as exc:
                sys.stderr.write(
                    f"agenttalk lane deliver: pending delivery recovery failed ({exc}).\n"
                )
                return 2
            print(
                f"delivered lane {args.id} @ {delivered_head[:12]} "
                f"(GO); evidence: {artifact}"
                + ("; cleanup pending" if cleanup_pending else "")
            )
            return 0

        current = lane_snapshot
        if _release_class_lane(current) and not current.get("worktree_path"):
            sys.stderr.write(
                "agenttalk lane deliver: HOLD - release-class lane has no provisioned "
                "worktree; legacy no-worktree waivers cannot authorize delivery.\n"
            )
            return 3

        cooperating_before = lane_mod.cooperating_input_fingerprint(store)
        try:
            head, provenance = _lane_candidate(
                store, current, getattr(args, "head", None), delivery=True,
            )
        except lane_mod.LaneError as exc:
            sys.stderr.write(f"agenttalk lane deliver: {exc}\n")
            msg = str(exc)
            if (current.get("worktree_path") or "tracked changes" in msg
                    or "--head does not match" in msg):
                return 3
            return 2
        others = []
        with store._config_lock():
            data = lane_mod.load_lanes(store)
            observed = (data.get("lanes") or {}).get(args.id)
            if (not isinstance(observed, dict)
                    or lane_mod.fingerprint(observed) != expected_lane_fingerprint):
                sys.stderr.write(
                    "agenttalk lane deliver: lane changed before evaluation; retry.\n"
                )
                return 3
            if lane_mod.active_lane_fingerprints(data, exclude=args.id) != expected_active:
                sys.stderr.write(
                    "agenttalk lane deliver: active-lane set changed before evaluation; "
                    "retry.\n"
                )
                return 3
            others = [
                lane for lane in lane_mod.reservation_lanes(data)
                if lane.get("lane_id") != args.id
            ]
        verdict, ctx = _lane_eval(
            store, current, others, head, getattr(args, "gate_scope", None),
        )
        if verdict["verdict"] != lane_mod.VERDICT_GO:
            sys.stderr.write("agenttalk lane deliver: HOLD - lane stays active.\n")
            _print_lane_verdict(args.id, verdict, ctx)
            return 3
        try:
            final_head, final_provenance = _lane_candidate(
                store, current, getattr(args, "head", None), delivery=True,
            )
            final_target = _lane_resolve(store, current.get("target_ref"))
        except lane_mod.LaneError as exc:
            sys.stderr.write(
                f"agenttalk lane deliver: final Git recheck failed ({exc}); retry.\n"
            )
            return 3
        if (final_head != head or final_target != ctx.get("target_head_now")
                or lane_mod.fingerprint(final_provenance or {})
                != lane_mod.fingerprint(provenance or {})):
            sys.stderr.write(
                "agenttalk lane deliver: target/head/worktree changed during evaluation; "
                "NO evidence committed, retry.\n"
            )
            return 3
        provenance = final_provenance
        cooperating_after = lane_mod.cooperating_input_fingerprint(store)
        if cooperating_before != cooperating_after:
            sys.stderr.write(
                "agenttalk lane deliver: cooperating config/domain/gate/epoch inputs "
                "changed during evaluation; NO evidence committed, retry.\n"
            )
            return 3

        ctx["cooperating_input_fingerprint"] = cooperating_after
        evaluation_snapshot = lane_mod.build_evaluation_snapshot(
            lane=current, active_lanes=others, context=ctx,
            worktree_provenance=provenance,
        )
        try:
            pending = lane_mod.write_prepared_delivery_artifact(
                store, lane=current, head_sha=head, verdict=verdict,
                changed=ctx["changed"], merge=ctx["merge"],
                gate_check=ctx["gate_check"], delivered_by=actor, at=_iso_now(),
                evaluation_snapshot=evaluation_snapshot,
                worktree_provenance=provenance,
            )
        except (OSError, lane_mod.LaneError) as exc:
            sys.stderr.write(
                f"agenttalk lane deliver: prepared artifact write failed ({exc}); "
                "lane stays active.\n"
            )
            return 2
        pending["lane_id"] = args.id
        pending["input_fingerprint"] = cooperating_after
        pending["evaluation_fingerprint"] = lane_mod.fingerprint(evaluation_snapshot)
        pending["lane_snapshot"] = dict(current)
        pending["active_lane_fingerprints"] = expected_active
        pending["terminal_rebound"] = False
        cas_input = lane_mod.cooperating_input_fingerprint(store)

        cas_error = None
        competing_delivery = False
        try:
            with store._exclusive_lock(
                    _lane_reset_lock_path(store), what="lane delivery/reset lock"):
                with store._config_lock():
                    data = lane_mod.load_lanes(store)
                    latest = (data.get("lanes") or {}).get(args.id)
                    if (isinstance(latest, dict)
                            and latest.get("status") == lane_mod.STATUS_DELIVERED):
                        if (latest.get("instance_id") == current.get("instance_id")
                                and latest.get("generation") == current.get("generation")):
                            winner_head = latest.get("delivered_head")
                            if winner_head == head:
                                competing_delivery = True
                            else:
                                cas_error = (
                                    "same lane instance was delivered concurrently at winner "
                                    f"head {str(winner_head)[:12]}; requested head {head[:12]} "
                                    "cannot converge"
                                )
                        else:
                            cas_error = (
                                "a replacement lane instance was delivered during delivery"
                            )
                    elif (not isinstance(latest, dict)
                          or latest.get("status") != lane_mod.STATUS_ACTIVE):
                        cas_error = "lane is no longer the active instance that was evaluated"
                    elif (latest.get("instance_id") != current.get("instance_id")
                          or latest.get("generation") != current.get("generation")
                          or lane_mod.fingerprint(latest) != expected_lane_fingerprint):
                        cas_error = "lane instance/generation changed during delivery"
                    elif lane_mod.active_lane_fingerprints(data, exclude=args.id) != expected_active:
                        cas_error = "active-lane set changed during delivery"
                    elif cas_input != cooperating_after:
                        cas_error = "cooperating config/domain/gate/epoch inputs changed"
                    else:
                        latest["status"] = lane_mod.STATUS_DELIVERED
                        latest["delivered_head"] = head
                        latest["delivered_by"] = actor
                        latest["delivered_at"] = pending["started_at"]
                        latest["delivery_transaction_id"] = pending["transaction_id"]
                        latest["publish_pending"] = dict(pending)
                        latest["prepared_artifact"] = pending["prepared_artifact"]
                        latest["cleanup_pending"] = bool(latest.get("worktree_path"))
                        if latest.get("worktree_path"):
                            latest["worktree_state"] = lane_mod.STATUS_CLEANUP_PENDING
                        lane_mod.save_lanes(store, data)
        except (OSError, TimeoutError, lane_mod.LaneError) as exc:
            persisted = False
            try:
                with store._config_lock():
                    saved = (lane_mod.load_lanes(store).get("lanes") or {}).get(args.id)
                    persisted = isinstance(saved, dict) and _lane_transaction_matches(
                        saved, pending,
                    )
            except (OSError, lane_mod.LaneError, TimeoutError):
                pass
            if not persisted:
                _lane_remove_prepared(pending)
            sys.stderr.write(
                f"agenttalk lane deliver: first delivery state save failed ({exc}); "
                "no consumable artifact was published.\n"
            )
            return 2

        if competing_delivery:
            _lane_remove_prepared(pending)
        elif cas_error:
            _lane_remove_prepared(pending)
            sys.stderr.write(
                f"agenttalk lane deliver: CAS refused ({cas_error}); lane was not "
                "committed.\n"
            )
            return 3

        try:
            with store._exclusive_lock(
                    _lane_transaction_lock_path(store, args.id),
                    what=f"lane {args.id} delivery transaction lock"):
                try:
                    terminal_error, terminal_conflicts = _lane_prepare_publication(
                        store, args.id,
                    )
                except (OSError, lane_mod.LaneError) as exc:
                    sys.stderr.write(
                        f"agenttalk lane deliver: terminal input binding failed ({exc}); "
                        "no consumable evidence was published.\n"
                    )
                    return 2
                if terminal_error:
                    sys.stderr.write(_lane_terminal_boundary_error(
                        terminal_error, terminal_conflicts,
                    ))
                    return 3
                artifact, cleanup_pending, delivered_head = _lane_finalize_delivery(
                    store, args.id,
                )
        except _LaneTerminalBoundaryChanged as exc:
            sys.stderr.write(_lane_terminal_boundary_error(
                exc.error, exc.conflicts,
            ))
            return 3
        except (OSError, TimeoutError, lane_mod.LaneError, GitWriteError) as exc:
            sys.stderr.write(
                f"agenttalk lane deliver: delivery is checkpointed but pending recovery "
                f"({exc}); retry the same command.\n"
            )
            return 2
        print(
            f"delivered lane {args.id} @ {delivered_head[:12]} (GO); evidence: {artifact}"
            + ("; cleanup pending" if cleanup_pending else "")
        )
        return 0

    if action == "recover":
        lane_id = lane_mod.validate_lane_id(args.id)
        try:
            with store._exclusive_lock(
                    _lane_transaction_lock_path(store, lane_id),
                    what=f"lane {lane_id} delivery transaction lock"):
                outcome, conflicts = _lane_recover_delivery(
                    store, lane_id, reason=args.reason,
                )
        except (OSError, TimeoutError, lane_mod.LaneError) as exc:
            sys.stderr.write(f"agenttalk lane recover: {exc}\n")
            return 2
        if outcome == "committed":
            sys.stderr.write(
                "agenttalk lane recover: an authoritative committed final exists; "
                "run `lane deliver` to complete its publication marker.\n"
            )
            return 3
        if outcome == "held":
            sys.stderr.write(
                "agenttalk lane recover: restoring ACTIVE would overlap current path "
                f"reservation(s) {conflicts}; resolve them and retry recovery.\n"
            )
            return 3
        print(
            f"recovered lane {lane_id} to ACTIVE for fresh evaluation; "
            "orphaned prepared evidence remains nonconsumable"
        )
        return 0

    if action == "workspace":
        data = lane_mod.load_lanes(store)
        lane = (data.get("lanes") or {}).get(args.id)
        if not isinstance(lane, dict):
            sys.stderr.write(f"agenttalk lane workspace: no lane {args.id!r}.\n")
            return 2
        path = lane.get("worktree_path")
        payload = {
            "lane_id": args.id,
            "worktree_path": path,
            "worktree_branch": lane_mod.lane_branch(args.id),
            "worktree_state": lane.get("worktree_state"),
            "worktree_waived": bool(lane.get("worktree_waived")),
        }
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2))
        elif path:
            print(path)
        else:
            print(f"lane {args.id} has no worktree (waived={payload['worktree_waived']})")
            return 3
        return 0

    if action == "abandon":
        lane_id = lane_mod.validate_lane_id(args.id)
        branch_delete_block_reason = None
        with store._config_lock():
            data = lane_mod.load_lanes(store)
            lane = (data.get("lanes") or {}).get(lane_id)
            if isinstance(lane, dict):
                if lane_mod.delivery_recovery_required(lane):
                    sys.stderr.write(
                        "agenttalk lane abandon: delivered lane publication marker is not "
                        "explicitly complete; retry lane deliver or repair the corrupt marker "
                        "before abandon.\n"
                    )
                    return 2
                lane["status"] = lane_mod.STATUS_ABANDONED
                if lane.get("worktree_path"):
                    branch_delete_block_reason = "worktree cleanup did not complete"
                    if _lane_worktree_idle(store, lane):
                        try:
                            _verify_lane_worktree(store, lane)
                            rc, _out, err = _git_write(
                                store.root, ["worktree", "remove", "--", str(lane["worktree_path"])])
                            lane["worktree_state"] = (
                                lane_mod.STATUS_ABANDONED if rc == 0
                                else lane_mod.STATUS_CLEANUP_FAILED)
                            if rc != 0:
                                lane["worktree_cleanup_error"] = err.strip()[:500]
                                branch_delete_block_reason = (
                                    lane["worktree_cleanup_error"] or "worktree removal failed")
                            else:
                                branch_delete_block_reason = None
                        except lane_mod.LaneError as e:
                            msg = str(e)[:500] or "worktree cleanup pending"
                            lane["worktree_state"] = lane_mod.STATUS_CLEANUP_PENDING
                            lane["worktree_cleanup_error"] = msg
                            branch_delete_block_reason = msg
                        except GitWriteError as e:
                            msg = str(e)[:500] or "worktree cleanup failed"
                            lane["worktree_state"] = lane_mod.STATUS_CLEANUP_FAILED
                            lane["worktree_cleanup_error"] = msg
                            branch_delete_block_reason = msg
                    else:
                        lane["worktree_state"] = lane_mod.STATUS_CLEANUP_PENDING
                        lane["worktree_cleanup_error"] = "worktree has an active or pending launch"
                        branch_delete_block_reason = lane["worktree_cleanup_error"]
                lane_mod.save_lanes(store, data)
        deleted = False
        if getattr(args, "delete_branch", False):
            ok, reason = (
                (False, branch_delete_block_reason)
                if branch_delete_block_reason else
                _lane_branch_delete_safe(store.root, lane_id, args.target)
            )
            if not ok:
                sys.stderr.write(
                    f"agenttalk lane abandon: branch {lane_mod.lane_branch(lane_id)!r} "
                    f"not deleted - {reason}.\n")
            else:
                rc, _out, err = _git_write(store.root, ["update-ref", "-d", lane_mod.lane_ref(lane_id)])
                if rc == 0:
                    deleted = True
                else:
                    sys.stderr.write(f"agenttalk lane abandon: branch delete failed: {err.strip()}\n")
        print(f"abandoned lane {lane_id}" + ("; branch deleted" if deleted else ""))
        return 0

    if action == "gc":
        try:
            data = lane_mod.load_lanes(store)
        except lane_mod.LaneError:
            data = {"lanes": {}}
        lane_records = {k: v for k, v in (data.get("lanes") or {}).items() if isinstance(v, dict)}
        wt_by_branch = {
            rec.get("branch", "").removeprefix("refs/heads/"): rec
            for rec in _worktree_list(store.root)
            if isinstance(rec.get("branch"), str) and rec.get("branch", "").startswith("refs/heads/lane/")
        }
        managed_wt_by_branch = _managed_worktree_paths(store)
        rc, refs_out = _git(store.root, ["for-each-ref", "--format=%(refname:short)", "refs/heads/lane/"])
        branches = [b.strip() for b in refs_out.splitlines() if rc == 0 and b.strip()]
        items = []
        for branch in sorted(set(branches) | set(wt_by_branch) | set(managed_wt_by_branch)):
            lane_id = branch.removeprefix("lane/")
            try:
                lane_mod.validate_lane_id(lane_id)
            except lane_mod.LaneError:
                continue
            lane = lane_records.get(lane_id)
            status = lane.get("status") if isinstance(lane, dict) else "orphaned"
            wt_rec = wt_by_branch.get(branch)
            wt_path = (wt_rec.get("worktree") if wt_rec else None) or managed_wt_by_branch.get(branch)
            safe, reason = _lane_branch_delete_safe(store.root, lane_id, args.target)
            branch_allowed, branch_allowed_reason = _lane_branch_gc_allowed(
                lane if isinstance(lane, dict) else None)
            if not branch_allowed:
                safe = False
                reason = branch_allowed_reason if reason.startswith("branch tip is") else reason
            wt_safe, wt_reason = _lane_worktree_remove_safe(
                store, lane_id, lane if isinstance(lane, dict) else None, wt_path, safe)
            item = {
                "lane_id": lane_id,
                "branch": branch,
                "status": status,
                "worktree": wt_path,
                "worktree_remove_safe": wt_safe,
                "worktree_reason": wt_reason,
                "branch_delete_safe": safe,
                "reason": reason,
            }
            if getattr(args, "delete", False):
                if wt_path and wt_safe:
                    try:
                        _git_write(store.root, ["worktree", "remove", "--", wt_path])
                        item["worktree_removed"] = True
                    except GitWriteError as e:
                        item["worktree_removed"] = False
                        item["remove_error"] = str(e)
                if safe:
                    rc2, _out, err = _git_write(
                        store.root, ["update-ref", "-d", lane_mod.lane_ref(lane_id)])
                    item["branch_deleted"] = rc2 == 0
                    if rc2 != 0:
                        item["delete_error"] = err.strip()
            items.append(item)
        if getattr(args, "json", False):
            print(json.dumps({"dry_run": not bool(getattr(args, "delete", False)),
                              "items": items}, indent=2))
        else:
            print("lane gc " + ("delete" if getattr(args, "delete", False) else "dry-run"))
            for item in items:
                print(f"  {item['lane_id']}: {item['status']} {item.get('worktree') or '[no worktree]'} "
                      f"branch-delete={'yes' if item['branch_delete_safe'] else 'manual'}")
        return 0

    if action == "status":
        data = lane_mod.load_lanes(store)
        active = lane_mod.active_lanes(data)
        recovery = []
        for lane in lane_mod.recovery_lanes(data):
            diagnosis, detail = _lane_recovery_diagnosis(store, lane)
            recovery.append({
                **lane,
                "recovery_state": diagnosis,
                "recovery_detail": detail,
            })
        if getattr(args, "json", False):
            print(json.dumps([*active, *recovery], indent=2))
            return 0
        if not active and not recovery:
            print("active lanes: none")
            return 0
        reg = _load_domain_registry(store)
        cur_epoch = store.current_epoch()
        if active:
            print(f"active lanes ({len(active)}):")
            for lane in active:
                stale = []
                if lane.get("epoch_at_assign") != cur_epoch:
                    stale.append("epoch")
                if lane.get("registry_hash_at_assign") != reg.registry_hash:
                    stale.append("registry")
                tag = f" STALE[{','.join(stale)}]" if stale else ""
                if lane.get("worktree_path"):
                    wt = f" worktree={lane.get('worktree_state') or lane_mod.STATUS_ACTIVE}"
                elif lane.get("worktree_waived"):
                    wt = " worktree=waived"
                else:
                    wt = " worktree=missing"
                print(
                    f"  {lane['lane_id']}: {lane.get('assignee')} @ {lane.get('domain_id')} "
                    f"{lane.get('path_subset') or '[whole domain]'} -> "
                    f"{lane.get('target_ref')}{tag}{wt}"
                )
        else:
            print("active lanes: none")
        if recovery:
            print(f"recovery lanes ({len(recovery)}):")
            for lane in recovery:
                print(
                    f"  {lane.get('lane_id', '?')}: {lane['recovery_state']} - "
                    f"{lane['recovery_detail']} (run `lane deliver` or `lane recover`)"
                )
        return 0

    sys.stderr.write(
        "agenttalk lane: expected assign, check, deliver, recover, workspace, abandon, gc, "
        "status, or approve-shared.\n")
    return 2


# ----------------------------------------------------------------- knowledge (P2)
#
# Knowledge layer: the CLI/git adapter resolves anchor I/O (HEAD, reachability,
# anchor-path diff, target existence) and hands knowledge.compute_staleness
# already-resolved data - the staleness derivation stays pure + anchor-relative.

def _kn_full_head(store) -> str | None:
    rc, out = _git(store.root, ["rev-parse", "HEAD"])
    out = out.strip()
    return out if rc == 0 and lane_mod._FULL_SHA_RE.match(out) else None


def _knowledge_anchor_status(store, note: dict) -> dict:
    from agenttalk import knowledge as kn
    anchor = note.get("anchor") or {}
    kind = anchor.get("kind")
    vsha = note.get("verified_against_sha")
    head = _kn_full_head(store)
    status = {"sha_reachable": None, "head_moved": False, "anchor_changed": None,
              "anchor_exists": True, "evidence_match": None, "target_resolvable": True}
    if vsha:
        if head:
            status["head_moved"] = (vsha != head)
            rc, _ = _git(store.root, ["merge-base", "--is-ancestor", vsha, "HEAD"])
            status["sha_reachable"] = (rc == 0)
        else:
            status["sha_reachable"] = None   # can't determine -> stale
    p = kn.anchor_path(anchor)
    if p is not None:
        rce, _ = _git(store.root, ["cat-file", "-e", f"HEAD:{p}"])
        status["anchor_exists"] = (rce == 0)
        if vsha and head and status.get("sha_reachable"):
            rcd, out = _git(store.root, ["diff", "--name-only", "-M", "-C", f"{vsha}..HEAD", "--", p])
            status["anchor_changed"] = bool(out.strip()) if rcd == 0 else None
        elif not vsha:
            status["anchor_changed"] = False   # no baseline; rely on existence
        else:
            status["anchor_changed"] = None
        if kind == "symbol":
            status["evidence_match"] = None      # v1: no parser -> weak (caution)
    elif kind == "sha":
        rcs, _ = _git(store.root, ["cat-file", "-e", f"{anchor.get('sha', '')}^{{commit}}"])
        status["target_resolvable"] = (rcs == 0)
    elif kind == "request":
        rid = anchor.get("request_id")
        mid = anchor.get("msg_id")
        try:
            msgs = list(store.valid_messages())
            if mid:
                # C4b: msg_id is EXACT - resolve that precise message id (NO fallback to
                # any other same-request message). If request_id is also present it must
                # match the found message's request_id.
                match = next((m for m in msgs if getattr(m, "id", None) == mid), None)
                status["target_resolvable"] = (
                    match is not None
                    and (not rid
                         or (getattr(match, "meta", None) or {}).get("request_id") == rid))
            elif rid:
                status["target_resolvable"] = any(
                    (getattr(m, "meta", None) or {}).get("request_id") == rid for m in msgs)
            else:
                status["target_resolvable"] = False   # neither id -> unresolvable
        except Exception:  # noqa: BLE001 - a scan failure must not crash a read
            # C4b: a scan/read failure is UNRESOLVABLE, never inferred fresh (was True).
            status["target_resolvable"] = False
    return status


def _knowledge_process_is_lead(store, actor: str) -> bool:
    cfg = store.load_config()
    roster = cfg.get("agents") or []
    roles = cfg.get("roles") or {}
    return actor in roster and isinstance(roles.get(actor), str) \
        and roles[actor].casefold() == "lead"


def _knowledge_process_curators(store) -> list[str]:
    cfg = store.load_config()
    roster = cfg.get("agents") or []
    curators: list[str] = []
    liaison = store.operator_facing()
    if liaison:
        curators.append(liaison)
    for agent in roster:
        role = (cfg.get("roles") or {}).get(agent)
        if isinstance(role, str) and role.casefold() == "lead" and agent not in curators:
            curators.append(agent)
    return curators


def _knowledge_resolve_domain_for_publish(store, args, reg):
    from agenttalk import knowledge as kn
    note_type = args.type
    domain_id = args.domain or (kn.PROCESS_DOMAIN if note_type == kn.TYPE_LESSON else None)
    if not domain_id:
        raise kn.KnowledgeError("domain is required")
    domains = reg.data.get("domains") or {}
    effective = kn.effective_domain(domain_id, note_type, domains)
    if not effective["exists"]:
        known = sorted(domains)
        raise kn.KnowledgeError(f"unknown domain {domain_id!r} (known: {known})")
    return domain_id, effective


def _knowledge_resolve_registry_curators(store, domain_id: str, reg):
    from agenttalk import knowledge as kn
    dom_entry = (reg.data.get("domains") or {}).get(domain_id)
    if not dom_entry:
        raise kn.KnowledgeError(f"unknown domain {domain_id!r}")
    cfg = store.load_config()
    return (dom.resolve_refset(dom_entry.get("owners") or {}, cfg),
            dom.resolve_refset(dom_entry.get("curators") or {}, cfg))


def _knowledge_resolve_curators_for_note(store, note: dict, reg):
    from agenttalk import knowledge as kn
    domain_id = note.get("domain_id")
    domains = reg.data.get("domains") or {}
    effective = kn.effective_domain(domain_id, note.get("type"), domains)
    if not effective["exists"]:
        raise kn.KnowledgeError(f"unknown domain {domain_id!r}")
    if effective["virtual"]:
        return [], list(_knowledge_process_curators(store))
    return _knowledge_resolve_registry_curators(store, domain_id, reg)


def _kn_split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


def _kn_anchor_from_args(args):
    from agenttalk import knowledge as kn
    if not getattr(args, "anchor_kind", None):
        return None
    spec = {"kind": args.anchor_kind}
    for f in ("path", "symbol", "request_id", "msg_id", "mission", "wp_id", "sha"):
        v = getattr(args, f, None)
        if v:
            spec[f] = v
    return kn.validate_anchor(spec)


def _kn_lesson_from_args(args, actor: str, anchor: dict | None):
    from agenttalk import knowledge as kn
    lesson = {
        "scope": getattr(args, "scope", None),
        "trigger": getattr(args, "trigger", None),
        "evidence_ref": getattr(args, "evidence_ref", None),
        "applies_to": _kn_split_csv(getattr(args, "applies_to", None)),
        "owner": getattr(args, "owner", None) or actor,
        "review_after": getattr(args, "review_after", None),
        "expires_at": getattr(args, "expires_at", None),
        "supersedes": _kn_split_csv(getattr(args, "supersedes", None)),
    }
    if anchor is not None:
        lesson["anchor"] = anchor
    return kn.validate_lesson(
        lesson, default_owner=actor, default_status=kn.LESSON_STATUS_PROPOSED)


def _kn_publish_preflight(args, actor: str) -> tuple[list[str], dict | None, dict | None]:
    """Validate publish fields without registry, Git, or event-store I/O."""
    from agenttalk import knowledge as kn

    errors: list[str] = []
    try:
        kn.validate_key(args.key)
    except kn.KnowledgeError as exc:
        errors.append(str(exc))
    try:
        kn.validate_body(args.message)
    except kn.KnowledgeError as exc:
        errors.append(str(exc))

    is_lesson = args.type == kn.TYPE_LESSON
    if not is_lesson and not args.domain:
        errors.append("domain is required for non-lesson notes")

    anchor_fields = (
        "path", "symbol", "request_id", "msg_id", "mission", "wp_id", "sha")
    has_anchor_values = any(getattr(args, field, None) for field in anchor_fields)
    anchor: dict | None = None
    anchor_kind = getattr(args, "anchor_kind", None)
    if not anchor_kind:
        if not is_lesson:
            errors.append("anchor-kind is required for non-lesson notes")
        elif has_anchor_values:
            errors.append("anchor-kind is required when lesson anchor fields are supplied")
    else:
        required_by_kind = {
            "path": ("path",),
            "symbol": ("path", "symbol"),
            "request": ("request_id",),
            "wp": ("mission", "wp_id"),
            "sha": ("sha",),
        }
        missing = [
            field for field in required_by_kind.get(anchor_kind, ())
            if not getattr(args, field, None)
        ]
        errors.extend(
            f"anchor.{field} is required for anchor kind {anchor_kind}"
            for field in missing
        )
        if not missing:
            try:
                anchor = _kn_anchor_from_args(args)
            except kn.KnowledgeError as exc:
                errors.append(str(exc))

    lesson: dict | None = None
    lesson_only = (
        ("scope", "--scope"),
        ("trigger", "--trigger"),
        ("evidence_ref", "--evidence-ref"),
        ("applies_to", "--applies-to"),
        ("owner", "--owner"),
        ("review_after", "--review-after"),
        ("expires_at", "--expires-at"),
        ("supersedes", "--supersedes"),
    )
    if not is_lesson:
        errors.extend(
            f"{flag} is only valid with --type lesson"
            for field, flag in lesson_only
            if getattr(args, field, None) is not None
        )
        return errors, anchor, None

    for field, flag in lesson_only:
        if field in {"applies_to", "owner", "supersedes"}:
            continue
        if not getattr(args, field, None):
            errors.append(f"{flag} is required for --type lesson")

    for field, flag in (("trigger", "--trigger"), ("evidence_ref", "--evidence-ref")):
        value = getattr(args, field, None)
        if isinstance(value, str) and len(value.encode("utf-8")) > kn.LESSON_TEXT_MAX_BYTES:
            errors.append(
                f"{flag} is above the {kn.LESSON_TEXT_MAX_BYTES}-byte cap")

    try:
        kn.validate_lesson_owner(getattr(args, "owner", None) or actor)
    except kn.KnowledgeError as exc:
        errors.append(str(exc))

    applies_to = _kn_split_csv(getattr(args, "applies_to", None))
    if len(applies_to) > kn.LESSON_TAG_LIMIT:
        errors.append(
            f"--applies-to may contain at most {kn.LESSON_TAG_LIMIT} tags")
    for tag in applies_to:
        try:
            kn.validate_lesson_tag(tag)
        except kn.KnowledgeError as exc:
            errors.append(str(exc))

    supersedes = _kn_split_csv(getattr(args, "supersedes", None))
    if len(supersedes) > kn.LESSON_SUPERSEDES_LIMIT:
        errors.append(
            f"--supersedes may contain at most {kn.LESSON_SUPERSEDES_LIMIT} keys")
    for key in supersedes:
        try:
            kn.validate_key(key)
        except kn.KnowledgeError as exc:
            errors.append(f"supersedes: {exc}")

    parsed_dates: dict[str, datetime] = {}
    for field, flag in (("review_after", "--review-after"),
                        ("expires_at", "--expires-at")):
        value = getattr(args, field, None)
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed_dates[field] = parsed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            errors.append(f"{flag} must be an ISO date or datetime")
    if ("review_after" in parsed_dates and "expires_at" in parsed_dates
            and parsed_dates["expires_at"] <= parsed_dates["review_after"]):
        errors.append("--expires-at must be after --review-after")

    if not errors:
        try:
            lesson = _kn_lesson_from_args(args, actor, anchor)
        except kn.KnowledgeError as exc:
            errors.append(str(exc))
    return errors, anchor, lesson


def _kn_lesson_marker(verdict: dict) -> str:
    from agenttalk import lesson_context as lc
    return lc.lesson_marker(verdict)


def _kn_trim(value: object, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _kn_lesson_dict(note: dict, verdict: dict) -> dict:
    from agenttalk import lesson_context as lc
    return lc.lesson_dict(note, verdict)


def _kn_print_lesson(note: dict, verdict: dict) -> None:
    lesson = note.get("lesson") or {}
    tag = note.get("authority", {}).get("state", "?")
    status = lesson.get("status") or "?"
    flags = "  " + _kn_lesson_marker(verdict)
    print(f"  [lesson] {note.get('domain_id')}/{note.get('key')} "
          f"({tag}, {lesson.get('scope')}, {status}){flags}")
    print(f"      trigger: {_kn_trim(lesson.get('trigger'), 180)}")
    print(f"      lesson: {_kn_trim(note.get('body'), 200)}")
    print(f"      evidence: {_kn_trim(lesson.get('evidence_ref'), 160)}")


def _kn_format_lesson_line(note: dict, verdict: dict) -> str:
    from agenttalk import lesson_context as lc
    return lc.format_lesson_line(note, verdict)


def _kn_print_note(note: dict, verdict: dict) -> None:
    from agenttalk import knowledge as kn
    if note.get("type") == kn.TYPE_LESSON:
        _kn_print_lesson(note, verdict)
        return
    a = note.get("anchor") or {}
    tag = note.get("authority", {}).get("state", "?")
    flags = ""
    if verdict.get("hard_stale"):
        flags = "  STALE[" + ",".join(verdict["stale_reasons"]) + "]"
    elif verdict.get("caution_flags"):
        flags = "  caution[" + ",".join(verdict["caution_flags"]) + "]"
    body0 = (note.get("body", "") or "").splitlines()
    print(f"  [{note.get('type')}] {note.get('domain_id')}/{note.get('key')} ({tag}){flags}")
    print(f"      {(body0[0] if body0 else '')[:200]}")
    print(f"      -> {a.get('kind')}:{a.get('path') or a.get('request_id') or a.get('sha') or a.get('wp_id') or ''}")


def cmd_knowledge(args: argparse.Namespace) -> int:
    """Knowledge layer MVP (durable pointer notes; see knowledge.py)."""
    from agenttalk import knowledge as kn
    store = _get_store(args)
    action = getattr(args, "knowledge_cmd", None)
    roster = store.load_config().get("agents") or []

    if action == "publish":
        actor = _resolve_self(getattr(args, "actor", None), roster=roster)
        errors, anchor, lesson = _kn_publish_preflight(args, actor)
        if errors:
            for error in errors:
                sys.stderr.write(f"agenttalk knowledge publish: {error}\n")
            return 2

        # Resolve Git only after the pure aggregate preflight succeeds.
        if args.verified_against:
            try:
                vsha = _lane_resolve(store, args.verified_against)
            except lane_mod.LaneError as e:
                sys.stderr.write(f"agenttalk knowledge publish: {e}\n")
                return 2
        else:
            vsha = _kn_full_head(store)
        with store._config_lock():
            reg = _load_domain_registry(store)
            try:
                domain_id, effective = _knowledge_resolve_domain_for_publish(
                    store, args, reg)
                dentry = effective["entry"]
                if not effective["virtual"]:
                    owners = dom.resolve_refset(
                        dentry.get("owners") or {}, store.load_config())
                    curators = dom.resolve_refset(
                        dentry.get("curators") or {}, store.load_config())
                else:
                    owners, curators = [], _knowledge_process_curators(store)
                resolved_from = (
                    "curator" if actor in curators else
                    "owner" if actor in owners else
                    "lead" if (actor in _close_lead_set(store)
                               or _knowledge_process_is_lead(store, actor)) else
                    "active_agent"
                )
                events, _problems = kn.read_events(store)
                live = kn.current_view(events).get((domain_id, args.key))
                if (live is not None and not kn.is_retracted(live)
                        and live.get("type") != args.type):
                    raise kn.KnowledgeError(
                        f"live key {domain_id}/{args.key} already has type "
                        f"{live.get('type')!r}; changing a key's type is refused")
                evt = kn.new_publish_event(
                    note_id=kn.new_note_id(), key=args.key, type=args.type,
                    domain_id=domain_id, body=args.message, anchor=anchor,
                    verified_against_sha=vsha,
                    domain_registry_hash=reg.registry_hash,
                    domain_definition_hash=effective["definition_hash"],
                    author=actor, resolved_from=resolved_from, at=_iso_now(),
                    lesson=lesson)
            except kn.KnowledgeError as e:
                sys.stderr.write(f"agenttalk knowledge publish: {e}\n")
                return 2
            kn.write_event_locked(store, evt)
        print(f"published note {evt['id']} {domain_id}/{args.key} ({args.type}, uncurated)")
        return 0

    if action == "curate":
        sub = getattr(args, "curate_cmd", None)
        if sub not in ("verify", "retract"):
            sys.stderr.write("agenttalk knowledge curate: expected verify or retract.\n")
            return 2
        actor = _resolve_self(getattr(args, "actor", None), roster=roster)
        with store._config_lock():
            reg = _load_domain_registry(store)
            events, _ = kn.read_events(store)
            view = kn.current_view(events)
            base = view.get((args.domain, args.key))
            if not base or kn.is_retracted(base):
                sys.stderr.write(f"agenttalk knowledge curate: no live note {args.domain}/{args.key}.\n")
                return 2
            try:
                owners, curators = _knowledge_resolve_curators_for_note(store, base, reg)
            except kn.KnowledgeError as e:
                sys.stderr.write(f"agenttalk knowledge curate: {e}\n")
                return 2
            # CURATION is ENFORCED (it gates the verified/authoritative set): owner/
            # curator of the domain, or a lead override. For the reserved lesson
            # process domain, the virtual liaison/lead curators apply only when no
            # real registry domain named process exists.
            is_virtual_process_lesson = (
                kn.effective_domain(
                    base.get("domain_id"), base.get("type"),
                    reg.data.get("domains") or {},
                )["virtual"]
            )
            resolved_from = kn.resolve_curation_authority(
                actor, owner_agents=owners, curator_agents=curators,
                is_lead=(actor in _close_lead_set(store) or
                         (is_virtual_process_lesson and _knowledge_process_is_lead(store, actor))))
            if resolved_from is None:
                sys.stderr.write(
                    f"agenttalk knowledge curate: {actor!r} is not an owner/curator of "
                    f"{args.domain!r} (or a lead) - refusing (curation gates the verified set).\n")
                return 2
            try:
                effective = kn.effective_domain(
                    base.get("domain_id"), base.get("type"),
                    reg.data.get("domains") or {},
                )
                evt = kn.new_curate_event(base=base, action=sub, curated_by=actor,
                                          resolved_from=resolved_from, at=_iso_now(),
                                          reason=args.reason,
                                          domain_registry_hash=reg.registry_hash,
                                          domain_definition_hash=effective["definition_hash"])
            except kn.KnowledgeError as e:
                sys.stderr.write(f"agenttalk knowledge curate: {e}\n")
                return 2
            # This A/B check linearizes supported registry writers, which honor the
            # shared config lock. An out-of-band hand edit can bypass that lock; the
            # stamped subject hash then makes the event hard-stale on its first read.
            current_reg = _load_domain_registry(store)
            if current_reg.registry_hash != reg.registry_hash:
                sys.stderr.write(
                    "agenttalk knowledge curate: domain registry changed during "
                    "curation; no event was appended. Retry against the new registry.\n"
                )
                return 2
            # C4c: route through the SAME durable writer publish uses (one append path,
            # fsync). We already hold _config_lock (curate is read-view-build-append under
            # one lock), so call the locked-internal helper - not append_event (re-lock).
            kn.write_event_locked(store, evt)
        print(f"{sub} {args.domain}/{args.key} by {actor} ({resolved_from})")
        return 0

    if action in ("pull", "search", "onboard"):
        type_filter = getattr(args, "type", None)
        scope = getattr(args, "scope", None)
        lesson_tags = _kn_split_csv(getattr(args, "tags", None))
        if (scope or lesson_tags) and type_filter not in (None, kn.TYPE_LESSON):
            sys.stderr.write(
                "agenttalk knowledge: --scope/--tags can only be combined with "
                "--type lesson (or no --type).\n"
            )
            return 2
        for field in ("limit", "lesson_limit"):
            value = getattr(args, field, None)
            if isinstance(value, int) and value < 0:
                sys.stderr.write(
                    f"agenttalk knowledge: --{field.replace('_', '-')} must be zero or greater.\n"
                )
                return 2
        output_schema = getattr(args, "output_schema", None)
        if output_schema and not getattr(args, "json", False):
            sys.stderr.write("agenttalk knowledge: --output-schema requires --json.\n")
            return 2
        if output_schema == "legacy" and type_filter == kn.TYPE_LESSON:
            sys.stderr.write(
                "agenttalk knowledge: legacy output is the pointer-only array and "
                "cannot be combined with --type lesson.\n"
            )
            return 2
        if output_schema == "legacy" and (scope or lesson_tags):
            sys.stderr.write(
                "agenttalk knowledge: legacy pointer-only output cannot be combined "
                "with lesson scope/tag filters.\n"
            )
            return 2

        events, read_problems = kn.read_events(store)
        views, semantic_problems = kn.resolve_views_with_problems(events)
        reg = _load_domain_registry(store)
        anchor_status_by_id: dict[str, dict] = {}
        for rec in views.values():
            for note in (rec.get("latest"), rec.get("curated")):
                if note is None or note.get("type") == kn.TYPE_LESSON:
                    continue
                note_id = str(note.get("id") or "")
                if note_id not in anchor_status_by_id:
                    anchor_status_by_id[note_id] = _knowledge_anchor_status(store, note)

        if action == "onboard":
            note_limit = getattr(args, "limit", 20)
            lesson_limit = getattr(args, "lesson_limit", 5)
        elif action == "pull":
            note_limit = None
            lesson_limit = getattr(args, "limit", 5)
        else:
            note_limit = None
            lesson_limit = getattr(args, "limit", None)
        exclude_lessons = (
            bool(getattr(args, "exclude_lessons", False))
            or output_schema == "legacy"
        )
        selected = kn.select_knowledge_view(
            views,
            domains=reg.data.get("domains") or {},
            registry_hash=reg.registry_hash,
            anchor_status_by_id=anchor_status_by_id,
            semantic_problems=semantic_problems,
            domain_id=getattr(args, "domain", None),
            type_filter=type_filter,
            scope=scope,
            tags=lesson_tags,
            query=(getattr(args, "query", None) if action == "search" else None),
            include_uncurated=getattr(args, "include_uncurated", False),
            include_stale=getattr(args, "include_stale", False),
            note_limit=note_limit,
            lesson_limit=lesson_limit,
            context_scope=scope or kn.PROCESS_DOMAIN,
            exclude_lessons=exclude_lessons,
        )
        selected["problems"] = [*read_problems, *selected["problems"]]
        note_rows = selected["notes"]
        lesson_rows = selected["lessons"]

        if getattr(args, "json", False):
            if output_schema == "legacy":
                print(json.dumps(
                    [{**{key: value for key, value in note.items() if key != "view"},
                      "_verdict": verdict}
                     for note, verdict in note_rows],
                    indent=2,
                ))
                return 0
            if type_filter == kn.TYPE_LESSON:
                print(json.dumps(
                    [_kn_lesson_dict(note, verdict) for note, verdict in lesson_rows],
                    indent=2,
                ))
                return 0
            if type_filter is not None:
                print(json.dumps(
                    [{**{key: value for key, value in note.items() if key != "view"},
                      "_verdict": verdict}
                     for note, verdict in note_rows],
                    indent=2,
                ))
                return 0
            print(json.dumps({
                "schema_version": "knowledge-view-v1",
                "notes": [{**note, "_verdict": verdict}
                          for note, verdict in note_rows],
                "lessons": [{**note, "_verdict": verdict}
                            for note, verdict in lesson_rows],
                "totals": selected["totals"],
                "truncation": selected["truncation"],
                "problems": selected["problems"],
            }, indent=2))
            return 0

        label = "matching" if action == "search" else (
            "selected" if (getattr(args, "include_stale", False)
                           or getattr(args, "include_uncurated", False))
            else "active"
        )
        problem_text = (
            f"; {len(selected['problems'])} ledger problem(s) (see doctor)"
            if selected["problems"] else ""
        )
        if type_filter == kn.TYPE_LESSON:
            total = selected["totals"]["lessons"]
            shown = len(lesson_rows)
            count = f"{shown} shown of {total}" if shown != total else str(total)
            print(f"knowledge {action}: {count} {label} lesson(s){problem_text}")
            for note, verdict in lesson_rows:
                _kn_print_lesson(note, verdict)
            return 0
        if type_filter is not None:
            total = selected["totals"]["notes"]
            shown = len(note_rows)
            count = f"{shown} shown of {total}" if shown != total else str(total)
            print(f"knowledge {action}: {count} {label} note(s){problem_text}")
            for note, verdict in note_rows:
                _kn_print_note(note, verdict)
            return 0

        who = (
            " for " + args.for_agent
            if action == "onboard" and getattr(args, "for_agent", None)
            else ""
        )
        print(
            f"knowledge {action}{who}: {selected['totals']['notes']} {label} note(s); "
            f"{selected['totals']['lessons']} {label} lesson(s){problem_text}"
        )
        print(
            f"Notes ({len(note_rows)} shown of {selected['totals']['notes']}):"
        )
        current_domain = None
        for note, verdict in note_rows:
            if action == "onboard" and note.get("domain_id") != current_domain:
                current_domain = note.get("domain_id")
                print(f"domain {current_domain}:")
            _kn_print_note(note, verdict)
        if not exclude_lessons:
            print(
                f"Lessons ({len(lesson_rows)} shown of "
                f"{selected['totals']['lessons']}):"
            )
            for note, verdict in lesson_rows:
                _kn_print_lesson(note, verdict)
        return 0

    sys.stderr.write(
        "agenttalk knowledge: expected publish, curate, pull, search, or onboard.\n")
    return 2


def _onboarding_print_run(run: dict, *, detail: bool = False) -> None:
    counts = run.get("counts") or {}
    state = run.get("state") or "unknown"
    title = run.get("title") or run.get("id") or "onboarding"
    print(f"{run.get('id')}: {title} [{state}]")
    if run.get("objective"):
        print(f"  objective: {run.get('objective')}")
    if run.get("base_ref"):
        print(f"  base: {run.get('base_ref')}")
    if run.get("lead"):
        print(f"  lead: {run.get('lead')}")
    print(
        "  "
        f"segments {counts.get('accepted_segments', 0)}/{counts.get('segments', 0)} accepted; "
        f"claims {counts.get('confirmed_claims', 0)}/{counts.get('claims', 0)} confirmed; "
        f"open drift {counts.get('open_drift', 0)}; "
        f"open unknowns {counts.get('open_unknowns', 0)}"
        + (f"; blockers {counts.get('blocking_unknowns', 0)}"
           if counts.get("blocking_unknowns") else "")
    )
    if not detail:
        return
    records = run.get("records") or {}
    for kind in (ob.KIND_SEGMENT, ob.KIND_CLAIM, ob.KIND_DRIFT, ob.KIND_UNKNOWN):
        rows = records.get(kind) or []
        if not rows:
            continue
        print(f"  {kind}:")
        for row in rows:
            extras = []
            if row.get("segment"):
                extras.append(f"segment={row.get('segment')}")
            if row.get("owner"):
                extras.append(f"owner={row.get('owner')}")
            if row.get("source"):
                extras.append(f"source={row.get('source')}")
            if row.get("confidence"):
                extras.append(f"confidence={row.get('confidence')}")
            if row.get("blocking"):
                extras.append("blocking")
            suffix = f" ({', '.join(extras)})" if extras else ""
            print(f"    - {row.get('key')} [{row.get('status')}]{suffix}: {row.get('summary')}")


def cmd_onboarding(args: argparse.Namespace) -> int:
    """Native project-onboarding evidence ledger."""
    store = _get_store(args)
    action = getattr(args, "onboarding_cmd", None)
    roster = store.load_config().get("agents") or []

    if action == "create":
        actor = _resolve_self(getattr(args, "actor", None), roster=roster)
        run_id = getattr(args, "run_id", None) or ob.new_run_id()
        try:
            evt = ob.new_create_event(
                run_id=run_id,
                title=args.title,
                objective=getattr(args, "objective", None),
                base_ref=getattr(args, "base_ref", None),
                lead=actor,
                state=getattr(args, "state", None) or "scanning",
                at=_iso_now(),
            )
            ob.create_run(store, evt)
        except ob.OnboardingError as e:
            sys.stderr.write(f"agenttalk onboarding create: {e}\n")
            return 2
        run, _ = ob.get_run(store, evt["run_id"])
        if args.json:
            print(json.dumps(run or evt, indent=2))
        else:
            print(f"created onboarding run {evt['run_id']}")
        return 0

    if action == "list":
        try:
            payload = ob.list_runs(store, limit=getattr(args, "limit", None))
        except ob.OnboardingError as e:
            sys.stderr.write(f"agenttalk onboarding list: {e}\n")
            return 2
        if args.json:
            print(json.dumps(payload, indent=2))
            return 0
        runs = payload.get("runs") or []
        print(
            f"onboarding runs: {len(runs)}"
            + (f" shown of {payload.get('total', 0)}" if payload.get("truncated") else "")
        )
        for run in runs:
            _onboarding_print_run(run)
        if payload.get("problems"):
            print(f"ledger problems: {len(payload['problems'])}")
        return 0

    if action == "show":
        try:
            run, problems = ob.get_run(store, args.run_id)
        except ob.OnboardingError as e:
            sys.stderr.write(f"agenttalk onboarding show: {e}\n")
            return 2
        if run is None:
            sys.stderr.write(f"agenttalk onboarding show: no onboarding run {args.run_id!r}\n")
            return 2
        run["problems"] = problems
        if args.json:
            print(json.dumps(run, indent=2))
        else:
            _onboarding_print_run(run, detail=True)
            if problems:
                print(f"  ledger problems: {len(problems)}")
        return 0

    if action == "state":
        actor = _resolve_self(getattr(args, "actor", None), roster=roster)
        try:
            run, _ = ob.get_run(store, args.run_id)
            if run is None:
                raise ob.OnboardingError(f"no onboarding run {args.run_id!r}")
            evt = ob.new_state_event(
                run_id=args.run_id,
                state=args.state,
                actor=actor,
                summary=getattr(args, "summary", None),
                at=_iso_now(),
            )
            ob.append_event(store, evt)
        except ob.OnboardingError as e:
            sys.stderr.write(f"agenttalk onboarding state: {e}\n")
            return 2
        if args.json:
            run, _ = ob.get_run(store, args.run_id)
            print(json.dumps(run, indent=2))
        else:
            print(f"onboarding {args.run_id}: state -> {args.state}")
        return 0

    if action == "record":
        actor = _resolve_self(getattr(args, "actor", None), roster=roster)
        try:
            run, _ = ob.get_run(store, args.run_id)
            if run is None:
                raise ob.OnboardingError(f"no onboarding run {args.run_id!r}")
            evt = ob.new_record_event(
                run_id=args.run_id,
                kind=args.kind,
                key=args.key,
                status=args.status,
                summary=args.summary,
                actor=actor,
                segment=getattr(args, "segment", None),
                owner=getattr(args, "owner", None),
                checkers=getattr(args, "checker", None),
                refs=getattr(args, "ref", None),
                paths=getattr(args, "path", None),
                source=getattr(args, "source", None),
                confidence=getattr(args, "confidence", None),
                blocking=bool(getattr(args, "blocking", False)),
                at=_iso_now(),
            )
            ob.append_event(store, evt)
        except ob.OnboardingError as e:
            sys.stderr.write(f"agenttalk onboarding record: {e}\n")
            return 2
        if args.json:
            print(json.dumps(evt, indent=2))
        else:
            print(f"recorded {args.kind} {args.key} [{args.status}] in {args.run_id}")
        return 0

    sys.stderr.write("agenttalk onboarding: expected create|list|show|state|record.\n")
    return 2


def cmd_prune(args: argparse.Namespace) -> int:
    """Quarantine invalid message files (#17, 0.15.0).

    Move-only and recoverable: files go to `.agenttalk/quarantine/`
    (collision-suffixed, never overwritten, never deleted by the tool);
    restore = move the file back into messages/ by hand. The selection
    is exactly what status/doctor report as INVALID — same gate walk,
    path-paired at scan time. Valid files are untouched by construction.
    """
    store = _get_store(args)
    if not getattr(args, "invalid", False):
        sys.stderr.write(
            "agenttalk prune: pass --invalid (the only selector today; "
            "future selectors are reserved).\n"
        )
        return 2
    records = store.quarantine_invalid(dry_run=args.dry_run)
    if args.json:
        print(json.dumps({
            "selected": records,
            "moved": ([] if args.dry_run
                      else [r["to"] for r in records if r.get("to")]),
            "dry_run": bool(args.dry_run),
        }, indent=2))
        return 0
    if not records:
        print("nothing to prune — no invalid messages.")
        return 0
    verb = "would move" if args.dry_run else "moved"
    if not args.quiet:
        for r in records:
            print(f"  {r['id']}  {verb} -> {r['to']}   ({r['reason']})")
    print(f"({len(records)} file(s) {verb}; quarantine is recoverable — "
          f"restore by moving the file back into messages/)")
    return 0


def _build_attention_block(args: argparse.Namespace) -> dict | None:
    """Build the typed meta.attention block from the escalate flags, or None if the caller
    supplied no typed fields (an untyped escalation stays valid). schema_version is always
    stamped so a present block is versioned."""
    fields = {
        "decision": getattr(args, "decision", None),
        "why_it_matters": getattr(args, "why", None),
        "recommendation": getattr(args, "recommendation", None),
        "risk_if_ignored": getattr(args, "risk_if_ignored", None),
        "risk_severity": getattr(args, "risk_severity", None),
        "confidence": getattr(args, "confidence", None),
        "priority": getattr(args, "priority", None),
        "needed_by": getattr(args, "needed_by", None),
    }
    options = getattr(args, "option", None) or []
    affected = getattr(args, "affected", None) or []
    present = [v for v in fields.values() if v is not None] + list(options) + list(affected)
    if not present:
        return None
    block = {"schema_version": 1}
    block.update({k: v for k, v in fields.items() if v is not None})
    if options:
        block["options"] = list(options)
    if affected:
        block["affected"] = list(affected)
    return block


def cmd_escalate(args: argparse.Namespace) -> int:
    """Route an operator-input question to the operator-facing agent (#18).

    Resolves the liaison from the roster's `operator_facing` designation
    (explicit `--to` overrides), mints an `esc-` request_id, and sends an
    ordinary tracked `question` carrying `needs_operator=true`. REFUSES
    loudly (exit 2) when no liaison is resolvable — an escalation that
    lands nowhere is exactly the invisible failure this exists to kill.
    Advisory routing only: never authorization (C-007).
    """
    store = _get_store(args)
    cfg = store.load_config()
    roster = cfg.get("agents") or []
    sender = _resolve_self(args.sender, roster=roster)
    body = _read_body(args)
    if not body:
        sys.stderr.write(
            "agenttalk escalate: empty body (use -m TEXT, --file PATH, or pipe "
            "stdin).\n  State the decision you need from the operator, the "
            "options, and your recommendation.\n"
        )
        return 2
    if args.to:
        try:
            validate_agent_name(args.to)
        except ValueError as e:
            sys.stderr.write(f"agenttalk escalate: {e}\n")
            return 2
        _ensure_in_roster(args.to, roster, label="escalation target")
        target = args.to
    else:
        try:
            operator_identity, lead_chat_lead = store.lead_chat_identities()
        except ValueError:
            operator_identity, lead_chat_lead = None, None
        target = operator_identity if sender == lead_chat_lead else store.operator_facing()
        if target is None:
            # No usable liaison. Fall back to the team's single lead rather than
            # stranding the escalation (0.24.0, feedback 3.1). The at-most-one
            # invariant makes `sole_lead()` unambiguous; it is None for zero or
            # (legacy) multiple leads, in which case we still refuse loudly.
            lead = store.sole_lead()
            if lead is not None and lead != sender:
                target = lead
                if not args.quiet:
                    sys.stderr.write(
                        f"agenttalk escalate: no operator-facing liaison is "
                        f"configured; routing to the lead {lead!r}.\n"
                    )
            else:
                raw = store.operator_facing_raw()
                if lead is not None and lead == sender:
                    sys.stderr.write(
                        "agenttalk escalate: no liaison is configured and you "
                        "are the lead — ask your operator directly, or pass "
                        "--to <agent> explicitly.\n"
                    )
                elif raw:
                    sys.stderr.write(
                        f"agenttalk escalate: configured liaison {raw!r} is not "
                        f"in the roster {sorted(roster)}, and no lead is set — "
                        f"fix the liaison with `agenttalk roster "
                        f"set-operator-facing <agent>` (or --clear), designate a "
                        f"lead with `agenttalk roster set-role <agent> lead`, or "
                        f"pass --to <agent> explicitly.\n"
                    )
                else:
                    sys.stderr.write(
                        "agenttalk escalate: no operator-facing liaison and no "
                        "lead are configured — run `agenttalk roster "
                        "set-operator-facing <agent>` or `agenttalk roster "
                        "set-role <agent> lead`, or pass --to <agent> "
                        "explicitly.\n"
                    )
                return 2
    if target == sender:
        sys.stderr.write(
            f"agenttalk escalate: {sender} IS the operator-facing agent — "
            f"you own the operator channel; ask your operator directly.\n"
        )
        return 2
    meta = _parse_meta(args.meta)
    meta["needs_operator"] = "true"  # force-set: the bucket discriminator
    origin_request = getattr(args, "origin_request", None)
    origin_id = getattr(args, "origin_id", None)
    if bool(origin_request) != bool(origin_id):
        sys.stderr.write(
            "agenttalk escalate: --origin-request and --origin-id must be supplied together.\n"
        )
        return 2
    if origin_request:
        meta["origin_request_id"] = origin_request
        meta["origin_inbound_id"] = origin_id
        meta["in_reply_to"] = origin_id
    # Typed attention enrichment (0.56.0): escalate OWNS meta.attention - build it ONLY from
    # the typed flags (a caller --meta attention=... is not a supported typed input). Strict
    # CLI validation: a malformed typed block exits 2 BEFORE any write (gate 4); the reader
    # side is separately fail-safe. An escalation with no typed flags stays valid + untyped.
    from agenttalk import attention as _attn
    att_block = _build_attention_block(args)
    if att_block is not None:
        errs = _attn.validate_attention_meta({"attention": att_block})
        if errs:
            sys.stderr.write("agenttalk escalate: invalid typed attention field(s):\n  - "
                             + "\n  - ".join(errs) + "\n")
            return 2
        meta["attention"] = att_block
    if "request_id" not in meta:
        meta["request_id"] = "esc-" + uuid.uuid4().hex[:12]
    operation_nonce = getattr(args, "operation_nonce", None)
    existing, operation_error = _operation_idempotency(
        store,
        sender=sender,
        recipient=target,
        body=body,
        kind="question",
        operation="terminal",
        meta=meta,
        nonce=operation_nonce,
    )
    if operation_error is not None:
        sys.stderr.write(f"agenttalk escalate: {operation_error}.\n")
        return 2
    if existing is not None:
        if not args.quiet:
            print(f"(escalation operation already recorded: id={existing.id})")
        print(f"request_id={(existing.meta or {}).get('request_id', meta['request_id'])}")
        return 0
    try:
        if operation_nonce is not None:
            msg, published = store.send_operation(
                sender=sender,
                recipient=target,
                body=body,
                kind="question",
                subject=args.subject or "operator input needed",
                meta=meta,
                operation_nonce=operation_nonce,
                operation_digest=str(meta["operation_digest"]),
            )
        else:
            msg = store.send(
                sender=sender,
                recipient=target,
                body=body,
                kind="question",
                subject=args.subject or "operator input needed",
                meta=meta,
            )
            published = True
    except ValueError as exc:
        sys.stderr.write(f"agenttalk escalate: {exc}.\n")
        return 2
    if not published:
        if not args.quiet:
            print(f"(escalation operation already recorded: id={msg.id})")
        print(f"request_id={(msg.meta or {}).get('request_id', meta['request_id'])}")
        return 0
    if not args.quiet:
        print(render(msg, header=f"AGENTTALK :: ESCALATE  {sender} -> {target}"))
    # Always print the machine-parseable correlation line: the caller's
    # next move is `agenttalk wait --to-request <this>`.
    print(f"request_id={meta['request_id']}")
    return 0


def _attn_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _wrapper_notice_has_canonical_row(store: Store, meta: dict, sender: str) -> bool:
    return th.wrapper_notice_has_canonical_row(store, meta, sender)


def _needs_operator_items(store: Store, for_agent: str, now) -> list[dict]:
    """Pending needs_operator escalations from the liaison's thread view + the opener meta,
    with wrapper dead-letter/config-blocked twins coalesced (reviewer-2 F6). Requires a
    resolved for-agent (the caller guards None)."""
    from agenttalk import attention as A
    msgs = store.valid_messages()
    opener_meta: dict[str, dict] = {}
    opener_sender: dict[str, str] = {}
    for m in msgs:
        rid = (m.meta or {}).get("request_id")
        if rid and (m.meta or {}).get("needs_operator") == "true" and rid not in opener_meta:
            opener_meta[rid] = m.meta or {}
            opener_sender[rid] = m.sender
    rows = th.derive_threads(
        msgs, agent=for_agent, cursor=store.cursor(for_agent), now=now,
        closed_rids=_closed_rids(store, for_agent), retired=set(store.retired_agents()))
    pending = [{"request_id": t.request_id, "subject": t.subject,
                "sender": opener_sender.get(t.request_id, ""),
                "age_seconds": t.age_seconds, "meta": opener_meta.get(t.request_id, {})}
               for t in rows
               if t.needs_operator and t.operator_state == "pending"
               and t.opener_recipient == for_agent and t.opener_sender != for_agent]
    # COALESCE wrapper dead-letter / config-blocked notices: the wrapper emits a needs_operator
    # TWIN of a message it dead-lettered or parked. That twin is redundant with the canonical
    # dead_letter sink row / config_blocked hold row, so SUPPRESS it - but ONLY when the
    # canonical row actually exists (else the notice is the sole signal and must be kept).
    # Coalescing means a resolve/disposition on the canonical row removes BOTH.
    pending = [p for p in pending
               if not _wrapper_notice_has_canonical_row(store, p["meta"], p["sender"])]
    return A.needs_operator_items(pending)


def _collect_attention_items(store: Store, *, for_agent: str | None, roster: list[str]) -> list[dict]:
    """Read every attention source, each INDEPENDENTLY FAIL-SAFE: one bad source yields a
    bounded source_error item, never blanks the queue (gate 8). Reuses PURE derivations
    (derive_threads, read_config_blocked_hold, list_dead_letters, check_gates,
    lead_loop_state) - never scrapes doctor/status text. ``for_agent`` may be None (no
    liaison/sole-lead resolved): the per-recipient needs_operator branch is then SKIPPED and
    only the global sources are projected (codex F4, read-only view)."""
    from agenttalk import attention as A
    items: list[dict] = []
    now = datetime.now(timezone.utc)
    # needs_operator: pending escalations from the liaison's thread view + the opener meta.
    # SKIPPED (not an error) when no for-agent resolved - cmd_attention adds a no_liaison
    # warning instead, so the read-only view still surfaces the global sources (codex F4).
    if for_agent:
        try:
            items += _needs_operator_items(store, for_agent, now)
        except Exception as e:  # noqa: BLE001 - a source read must never crash the queue
            items.append(A.source_error_item("needs_operator", str(e)))
    # config_blocked holds (per roster agent)
    try:
        holds = [h for a in roster if (h := store.read_config_blocked_hold(a))]
        items += A.config_blocked_items(holds)
    except Exception as e:  # noqa: BLE001
        items.append(A.source_error_item("config_blocked", str(e)))
    # supervisor-owned process-tree HOLDs are global: they must remain visible
    # even when no liaison/sole lead can be resolved.
    try:
        supervisor_state = _read_supervisor_state(store)
        try:
            supervisor_config = _load_supervisor_config(store)
        except Exception:  # noqa: BLE001 - the HOLD must survive bad config
            supervisor_config = None
        try:
            store_config = store.load_config()
        except Exception:  # noqa: BLE001 - fail closed without blanking the HOLD
            store_config = None
        restart_requests: dict[str, dict] = {}
        for name in A.configured_process_tree_hold_agents(supervisor_state):
            try:
                marker = store.read_restart_request(name)
            except Exception:  # noqa: BLE001 - optional context, not the signal
                marker = None
            if isinstance(marker, dict):
                restart_requests[name] = marker
        reset_admissions = sup.evaluate_process_tree_reset_admissions(
            store,
            supervisor_state,
            actor=for_agent,
            identity_gone=_owner_identity_gone,
        )
        launch_requests = sup.active_ephemeral_launch_markers(
            store,
            supervisor_state,
        )
        launch_deliveries = sup.active_ephemeral_one_shot_deliveries(
            store,
            supervisor_state,
            launch_requests,
        )
        lane_workspaces = sup.active_ephemeral_lane_workspaces(store)
        items += A.process_tree_hold_items(
            supervisor_state,
            supervisor_config=supervisor_config,
            store_config=store_config,
            root=store.root,
            restart_requests=restart_requests,
            launch_requests=launch_requests,
            launch_deliveries=launch_deliveries,
            lane_workspaces=lane_workspaces,
            reset_admissions=reset_admissions,
            now_epoch=time.time(),
        )
    except Exception as e:  # noqa: BLE001
        items.append(A.source_error_item("process_tree_hold", str(e)))
    # dead-letter (ALL; build_queue hides resolved via the resolve_dead_letter disposition)
    try:
        items += A.dead_letter_items(store.list_dead_letters())
    except Exception as e:  # noqa: BLE001
        items.append(A.source_error_item("dead_letter", str(e)))
    # gate HOLDs (cheap state read, no git/lane recompute)
    try:
        items += A.gate_hold_items(gate_mod.check_gates(store.root).get("blockers", []))
    except Exception as e:  # noqa: BLE001
        items.append(A.source_error_item("gate_hold", str(e)))
    # lead-loop unarmed (managed agents; PURE lead_loop_state, not doctor text)
    try:
        signals = []
        for a in store.managed_lead_loop_agents():
            st = store.lead_loop_state(a)
            if st.get("managed") and not st.get("armed"):
                signals.append({"agent": a, "reason": st.get("reason") or "lead-loop unarmed"})
        items += A.lead_unarmed_items(signals)
    except Exception as e:  # noqa: BLE001
        items.append(A.source_error_item("lead_unarmed", str(e)))
    # Explicit coordination stalls (pure detector; generic idle never enters).
    try:
        from agenttalk import coordination_stall as _coordination_stall

        snapshot = _coordination_stall.build_snapshot(store)
        items += A.coordination_stall_items(snapshot.get("items") or [])
    except Exception as e:  # noqa: BLE001
        items.append(A.source_error_item("coordination_stall", str(e)))
    # capacity (threshold-tripped only, from the cheap persisted snapshots - gate 8)
    try:
        items += A.capacity_items(_tripped_capacity_signals(store))
    except Exception as e:  # noqa: BLE001
        items.append(A.source_error_item("capacity", str(e)))
    # close HOLDs (published closes whose snapshotted final verdict is HOLD - a CHEAP read of
    # the persisted record, NO gate recompute; a malformed record degrades to a warning row)
    try:
        holds, degraded = _published_close_holds(store)
        items += A.close_hold_items(holds)
        if degraded:
            items.append(A.source_error_item(
                "close_hold", f"{degraded} close record(s) unreadable/malformed (skipped)"))
    except Exception as e:  # noqa: BLE001
        items.append(A.source_error_item("close_hold", str(e)))
    return items


# Threshold above which a capacity snapshot is worth an operator's attention (gate 8: cheap,
# no recompute). Below this a snapshot is routine headroom and is NOT surfaced.
_CAPACITY_TRIP_PCT = 90.0


def _tripped_capacity_signals(store: Store) -> list[dict]:
    """Threshold-tripped capacity signals from the cheap persisted snapshots. A signal fires
    ONLY when an agent actually hit a rate limit or is near budget/context exhaustion - not
    for routine headroom - so the queue is not flooded with passive telemetry (gate 8)."""
    signals: list[dict] = []
    for agent, snap in (store.read_all_capacities() or {}).items():
        if not isinstance(snap, dict):
            continue
        rl = snap.get("rate_limit_reached_type")
        prim = snap.get("primary_used_percent")
        ctx = snap.get("context_used_percent")
        if isinstance(rl, str) and rl.strip():
            signals.append({"agent": agent, "kind": "rate_limit",
                            "detail": f"rate limit reached: {rl}"})
        elif isinstance(prim, (int, float)) and prim >= _CAPACITY_TRIP_PCT:
            signals.append({"agent": agent, "kind": "budget",
                            "detail": f"primary budget {prim:.0f}% used"})
        elif isinstance(ctx, (int, float)) and ctx >= _CAPACITY_TRIP_PCT:
            signals.append({"agent": agent, "kind": "context",
                            "detail": f"context window {ctx:.0f}% full"})
    return signals


def _published_close_holds(store: Store) -> tuple[list[dict], int]:
    """Return ([{close_id, scope, verdict, reason, revision}], degraded_count) for PUBLISHED
    closes whose snapshotted final verdict is HOLD. CHEAP: reads each persisted record's own
    published snapshot (record['final']), NO gate recompute (gate 8). A malformed/unreadable
    record is counted as degraded and skipped, never crashing the projection."""
    from agenttalk import close as close_mod
    holds: list[dict] = []
    degraded = 0
    cdir = close_mod.closes_dir(store)
    if not cdir.is_dir():
        return holds, degraded
    for p in sorted(cdir.glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            degraded += 1
            continue
        if not isinstance(rec, dict):
            degraded += 1
            continue
        final = rec.get("final")
        if rec.get("status") == close_mod.PUBLISHED and isinstance(final, dict) \
                and final.get("verdict") == close_mod.VERDICT_HOLD:
            holds.append({"close_id": rec.get("close_id") or p.stem,
                          "scope": rec.get("scope"), "verdict": "HOLD",
                          "reason": final.get("reason"), "revision": rec.get("revision")})
    return holds, degraded


def _resolve_disposition_actor(store: Store, args: argparse.Namespace) -> str | None:
    """The disposition actor is the OPERATOR-FACING liaison (or the sole lead when no
    liaison is configured), resolved from --from / $AGENTTALK_SELF. NO --by flag (gate 5):
    a disposition is an operator-attention decision and only the liaison/sole-lead may make
    it. Returns the actor, or None (caller exits 2) when the resolved identity is not
    authorized."""
    roster = store.load_config().get("agents") or []
    who = _resolve_self(getattr(args, "sender", None), roster=roster)
    liaison = store.operator_facing()
    if liaison is not None:
        return who if who == liaison else None
    sole = store.sole_lead()
    return who if (sole is not None and who == sole) else None


def _attention_input_warnings(problems: list, no_liaison: bool) -> list[dict]:
    """The degraded-input warning envelope, shared by the queue view and --stats so a
    stats read can never look complete while its inputs are partial (a torn disposition
    log) or the needs_operator source is skipped (no liaison)."""
    warnings: list[dict] = []
    if problems:
        warnings.append({"disposition_log": f"{len(problems)} torn/invalid line(s)"})
    if no_liaison:
        warnings.append(
            {"no_liaison": "no operator-facing liaison or sole lead is configured; "
                           "needs_operator escalations are not shown - pass --for <agent>. "
                           "Dispositions require an authorized liaison/sole-lead."})
    return warnings


def cmd_attention(args: argparse.Namespace) -> int:
    """Operator attention queue: a derived, ranked, deduped read-only view over existing
    signals, plus operator dispositions. Creates no work objects and mutates nothing except
    the disposition log."""
    from agenttalk import attention as A
    store = _get_store(args)
    sub = getattr(args, "attn_cmd", None)
    if sub in ("defer", "dismiss", "answered-elsewhere"):
        return _cmd_attention_disposition(store, args, sub)
    roster = store.load_config().get("agents") or []
    for_agent = (getattr(args, "for_agent", None) or store.operator_facing()
                 or store.sole_lead())
    # READ-ONLY (view/show) SURFACES even with no liaison/sole-lead (codex F4): the global
    # sources (config_blocked, dead_letter, gate/close HOLDs, capacity, lead_unarmed) do not
    # need a for-agent; only per-recipient needs_operator escalations are skipped, with a
    # WARNING. Exit 2 is reserved for the disposition WRITE subcommands (they gate on an
    # authorized actor in _cmd_attention_disposition), never the read-only view.
    no_liaison = for_agent is None
    items = _collect_attention_items(store, for_agent=for_agent, roster=roster)
    disps, problems = A.read_dispositions(store)
    input_warnings = _attention_input_warnings(problems, no_liaison)
    if getattr(args, "stats", False):
        stats = A.compute_stats(items, disps, now_iso=_attn_now_iso())
        if getattr(args, "json", False):
            print(json.dumps({"stats": stats, "warnings": input_warnings, "for": for_agent},
                             ensure_ascii=False, indent=2))
        else:
            _print_attention_stats(stats, for_agent, input_warnings)
        return 0
    all_flag = getattr(args, "all", False)
    q = A.build_queue(items, disps, now_iso=_attn_now_iso(),
                      include_deferred=all_flag or getattr(args, "include_deferred", False),
                      include_dismissed=all_flag or getattr(args, "include_dismissed", False),
                      include_resolved=all_flag or getattr(args, "include_resolved", False))
    for w in input_warnings:
        q.setdefault("warnings", []).append(w)
    rows = q["items"]
    if sub == "show":
        rows = [it for it in rows if it["item_id"] == getattr(args, "item", None)]
        if not rows:
            sys.stderr.write(f"agenttalk attention show: no item {getattr(args, 'item', None)!r} "
                             "in view (a deferred/dismissed/resolved item needs --all or the "
                             "matching --include-* flag).\n")
            return 1
    if getattr(args, "source", None):
        rows = [it for it in rows if it["source"] == args.source]
    if getattr(args, "limit", None):
        rows = rows[: args.limit]
    if getattr(args, "json", False):
        print(json.dumps({**q, "items": rows, "for": for_agent}, ensure_ascii=False, indent=2))
        return 0
    _print_attention(rows, q["summary"], for_agent, q.get("warnings") or [])
    return 0


def _print_attention_stats(stats: dict, for_agent: str | None,
                           warnings: list | None = None) -> None:
    print(f"attention stats for {for_agent or '(no liaison configured)'}")
    print(f"  surfaced active: {stats.get('surfaced_active', 0)}")
    for src, n in (stats.get("active_by_source") or {}).items():
        print(f"    {src:<15} {n}")
    disp = stats.get("dispositioned") or {}
    print(f"  dispositioned: deferred={disp.get('deferred', 0)} "
          f"dismissed={disp.get('dismissed', 0)} resolved={disp.get('resolved', 0)} "
          f"answered_elsewhere={disp.get('answered_elsewhere', 0)}")
    dwell = stats.get("oldest_active_age_seconds") or 0
    print(f"  oldest active dwell: {dwell}s")
    for w in warnings or []:
        for msg in (w.values() if isinstance(w, dict) else [w]):
            print(f"  ! {msg}")


def _print_attention(rows: list[dict], summary: dict, for_agent: str | None,
                     warnings: list | None = None) -> None:
    print(f"attention for {for_agent or '(no liaison configured)'}  "
          f"(active={summary.get('active_count', 0)}, "
          f"deferred={summary.get('deferred_count', 0)})")
    for w in warnings or []:
        for msg in (w.values() if isinstance(w, dict) else [w]):
            print(f"  ! {msg}")
    if not rows:
        print("  (nothing needs the operator right now)")
        return
    for it in rows:
        prio = it.get("priority", "unknown")
        title = it.get("title") or it.get("decision") or it["item_id"]
        line = f"  [{prio:<6}] {it['source']:<15} {title}"
        if it.get("state") != "active":
            line += f"  ({it['state']})"
        dups = len(it.get("duplicates", []))
        if dups:
            line += f"  (+{dups} dup)"
        print(line)
        if it.get("recommendation"):
            print(f"           rec: {it['recommendation']}")
        operator_argv = it.get("operator_argv")
        if (
            isinstance(operator_argv, list)
            and all(isinstance(token, str) for token in operator_argv)
        ):
            print(
                "           currently admitted remedy argv: "
                + json.dumps(operator_argv, ensure_ascii=False)
            )
        launch = it.get("configured_launch")
        if isinstance(launch, dict) and isinstance(launch.get("argv"), list):
            print(
                "           configured detached launch argv: "
                + json.dumps(launch["argv"], ensure_ascii=False)
            )
            print(f"           configured launch cwd: {launch.get('cwd') or '(none)'}")
            if isinstance(launch.get("environment"), dict):
                print(
                    "           configured launch environment guidance "
                    "(child value not verified): "
                    + json.dumps(launch["environment"], ensure_ascii=False)
                )
            if launch.get("environment_note"):
                print(f"           configured launch note: {launch['environment_note']}")
        for w in it.get("warnings", []):
            print(f"           ! {w}")
        print(f"           id: {it['item_id']}")


def _cmd_attention_disposition(store: Store, args: argparse.Namespace, sub: str) -> int:
    from agenttalk import attention as A
    actor = _resolve_disposition_actor(store, args)
    if actor is None:
        sys.stderr.write("agenttalk attention: only the operator-facing liaison (or the "
                         "sole lead when none is configured) may disposition an item; "
                         "resolve --from/$AGENTTALK_SELF to that identity.\n")
        return 2
    reason = getattr(args, "reason", None)
    if not reason or not reason.strip():
        sys.stderr.write(f"agenttalk attention {sub}: --reason is required.\n")
        return 2
    roster = store.load_config().get("agents") or []
    for_agent = store.operator_facing() or store.sole_lead() or actor
    item = next((it for it in _collect_attention_items(store, for_agent=for_agent, roster=roster)
                 if it["item_id"] == getattr(args, "item", None)), None)
    if item is None:
        sys.stderr.write(f"agenttalk attention {sub}: unknown item "
                         f"{getattr(args, 'item', None)!r} (see `agenttalk attention`).\n")
        return 2
    action = {"defer": A.ACTION_DEFER, "dismiss": A.ACTION_DISMISS,
              "answered-elsewhere": A.ACTION_ANSWERED_ELSEWHERE}[sub]
    if not A.allowed_action_for_source(action, item["source"], advisory=item.get("advisory", False)):
        sys.stderr.write(f"agenttalk attention {sub}: '{sub}' is not allowed for a "
                         f"{item['source']} item (blocking items must be repaired, answered, "
                         f"or deferred - not dismissed).\n")
        return 2
    if sub == "defer":
        until = getattr(args, "until", None)
        if not until:
            sys.stderr.write("agenttalk attention defer: --until <ISO> is required.\n")
            return 2
        # Normalize/validate the ISO on WRITE so a malformed value can never be persisted
        # and later hide a blocking item (codex F2). Store the canonical form.
        until_dt = A.parse_iso_dt(until)
        if until_dt is None:
            sys.stderr.write(f"agenttalk attention defer: --until {until!r} is not a valid "
                             "ISO-8601 date/datetime.\n")
            return 2
        until_canonical = until_dt.isoformat().replace("+00:00", "Z")
    else:
        until_canonical = None
    event = {
        "schema_version": A.SCHEMA_VERSION, "event_id": "att-" + uuid.uuid4().hex[:12],
        "item_id": item["item_id"], "source": item["source"], "action": action,
        "actor": actor, "reason": reason, "at": _attn_now_iso(),
        "until": until_canonical,
        "evidence": getattr(args, "evidence", None),
        "source_snapshot": {"source_hash": item["source_hash"], "refs": item.get("source_refs", [])},
    }
    A.append_disposition(store, event)
    print(f"attention {sub}: {item['item_id']} by {actor}")
    return 0


# The relay handlers are AUTHORITATIVE for every control / audit / routing meta key on
# this operator-authority surface (lead WP4 P3 - audit-trail integrity). A caller --meta
# can never forge an audit discriminator (e.g. operator_command_override on a non-override
# command, or a sibling operator_command on an answer) nor graft a routing/threading key
# onto a relayed message: we SCRUB the full reserved set after parsing, then stamp ONLY
# what each command owns. (request_id is handled separately: operator-command REJECTS a
# caller one outright; operator-answer fixes it to the answered thread's id.)
_RELAY_RESERVED_META = (
    "operator_command", "operator_answer", "operator_origin",
    "operator_command_override", "override_reason", "needs_operator",
    "broadcast_id", "in_reply_to", "target_msg_id",
)


def cmd_relay(args: argparse.Namespace) -> int:
    """The MECHANICAL LIAISON RELAY (lead-loop Slice 2 WP4): typed wrappers over the
    EXISTING reply/send plumbing so the operator's words cross the human<->bus boundary
    with an audit stamp - NO new message KIND, NO new transport. The lead-loop->operator
    direction stays the existing `agenttalk escalate` (a needs_operator question).

      * operator-answer: the liaison relays the OPERATOR's answer to a PENDING
        needs_operator escalation back to the asking lead-loop (a VALIDATED
        reply-on-thread carrying meta.operator_answer + operator_origin).
      * operator-command: the liaison relays a SPONTANEOUS operator instruction to a
        managed lead-loop (a question/message carrying meta.operator_command +
        operator_origin). FAIL-CLOSED to the current operator-facing liaison.
    """
    store = _get_store(args)
    cfg = store.load_config()
    roster = cfg.get("agents") or []
    sender = _resolve_self(args.sender, roster=roster)
    sub = getattr(args, "relay_cmd", None)
    if sub == "operator-answer":
        return _relay_operator_answer(store, sender, roster, args)
    if sub == "operator-command":
        return _relay_operator_command(store, sender, roster, args)
    sys.stderr.write("agenttalk relay: a subcommand is required "
                     "(operator-answer | operator-command).\n")
    return 2


def _relay_operator_answer(store, sender: str, roster: list, args) -> int:
    """relay operator-answer: VALIDATE that ``--to-request`` is a PENDING needs_operator
    opener addressed to this liaison, then send a normal reply on that thread stamped
    with operator_answer + operator_origin so it routes back to the asking lead-loop's
    own mailbox (the structural escalate -> operator-answers -> lead-loop path)."""
    rid = args.to_request
    body = _read_body(args)
    if not body:
        sys.stderr.write("agenttalk relay operator-answer: empty body (use -m TEXT, "
                         "--file PATH, or pipe stdin) - relay the operator's answer.\n")
        return 2
    meta = _parse_meta(args.meta)
    for k in _RELAY_RESERVED_META:
        meta.pop(k, None)                            # SCRUB: handler-authoritative audit meta
    result = store.send_operator_answer_atomic(
        actor=sender, request_id=rid, body=body,
        subject=args.subject, extra_meta=meta)
    if not result.ok:
        code = result.denial_code or "denied"
        if code == "not_found":
            sys.stderr.write(f"agenttalk relay operator-answer: no thread {rid!r} involving "
                             f"{sender!r} (unknown id or not your thread).\n")
        elif code == "not_operator":
            sys.stderr.write(f"agenttalk relay operator-answer: thread {rid!r} is not an operator "
                             f"escalation (needs_operator). Use `agenttalk reply` for an ordinary "
                             f"thread.\n")
        elif code == "not_owed":
            sys.stderr.write(f"agenttalk relay operator-answer: {result.detail} - only the "
                             "addressed liaison relays its answer.\n")
        elif code == "self_answer":
            sys.stderr.write(f"agenttalk relay operator-answer: {result.detail}.\n")
        elif code == "operator_answer_lock_unavailable":
            sys.stderr.write("agenttalk relay operator-answer: could not acquire the operator "
                             "answer lock; no answer was sent.\n")
        elif code == "operator_answer_state_unreadable":
            sys.stderr.write("agenttalk relay operator-answer: could not read current "
                             "operator-answer state; no answer was sent.\n")
        elif code == "operator_answer_send_rejected":
            sys.stderr.write(f"agenttalk relay operator-answer: send rejected ({result.detail}); "
                             "no answer was sent.\n")
        else:
            sys.stderr.write(f"agenttalk relay operator-answer: escalation {rid!r} is not pending "
                             f"({result.detail}) - nothing pending to answer.\n")
        return 2
    msg = result.message
    if msg is None:
        sys.stderr.write("agenttalk relay operator-answer: answer state was inconclusive; "
                         "no answer was sent.\n")
        return 2
    target = msg.recipient                           # back to the asking lead-loop / agent
    if not args.quiet:
        print(render(msg, header=f"AGENTTALK :: RELAY operator-answer  {sender} -> {target}"))
    print(f"request_id={rid}")
    return 0


def _relay_operator_command(store, sender: str, roster: list, args) -> int:
    """relay operator-command: send a SPONTANEOUS operator instruction to a managed
    lead-loop, stamped operator_command + operator_origin. FAIL-CLOSED to the current
    operator-facing liaison (an audited --override + --reason is the only exception).
    INFER --to only when exactly ONE managed lead-loop exists; otherwise REQUIRE it."""
    body = _read_body(args)
    if not body:
        sys.stderr.write("agenttalk relay operator-command: empty body (use -m TEXT, "
                         "--file PATH, or pipe stdin) - relay the operator's instruction.\n")
        return 2
    kind = args.kind or "question"
    if kind not in ("question", "message"):
        sys.stderr.write("agenttalk relay operator-command: --kind must be question or message.\n")
        return 2
    # FAIL CLOSED: only the configured operator-facing liaison relays an operator command
    # (the mechanical relay, not liaison memory). An audited override needs a reason.
    liaison = store.operator_facing()
    override = bool(getattr(args, "override", False))
    if sender != liaison:
        if not override:
            sys.stderr.write(
                f"agenttalk relay operator-command: {sender!r} is not the current "
                f"operator-facing liaison ({liaison!r}); relaying an operator command "
                f"requires the configured liaison (or --override --reason for an audited "
                f"exception).\n")
            return 2
        if not (getattr(args, "reason", None) or "").strip():
            sys.stderr.write("agenttalk relay operator-command: --override requires --reason "
                             "(an audited exception must record why).\n")
            return 2
    # Resolve --to: infer ONLY when exactly one managed lead-loop exists, else require it.
    managed = sorted(a for a in store.managed_lead_loop_agents()
                     if store.is_managed_lead_loop(a))
    if args.to:
        try:
            validate_agent_name(args.to)
        except ValueError as e:
            sys.stderr.write(f"agenttalk relay operator-command: {e}\n")
            return 2
        _ensure_in_roster(args.to, roster, label="operator-command target")
        target = args.to
    elif len(managed) == 1:
        target = managed[0]
    elif not managed:
        sys.stderr.write("agenttalk relay operator-command: no managed lead-loop is "
                         "configured; pass --to <agent>.\n")
        return 2
    else:
        sys.stderr.write(f"agenttalk relay operator-command: {len(managed)} managed lead-loops "
                         f"({managed}); pass --to <agent> to disambiguate.\n")
        return 2
    if target == sender:
        sys.stderr.write(f"agenttalk relay operator-command: target {target!r} is the sender - "
                         f"a liaison does not relay a command to itself.\n")
        return 2
    meta = _parse_meta(args.meta)
    if "request_id" in meta:
        # operator-command OWNS its correlation id - reject a caller-supplied one outright
        # (codex WP4 MAJOR). Otherwise a spontaneous command could GRAFT onto an existing
        # thread (question) or a fire-and-forget message could carry a tracked id. The
        # command-owned contract: a question always mints a FRESH opc- id; a message has none.
        sys.stderr.write("agenttalk relay operator-command: --meta request_id is not allowed "
                         "- the command owns its correlation id (a question mints a fresh "
                         "opc- id; a message has none).\n")
        return 2
    for k in _RELAY_RESERVED_META:
        meta.pop(k, None)                            # SCRUB: handler-authoritative audit meta
    meta["operator_command"] = "true"
    meta["operator_origin"] = sender
    if override and sender != liaison:
        # the audited-exception markers are stamped ONLY on the real --override path, so a
        # caller --meta can never forge an authz exception on a normal command (P3).
        meta["operator_command_override"] = "true"
        meta["override_reason"] = args.reason
    # A question opens its OWN tracked thread (the lead-loop's response correlates back);
    # a message is fire-and-forget with no id.
    if kind == "question":
        meta["request_id"] = "opc-" + uuid.uuid4().hex[:12]
    msg = store.send(sender=sender, recipient=target, kind=kind,
                     subject=args.subject or "operator command", body=body, meta=meta)
    if not args.quiet:
        print(render(msg, header=f"AGENTTALK :: RELAY operator-command  {sender} -> {target}"))
    if meta.get("request_id"):
        print(f"request_id={meta['request_id']}")
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    """Send a `proposal`: a concrete solution/approach for the peer to
    accept / reject / counter.

    Distinct from `question` (open-ended) and `review-request` (review
    of existing work). Auto-mints a `pp-` request_id so the peer's
    `proposal-response` can be correlated by `agenttalk threads`. The
    peer replies with `agenttalk reply --kind proposal-response --meta
    status=accepted|rejected|countered`; a counter is a fresh proposal
    sent with `--in-reply-to <this id>`.
    """
    store = _get_store(args)
    cfg = store.load_config()
    roster = cfg.get("agents") or []
    sender = _resolve_self(args.sender, roster=roster)
    recipient = _resolve_peer(args.recipient, cfg, sender)
    body = _read_body(args)
    if not body:
        # A proposal with no body is meaningless and would still open a
        # tracked thread, so — unlike send/reply — there is no
        # --allow-empty escape hatch here.
        sys.stderr.write(
            "agenttalk propose: empty body (use -m TEXT, --file PATH, or pipe "
            "stdin).\n"
            "  A proposal should state: Problem / Proposed solution / "
            "Alternatives considered / Tradeoffs / Decision requested.\n"
        )
        return 2
    meta = _parse_meta(args.meta)
    if args.in_reply_to:
        meta.setdefault("in_reply_to", args.in_reply_to)
    _maybe_autogen_request_id("proposal", meta, quiet=args.quiet)
    msg = store.send(
        sender=sender,
        recipient=recipient,
        body=body,
        kind="proposal",
        subject=args.subject or "",
        meta=meta,
    )
    if not args.quiet:
        print(render(msg, header=f"AGENTTALK :: PROPOSAL  {msg.sender} -> {msg.recipient}"))
    if args.print_id:
        # Print the correlation id (the "proposal id"), not the message
        # id — that's the token a counter references via --in-reply-to.
        print(msg.meta.get("request_id", msg.id))
    return 0


def cmd_broadcast(args: argparse.Namespace) -> int:
    """Send one message to a whole group (or `--all`) via fan-out.

    Resolves the audience to concrete recipients and writes ONE
    point-to-point message per member (excluding the sender), all sharing
    a `b-` `broadcast_id` and an `audience` label. The per-agent
    mailbox/cursor model is untouched — each member just receives their
    own copy. `--kind question` creates a tracked obligation per
    recipient (the "everyone please weigh in" pattern), visible as a
    multi-party thread in `agenttalk threads`; `note`/`message` are FYI
    fan-out with no obligation. "Reply-all" is just another broadcast.
    """
    store = _get_store(args)
    cfg = store.load_config()
    roster = cfg.get("agents") or []
    sender = _resolve_self(args.sender, roster=roster)
    # --resume <bid> (#16, 0.15.0): the one-command recovery for a partial
    # fan-out. Reconstructs the batch from the FROZEN copies themselves
    # (kind, subject, body, and every frozen meta key come from an
    # existing copy - the plan was sealed at first send) and writes only
    # the missing copies. Broadcaster-only.
    resume = getattr(args, "resume", None)
    if resume:
        if (args.message or getattr(args, "file", None) or args.subject
                or args.kind != "message" or args.meta
                or getattr(args, "response_policy", None)
                or getattr(args, "response_quorum", None) is not None):
            sys.stderr.write(
                "agenttalk broadcast: --resume re-sends the ORIGINAL frozen "
                "copies - it takes no body/subject/kind/meta overrides.\n")
            return 2
        copies = [m for m in store.valid_messages()
                  if (m.meta or {}).get("broadcast_id") == resume]
        if not copies:
            sys.stderr.write(
                f"agenttalk broadcast: no broadcast {resume!r} is visible - "
                f"check the id (`agenttalk threads --for {sender}`).\n")
            return 2
        proto = copies[0]
        if proto.sender != sender:
            sys.stderr.write(
                f"agenttalk broadcast: only the broadcaster "
                f"({proto.sender!r}) may resume batch {resume!r}.\n")
            return 2
        resolved = [x for x in
                    ((proto.meta or {}).get("audience_resolved") or "").split(",") if x]
        if not resolved:
            sys.stderr.write(
                f"agenttalk broadcast: batch {resume!r} carries no frozen "
                f"audience (pre-0.15.0 broadcast) - re-send by hand with "
                f"--meta request_id/broadcast_id/audience set.\n")
            return 2
        existing = {m.recipient for m in copies}
        missed = [r for r in resolved if r not in existing]
        if not missed:
            # Already complete. Honor --json so a consumer polling resume
            # always gets parseable stdout (0.18.0 fresh-eyes M1).
            if getattr(args, "json", False):
                print(json.dumps({"batch_id": resume,
                                  "delivered": sorted(existing),
                                  "missed": []}, indent=2))
            else:
                print(f"(batch {resume} complete - nothing to resume)")
            return 0
        # 0.18.0 (FR-005): a frozen recipient that has since been retired can
        # never receive — `store.send` refuses it. Skip-and-report it as
        # `dropped` instead of trapping the broadcaster at a permanent exit 5
        # ("resume again" could never succeed). Only genuinely-active missing
        # copies are (re)sent.
        retired = set(store.retired_agents())
        dropped = [r for r in missed if r in retired]
        to_send = [r for r in missed if r not in retired]
        if not to_send:
            # Everything still missing is retired → nothing to send; the
            # batch is as complete as it can ever be. Resolve (exit 0).
            delivered = sorted(existing)
            manifest = {"batch_id": resume, "delivered": delivered, "missed": []}
            if dropped:
                manifest["dropped"] = sorted(dropped)
            if getattr(args, "json", False):
                # --json: ONLY a parseable JSON object on stdout (no human line).
                print(json.dumps(manifest, indent=2))
            elif not args.quiet:
                print(f"delivered=[{', '.join(delivered)}]")
                print(f"dropped=[{', '.join(sorted(dropped))}]  (retired)")
                print(f"(batch {resume} resolved: all remaining recipients "
                      f"are retired and were dropped)")
            return 0
        sent_resume: list = []
        failure: Exception | None = None
        for r in to_send:
            try:
                msg = store.send(
                    sender=sender, recipient=r, body=proto.body,
                    kind=proto.kind, subject=proto.subject,
                    meta=dict(proto.meta or {}),
                )
            except Exception as e:  # noqa: BLE001 - account every failure
                failure = e
                break
            sent_resume.append(msg)
        if failure is not None:
            delivered = sorted(existing | {m.recipient for m in sent_resume})
            # still_missed = ACTIVE recipients we failed to (re)send; retired
            # names are reported under `dropped`, never as a partial-failure.
            still_missed = [r for r in to_send if r not in delivered]
            manifest = {"batch_id": resume, "delivered": delivered,
                        "missed": still_missed}
            if dropped:
                manifest["dropped"] = sorted(dropped)
            if getattr(args, "json", False):
                print(json.dumps(manifest, indent=2))
            else:
                print(f"delivered=[{', '.join(delivered)}]")
                print(f"missed=[{', '.join(still_missed)}]")
                if dropped:
                    print(f"dropped=[{', '.join(sorted(dropped))}]  (retired)")
            sys.stderr.write(
                f"agenttalk broadcast: resume of {resume} STILL partial - "
                f"copy for {still_missed[0]!r} failed: {failure}\n")
            return 5
        # Success: all active copies sent. Build ONE manifest; --json emits
        # only parseable JSON, otherwise the friendly summary (respecting
        # --quiet). `missed` is empty here by definition; `dropped` carries
        # the retired skips so the JSON success path is contract-complete.
        delivered = sorted(existing | {m.recipient for m in sent_resume})
        manifest = {"batch_id": resume, "delivered": delivered, "missed": []}
        if dropped:
            manifest["dropped"] = sorted(dropped)
        if getattr(args, "json", False):
            print(json.dumps(manifest, indent=2))
        elif not args.quiet:
            msg = (f"(batch {resume} resumed: {len(sent_resume)} missing "
                   f"cop{'ies' if len(sent_resume) != 1 else 'y'} sent: "
                   f"{', '.join(m.recipient for m in sent_resume)}")
            if dropped:
                msg += f"; dropped retired: {', '.join(sorted(dropped))}"
            print(msg + ")")
        return 0
    # Audience resolution: role targets (#15, 0.15.0) resolve via the
    # roles map; groups/all via the existing resolver. Explicit flags —
    # never an implicit fallback from one map to the other (a role/group
    # name collision must stay unambiguous).
    to_role = getattr(args, "to_role", None)
    if to_role is not None and not to_role.strip():
        sys.stderr.write("agenttalk broadcast: --to-role needs a role name.\n")
        return 2
    if to_role:
        target = to_role
        audience_kind = "role"
        try:
            recipients = store.resolve_role_audience(to_role, exclude=sender)
        except ValueError as e:
            sys.stderr.write(f"agenttalk broadcast: {e}\n")
            return 2
    else:
        target = "all" if args.all else args.to_group
        audience_kind = "all" if args.all else "group"
        try:
            recipients = store.resolve_audience(target, exclude=sender)
        except ValueError as e:
            sys.stderr.write(f"agenttalk broadcast: {e}\n")
            return 2
    if not recipients:
        sys.stderr.write(
            f"agenttalk broadcast: audience '{target}' has no recipients "
            f"besides {sender}.\n"
        )
        return 2
    body = _read_body(args)
    if not body and not args.allow_empty:
        sys.stderr.write(
            "agenttalk broadcast: empty body (use -m TEXT, --file PATH, pipe "
            "stdin, or --allow-empty)\n"
        )
        return 2
    meta_base = _parse_meta(args.meta)
    # broadcast OWNS the correlation id: request_id and broadcast_id are
    # always the SAME value, so the id we print is exactly what recipients
    # echo with `reply --to-request`. Pop any user-supplied keys (a stale
    # request_id would otherwise desync the thread from the printed id) and
    # reject a conflicting pair.
    supplied_bid = meta_base.pop("broadcast_id", None)
    supplied_rid = meta_base.pop("request_id", None)
    if supplied_bid and supplied_rid and supplied_bid != supplied_rid:
        sys.stderr.write(
            "agenttalk broadcast: --meta request_id and --meta broadcast_id "
            "must be the same value (a broadcast uses one correlation id).\n"
        )
        return 2
    bid = supplied_bid or supplied_rid or ("b-" + uuid.uuid4().hex[:12])
    response_policy = getattr(args, "response_policy", None)
    response_quorum = getattr(args, "response_quorum", None)
    if args.kind == "question":
        response_policy = response_policy or "each"
        if response_policy == "quorum":
            if response_quorum is None or not 1 <= response_quorum <= len(recipients):
                sys.stderr.write(
                    "agenttalk broadcast: --response-policy quorum requires "
                    "--response-quorum N within the frozen audience size.\n"
                )
                return 2
        elif response_quorum is not None:
            sys.stderr.write(
                "agenttalk broadcast: --response-quorum is valid only with "
                "--response-policy quorum.\n"
            )
            return 2
    elif response_policy is not None or response_quorum is not None:
        sys.stderr.write(
            "agenttalk broadcast: response policy applies only to --kind question.\n"
        )
        return 2
    audience_label = "all" if args.all else target
    sent: list = []
    # Delivery accounting (#16, 0.15.0): every copy freezes the fan-out
    # facts at send time (audience kind/label/members + batch_total) —
    # display/audit/incompleteness data, never an obligation source
    # (derivation stays opener-copy-based). The loop is wrapped so a
    # mid-fan-out failure reports EXACTLY who got the message and who
    # did not (exit 5) instead of dying silently partway — there is no
    # multi-file atomicity on a local FS, so honesty replaces rollback.
    failure: Exception | None = None
    # B3 (#19): snapshot the global epoch ONCE for the whole fan-out, so every
    # copy of this broadcast_id shares one epoch_at_send even if a barrier lands
    # mid-loop (send() leaves a supplied value intact; --resume preserves it
    # from the frozen copies). Only opener kinds carry an epoch stamp.
    epoch_snapshot = store.current_epoch() if args.kind in OPENER_KINDS else None
    for r in recipients:
        meta = dict(meta_base)
        # Reuse request_id as the correlation token (so existing
        # reply/threads machinery works) AND tag the broadcast so thread
        # derivation switches to its multi-party view. Both keys are
        # force-set to the one id — never setdefault (issue: a supplied
        # request_id could split the thread key from the printed id).
        meta["request_id"] = bid
        meta["broadcast_id"] = bid
        meta["audience"] = audience_label
        meta["audience_kind"] = audience_kind
        if audience_kind == "role":
            meta["audience_role"] = target
        meta["audience_resolved"] = ",".join(recipients)
        meta["batch_total"] = str(len(recipients))
        if args.kind == "question":
            meta["broadcast_policy_version"] = 1
            meta["membership_snapshot"] = list(recipients)
            meta["response_policy"] = response_policy
            if response_policy == "quorum":
                meta["response_quorum"] = response_quorum
        if args.kind in OPENER_KINDS:
            # one epoch for the whole batch (B3); pre-set so send() does not
            # recompute current_epoch() per copy.
            meta["epoch_at_send"] = epoch_snapshot
        try:
            msg = store.send(
                sender=sender, recipient=r, body=body,
                kind=args.kind, subject=args.subject or "", meta=meta,
            )
        except Exception as e:  # noqa: BLE001 — any failure must be accounted
            failure = e
            break
        sent.append(msg)
    if failure is not None:
        delivered = [m.recipient for m in sent]
        missed = [r for r in recipients if r not in delivered]
        if getattr(args, "json", False):
            print(json.dumps({"batch_id": bid, "delivered": delivered,
                              "missed": missed}, indent=2))
        else:
            print(f"delivered=[{', '.join(delivered)}]")
            print(f"missed=[{', '.join(missed)}]")
        if delivered:
            sys.stderr.write(
                f"agenttalk broadcast: PARTIAL fan-out — copy for "
                f"{missed[0]!r} failed: {failure}\n"
                f"  Recover with `agenttalk broadcast --from {sender} "
                f"--resume {bid}` (re-sends the missing frozen copies), or "
                f"rescind the thread (`agenttalk rescind --to-request {bid}`).\n"
            )
        else:
            # Zero copies landed: there is nothing on disk to resume or
            # rescind (fresh-eyes 0.15.0 note 1) — the only recovery is
            # re-running the broadcast itself.
            sys.stderr.write(
                f"agenttalk broadcast: fan-out failed before ANY copy was "
                f"written ({failure}) — nothing to resume; re-run the "
                f"broadcast.\n"
            )
        return 5
    if not args.quiet:
        print(
            f"(broadcast {bid} [{args.kind}] {sender} -> @{audience_label}: "
            f"{len(sent)} recipient{'s' if len(sent) != 1 else ''}: "
            f"{', '.join(recipients)})"
        )
    if args.print_id:
        print(bid)
    return 0


def cmd_barrier(args: argparse.Namespace) -> int:
    """Fire a global epoch barrier (#19 Phase A, RFC §"Global Epochs").

    A barrier is ONE ordinary, self-addressed `kind=message` carrying
    `meta.barrier={version,scope:global,type:epoch-bump}` — NOT a new kind, NOT
    a fan-out — whose message id becomes the new global epoch. Any ACTIVE
    roster member may bump (a deliberate trusted-team global-stall lever);
    `_resolve_self` already refuses a retired/unknown sender with exit 2.
    Agents then run `check --to-request <rid> --epoch` before acting on
    anything opened before this point.
    """
    store = _get_store(args)
    action = getattr(args, "barrier_cmd", None)
    if action != "bump":
        sys.stderr.write("agenttalk barrier: the only action is `bump`.\n")
        return 2
    scope = getattr(args, "scope", "global") or "global"
    if scope != "global":
        sys.stderr.write(
            f"agenttalk barrier bump: --scope must be 'global' in this release "
            f"(got {scope!r}); other scopes are reserved.\n")
        return 2
    sender = _resolve_self(getattr(args, "from_agent", None),
                           roster=store.load_config().get("agents") or [])
    msg = store.send(
        sender=sender, recipient=sender, kind="message",
        subject="epoch bump", body=getattr(args, "message", None) or "",
        meta={"barrier": {"version": 1, "scope": "global", "type": "epoch-bump"}},
    )
    if getattr(args, "json", False):
        print(json.dumps({"epoch": msg.id, "scope": "global"}, indent=2))
    else:
        print(f"barrier: global epoch bumped — new epoch id {msg.id}")
        print("  run `agenttalk check --to-request <rid> --epoch` before acting "
              "on anything opened before this point.")
    return 0


def _roster_expertise(store, *, json_out: bool) -> int:
    """Per-domain expertise DERIVED from existing evidence (no expertise registry):
    strong = domain owners/reviewers/curators (domains.json) + lane-delivery history
    by domain; weak = CURATED note authors by domain (raw uncurated notes are
    gameable volume, excluded). Knowledge dependency on Phase 0 domains."""
    from agenttalk import knowledge as kn
    cfg = store.load_config()
    reg = _load_domain_registry(store)
    domains = reg.data.get("domains") or {}
    # lane delivery history by domain (who actually shipped)
    delivered: dict[str, list[str]] = {}
    ddir = store.dir / "lane-deliveries"
    if ddir.exists():
        for f in sorted(ddir.glob("*.json")):
            try:
                art = json.loads(f.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            d = art.get("domain_id")
            who = art.get("assignee")
            if isinstance(d, str) and isinstance(who, str):
                delivered.setdefault(d, []).append(who)
    # curated note authors by domain (WEAK secondary signal; raw uncurated notes are
    # gameable volume, excluded). C4a: use the CURATED view, not latest - so a later
    # uncurated publish for the same (domain_id,key) cannot erase the verified author's
    # credit (the curated event survives in resolve_views even when latest is uncurated).
    events, _ = kn.read_events(store)
    curated_authors: dict[str, dict] = {}
    for rec in kn.resolve_views(events).values():
        note = rec.get("curated")
        if note is None or rec.get("tombstoned") or kn.is_retracted(note):
            continue
        if note.get("type") == kn.TYPE_LESSON:
            continue
        d = note.get("domain_id")
        a = note.get("author")
        if isinstance(d, str) and isinstance(a, str):
            curated_authors.setdefault(d, {}).setdefault(a, 0)
            curated_authors[d][a] += 1
    out = {}
    for did, dentry in sorted(domains.items()):
        out[did] = {
            "owners": dom.resolve_refset(dentry.get("owners") or {}, cfg),
            "reviewers": dom.resolve_refset(dentry.get("reviewers") or {}, cfg),
            "curators": dom.resolve_refset(dentry.get("curators") or {}, cfg),
            "delivered_lanes": sorted(set(delivered.get(did, []))),
            "curated_notes_by": curated_authors.get(did, {}),
        }
    if json_out:
        print(json.dumps(out, indent=2))
        return 0
    if not out:
        print("expertise: no domains defined (author .agenttalk/domains.json)")
        return 0
    print(f"expertise by domain ({len(out)}):")
    for did, e in out.items():
        print(f"  {did}: owners={e['owners'] or '-'} reviewers={e['reviewers'] or '-'} "
              f"curators={e['curators'] or '-'}")
        if e["delivered_lanes"]:
            print(f"      shipped lanes: {', '.join(e['delivered_lanes'])}")
        if e["curated_notes_by"]:
            print(f"      curated notes: {', '.join(f'{a}×{n}' for a, n in e['curated_notes_by'].items())}")
    return 0


def cmd_avatar(args: argparse.Namespace) -> int:
    """List and set display-avatar preferences."""
    store = _get_store(args)
    action = getattr(args, "avatar_cmd", None)
    if action == "list":
        items = avatar_mod.available_avatars()
        if getattr(args, "json", False):
            print(json.dumps({"avatars": items}, indent=2))
            return 0
        print(f"avatars ({len(items)}):")
        width = max((len(item["id"]) for item in items), default=14)
        groups = [
            ("", "Originals"),
            ("hexagon", "hexagon"),
            ("oval-muted", "oval-muted"),
            ("oval-vivid", "oval-vivid"),
            ("rounded-square", "rounded-square"),
            ("star", "star"),
            ("triangle", "triangle"),
        ]
        for shape, label in groups:
            grouped = [item for item in items if item.get("shape", "") == shape]
            if not grouped:
                continue
            print(f"  {label}:")
            for item in grouped:
                print(f"    {item['id']:<{width}} {item['file']}")
        return 0
    if action == "set":
        cfg = store.load_config()
        agent = _resolve_self(getattr(args, "from_agent", None),
                              roster=cfg.get("agents") or [])
        store.set_avatar(agent, args.avatar_id)
        print(f"avatar: {agent} -> {avatar_mod.normalize_avatar_id(args.avatar_id)}")
        return 0
    if action == "clear":
        cfg = store.load_config()
        agent = _resolve_self(getattr(args, "from_agent", None),
                              roster=cfg.get("agents") or [])
        store.clear_avatar(agent)
        print(f"avatar: cleared {agent}")
        return 0
    if action == "set-operator":
        store.set_operator_avatar(args.avatar_id)
        print(
            f"avatar: {avatar_mod.OPERATOR_PRINCIPAL} -> "
            f"{avatar_mod.normalize_avatar_id(args.avatar_id)}"
        )
        return 0
    if action == "clear-operator":
        store.clear_operator_avatar()
        print(f"avatar: cleared {avatar_mod.OPERATOR_PRINCIPAL}")
        return 0
    sys.stderr.write("agenttalk avatar: choose list, set, clear, set-operator, or clear-operator\n")
    return 2


def cmd_roster(args: argparse.Namespace) -> int:
    """View or manage the roster, roles, and groups.

    `roster` (no subcommand) shows the team; `add` / `remove` /
    `set-role` / `set-group` are deliberate local admin ops (not a
    security boundary, not process supervision).
    """
    store = _get_store(args)
    action = getattr(args, "roster_cmd", None)
    if action == "add":
        name = args.name
        # Validate BEFORE any state-file probe: agent_active()/suggest read
        # .agenttalk/state/<name>.* paths, so an unsafe name must be rejected up
        # front (codex-reviewer-1 r1 - no path interpolation of a raw name).
        validate_agent_name(name)
        already = name in (store.load_config().get("agents", []) or [])
        active = store.agent_active(name)
        if getattr(args, "unique", False) and active:
            # FRESH self-join guard: refuse to adopt a name a LIVE agent holds,
            # and suggest a free variant. Exit 3 (distinct from usage exit 2) so a
            # skill/automation can branch and adopt the suggestion.
            suggested = store.suggest_unique_name(name)
            if getattr(args, "json", False):
                print(json.dumps({"refused": True, "active_holder": name,
                                  "suggested": suggested}, indent=2))
            else:
                sys.stderr.write(
                    f"agenttalk roster add --unique: {name!r} is an ACTIVE identity "
                    f"(fresh heartbeat or a live waiter) - refusing to re-bind it. "
                    f"Join as {suggested!r} instead (set $AGENTTALK_SELF to it).\n")
            return 3
        store.add_agent(name, role=getattr(args, "role", None),
                        groups=getattr(args, "group", None),
                        trust_class=getattr(args, "trust_class", None))
        if already and active:
            # plain (idempotent) add that re-binds a name a LIVE agent holds:
            # non-fatal warning (catches the rejoin/re-init-misuse path) - the
            # add still succeeds (exit 0) so deliberate re-init is unaffected.
            sys.stderr.write(
                f"WARNING: {name!r} already has a LIVE owner (fresh heartbeat or a "
                f"live waiter); `roster add` is idempotent so this did NOT create a "
                f"second identity, but if you are a NEW agent use "
                f"`roster add <name> --unique` to claim a unique name.\n")
        print(f"roster: added {name}")
        return 0
    if action == "remove":
        # FR-007: refuse by default with a retire hint; --force removes
        # mechanically (no tombstone) and warns about history-read breakage.
        # The store primitive stays mechanical; this is the policy/UX layer.
        if not getattr(args, "force", False):
            sys.stderr.write(
                f"agenttalk roster remove: removing {args.name!r} breaks "
                f"historical readability for its messages. Use "
                f"`agenttalk roster retire {args.name}` to keep history valid "
                f"(recommended), or pass --force to remove anyway.\n")
            return 2
        store.remove_agent(args.name)
        sys.stderr.write(
            f"WARNING: removed {args.name!r} with --force; its historical "
            f"messages will now FAIL roster validation (no tombstone kept). "
            f"`roster retire` is the safe alternative.\n")
        print(f"roster: removed {args.name}")
        return 0
    if action == "retire":
        # #19 Phase A: permanent tombstone. ValueError (not active / already
        # retired) -> exit 2 via main().
        cfg2 = store.retire_agent(args.name, reason=getattr(args, "reason", None))
        if getattr(args, "json", False):
            print(json.dumps({"retired": cfg2.get("retired", [])}, indent=2))
            return 0
        print(f"roster: retired {args.name} — permanent tombstone; its name "
              f"cannot be re-bound and its history stays valid.")
        return 0
    if action == "rename":
        old, new = args.old, args.new
        if getattr(args, "drain_check", False):
            owed = store._drain_check(old)
            if owed:
                sys.stderr.write(
                    f"agenttalk roster rename: {old!r} still has "
                    f"{len(owed)} open thread(s) owed to/from it:\n")
                for r in owed:
                    sys.stderr.write(
                        f"  {r.get('request_id')}  {r.get('state')}  "
                        f"(peer {r.get('peer')})\n")
                sys.stderr.write(
                    "drain them, or omit --drain-check to rename anyway.\n")
                return 2
        store.rename_agent(old, new, reason=getattr(args, "reason", None))
        print(f"roster: renamed {old} -> {new} ({old} is now a tombstone; "
              f"role / groups / operator-facing carried over to {new}).")
        return 0
    if action == "forward":
        # B4: forward a specific owed request from a retired identity to a live
        # agent. ValueError (not retired / not active / not owed / second hop /
        # missing sender) -> exit 2 via main().
        msg = store.forward_retired(
            args.name, args.to, args.to_request,
            from_agent=getattr(args, "from_agent", None),
            reason=getattr(args, "reason", None))
        print(f"roster: forwarded {args.name}'s request {args.to_request} "
              f"to {args.to} (sender {msg.sender}; note {msg.id}).")
        return 0
    if action == "set-role":
        demoted = store.set_role(args.name, args.role)
        # At-most-one-lead invariant: if assigning lead moved it off another
        # agent, say so in one line — no --force two-step (0.24.0, feedback 3.1).
        if demoted:
            print(f"roster: demoted {', '.join(demoted)}, promoted "
                  f"{args.name} to {args.role}")
        else:
            print(f"roster: {args.name} role={args.role}")
        return 0
    if action == "set-group":
        members = [m.strip() for m in args.members.split(",") if m.strip()]
        store.set_group(args.group, members)
        print(f"roster: group '{args.group}' = {', '.join(members) or '(empty)'}")
        return 0
    if action == "set-trust-class":
        value = None if getattr(args, "clear", False) else args.trust_class
        if value is None and not getattr(args, "clear", False):
            sys.stderr.write(
                "agenttalk roster set-trust-class: provide a trust class or --clear\n"
            )
            return 2
        store.set_trust_class(args.name, value)
        print(f"roster: {args.name} trust_class={value or '(native/default)'}")
        return 0
    if action == "set-operator-facing":
        # Single-slot designation (#18): "two liaisons" is unrepresentable.
        # Advisory routing metadata only — never authorization (C-007).
        if getattr(args, "clear", False):
            if getattr(args, "name", None):
                sys.stderr.write(
                    "agenttalk roster set-operator-facing: pass a name OR "
                    "--clear, not both\n")
                return 2
            store.set_operator_facing(None)
            print("roster: operator-facing cleared")
            return 0
        if not getattr(args, "name", None):
            sys.stderr.write(
                "agenttalk roster set-operator-facing: need an agent name "
                "(or --clear)\n")
            return 2
        store.set_operator_facing(args.name)  # ValueError -> exit 2 via main()
        print(f"roster: {args.name} is now operator-facing (the liaison)")
        return 0

    if action is None and getattr(args, "expertise", False):
        return _roster_expertise(store, json_out=getattr(args, "json", False))

    # show
    cfg = store.load_config()
    roster = cfg.get("agents", []) or []
    roles = cfg.get("roles", {}) or {}
    groups = cfg.get("groups", {}) or {}
    trust_classes = cfg.get("trust_classes", {}) or {}
    liaison = store.operator_facing()
    self_name = os.environ.get("AGENTTALK_SELF")
    if self_name not in roster:
        self_name = None
    if getattr(args, "json", False):
        print(json.dumps({
            "agents": roster,
            "roles": roles,
            "groups": groups,
            "trust_classes": trust_classes,
            "operator_facing": liaison,
            "self": self_name,
        }, indent=2))
        return 0
    print(f"roster ({len(roster)} agent{'s' if len(roster) != 1 else ''}):")
    for a in roster:
        you = " (you)" if a == self_name else ""
        role = roles.get(a) or "-"
        member_of = [g for g, ms in groups.items() if a in ms]
        gl = ", ".join(member_of) if member_of else "-"
        of = "  [operator-facing]" if a == liaison else ""
        trust = trust_classes.get(a) or "native/default"
        print(f"  {a}{you}  role={role}  trust={trust}  groups=[{gl}]{of}")
    if groups:
        print("groups:")
        for g, ms in groups.items():
            print(f"  @{g}: {', '.join(ms) or '(empty)'}")
    print(f"  @all: {', '.join(roster)}  (implicit)")
    return 0


def _load_domain_registry(store: Store) -> dom.Registry:
    return dom.load_registry(store.dir / dom.FILENAME, store.load_config())


def _domain_ref_text(label: str, refset: dict, cfg: dict) -> str:
    parts: list[str] = []
    for key in ("agents", "groups", "roles"):
        values = refset.get(key) or []
        if values:
            parts.append(f"{key}={','.join(values)}")
    resolved = dom.resolve_refset(refset, cfg)
    suffix = f" -> {', '.join(resolved)}" if resolved else ""
    return f"{label}: {'; '.join(parts) if parts else '-'}{suffix}"


def cmd_domain(args: argparse.Namespace) -> int:
    """View and validate the durable domain registry.

    Phase 0 is intentionally read-only: users can hand-edit
    ``.agenttalk/domains.json`` and use these pure-core commands to inspect and
    validate it. Lane/knowledge/lease mutation is deliberately out of scope.
    """
    store = _get_store(args)
    registry = _load_domain_registry(store)
    cfg = store.load_config()
    action = getattr(args, "domain_cmd", None) or "list"
    domains = registry.data["domains"]

    if action == "validate":
        payload = {
            "valid": True,
            "source_exists": registry.source_exists,
            "path": str(registry.path),
            "registry_hash": registry.registry_hash,
            "domain_count": len(domains),
            "shared_path_count": len(registry.data["shared_paths"]),
        }
        if getattr(args, "json", False):
            print(json.dumps(payload, indent=2))
        else:
            source = str(registry.path) if registry.source_exists else f"{registry.path} (missing; empty registry)"
            print(
                f"domain registry: valid ({payload['domain_count']} domains, "
                f"{payload['shared_path_count']} shared paths)"
            )
            print(f"  source: {source}")
            print(f"  hash: {registry.registry_hash}")
        return 0

    if action == "list":
        items = [
            {
                "id": domain_id,
                "title": entry["title"],
                "owned_glob_count": len(entry["owned_globs"]),
            }
            for domain_id, entry in domains.items()
        ]
        if getattr(args, "json", False):
            print(json.dumps({
                "registry_hash": registry.registry_hash,
                "source_exists": registry.source_exists,
                "domains": items,
            }, indent=2))
        else:
            print(f"domains ({len(items)})  hash={registry.registry_hash}")
            if not registry.source_exists:
                print(f"  {registry.path} is missing; showing an empty registry")
            for item in items:
                print(
                    f"  {item['id']}  {item['title']} "
                    f"({item['owned_glob_count']} owned glob"
                    f"{'s' if item['owned_glob_count'] != 1 else ''})"
                )
        return 0

    if action == "show":
        domain_id = args.domain_id
        if domain_id not in domains:
            raise ValueError(f"unknown domain {domain_id!r} (known: {sorted(domains)})")
        entry = domains[domain_id]
        if getattr(args, "json", False):
            payload = dict(entry)
            payload["id"] = domain_id
            payload["resolved"] = {
                "owners": dom.resolve_refset(entry["owners"], cfg),
                "reviewers": dom.resolve_refset(entry["reviewers"], cfg),
                "curators": dom.resolve_refset(entry["curators"], cfg),
            }
            print(json.dumps(payload, indent=2))
        else:
            print(f"domain {domain_id}: {entry['title']}")
            if entry.get("description"):
                print(f"  description: {entry['description']}")
            print(f"  {_domain_ref_text('owners', entry['owners'], cfg)}")
            print(f"  {_domain_ref_text('reviewers', entry['reviewers'], cfg)}")
            print(f"  {_domain_ref_text('curators', entry['curators'], cfg)}")
            print("  owned_globs:")
            for glob in entry["owned_globs"]:
                print(f"    - {glob}")
        return 0

    if action == "check-path":
        verdicts = dom.check_paths(
            registry.data, args.paths, casefold_paths=getattr(args, "casefold_paths", None),
        )
        if getattr(args, "json", False):
            print(json.dumps({
                "registry_hash": registry.registry_hash,
                "paths": verdicts,
            }, indent=2))
        else:
            for verdict in verdicts:
                if verdict["overlap"]:
                    status = "OVERLAP"
                elif verdict["owned"]:
                    status = "owned"
                else:
                    status = "UNOWNED"
                bits: list[str] = []
                if verdict["domains"]:
                    bits.append("domains=" + ",".join(verdict["domains"]))
                if verdict["shared_paths"]:
                    shared = [
                        f"{m['glob']}[{m['category']}:{m['requires']}]"
                        for m in verdict["shared_paths"]
                    ]
                    bits.append("shared=" + ",".join(shared))
                if verdict["casefold_paths"]:
                    bits.append("casefold=true")
                suffix = f"  {'; '.join(bits)}" if bits else ""
                print(f"{verdict['path']}: {status}{suffix}")
        return 0

    raise ValueError(f"unknown domain action {action!r}")


def _stdout_is_fifo() -> bool:
    """Return True only when stdout can be positively identified as a pipe."""
    try:
        return stat.S_ISFIFO(os.fstat(sys.stdout.fileno()).st_mode)
    except (AttributeError, OSError, TypeError, ValueError):
        # Capture streams and other file-like objects may not expose a usable fd.
        # Preserve their existing behavior; only a confirmed FIFO is unsafe.
        return False


def _guard_unbounded_consuming_pipe(*, ack: bool, limit: int | None) -> None:
    if ack and limit is None and _stdout_is_fifo():
        raise ValueError(
            "refusing an unbounded consuming read on a pipe; "
            "pass --limit N (or -n N) and consume that bounded page directly"
        )


def _recv_prefix_length(
    kinds: list[str],
    *,
    include_control: bool,
    limit: int | None,
) -> int:
    """Length of the raw prefix ending at the Nth surfaced message."""
    if limit is None:
        return len(kinds)
    visible = 0
    for index, kind in enumerate(kinds):
        if include_control or kind not in CONTROL_KINDS:
            visible += 1
            if visible == limit:
                return index + 1
    return len(kinds)


def _write_stdout_line(text: str) -> None:
    """Write and flush one complete output record or raise before it is acked."""
    payload = f"{text}\n"
    written = sys.stdout.write(payload)
    if written != len(payload):
        raise OSError(f"short write to stdout: wrote {written!r} of {len(payload)} characters")
    sys.stdout.flush()


def _do_recv(
    store: Store,
    agent: str,
    *,
    since: str | None,
    ack: bool,
    include_control: bool,
    quiet: bool,
    emit_hint: bool,
    limit: int | None,
) -> int:
    """Shared inbox-print path behind both `recv` and `drain`.

    `drain` is exactly `recv --ack` with the hint suppressed, so the
    two can never diverge (issue #5 constraint). `--ack` advances the
    cursor past the newest message INCLUDING hidden control-plane kinds
    (composing) even when nothing visible was printed — that's what
    lets `drain` clear a stale-control/cursor backlog.
    """
    _guard_unbounded_consuming_pipe(ack=ack, limit=limit)
    cursor = since if since is not None else store.cursor(agent)
    msgs = store.messages_for(agent, since_id=cursor or None)
    prefix_length = _recv_prefix_length(
        [message.kind for message in msgs],
        include_control=include_control,
        limit=limit,
    )
    msgs = msgs[:prefix_length]
    # Hide control-plane kinds (composing) from the default view — they
    # are wait-loop signals, not agent content. --include-control opts
    # back in for debugging.
    visible = msgs if include_control else [m for m in msgs if m.kind not in CONTROL_KINDS]
    if not visible:
        if not quiet:
            _write_stdout_line(f"(no new messages for {agent})")
        if ack and msgs:
            store.advance_cursor(agent, msgs[-1].id)
        return 0
    for m in visible:
        _write_stdout_line(
            render(m, header=f"AGENTTALK :: INBOX  {m.sender} -> {m.recipient}")
        )
        if ack:
            store.advance_cursor(agent, m.id)
    if ack:
        # Once the complete page is delivered, preserve the established behavior
        # of consuming any trailing hidden control messages in that raw snapshot.
        if msgs[-1].id != visible[-1].id:
            store.advance_cursor(agent, msgs[-1].id)
    elif emit_hint and not quiet:
        # Default recv only PEEKS — the cursor did not move, so these
        # same messages re-print on the next call and `unread` climbs
        # unbounded (the issue #5 footgun). Nudge toward the consuming
        # verbs. Suppressed when --since is used (intentional history
        # inspection) and when --ack already consumed.
        sys.stderr.write(
            f"hint: recv only peeks — cursor[{agent}] did NOT move. "
            f"Use `agenttalk drain --for {agent}` (or `recv --ack`) to "
            f"consume + advance.\n"
        )
    return 0


def _do_recv_json(
    store: Store,
    agent: str,
    *,
    since: str | None,
    ack: bool,
    include_control: bool,
    limit: int | None,
) -> int:
    """`recv --json`: a CLI MIRROR over the SAME in-process recv_api functions the
    wrapper uses - NOT a second implementation. It routes the cursor/floor/control
    semantics entirely through recv_api.records + recv_api.commit (no duplicated
    cursor logic here); --since / --include-control are knobs ON recv_api. The
    wrapper itself uses recv_api in-process and never shells this."""
    from .wrapper import recv_api

    _guard_unbounded_consuming_pipe(ack=ack, limit=limit)
    raw = recv_api.records(store, agent, since=since, include_control=True)
    prefix_length = _recv_prefix_length(
        [record["kind"] for record in raw],
        include_control=include_control,
        limit=limit,
    )
    raw = raw[:prefix_length]
    recs = raw if include_control else [
        record for record in raw if record["kind"] not in CONTROL_KINDS
    ]
    for rec in recs:
        _write_stdout_line(json.dumps(rec, ensure_ascii=False))
        if ack:
            recv_api.commit(store, agent, rec)
    if ack and raw and (not recs or raw[-1]["id"] != recs[-1]["id"]):
        # The one control-inclusive snapshot is also the commit authority. This
        # prevents a message arriving after output selection from being consumed.
        recv_api.commit(store, agent, raw[-1])
    return 0


def cmd_recv(args: argparse.Namespace) -> int:
    store = _get_store(args)
    agent = _resolve_self(args.agent, roster=store.load_config().get("agents") or [])
    blocked = _guard_lead_loop_consumer(store, agent, verb="recv")
    if blocked is not None:
        return blocked
    if getattr(args, "json", False):
        return _do_recv_json(store, agent, since=args.since, ack=args.ack,
                             include_control=args.include_control, limit=args.limit)
    return _do_recv(
        store,
        agent,
        since=args.since,
        ack=args.ack,
        include_control=args.include_control,
        quiet=args.quiet,
        limit=args.limit,
        # Only nudge for the plain inspecting recv. Explicit --since is
        # deliberate history browsing, so don't nag there.
        emit_hint=args.since is None,
    )


def cmd_drain(args: argparse.Namespace) -> int:
    """Print all unread for an agent AND advance the cursor to newest.

    The single obvious "consume my inbox" verb that issue #5 found
    missing. Mechanically identical to `recv --ack`: shares `_do_recv`
    with ack forced on, so it advances past hidden control messages too.
    """
    store = _get_store(args)
    agent = _resolve_self(args.agent, roster=store.load_config().get("agents") or [])
    blocked = _guard_lead_loop_consumer(store, agent, verb="drain")
    if blocked is not None:
        return blocked
    return _do_recv(
        store,
        agent,
        since=None,  # drain always consumes from the cursor forward
        ack=True,
        include_control=args.include_control,
        quiet=args.quiet,
        limit=args.limit,
        emit_hint=False,  # drain IS the remedy; never hint
    )


def _write_waiting_marker(
    store: Store, agent: str, *, cursor_at_start: str, timeout: float,
    deadline: float | None, wait_token: str | None = None,
    to_request: str | None = None, kind: str | None = None,
) -> None:
    """Best-effort write of the observational `.waiting` marker.

    Records who is blocked, since when, on what cursor, and (for a
    bounded wait) the current epoch deadline so `status` can tell a
    live wait from an orphaned file left by a crashed shell. Any write
    failure is swallowed — this is diagnostics, never correctness.
    """
    try:
        marker = {
            "agent": agent,
            "pid": os.getpid(),
            "since": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "cursor_at_start": cursor_at_start or "",
            "timeout_seconds": timeout,
            # epoch seconds; None when --timeout 0 (waits forever). Updated
            # in place when composing pings push the deadline out.
            "deadline_epoch": deadline,
        }
        if wait_token:
            marker["wait_token"] = wait_token
        if to_request:
            marker["to_request"] = to_request
        if kind:
            marker["kind"] = kind
        store.write_waiting(agent, marker)
    except OSError:
        pass


def _wait_was_superseded(store: Store, agent: str, wait_token: str) -> bool:
    try:
        return store.waiting_superseded(agent, wait_token) is not None
    except Exception:  # noqa: BLE001 - observability only
        return False


def _print_wait_superseded(agent: str, *, rid: str | None) -> None:
    if rid:
        sys.stderr.write(
            f"(superseded: wait on thread {rid} for {agent} "
            "was replaced by a newer waiter)\n"
        )
    else:
        sys.stderr.write(
            f"(superseded: wait for {agent} was replaced by a newer waiter)\n"
        )


def _clear_waiting_marker(store: Store, agent: str, wait_token: str) -> None:
    store.clear_waiting_if_token(agent, wait_token)
    store.clear_waiting_superseded(agent, wait_token)


def _next_backoff(cur: float, base: float, cap: float, activity: bool) -> float:
    """Next poll-loop sleep under adaptive backoff (fix #3).

    Reset to ``base`` the moment the bus shows activity; otherwise grow x2
    toward ``cap``. When ``cap <= base`` (backoff disabled) this ALWAYS
    returns ``base``, so the fixed-interval polling behavior is byte-identical
    to pre-backoff. Pure function — no clock, no I/O — so it is unit-tested
    without any wall-clock flakiness.
    """
    cap_eff = max(base, cap)
    if activity:
        return base
    return min(cap_eff, cur * 2.0)


def _clamp_sleep(desired: float, now: float, deadline: float | None,
                 last_heartbeat: float, heartbeat_interval: float) -> float:
    """Clamp a desired backoff sleep so it never overshoots a timing boundary.

    Backoff may grow the idle poll interval to several seconds, but the loop
    must still (a) detect the timeout deadline on time and (b) keep the
    heartbeat cadence honest. So the actual sleep is the MIN of the desired
    backoff, the time left to the deadline, and the time to the next heartbeat
    due — floored at 0. Pure function; unit-tested without a clock.
    """
    eff = desired
    if deadline is not None:
        eff = min(eff, deadline - now)
    if heartbeat_interval > 0:
        eff = min(eff, last_heartbeat + heartbeat_interval - now)
    return max(0.0, eff)


def _print_rescinded(store: Store, rid: str, row) -> None:
    """Render the RESCINDED wake: banner + the deciding rescind message."""
    print(f"AGENTTALK :: RESCINDED (thread {rid}) — do not act on this request")
    deciding = next(
        (m for m in store.valid_messages() if m.id == row.rescind_msg_id), None,
    )
    if deciding is not None:
        print(render(deciding,
                     header=f"AGENTTALK :: RESCIND  {deciding.sender} -> {deciding.recipient}"))
    else:  # provenance fields still tell the story
        print(f"  rescinded by {row.rescinded_by} at {row.rescind_at} "
              f"(msg {row.rescind_msg_id})")
        if row.rescind_reason:
            print(f"  reason: {row.rescind_reason}")


def _print_requester_terminal(terminal: dict) -> int:
    state = terminal.get("state")
    if state == "delivery_failed":
        print("AGENTTALK :: DELIVERY FAILED")
        code = 4
    elif state == "operator_resolved":
        print("AGENTTALK :: OPERATOR RESOLVED")
        code = 7
    else:
        print("AGENTTALK :: REQUESTER TERMINAL")
        code = 7
    print(json.dumps(terminal, ensure_ascii=False, sort_keys=True))
    return code


def _scoped_wait(store: Store, agent: str, args: argparse.Namespace, wait_token: str) -> int:
    """Block until a message on ONE thread (request_id) arrives — ignoring
    unrelated traffic — and return only that match.

    NON-CONSUMING: advances only the per-thread `seen_msg_id` pointer
    (so a wait->process->wait loop progresses), NEVER the global cursor —
    so unrelated inbox traffic stays unread for a later `drain`. `closed`
    is untouched: seeing a message is not handling it (use `ack
    --to-request` to close). Kills the "wake on any message" churn that
    was the top friction in the 4-agent run.
    """
    rid = args.to_request
    kind_filter = getattr(args, "kind", None)
    from .wrapper.obligations import requester_terminal_for

    terminal = requester_terminal_for(store, rid, agent)
    if terminal is not None:
        return _print_requester_terminal(terminal)
    # Rescind wake (#12): a superseded request must never be waited on.
    # Checked at entry (the rescind may predate this wait entirely) and
    # re-evaluated when a rescind on this rid arrives mid-wait. Exit 3 —
    # distinct from reply (0) and timeout (1) per the exit-code contract.
    row = _thread_row_for(store, agent, rid)
    if row is not None and row.state == "closed-superseded":
        _print_rescinded(store, rid, row)
        return 3
    deadline = time.time() + args.timeout if args.timeout > 0 else None
    interval = max(0.1, args.interval)
    heartbeat_interval = max(0.0, args.heartbeat_interval)
    grace = max(0.0, args.grace)
    composing_extend = max(0.0, args.composing_extend)
    last_heartbeat = 0.0
    seen_composing: set[str] = set()
    seen_rescinds: set[str] = set()
    total_extended = 0.0
    grace_used = False
    # Snapshot the newest inbox id NOW. Only composings that arrive AFTER
    # this (the peer drafting DURING this wait) may extend the deadline.
    # A scoped wait never advances the global cursor, so a stale unread
    # composing would otherwise be re-seen by every future scoped wait and
    # re-extend it indefinitely (Codex review of review-012-001).
    baseline = max((m.id for m in store.messages_for(agent)), default="")
    # Adaptive poll backoff (fix #3): same contract as cmd_wait. Activity is
    # any inbox id beyond `baseline` (a fresh message/composing/rescind);
    # old traffic in (scan_since, baseline] never resets the backoff.
    base = interval
    cap = max(base, max(0.0, args.max_poll_interval))
    cur_sleep = base
    last_seen_max_id = baseline
    if heartbeat_interval > 0:
        store.write_heartbeat(agent)
        last_heartbeat = time.time()
    _write_waiting_marker(
        store, agent, cursor_at_start=store.thread_seen(agent, rid),
        timeout=args.timeout, deadline=deadline, wait_token=wait_token,
        to_request=rid, kind=kind_filter,
    )
    try:
        while True:
            if _wait_was_superseded(store, agent, wait_token):
                _print_wait_superseded(agent, rid=rid)
                return 6
            terminal = requester_terminal_for(store, rid, agent)
            if terminal is not None:
                return _print_requester_terminal(terminal)
            # Floor delivery at BOTH the per-thread seen pointer AND the
            # global cursor: a message already consumed globally (a `drain`
            # or plain `wait` advanced the cursor past it) must NOT be
            # re-surfaced by a scoped wait — otherwise, e.g., after draining
            # and answering a needs-info, `wait --to-request` would re-show
            # that old needs-info instead of awaiting the next reply. On a
            # match we still advance ONLY the thread pointer (never the
            # global cursor), so scoped wait stays non-consuming.
            floor = max(store.thread_seen(agent, rid), store.cursor(agent))
            # Perf fix #1: skip files at/below the cursor before they're
            # read. Bound the scan at min(floor, baseline), NOT floor: under
            # one-window-per-agent floor <= baseline always, but a concurrent
            # same-agent consumer could advance the global cursor mid-wait so
            # floor > baseline — and a fresh composing/rescind has id > baseline,
            # so scanning only from floor could skip a control message in
            # (baseline, floor]. min() keeps control-message detection intact.
            # Empty-string-safe: min("", x) == "" == full scan.
            scan_since = min(floor, baseline)
            scoped_msgs = store.messages_for(agent, since_id=scan_since)
            cur_max = max((m.id for m in scoped_msgs), default=last_seen_max_id)
            match = None
            for m in scoped_msgs:
                if m.kind in CONTROL_KINDS:
                    # A composing extends the deadline ONLY if it is fresh for
                    # this wait (id > baseline) AND not bound to a different
                    # thread (uncorrelated, or correlated to this rid) — so
                    # stale or unrelated control traffic can't stretch a
                    # targeted wait. Never counts as the thread match.
                    comp_rid = (m.meta or {}).get("request_id")
                    if (m.kind == "composing" and m.id > baseline
                            and comp_rid in (None, rid)
                            and m.id not in seen_composing):
                        seen_composing.add(m.id)
                        if (deadline is not None and composing_extend > 0
                                and total_extended < _COMPOSING_MAX_EXTEND_SECONDS):
                            amount = min(composing_extend,
                                         _COMPOSING_MAX_EXTEND_SECONDS - total_extended)
                            deadline += amount
                            total_extended += amount
                            grace_used = False
                            _write_waiting_marker(
                                store, agent, cursor_at_start=floor,
                                timeout=args.timeout, deadline=deadline,
                                wait_token=wait_token,
                                to_request=rid, kind=kind_filter,
                            )
                            if not args.quiet:
                                print(f"(composing from {m.sender}: deadline "
                                      f"extended by {amount:.0f}s, +{total_extended:.0f}s total)")
                    continue
                # A rescind on THIS rid is an outcome, not a match: it must
                # wake the waiter even when --kind filters replies, and it
                # is evaluated regardless of the floor (a concurrent drain
                # may have consumed it globally; supersession still holds).
                # Each rescind is evaluated once; a non-superseding one
                # (e.g. not from the requester) is skipped permanently.
                if m.kind == "rescind" and (m.meta or {}).get("request_id") == rid:
                    if m.id not in seen_rescinds:
                        seen_rescinds.add(m.id)
                        row = _thread_row_for(store, agent, rid)
                        if row is not None and row.state == "closed-superseded":
                            _print_rescinded(store, rid, row)
                            return 3
                    continue
                if (m.meta or {}).get("request_id") != rid:
                    continue
                if kind_filter and m.kind != kind_filter:
                    continue
                if m.id <= floor:
                    continue
                match = m
                break
            if match is not None:
                print(render(match, header=f"AGENTTALK :: RECEIVED (thread {rid})  "
                                           f"{match.sender} -> {match.recipient}"))
                store.mark_thread_seen(agent, rid, match.id)  # thread pointer only
                return 0
            if deadline is not None and time.time() >= deadline:
                if grace_used or grace <= 0:
                    if not args.quiet:
                        suffix = f" + {total_extended:.0f}s extended" if total_extended else ""
                        print(f"(timeout: no new messages on thread {rid} for "
                              f"{agent} in {args.timeout}s{suffix})")
                    return 1
                grace_used = True
                time.sleep(grace)
                continue
            if heartbeat_interval > 0 and time.time() - last_heartbeat >= heartbeat_interval:
                store.write_heartbeat(agent)
                last_heartbeat = time.time()
            # No match this poll. Reset to base IMMEDIATELY on fresh traffic
            # (so a real reply right after a composing isn't stuck behind a
            # capped sleep), then sleep clamped to the deadline / next
            # heartbeat, and grow only after an idle sleep. When disabled
            # (cap <= base) take the original fixed-interval sleep —
            # byte-identical to pre-backoff.
            if cap > base:
                activity = cur_max > last_seen_max_id
                if activity:
                    last_seen_max_id = cur_max
                    cur_sleep = base
                eff = _clamp_sleep(cur_sleep, time.time(), deadline,
                                   last_heartbeat, heartbeat_interval)
                time.sleep(eff)
                if not activity:
                    cur_sleep = _next_backoff(cur_sleep, base, cap, activity=False)
            else:
                time.sleep(interval)
    finally:
        _clear_waiting_marker(store, agent, wait_token)


def cmd_wait(args: argparse.Namespace) -> int:
    store = _get_store(args)
    agent = _resolve_self(args.agent, roster=store.load_config().get("agents") or [])
    blocked = _guard_lead_loop_consumer(store, agent, verb="wait")
    if blocked is not None:
        return blocked
    # Scoped wait: --to-request scopes to one thread (--kind optionally
    # refines it). --kind without --to-request is a usage error.
    if getattr(args, "kind", None) and not getattr(args, "to_request", None):
        sys.stderr.write("agenttalk wait: --kind requires --to-request <id>\n")
        return 2
    # 0.18.0 (FR-007): advisory duplicate-activation warning. One window per
    # agent is the assumed model; if another LIVE process is already waiting
    # as this agent in this store, say so. Best-effort and non-fatal — it
    # never blocks the wait and never changes the exit code. A stale/dead
    # marker (crash recovery) produces no warning. Checked here, before
    # either path overwrites the marker (the only point the prior owner is
    # still visible). This arm-gate also covers the scoped path (it runs
    # before the --to-request dispatch below).
    _now = time.time()
    _foreign = store.foreign_wait_pid(agent, os.getpid(), now=_now,
                                      stale_after=STALE_THRESHOLD_SECONDS)
    if _foreign is not None:
        # fix #4a (opt-in): refuse to stack a second live waiter on this
        # mailbox. Default stays WARN (re-arm patterns in sk-loop/listen rely
        # on it); only --refuse-stacked-wait turns the warning into exit 6.
        if getattr(args, "refuse_stacked_wait", False):
            sys.stderr.write(
                f"agenttalk wait: refusing to stack — another live process "
                f"(PID {_foreign}) already holds {agent!r}'s mailbox. Stop it "
                f"first, or omit --refuse-stacked-wait to run concurrently.\n")
            return 6
        sys.stderr.write(
            f"warning: another live process (PID {_foreign}) is already "
            f"waiting as {agent!r} in this store. One window per agent is "
            f"assumed; concurrent same-agent use can lose cursor/threadstate "
            f"updates.\n")
    else:
        # fix #4b: no LIVE foreign owner, but a CONFIRMED-DEAD one may have
        # left a ghost marker that makes `status` show a phantom waiter. Reap
        # it (no-op when the marker is absent, ours, or owned by a live proc).
        store.clear_dead_waiter(agent, os.getpid())
    # fix #4c: warn (non-blocking) when leftover poll loops accumulate. This
    # wait has not written its own marker yet, so count it explicitly (_live +
    # 1) — otherwise arming as the (cap+1)-th waiter wouldn't warn, which is
    # exactly the accumulation we want to surface as it happens.
    _live = store.live_waiter_count(now=_now, stale_after=STALE_THRESHOLD_SECONDS)
    if _live + 1 > WAITER_SOFTCAP:
        sys.stderr.write(
            f"warning: {_live + 1} live agenttalk waiters in this store "
            f"(> {WAITER_SOFTCAP}); leftover poll loops from old sessions may "
            f"be polling the bus. Consider stopping stale ones.\n")
    # Make this waiter observable to `status` ASAP — BEFORE the optional
    # (and potentially heavy) auto-compaction below — so arm latency can't hide
    # a live waiter. foreign_wait_pid + the soft-cap count above MUST stay
    # ahead of this write (the duplicate-owner check needs the prior owner's
    # marker visible, and the soft-cap's `_live + 1` self-count assumes ours
    # isn't written yet). Each wait path rewrites this same marker with its own
    # cursor/deadline once it starts looping — this early stamp just closes the
    # arm-latency observability gap. deadline mirrors the per-path computation.
    _early_cursor = (store.thread_seen(agent, args.to_request)
                     if getattr(args, "to_request", None) else store.cursor(agent))
    _early_deadline = _now + args.timeout if args.timeout > 0 else None
    wait_token = f"wait-{uuid.uuid4().hex[:12]}"
    _write_waiting_marker(store, agent, cursor_at_start=_early_cursor,
                          timeout=args.timeout, deadline=_early_deadline,
                          wait_token=wait_token,
                          to_request=getattr(args, "to_request", None),
                          kind=getattr(args, "kind", None))
    # Everything after the early marker write is wrapped in one try/finally so
    # a failure before the poll loop (the scoped entry-return, a cursor read
    # that raises, any pre-loop setup error) can't leak the .waiting marker —
    # writing the marker earlier (above) widened the cleanup scope (codex
    # review). The per-path while loops used to own this finally; it now covers
    # both paths plus the gap above them.
    try:
        # fix #2 (opt-in, OFF by default): opportunistic safe compaction once
        # the store grows past the threshold. Throttled + fail-safe.
        _maybe_auto_compact(store, now_epoch=_now)
        if getattr(args, "to_request", None):
            return _scoped_wait(store, agent, args, wait_token)
        deadline = time.time() + args.timeout if args.timeout > 0 else None
        interval = max(0.1, args.interval)
        heartbeat_interval = max(0.0, args.heartbeat_interval)
        grace = max(0.0, args.grace)
        composing_extend = max(0.0, args.composing_extend)
        cursor_at_start = store.cursor(agent)
        last_heartbeat = 0.0
        seen_composing: set[str] = set()
        total_extended = 0.0
        grace_used = False
        # Adaptive poll backoff (fix #3): grow the idle sleep from `base`
        # toward `cap`, reset to base on any inbox activity. cap <= base
        # disables it (byte-identical fixed-interval polling).
        # `last_seen_max_id` is the activity signal — any new
        # message/composing/rescind pushes the inbox's max id up, resetting
        # the sleep to base.
        base = interval
        cap = max(base, max(0.0, args.max_poll_interval))
        cur_sleep = base
        last_seen_max_id = cursor_at_start or ""
        if heartbeat_interval > 0:
            store.write_heartbeat(agent)
            last_heartbeat = time.time()
        # Refresh the marker with the loop's real (post-compaction) deadline +
        # freshly-read cursor; the early stamp above only covered arm latency.
        _write_waiting_marker(
            store, agent, cursor_at_start=cursor_at_start,
            timeout=args.timeout, deadline=deadline, wait_token=wait_token,
        )
        while True:
            if _wait_was_superseded(store, agent, wait_token):
                _print_wait_superseded(agent, rid=None)
                return 6
            msgs = store.messages_for(agent, since_id=cursor_at_start or None)
            cur_max = max((m.id for m in msgs), default=last_seen_max_id)
            for m in msgs:
                if m.kind in CONTROL_KINDS:
                    # Today the only control kind is `composing`; keep the
                    # branch keyed on m.kind so future control kinds slot in
                    # without another rewrite of the wait loop.
                    if m.kind != "composing" or m.id in seen_composing:
                        continue
                    seen_composing.add(m.id)
                    # Persistent ack of consumed control messages. The
                    # in-process `seen_composing` set dedupes within ONE
                    # wait call, but without advancing the on-disk cursor
                    # the same stale composing would re-extend every
                    # subsequent wait. With --no-ack we honor the user's
                    # "don't touch my cursor" intent, which means
                    # --no-ack callers DO pay the stale-ping cost on the
                    # next wait — documented tradeoff, same as for real
                    # messages under --no-ack.
                    if args.ack:
                        store.advance_cursor(agent, m.id)
                    if (
                        deadline is not None
                        and composing_extend > 0
                        and total_extended < _COMPOSING_MAX_EXTEND_SECONDS
                    ):
                        amount = min(
                            composing_extend,
                            _COMPOSING_MAX_EXTEND_SECONDS - total_extended,
                        )
                        deadline += amount
                        total_extended += amount
                        grace_used = False  # fresh activity — re-arm grace
                        # Keep the marker's deadline honest so status
                        # doesn't call a composing-extended wait stale.
                        _write_waiting_marker(
                            store, agent, cursor_at_start=cursor_at_start,
                            timeout=args.timeout, deadline=deadline,
                            wait_token=wait_token,
                        )
                        if not args.quiet:
                            print(
                                f"(composing from {m.sender}: "
                                f"deadline extended by {amount:.0f}s, "
                                f"+{total_extended:.0f}s total)"
                            )
                    continue
                # First real (non-control) message — surface and return.
                print(render(m, header=f"AGENTTALK :: RECEIVED  {m.sender} -> {m.recipient}"))
                if args.ack:
                    store.advance_cursor(agent, m.id)
                return 0
            if deadline is not None and time.time() >= deadline:
                if grace_used or grace <= 0:
                    if not args.quiet:
                        suffix = f" + {total_extended:.0f}s extended" if total_extended else ""
                        print(f"(timeout: no new messages for {agent} in {args.timeout}s{suffix})")
                    return 1
                # Post-timeout grace: one short sleep + one more inbox scan
                # before failing. Catches the "reply landed seconds after
                # the deadline" race that motivated 0.8.0.
                grace_used = True
                time.sleep(grace)
                continue
            if heartbeat_interval > 0 and time.time() - last_heartbeat >= heartbeat_interval:
                store.write_heartbeat(agent)
                last_heartbeat = time.time()
            # No real message this poll. With backoff enabled, reset the
            # interval to base IMMEDIATELY when the inbox advanced (a fresh
            # message/composing/rescind) so the very next poll is fast — then
            # sleep (clamped to the deadline / next heartbeat), and grow only
            # after an idle, no-activity sleep. Resetting BEFORE the sleep is
            # what keeps a real reply that lands right after a composing from
            # waiting out a full capped interval. When disabled (cap <= base)
            # take the original fixed-interval sleep — byte-identical to
            # pre-backoff.
            if cap > base:
                activity = cur_max > last_seen_max_id
                if activity:
                    last_seen_max_id = cur_max
                    cur_sleep = base
                eff = _clamp_sleep(cur_sleep, time.time(), deadline,
                                   last_heartbeat, heartbeat_interval)
                time.sleep(eff)
                if not activity:
                    cur_sleep = _next_backoff(cur_sleep, base, cap, activity=False)
            else:
                time.sleep(interval)
    finally:
        # Always retract the marker — message received, timeout, or the
        # KeyboardInterrupt that main() catches one frame up. Best-effort.
        _clear_waiting_marker(store, agent, wait_token)


def cmd_ack(args: argparse.Namespace) -> int:
    store = _get_store(args)
    agent = _resolve_self(args.agent, roster=store.load_config().get("agents") or [])
    blocked = _guard_lead_loop_consumer(store, agent, verb="ack")
    if blocked is not None:
        return blocked
    # Explicit thread closure (the manual escape hatch + the way to close
    # an off-contract/handled thread). Does NOT touch the global cursor.
    if getattr(args, "to_request", None):
        rid = args.to_request
        matches = [m for m in store.messages_for(agent)
                   if (m.meta or {}).get("request_id") == rid]
        seen = matches[-1].id if matches else None
        store.close_thread(agent, rid, seen_msg_id=seen, reason="manual")
        if matches:
            print(f"thread[{agent}:{rid}] closed")
        else:
            print(f"thread[{agent}:{rid}] closed (note: no messages found for "
                  f"request_id {rid} — recorded closure anyway)")
        return 0
    if args.id:
        store.advance_cursor(agent, args.id)
    else:
        msgs = store.messages_for(agent)
        if msgs:
            store.advance_cursor(agent, msgs[-1].id)
    print(f"cursor[{agent}] = {store.cursor(agent) or '(none)'}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    """Rejoin digest: what an agent needs to catch up after a restart.

    Summarizes identity + roster, actionable request threads (with who
    owes whom, age, and a deterministic next-action hint), the last
    terminal decision per thread, and recent unread FYI traffic kept
    SEPARATE from owed work. Pure derivation (+ threadstate); no writes.
    Fixes the production "restart leaves agents behind / asserts stale
    state" gap. Hints are deterministic commands only — never a plan.
    """
    store = _get_store(args)
    cfg = store.load_config()
    roster = cfg.get("agents") or []
    agent = _resolve_self(args.agent, roster=roster)
    msgs = store.valid_messages()
    cursor = store.cursor(agent)
    closed = _closed_rids(store, agent)
    rows = th.derive_threads(msgs, agent=agent, cursor=cursor, closed_rids=closed,
                             retired=set(store.retired_agents()))
    actionable = [t for t in rows if t.state in th.ACTIONABLE_STATES]

    # Per-thread last terminal decision (review-result / proposal-response).
    by_rid: dict[str, list] = {}
    for m in msgs:
        rid = (m.meta or {}).get("request_id")
        if isinstance(rid, str) and rid:
            by_rid.setdefault(rid, []).append(m)

    def _last_decision(rid: str):
        for m in reversed(by_rid.get(rid, [])):
            if m.kind in ("review-result", "proposal-response"):
                return {"kind": m.kind, "status": (m.meta or {}).get("status"),
                        "by": m.sender}
        return None

    # owe: who must act next. "read" (not "you") for reply-waiting — a
    # reply already landed; the next move is to CONSUME it, not fire off
    # another message (which would invite ping-pong).
    _owe = {"owed-inbound": "you", "reply-waiting": "read", "open-outbound": "peer"}

    def _hint(t) -> str:
        if t.needs_operator and t.operator_state == "pending" and t.state == "owed-inbound":
            return (f"agenttalk reply --to-request {t.request_id}   "
                    f"# OPERATOR INPUT NEEDED — ask your human, then relay")
        if t.state == "reply-waiting":
            return f"agenttalk drain --for {agent}   # a reply landed — read it first"
        if t.state == "owed-inbound":
            return f"agenttalk reply --to-request {t.request_id}   # you owe a response"
        if t.state == "open-outbound":
            return f"agenttalk wait --to-request {t.request_id}    # awaiting {t.peer}"
        return ""

    thread_payload = []
    for t in actionable:
        d = t.to_dict()
        d["owe"] = _owe.get(t.state, "-")
        d["last_decision"] = _last_decision(t.request_id)
        d["hint"] = _hint(t)
        _inject_next(d, t)  # #19 next-owner / next-action hint (CLI-surfaced)
        thread_payload.append(d)

    # Unread FYI: messages addressed to me, newer than my cursor, that are
    # NOT part of an actionable thread — kept separate from owed work.
    actionable_rids = {t.request_id for t in actionable}
    fyi = []
    for m in store.messages_for(agent, since_id=cursor or None):
        if m.kind in CONTROL_KINDS:
            continue
        if (m.meta or {}).get("request_id") in actionable_rids:
            continue
        fyi.append({"id": m.id, "from": m.sender, "kind": m.kind,
                    "subject": m.subject or "",
                    "broadcast": bool((m.meta or {}).get("broadcast_id"))})
    fyi = fyi[-10:]  # recent only

    # Rescinded threads a restarted agent might still act on: superseded
    # threads whose deciding rescind it has NOT yet consumed (id past the
    # cursor). Once drained, the flag stops nagging — the transcript
    # remains the permanent record. (#12, FR-004)
    rescinded = [
        t.to_dict() for t in rows
        if t.state == "closed-superseded"
        and isinstance(t.rescind_msg_id, str)
        and t.rescind_msg_id > (cursor or "")
    ]

    # The liaison's operator-input bucket (#18, FR-014): every pending
    # escalation addressed to this agent, surfaced separately from (and
    # in addition to) the actionable list.
    liaison = store.operator_facing()
    escalations = []
    if agent == liaison:
        escalations = [
            {**t.to_dict(), "hint": _hint(t)}
            for t in rows
            if t.needs_operator and t.operator_state == "pending"
            and t.role == "responder"
        ]

    lesson_rows: list[tuple[dict, dict]] = []
    lesson_warnings: list[str] = []
    lesson_context_scope = "process"
    from agenttalk import lesson_context as lc
    lesson_selection = lc.select_for_sync(
        store,
        msgs,
        thread_payload,
        explicit_tags=getattr(args, "lesson_tag", None),
        limit=5,
    )
    lesson_rows = lesson_selection.rows
    lesson_warnings = lesson_selection.warnings
    lesson_context_scope = lesson_selection.context_scope

    payload = {
        "agent": agent,
        "roster": roster,
        "roles": cfg.get("roles", {}) or {},
        "groups": cfg.get("groups", {}) or {},
        "counts": th.counts(rows),
        "threads": thread_payload,
        "unread_fyi": fyi,
    }
    if rescinded:
        payload["rescinded"] = rescinded       # additive: absent when none
    if agent == liaison:
        payload["escalations"] = escalations   # additive: liaison only
    if lesson_rows:
        payload["lessons"] = [_kn_lesson_dict(n, v) for (n, v) in lesson_rows]
        payload["lesson_context_scope"] = lesson_context_scope
    if lesson_warnings:
        payload["lesson_warnings"] = lesson_warnings
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"sync for {agent}  (you are {agent})")
    role = (cfg.get("roles", {}) or {}).get(agent)
    member_of = [g for g, ms in (cfg.get("groups", {}) or {}).items() if agent in ms]
    if role or member_of:
        print(f"  role={role or '-'}  groups=[{', '.join(member_of) or '-'}]")
    if agent == liaison:
        print("  operator-facing: yes (you are the liaison)")
    print(f"  roster: {', '.join(roster)}")
    c = payload["counts"]
    print(f"  threads: reply-waiting={c['reply-waiting']} owed-inbound={c['owed-inbound']} "
          f"open-outbound={c['open-outbound']} closed={c['closed']}"
          + (f" superseded={c['closed-superseded']}" if c.get("closed-superseded") else ""))
    if rescinded:
        print(f"RESCINDED ({len(rescinded)} — do NOT act on these):")
        for d in rescinded:
            r = d.get("rescind") or {}
            reason = f' — "{r.get("reason")}"' if r.get("reason") else ""
            print(f"  {d['request_id']}  {d['opener_kind']}  rescinded by "
                  f"{r.get('by')}{reason}")
    if agent == liaison and escalations:
        print(f"OPERATOR INPUT NEEDED ({len(escalations)} pending):")
        for d in escalations:
            age = _format_age(d["age_seconds"]) if d["age_seconds"] is not None else "?"
            subj = f' "{d["subject"]}"' if d["subject"] else ""
            print(f"  {d['request_id']}  from {d['peer']}  age={age}{subj}")
            print(f"      -> {d['hint']}")
    if lesson_warnings:
        for warning in lesson_warnings:
            print(f"WARN: {warning}")
    if lesson_rows:
        print(f"Lessons to check ({len(lesson_rows)}):")
        for n, v in lesson_rows:
            print(_kn_format_lesson_line(n, v))
    if thread_payload:
        print("actionable threads (owe / hint):")
        for d in thread_payload:
            age = _format_age(d["age_seconds"]) if d["age_seconds"] is not None else "?"
            dec = ""
            if d["last_decision"]:
                ld = d["last_decision"]
                dec = f"  last={ld['kind']}={ld.get('status') or '?'}"
            subj = f' "{d["subject"]}"' if d["subject"] else ""
            print(f"  [{d['state']}] {d['request_id']}  {d['opener_kind']}  "
                  f"peer={d['peer']}  owe={d['owe']}  age={age}{subj}{dec}")
            print(f"      -> {d['hint']}")
    else:
        print("  (no actionable threads — you're caught up)")
    if fyi:
        print(f"unread FYI (not owed work; {len(fyi)} recent):")
        for f in fyi:
            tag = "broadcast " if f["broadcast"] else ""
            subj = f' "{f["subject"]}"' if f["subject"] else ""
            print(f"  {f['id']}  {tag}{f['kind']} from {f['from']}{subj}")
        print(f"  (consume with `agenttalk drain --for {agent}`)")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    """Diagnostic identity view: effective root, resolved self/peer, roster
    membership + role/groups, and unread/owed counts.

    Lenient by design (never hard-exits on an unresolved identity — it's a
    'where am I / who am I' check), and warns on the common footguns: a
    misplaced `--root` (self not in the roster), or `AGENTTALK_SELF` unset.
    """
    store = _get_store(args)
    cfg = store.load_config()
    roster = cfg.get("agents") or []
    roles = cfg.get("roles", {}) or {}
    groups = cfg.get("groups", {}) or {}
    self_name = args.agent or os.environ.get("AGENTTALK_SELF")
    in_roster = bool(self_name and self_name in roster)
    peer = os.environ.get("AGENTTALK_PEER")
    if not peer and self_name:
        others = [a for a in roster if a != self_name]
        peer = others[0] if len(others) == 1 else None

    role = member_of = unread = owed = None
    if in_roster:
        role = roles.get(self_name)
        member_of = [g for g, ms in groups.items() if self_name in ms]
        unread = len(store.unread_for(self_name))
        rows = th.derive_threads(
            store.valid_messages(), agent=self_name, cursor=store.cursor(self_name),
            closed_rids=_closed_rids(store, self_name),
            retired=set(store.retired_agents()),
        )
        owed = sum(1 for t in rows if t.state in ("owed-inbound", "reply-waiting"))

    warnings: list[str] = []
    if not self_name:
        warnings.append("identity unresolved — set $AGENTTALK_SELF in this shell "
                        "or pass --for <agent>.")
    elif not in_roster:
        warnings.append(f"self '{self_name}' is NOT in the roster {sorted(roster)} "
                        f"— wrong --root, or a typo? (root above)")

    liaison = store.operator_facing()
    if args.json:
        payload = {
            "root": str(store.root), "self": self_name, "self_in_roster": in_roster,
            "peer": peer, "role": role, "groups": member_of or [],
            "roster": roster, "unread": unread, "owed": owed,
            "warnings": warnings,
        }
        # Strict additivity (NFR-001): liaison keys appear only when the
        # feature is in use — absent, not null (WP04 review blocker 1).
        if liaison is not None:
            payload["operator_facing"] = bool(self_name and self_name == liaison)
            payload["liaison"] = liaison
        print(json.dumps(payload, indent=2))
        return 0
    print(f"root:   {store.root}")
    tag = (" (in roster)" if in_roster else " (NOT in roster!)") if self_name else ""
    print(f"self:   {self_name or '(unresolved)'}{tag}")
    if peer:
        print(f"peer:   {peer}")
    if in_roster:
        print(f"role:   {role or '-'}   groups=[{', '.join(member_of) or '-'}]")
        print(f"unread: {unread}   owed (you must act): {owed}")
    if liaison is not None:
        of = "yes" if (self_name and self_name == liaison) else "no"
        print(f"operator-facing: {of} (liaison: {liaison})")
    print(f"roster: {', '.join(roster)}")
    for w in warnings:
        print(f"WARN:   {w}")
    return 0


def cmd_transcript(args: argparse.Namespace) -> int:
    store = _get_store(args)
    out = Path(args.out).resolve() if args.out else None
    path = tx.export(store, fmt=args.format, out=out)
    print(str(path))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Health check: did the user wire everything up correctly?"""
    root = Path(args.root).resolve() if getattr(args, "root", None) else None
    report = dr.run(root)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _render_doctor_human(report)
    # Exit codes respect the global contract documented in the README:
    # 0 = success (incl. warnings — they're informational),
    # 2 = error (the user needs to fix something). Exit 1 is reserved
    # for `agenttalk wait` timeout per the published contract.
    if report.overall == "error":
        return 2
    return 0


def cmd_commit_gate(args: argparse.Namespace) -> int:
    """Inspect or explicitly reset the detection-grade compliance breaker."""
    from .wrapper.obligations import DetectionCommitGate

    store = _get_store(args)
    roster = store.load_config().get("agents") or []
    agent = _resolve_self(args.agent, roster=roster)
    gate = DetectionCommitGate.from_environment(
        store,
        agent,
        fence="operator-cli",
    )
    if args.gate_action == "reset":
        actor = _resolve_self(args.sender, roster=roster)
        try:
            gate.reset_compliance_breaker(actor=actor, reason=args.reason)
        except (PermissionError, ValueError) as exc:
            sys.stderr.write(f"agenttalk commit-gate reset: {exc}\n")
            return 2
    status = gate.status()
    if args.json:
        print(json.dumps(status, indent=2))
    else:
        print(f"commit-gate {agent}: {status['status']}")
        if status.get("reason"):
            print(f"  reason: {status['reason']}")
        if status.get("breaker"):
            print(f"  breaker: {json.dumps(status['breaker'], sort_keys=True)}")
        if status.get("legacy_broadcast"):
            legacy = status["legacy_broadcast"]
            print(
                "  legacy-broadcast: "
                f"enforcement={legacy.get('enforcement')} "
                f"unenforced_total={legacy.get('unenforced_total')}"
            )
    return 0


def cmd_gateway(args: argparse.Namespace) -> int:
    """Manage the loopback-only watched OVH/Qwen trial gateway."""
    from agenttalk import ovh_gateway as gateway
    from agenttalk import ovh_gateway_service as service

    store = _get_store(args)
    action = args.gateway_action
    try:
        if action == "init":
            result = service.initialize_install(
                store.root,
                litellm_executable=args.litellm_executable,
                opening_micro_eur=args.opening_micro_eur,
                opening_evidence=args.opening_evidence,
            )
        elif action == "task-install":
            service.load_install_manifest(store.root)
            result = service.install_task(store.root)
        elif action == "start":
            result = service.start_task(store.root)
        elif action == "stop":
            result = service.stop_task(store.root, timeout_seconds=args.timeout)
        elif action == "reconfigure":
            result = service.reconfigure_endpoint(store.root)
        elif action == "runtime-rebind":
            result = service.rebind_runtime(
                store.root,
                litellm_executable=args.litellm_executable,
            )
        elif action == "run":
            return service.run_service(store.root)
        elif action == "reconcile":
            result = gateway.SpendLedger().reconcile(
                args.attempt_id,
                outcome=args.outcome,
                reason=args.reason,
            )
        elif action == "cap-install":
            issuer_token = gateway.read_secret_file(
                gateway.default_front_token_path()
            )
            result = gateway.SpendLedger().install_child_caps(
                issuer_token=issuer_token
            )
        elif action == "canary-verify":
            result = gateway.SpendLedger().verify_dashboard_canary(
                args.attempt_id,
                observed_delta_micro_eur=args.dashboard_delta_micro_eur,
            )
        elif action == "hold":
            result = gateway.SpendLedger().place_hold(reason=args.reason)
        elif action == "clear-hold":
            result = gateway.SpendLedger().clear_hold(reason=args.reason)
        elif action == "status":
            result = service.gateway_status(store.root)
        else:
            raise ValueError(f"unsupported gateway action {action!r}")
    except (
        service.GatewayLifecycleUnknown,
        service.LiteLLMRuntimeProbeUnknown,
    ) as exc:
        sys.stderr.write(f"agenttalk gateway {action}: {exc}\n")
        return 3
    except (gateway.GatewayError, OSError, ValueError) as exc:
        sys.stderr.write(f"agenttalk gateway {action}: {exc}\n")
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    if action == "status" and not result.get("ready"):
        return 2
    if action == "canary-verify" and not result.get("accepted"):
        return 2
    return 0


def _render_doctor_human(report) -> None:
    # Root is the FIRST line (0.14.0, #13) — same contract as whoami: the
    # wrong-root footgun must be diagnosable from line one.
    print(f"root: {report.project_root}")
    print("agenttalk doctor")
    print(f"  agenttalk version  {report.agenttalk_version}")
    # #37 Fix 2: the running module path + capabilities discriminate two writers
    # that report the same version (src checkout vs installed wheel) — compare
    # these across agents to spot a store-corrupting version skew.
    if report.agenttalk_module_path:
        print(f"  module path        {report.agenttalk_module_path}")
    if report.store_schema_capabilities:
        print(f"  store schema caps  {', '.join(report.store_schema_capabilities)}")
    print(f"  python version     {report.python_version}")
    print()
    badge = {"ok": "ok  ", "warn": "warn", "error": "FAIL"}
    width = max((len(c.name) for c in report.checks), default=0)
    for c in report.checks:
        print(f"  [{badge[c.status]}] {c.name:<{width}}  {c.details}")
        if c.fix and c.status != "ok":
            print(f"           {' ' * width}  fix: {c.fix}")
    print()
    print(f"overall: {report.overall.upper()}")


def cmd_hmac_init(args: argparse.Namespace) -> int:
    """Generate the HMAC signing key for this project.

    The key file's existence at the conventional per-user path
    AT THE PATH-DERIVED project_id automatically activates
    signature enforcement on both send and verify — there's no
    config flag to flip, AND no config-stored project_id, because
    anything in .agenttalk/config.json is attacker-writable.
    To disable enforcement later, delete the key file.
    """
    store = _get_store(args)
    project_id = store.project_id()  # always returns a value (path-derived)
    try:
        # No --key-file flag: the override path must be set via
        # AGENTTALK_HMAC_KEY_FILE if you need to change locations,
        # so every command (send/recv/wait/status/doctor) finds the
        # same key without an extra plumbing layer.
        path = _signing.init_key(project_id, force=args.force)
    except FileExistsError as e:
        sys.stderr.write(f"agenttalk hmac-init: {e}\n")
        return 2
    print(f"agenttalk: HMAC key written to {path}")
    print(f"  project_id: {project_id}")
    if os.name != "nt":
        print("  file mode:  0600 (user-readable only)")
    else:
        print("  reminder:   on Windows, ensure the per-user keys dir is")
        print("              NOT inherited by other accounts on this box.")
    print()
    print("Signature enforcement is now ACTIVE for this project:")
    print("  - Outbound messages from `agenttalk send` are signed automatically.")
    print("  - Inbound messages without a valid signature are silently skipped")
    print("    (visible in `agenttalk status` and `agenttalk doctor`).")
    print("To disable enforcement, delete the key file at the path above.")
    if os.environ.get("AGENTTALK_HMAC_KEY_FILE"):
        print()
        print("Note: AGENTTALK_HMAC_KEY_FILE is set in this env; every command")
        print("MUST run with the same env value or they'll look at the default path.")
    return 0


def _reset_in_minutes(epoch: object) -> int | None:
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
        return None
    delta = epoch - time.time()
    return max(0, int(delta // 60))


def _fmt_reset(epoch: object) -> str:
    mins = _reset_in_minutes(epoch)
    if mins is None:
        return "reset ?"
    if mins == 0:
        return "resets now"
    if mins < 60:
        return f"resets {mins}m"
    return f"resets {mins // 60}h{mins % 60:02d}m"


def _fmt_pct(used: object) -> str:
    return f"{used:.0f}% used" if isinstance(used, (int, float)) and not isinstance(used, bool) else "?% used"


def _print_capacity_row(agent: str, snap: dict, *, threshold: float, reset_soon_min: int,
                        context_threshold: float = 80.0) -> None:
    conf = capmod.effective_confidence(snap)
    if conf == "unknown":
        print(f"  {agent:<14} budget unknown (no readable signal on its side)")
        return
    p, pr = snap.get("primary_used_percent"), snap.get("primary_resets_at")
    s, sr = snap.get("secondary_used_percent"), snap.get("secondary_resets_at")
    ctx = snap.get("context_used_percent")
    flags: list[str] = []
    for label, used, reset in (("5h", p, pr), ("weekly", s, sr)):
        if isinstance(used, (int, float)) and not isinstance(used, bool) and used >= threshold:
            flags.append(f"{label} {used:.0f}%≥{threshold:.0f}")
        rin = _reset_in_minutes(reset)
        if rin is not None and 0 < rin <= reset_soon_min:
            flags.append(f"{label} resets in {rin}m")
    if isinstance(ctx, (int, float)) and not isinstance(ctx, bool) and ctx >= context_threshold:
        flags.append(f"context {ctx:.0f}%≥{context_threshold:.0f} (near compaction)")
    plan = snap.get("plan_type") or "?"
    ctx_seg = f"  context {ctx:.0f}%" if isinstance(ctx, (int, float)) and not isinstance(ctx, bool) else ""
    stale = "" if conf == "observed" else f" [{conf}]"
    warn = ("  ⚠ " + "; ".join(flags)) if flags else ""
    print(f"  {agent:<14} 5h {_fmt_pct(p)} ({_fmt_reset(pr)})  "
          f"weekly {_fmt_pct(s)} ({_fmt_reset(sr)}){ctx_seg}  plan={plan}{stale}{warn}")


def cmd_capacity(args: argparse.Namespace) -> int:
    """Advisory rate-limit budget: publish your own snapshot (refresh) or view
    the team's published budgets (show). STRICTLY advisory — a missing, stale,
    or unknown signal never blocks anything; it's a hint for the lead."""
    store = _get_store(args)
    roster = store.load_config().get("agents") or []
    if args.mode == "refresh":
        agent = _resolve_self(args.agent, roster=roster)
        snap = capmod.read_local(
            agent, source=args.source,
            statusline_path=args.statusline_path, sessions_dir=args.sessions_dir,
        )
        store.write_capacity(agent, snap.to_dict())
        print(f"agenttalk capacity: published {agent} "
              f"(source={snap.source}, confidence={snap.confidence})")
        if snap.source != "unknown":
            _print_capacity_row(agent, snap.to_dict(),
                                threshold=args.threshold, reset_soon_min=args.reset_soon_min,
                                context_threshold=args.context_threshold)
        else:
            print("  no local budget signal found — published an 'unknown' snapshot. "
                  "On Claude, configure a status line (or CC_STATUSLINE_DEBUG=1) so "
                  "rate_limits are dumped; on Codex this reads ~/.codex/sessions rollouts.")
        return 0

    # show
    caps = store.read_all_capacities()
    if not caps:
        print("agenttalk capacity: no budgets published yet — each agent runs "
              "`agenttalk capacity refresh` (advisory; lead reads this to plan work).")
        return 0
    print(f"team budget (advisory; flag ≥{args.threshold:.0f}% used, reset within "
          f"{args.reset_soon_min}m, or context ≥{args.context_threshold:.0f}%):")
    for agent in sorted(caps):
        _print_capacity_row(agent, caps[agent],
                            threshold=args.threshold, reset_soon_min=args.reset_soon_min,
                            context_threshold=args.context_threshold)
    return 0


def cmd_install_skills(args: argparse.Namespace) -> int:
    if args.devkit_only:
        claude = codex = False
        devkit = True
    else:
        claude = not args.codex_only
        codex = not args.claude_only
        devkit = not args.no_devkit  # dev-discipline pack installs by default
    if not (claude or codex or devkit):
        sys.stderr.write("agenttalk install-skills: nothing to do (everything excluded)\n")
        return 2

    claude_dir = Path(args.claude_dir) if args.claude_dir else None
    codex_dir = Path(args.codex_dir) if args.codex_dir else None
    claude_skills_dir = Path(args.claude_skills_dir) if args.claude_skills_dir else None
    codex_skills_dir = Path(args.codex_skills_dir) if args.codex_skills_dir else None

    res = iskl.install(
        claude=claude,
        codex=codex,
        devkit=devkit,
        claude_dir=claude_dir,
        codex_dir=codex_dir,
        claude_skills_dir=claude_skills_dir,
        codex_skills_dir=codex_skills_dir,
        force=args.force,
        dry_run=args.dry_run,
    )

    tails = {
        "would-skip": "  (differs; local edits preserved)",
        "would-overwrite": "  (differs; --force would overwrite)",
        "would-copy": "  (target absent; --dry-run sees fresh-install path)",
    }
    for a in res.actions:
        marker = {
            "copied": "+",
            "unchanged": "=",
            "skipped": "!",
            "would-copy": "?",
            "would-overwrite": "?",
            "would-skip": "?",
        }.get(a.status, " ")
        tail = tails.get(a.status, "")
        print(f"  {marker} {a.status:<16} {a.dst}{tail}")

    counts = res.counts()
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"\nagenttalk install-skills: {summary or 'no actions'}")

    if counts.get("skipped"):
        print(
            "\nSome targets differ from the bundled version and were not overwritten.\n"
            "Inspect first with `agenttalk install-skills --dry-run --force` (no writes),\n"
            "then re-run with `--force` to replace them, or diff manually first."
        )
    if counts.get("would-skip"):
        print(
            "\nDry run: targets marked `would-skip` differ from the bundled version\n"
            "and would be left alone in a normal run. Re-run with `--dry-run --force`\n"
            "to preview which files `--force` would overwrite."
        )
    if not args.dry_run and counts.get("copied"):
        print("Restart Claude Code / Codex to pick up the new skill files.")
    return 0


def cmd_codex_config(args: argparse.Namespace) -> int:
    project_dir = Path(args.project).resolve() if args.project else Path.cwd().resolve()
    config_path = Path(args.config_path) if args.config_path else cxc.default_config_path()

    if args.status:
        st = cxc.status(config_path, project_dir)
        print(f"config_path:     {st['config_path']}")
        print(f"config_exists:   {st['config_exists']}")
        print(f"project_dir:     {st['project_dir']}")
        print(f"section_present: {st['section_present']}")
        if st.get("duplicate_sections"):
            print(f"duplicate_sections: {st['duplicate_sections']}  "
                  "(INVALID TOML the codex CLI rejects — run "
                  "`agenttalk codex-config --enable` to collapse the duplicates)")
        for k, v in st["keys"].items():
            print(f"  {k:<18} {v if v is not None else '(unset)'}")
        return 0

    if args.disable:
        res = cxc.disable_project(config_path, project_dir)
    else:
        res = cxc.enable_project(config_path, project_dir)

    print(f"agenttalk codex-config: {res.action}")
    print(f"  config:  {res.config_path}")
    print(f"  project: {res.project_dir}")
    for change in res.changes:
        print(f"  - {change}")
    if res.action in ("created", "updated", "removed"):
        print("\nRestart Codex for changes to take effect.")
    return 0


def _resolve_reply_anchor(
    store: Store, sender: str, args: argparse.Namespace,
) -> tuple[object | None, str | None]:
    """Pick the message a `reply` is anchored to.

    With multiple threads open at once, "reply to the most recent
    message" is a footgun — you can echo the wrong thread's request_id
    and corrupt correlation. The explicit anchors fix that:

    - ``--to-id <message_id>``: the specific received message with that
      id. It MUST be addressed to ``sender`` (validated inbox only) —
      you can't reply to a message that wasn't sent to you.
    - ``--to-request <request_id>``: the latest non-control received
      message in that correlation thread.
    - neither: the most recent received non-control message (legacy).

    Returns ``(message, None)`` on success or ``(None, reason)`` so the
    caller can emit a precise error.
    """
    inbox = store.messages_for(sender)  # validated + addressed to me
    to_id = getattr(args, "to_id", None)
    to_request = getattr(args, "to_request", None)
    if to_id:
        for m in inbox:
            if m.id == to_id:
                return m, None
        return None, (
            f"--to-id {to_id} not found: no validated message with that id "
            f"is addressed to {sender}."
        )
    if to_request:
        matches = [
            m for m in inbox
            if (m.meta or {}).get("request_id") == to_request
            and m.kind not in CONTROL_KINDS
        ]
        if matches:
            return matches[-1], None
        return None, (
            f"--to-request {to_request}: no validated message addressed to "
            f"{sender} carries that request_id."
        )
    last = store.last_received_for(sender)
    if last is None:
        return None, (
            f"no messages in {sender}'s inbox to reply to. Use `agenttalk "
            f"send` to start a new thread."
        )
    return last, None


def cmd_reply(args: argparse.Namespace) -> int:
    """Reply to a received message (the most recent, or an explicit anchor).

    Auto-derives recipient (= sender of the anchored message) and echoes
    its `request_id` so the peer's handoff/consult/proposal flow can
    correlate the reply. Use `--to-id` / `--to-request` to anchor to a
    specific thread when several are open. Other meta keys are NOT
    echoed — explicit pass via `--meta` is the only way to attach more.
    """
    store = _get_store(args)
    cfg = store.load_config()
    roster = cfg.get("agents") or []
    sender = _resolve_self(args.sender, roster=roster)
    anchor, err = _resolve_reply_anchor(store, sender, args)
    if anchor is None:
        sys.stderr.write(f"agenttalk reply: {err}\n")
        return 2
    # --na (#15, 0.15.0): a structured not-applicable response — closes
    # the obligation like any answer, displayed distinctly so the asker
    # never mistakes "not my role" for a substantive reply. Question
    # threads only: review-request/proposal contracts require their
    # typed responses (FR-006).
    na = getattr(args, "na", False)
    # args.kind defaults to None so an EXPLICIT --kind (even
    # `--kind message`) is distinguishable - the WP02 review repro.
    kind = args.kind or "message"
    if na:
        if args.kind is not None:
            sys.stderr.write(
                "agenttalk reply: --na and --kind are mutually exclusive "
                "(an NA response is always kind=message).\n")
            return 2
        anchor_rid = (anchor.meta or {}).get("request_id")
        opener_kind = None
        if isinstance(anchor_rid, str) and anchor_rid:
            row = _thread_row_for(store, sender, anchor_rid)
            opener_kind = row.opener_kind if row is not None else None
        else:
            opener_kind = anchor.kind if anchor.kind in ("review-request", "proposal") else None
        if opener_kind in ("review-request", "proposal"):
            sys.stderr.write(
                f"agenttalk reply: --na is not valid on a {opener_kind} "
                f"thread — this thread needs a typed response: "
                f"{'review-result' if opener_kind == 'review-request' else 'proposal-response'}.\n")
            return 2
    dry = getattr(args, "dry_run", False)
    if dry and getattr(args, "await_reply", False):
        sys.stderr.write(
            "agenttalk reply: --await-reply cannot be combined with --dry-run "
            "because no outbound request is created.\n"
        )
        return 2
    if getattr(args, "await_reply", False) and kind not in {"review-request", "proposal"}:
        sys.stderr.write(
            "agenttalk reply: --await-reply requires a counter-review "
            "(--kind review-request) or counter-proposal (--kind proposal).\n"
        )
        return 2
    # --dry-run only resolves routing (recipient + request_id + kind) and
    # sends nothing, so it must NOT require a body. Skip the body read +
    # empty-body check entirely in that case.
    body = ""
    if not dry:
        if na and not (getattr(args, "message", None) or getattr(args, "file", None)):
            # NA is the one reply whose body may default — and it must
            # never fall into the implicit stdin sniff (the 0.14.0
            # rescind lesson: a bare command must not block on stdin).
            body = "n/a"
        else:
            body = _read_body(args)
        if not body and not args.allow_empty:
            sys.stderr.write("agenttalk reply: empty body (use -m TEXT, --file PATH, pipe stdin, or --allow-empty)\n")
            return 2
    meta = _parse_meta(args.meta)
    if na:
        meta["response"] = "not-applicable"  # the display discriminator
    # Correlation echo lives in reply_transport so the wrapper's draft
    # delivery (#201) applies IDENTICAL rules: in_reply_to anchor,
    # request_id echo except for thread-opening reply kinds, broadcast_id
    # echo only without a request_id. Explicit --meta always wins.
    reply_transport.echo_reply_correlation(
        meta, anchor_id=anchor.id, anchor_meta=anchor.meta, kind=kind,
    )
    _maybe_autogen_request_id(kind, meta, quiet=args.quiet)
    operation_nonce = getattr(args, "operation_nonce", None)
    existing, operation_error = _operation_idempotency(
        store,
        sender=sender,
        recipient=anchor.sender,
        body=body,
        kind=kind,
        operation="terminal",
        meta=meta,
        nonce=operation_nonce,
    )
    if operation_error is not None:
        sys.stderr.write(f"agenttalk reply: {operation_error}.\n")
        return 2
    if existing is not None:
        if not args.quiet:
            print(f"(reply operation already recorded: id={existing.id})")
        return 0
    await_record = _prepare_await_reply(
        store,
        sender=sender,
        kind=kind,
        meta=meta,
        source="reply",
        enabled=bool(getattr(args, "await_reply", False)),
    )
    _warn_missing_request_id(kind, meta)
    if dry:
        # Resolve-and-show without sending — guards against echoing the
        # wrong request_id when multiple threads are open. The reply routes
        # to the ANCHOR's sender, which for a broadcast is the thread
        # originator (who may not be the agent that needs the answer).
        rid = meta.get("request_id", "(none)")
        print(f"(dry-run) reply {sender} -> {anchor.sender}  thread={rid}  "
              f"kind={kind}; nothing sent.")
        return 0
    gate_mod.validate_response_status(kind, meta)
    gate_mod.validate_review_result_evidence(kind, meta)
    try:
        if operation_nonce is not None:
            msg, published = store.send_operation(
                sender=sender,
                recipient=anchor.sender,
                body=body,
                kind=kind,
                subject=args.subject or "",
                meta=meta,
                operation_nonce=operation_nonce,
                operation_digest=str(meta["operation_digest"]),
            )
        else:
            msg = store.send(
                sender=sender,
                recipient=anchor.sender,
                body=body,
                kind=kind,
                subject=args.subject or "",
                meta=meta,
            )
            published = True
    except ValueError as exc:
        sys.stderr.write(f"agenttalk reply: {exc}.\n")
        return 2
    if not published:
        if not args.quiet:
            print(f"(reply operation already recorded: id={msg.id})")
        return 0
    if not args.quiet:
        print(render(msg, header=f"AGENTTALK :: REPLY  {msg.sender} -> {msg.recipient}"))
    _register_await_reply(store, await_record, quiet=args.quiet)
    return 0


def cmd_tail(args: argparse.Namespace) -> int:
    """Stream messages as they arrive — passive monitor mode.

    Unlike `wait` and `recv`, `tail` never advances any agent's
    cursor and never writes a heartbeat, so a third terminal can
    watch the bus without interfering with the two active agents.

    By default starts with messages from now forward; pass
    `--from-start` to replay everything in the store first.

    Invalid messages (forged/tampered/corrupt — those that
    `messages_for()` would skip) are shown as a single-line
    WARNING with the id + reason, NOT rendered with their body.
    This preserves tail's forensic value without piping an
    untrusted body into the operator's terminal as if it were a
    normal message.
    """
    store = _get_store(args)
    cfg = store.load_config()
    # 0.18.0 (FR-004): validate against the KNOWN roster (active ∪ retired)
    # so a retired identity's historical messages still print instead of
    # showing as TAIL INVALID — matching valid_messages and the dashboard.
    roster = store._known_roster(cfg)  # noqa: SLF001 — D3 parity
    # Mirror the HMAC enforcement that messages_for applies, so a
    # tampered or unsigned message never gets rendered with its
    # body in tail's output — only as a body-free INVALID warning.
    require_sig = store.signing_enforced()
    project_id = store.project_id() if require_sig else None
    sig_key: bytes | None = None
    if require_sig:
        try:
            sig_key = _signing.load_key(project_id)
        except (FileNotFoundError, OSError, ValueError):
            sig_key = None
    seen: set[str] = set()
    if not args.from_start:
        # Treat existing messages as already-seen so we only show new ones.
        # Use _scan_messages so we cover BOTH valid and invalid up to "now".
        valid, invalid = store._scan_messages()
        for m in valid:
            seen.add(m.id)
        for mid, _ in invalid:
            seen.add(mid)
    interval = max(0.1, args.interval)
    deadline = time.time() + args.timeout if args.timeout > 0 else None
    try:
        while True:
            valid, invalid = store._scan_messages()
            # Render valid messages (subject to roster + signature validation)
            for m in valid:
                if m.id in seen:
                    continue
                seen.add(m.id)
                try:
                    m.validate(roster)
                except ValueError as e:
                    # Valid shape but failed roster/kind check — surface
                    # as a warning, never render the body.
                    sys.stderr.write(
                        f"AGENTTALK :: TAIL INVALID  id={m.id}  reason={e}\n"
                    )
                    continue
                if require_sig:
                    if sig_key is None:
                        sys.stderr.write(
                            f"AGENTTALK :: TAIL INVALID  id={m.id}  "
                            f"reason=signatures enforced but no key file loadable\n"
                        )
                        continue
                    try:
                        _signing.verify_message(
                            m.to_dict(), sig_key, expected_key_id=project_id,
                        )
                    except ValueError as e:
                        sys.stderr.write(
                            f"AGENTTALK :: TAIL INVALID  id={m.id}  reason={e}\n"
                        )
                        continue
                print(render(m, header=f"AGENTTALK :: TAIL  {m.sender} -> {m.recipient}"))
            # Surface parse/construction failures as warnings too
            for mid, reason in invalid:
                if mid in seen:
                    continue
                seen.add(mid)
                sys.stderr.write(
                    f"AGENTTALK :: TAIL INVALID  id={mid}  reason={reason}\n"
                )
            if deadline is not None and time.time() >= deadline:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def cmd_reset(args: argparse.Namespace) -> int:
    """Clear messages/cursors/heartbeats and start a fresh session."""
    store = _get_store(args)
    # Incomplete delivery is a journal, so reset blocks it; ordinary ACTIVE
    # coordination remains resettable with an operator-visible warning. The
    # dedicated lock serializes this check+teardown with the first pending save
    # without putting filesystem teardown under the global config lock.
    with store._exclusive_lock(_lane_reset_lock_path(store), what="lane delivery/reset lock"):
        try:
            lane_data = lane_mod.load_lanes(store)
        except lane_mod.LaneError as exc:
            sys.stderr.write(
                f"agenttalk reset: refusing to erase corrupt lane recovery state ({exc}); "
                "repair lanes.json before reset.\n"
            )
            return 2
        recovery = lane_mod.recovery_lanes(lane_data)
        if recovery:
            lane_ids = ", ".join(str(lane.get("lane_id") or "?") for lane in recovery)
            sys.stderr.write(
                "agenttalk reset: refusing to erase incomplete lane delivery journal(s) "
                f"({lane_ids}) while artifacts/worktrees survive; run `lane status`, then "
                "`lane deliver` or `lane recover` first.\n"
            )
            return 2
        active = lane_mod.active_lanes(lane_data)
        if active:
            sys.stderr.write(
                f"warning: reset will clear {len(active)} ACTIVE lane(s) "
                f"({', '.join(ln.get('lane_id', '?') for ln in active)}); lane "
                "coordination state does not survive reset (delivery artifacts under "
                ".agenttalk/lane-deliveries/ are NOT touched).\n")
        cfg, archive_path = store.reset(archive=args.archive)
    if archive_path is not None:
        print(f"archived previous session to: {archive_path}")
    else:
        print("previous session deleted (no archive — pass --archive to keep it)")
    print(f"new session_id: {cfg['session_id']}")
    print(f"roster:         {', '.join(cfg.get('agents', []))}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Start the read-only local web dashboard / obligation dashboard.

    Shared by both spellings (0.17.0): ``serve`` (single root, lands on
    the message log ``/``) and ``dashboard`` (multi-root via repeatable
    ``--store``, lands on ``/dashboard``). The spelling changes only
    root selection, the URL printed, and the command name in errors —
    it is the SAME server (same loopback wall, same routes).
    """
    from agenttalk import web as _web
    landing = getattr(args, "landing", "/")
    spelling = "dashboard" if landing == "/dashboard" else "serve"
    host = getattr(args, "host", "127.0.0.1")  # dashboard has no --host
    stores = getattr(args, "stores", None)
    extra: list = []
    if stores:
        # Each --store PATH **is** the project root — no upward walk
        # (research D4: an explicit path must not silently resolve to a
        # parent project). Missing stores WARN, never refuse: they show
        # as degraded roots in /api/state until initialized.
        paths = [Path(p).resolve() for p in stores]
        for p in paths:
            if not (p / ".agenttalk").is_dir():
                sys.stderr.write(
                    f"warning: {p} has no .agenttalk store yet — it will "
                    f"appear as a degraded root until initialized\n")
        descs = _web.make_descriptors(paths)
        store = descs[0].store
        extra = list(descs[1:])
    else:
        store = _get_store(args)
    try:
        srv = _web.make_server(store, host, args.port, quiet=args.quiet,
                               extra=extra,
                               enable_actions=getattr(args, "enable_actions", False))
    except ValueError as e:  # non-loopback host refusal — keep FIRST
        sys.stderr.write(f"agenttalk {spelling}: {e}\n")
        return 2
    except OSError as e:  # bind failure (FR-010, live repro 2026-06-07)
        sys.stderr.write(
            f"agenttalk {spelling}: could not bind {host}:{args.port} — {e}\n"
            f"  Another program is probably listening on this port.\n"
            f"  Try `--port 0` (the OS picks a free port) or another "
            f"--port.\n")
        return 2
    actual_port = srv.server_address[1]
    url = _web._format_url(host, actual_port)
    if landing == "/dashboard":
        sys.stderr.write(
            f"agenttalk: serving obligation dashboard at {url}dashboard\n")
    else:
        sys.stderr.write(f"agenttalk: serving read-only dashboard at {url}\n")
    sys.stderr.write("           (Ctrl-C to stop)\n")
    sys.stderr.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\nagenttalk: dashboard stopped\n")
    finally:
        srv.server_close()
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    """Step-0 bootstrap: optionally initialize an explicit root, then start the console."""
    from agenttalk import web as _web

    explicit_location = bool(args.here or args.path or getattr(args, "root", None))
    if args.path:
        root = Path(args.path).resolve()
    elif args.here:
        root = Path.cwd().resolve()
    elif getattr(args, "root", None):
        root = Path(args.root).resolve()
    else:
        root = find_root()
    store = Store(root)
    if not store.initialized():
        if not args.init_if_absent:
            sys.stderr.write(
                f"agenttalk start: not initialized at {root}; pass --init-if-absent "
                "with an explicit --here/--path/--root and --agents\n")
            return 2
        if not explicit_location or not args.agents:
            sys.stderr.write(
                "agenttalk start: --init-if-absent requires an explicit location "
                "(--here, --path, or global --root) and --agents a,b\n")
            return 2
        init_args = argparse.Namespace(path=str(root), root=str(root),
                                       agents=args.agents, force=False)
        rc = cmd_init(init_args)
        if rc != 0:
            return rc
    supervisor_present = (store.dir / "supervisor.ps1").exists()
    would_start_supervisor = bool(
        supervisor_present and not args.no_supervisor and os.name == "nt"
    )
    if args.dry_run:
        print(json.dumps({
            "root": str(root), "initialized": store.initialized(),
            "actions_enabled": bool(args.enable_actions),
            "supervisor_present": supervisor_present,
            "would_start_supervisor": would_start_supervisor,
        }, indent=2))
        return 0
    selected_host = None
    if would_start_supervisor:
        try:
            if args.pwsh:
                supervisor_lifecycle.select_powershell_host(
                    store, explicit_path=args.pwsh,
                )
            sup.validate_artifact_bundle(store, boundary="full")
            selected_host = supervisor_lifecycle.read_selected_host(store)
        except (OSError, sup.ArtifactValidationError,
                supervisor_lifecycle.SupervisorLifecycleError) as e:
            sys.stderr.write(f"agenttalk start: {e}\n")
            return 3
        warning = selected_host.get("_warning")
        if warning:
            sys.stderr.write(f"agenttalk start: WARN: {warning}\n")
    try:
        srv = _web.make_server(
            store, args.host, args.port, quiet=args.quiet,
            enable_actions=args.enable_actions)
    except ValueError as e:
        sys.stderr.write(f"agenttalk start: {e}\n")
        return 2
    except OSError as e:
        sys.stderr.write(
            f"agenttalk start: could not bind {args.host}:{args.port} - {e}\n")
        return 2
    actual_port = srv.server_address[1]
    url = _web._format_url(args.host, actual_port)
    proc = None
    if would_start_supervisor:
        try:
            with supervisor_lifecycle.selected_host_for_spawn(store) as launch_host:
                proc = subprocess.Popen(  # noqa: S603  # nosec B603
                    [str(launch_host["path"]), "-NoLogo", "-NoProfile",
                     "-NonInteractive", "-File", str(store.dir / "supervisor.ps1")],
                    cwd=str(store.dir),
                    stdout=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        except (OSError, supervisor_lifecycle.SupervisorLifecycleError) as e:
            srv.server_close()
            sys.stderr.write(f"agenttalk start: supervisor.ps1 launch failed ({e})\n")
            return 3
    if not args.no_browser:
        webbrowser.open(url)
    sys.stderr.write(f"agenttalk: serving team console at {url}\n")
    if proc is not None:
        sys.stderr.write(f"           supervisor started pid={proc.pid}\n")
    sys.stderr.write("           (Ctrl-C to stop)\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\nagenttalk: dashboard stopped\n")
    finally:
        srv.server_close()
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    """Send a `release` (stand-down) signal so a listener exits its loop.

    Distinct from `end`: NO transcript export and the agent may be restarted
    later. A single `--to` is a metadata-clean point-to-point send (opens no
    thread — no request_id/broadcast_id); `--to-group`/`--all` fan the same
    clean signal out with a lightweight delivered/missed report (re-run to
    retry — there is no `--resume` correlation id by design). Only the
    operator-facing / sole-lead sender's release is authoritative; the command
    warns (non-fatal) otherwise, and the listen skill is the enforcement point.
    """
    store = _get_store(args)
    cfg = store.load_config()
    sender = _resolve_self(args.sender, roster=cfg.get("agents") or [])
    if sum(bool(x) for x in (args.recipient, args.to_group, args.all)) != 1:
        sys.stderr.write("agenttalk release: specify exactly one of "
                         "--to <agent>, --to-group <group>, or --all\n")
        return 2
    # Stand-down AUTHORITY envelope (0.39.0): a release only stands a listener down
    # when it is a typed, authority-marked control from the authorized relay. Exactly
    # one mode is required - --relay-human (relaying a human operator's decision) or
    # --emergency (the lead's narrow malfunctioning/rogue override) - and BOTH require
    # a --reason. A bare/unmarked release sends NOTHING (exit 2).
    relay_human = bool(getattr(args, "relay_human", False))
    emergency = bool(getattr(args, "emergency", False))
    if relay_human == emergency:   # neither or both
        sys.stderr.write(
            "agenttalk release: specify exactly one authority mode - --relay-human "
            "(you are relaying a human operator's stand-down decision) or --emergency "
            "(narrow lead override for a malfunctioning/rogue agent). A bare release "
            "stands no one down.\n")
        return 2
    reason = (args.message if args.message is not None else None)
    if reason is None and args.file:
        reason = _read_body(args)
    if not (reason and reason.strip()):
        sys.stderr.write(
            "agenttalk release: --reason (-m) is required - record WHY (the human's "
            "decision, or why an emergency could not wait for human confirmation).\n")
        return 2
    # Authority FAILS CLOSED: only the operator-facing liaison, else the sole
    # role=lead, may relay a loop-exit. No liaison + no sole lead -> exit 2, NO
    # message (configure one; `roster set-operator-facing` / a single `set-role lead`).
    if not store.loop_exit_relay_authorized(sender):
        sys.stderr.write(
            f"agenttalk release: {sender!r} is not the authorized stand-down relay "
            "(operator-facing liaison, else the sole role=lead) - refusing, NO message "
            "sent. Stand-down is a human-relayed act; configure a liaison or a single "
            "lead.\n")
        return 2
    if relay_human:
        release_meta = {"release_authority": "human", "operator_decision": "true",
                        "authority_reason": reason}
    else:
        release_meta = {"release_authority": "emergency", "emergency": "true",
                        "operator_report_required": "true", "authority_reason": reason}
    body = reason
    if args.recipient:
        # Single target: let store.send validate (self-mail / off-roster /
        # retired -> ValueError -> exit 2 via main). Clean meta: kind=release
        # is not an opener, so send() mints no request_id/broadcast_id.
        store.send(sender=sender, recipient=args.recipient, body=body,
                   kind="release", meta=release_meta)
        if not args.quiet:
            mode = "emergency" if emergency else "human-relayed"
            print(f"released ({mode}): {args.recipient} (stood down)")
        return 0
    # Group / all: resolve to a concrete active recipient list and fan out the
    # same clean signal. No correlation id, so no --resume — re-run to retry.
    target = "all" if args.all else args.to_group
    try:
        recipients = store.resolve_audience(target, exclude=sender)
    except ValueError as e:
        sys.stderr.write(f"agenttalk release: {e}\n")
        return 2
    if not recipients:
        sys.stderr.write(
            f"agenttalk release: audience {target!r} has no recipients "
            f"besides {sender}.\n")
        return 2
    sent: list = []
    failure: Exception | None = None
    for r in recipients:
        try:
            sent.append(store.send(sender=sender, recipient=r, body=body,
                                   kind="release", meta=release_meta))
        except Exception as e:  # noqa: BLE001 — account every copy
            failure = e
            break
    if failure is not None:
        delivered = [m.recipient for m in sent]
        missed = [r for r in recipients if r not in delivered]
        if not args.quiet:
            print(f"delivered=[{', '.join(delivered)}]")
            print(f"missed=[{', '.join(missed)}]")
        sys.stderr.write(
            f"agenttalk release: partial — copy for {missed[0]!r} failed: "
            f"{failure}. Re-run to retry the missed recipients.\n")
        return 5
    if not args.quiet:
        n = len(sent)
        print(f"released: {', '.join(m.recipient for m in sent)} "
              f"({n} agent{'s' if n != 1 else ''} stood down)")
    return 0


def _load_supervisor_config(
    store: Store,
    *,
    expected_sha256: str | None = None,
    require_powershell_transport: bool = False,
) -> dict:
    return sup.load_supervisor_config(
        store.dir / "supervisor.json",
        expected_sha256=expected_sha256,
        powershell_transport_store=(
            store if require_powershell_transport else None
        ),
    )


def cmd_heartbeat(args: argparse.Namespace) -> int:
    """Stamp this agent's ACTIVITY heartbeat (the supervisor's stuck signal).

    Wired as a Claude PostToolUse / Codex hook so it fires on every tool call
    while the model is WORKING (the wait loop already stamps it while IDLE), so
    the heartbeat is fresh in both states and goes stale only when the model is
    genuinely stuck. THROTTLED: a no-op when the heartbeat is younger than
    --min-interval, so the per-tool-call hook never pays Python startup more
    than once per interval (the stuck threshold is ~120s; ~5s granularity is
    plenty).
    """
    if getattr(args, "fallback_for", None) and not getattr(args, "hook", False):
        sys.stderr.write("agenttalk heartbeat: --fallback-for requires --hook\n")
        return 2
    if getattr(args, "hook", False):
        # HOOK MODE (wired as PostToolUse / Codex hook): must NEVER block a tool
        # call AND must stay SILENT (it fires on every tool call, so any output
        # would spam the transcript). The strict helpers (_get_store /
        # _resolve_self) write to stderr BEFORE raising SystemExit, so catching the
        # exit is not enough - we must REDIRECT stdout+stderr to a throwaway sink
        # around the whole call, then swallow every error and exit 0 (unresolved/
        # off-roster identity, uninitialized/missing store, write failure, anything).
        # Manual `agenttalk heartbeat` keeps the strict, noisy path below.
        sink = io.StringIO()
        try:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                return _do_heartbeat(args)
        except SystemExit:
            return 0   # _resolve_self / _get_store usage-exit -> soft no-op
        except Exception:  # noqa: BLE001 - a hook must never propagate / block
            return 0
    return _do_heartbeat(args)


def _do_heartbeat(args: argparse.Namespace) -> int:
    """Strict heartbeat stamp (throttled). Raises on a bad identity / store."""
    store = _get_store(args)
    roster = store.load_config().get("agents") or []
    agent = _resolve_heartbeat_agent(args, roster=roster)
    min_interval = max(0.0, args.min_interval)
    if min_interval > 0:
        hb = store.read_heartbeat(agent)
        if hb is not None and (time.time() - hb.timestamp()) < min_interval:
            return 0  # still fresh — throttled no-op
    store.write_heartbeat(agent)
    return 0


def _resolve_heartbeat_agent(args: argparse.Namespace, *, roster: list[str]) -> str:
    if getattr(args, "agent", None) or os.environ.get("AGENTTALK_SELF"):
        return _resolve_self(args.agent, roster=roster)
    if getattr(args, "hook", False) and getattr(args, "fallback_for", None):
        fallback = args.fallback_for
        try:
            validate_agent_name(fallback)
        except ValueError as e:
            sys.stderr.write(f"agenttalk: {e}\n")
            sys.exit(2)
        _ensure_in_roster(fallback, roster, label="fallback")
        return fallback
    return _resolve_self(args.agent, roster=roster)


def _checkpoint_root_hint(args: argparse.Namespace) -> Path | None:
    raw = getattr(args, "root", None) or os.environ.get("AGENTTALK_ROOT")
    if raw:
        try:
            return Path(raw).resolve()
        except BaseException:  # called only from a fail-soft hook error path
            return None
    try:
        return find_root()
    except BaseException:  # called only from a fail-soft hook error path
        return None


def _resolve_checkpoint_agent(args: argparse.Namespace, *, roster: list[str]) -> str:
    if not getattr(args, "hook", False):
        return _resolve_self(args.agent, roster=roster)
    explicit = getattr(args, "agent", None)
    name = explicit or os.environ.get("AGENTTALK_SELF")
    if not name:
        name = getattr(args, "fallback_for", None)
    if not name:
        raise ValueError(
            "no agent identity: pass --for or set AGENTTALK_SELF"
        )
    validate_agent_name(name)
    if roster and name not in roster:
        raise ValueError(
            f"agent {name!r} is not in the project roster {sorted(roster)}"
        )
    return name


def _checkpoint_fallback_requires_hook(args: argparse.Namespace) -> int | None:
    if getattr(args, "fallback_for", None) and not getattr(args, "hook", False):
        sys.stderr.write(
            "agenttalk checkpoint: --fallback-for requires --hook\n"
        )
        return 2
    return None


def _do_checkpoint_save(args: argparse.Namespace) -> Path:
    store = _get_store(args)
    config = (
        checkpoint_mod.read_checkpoint_config(store)
        if getattr(args, "hook", False)
        else store.load_config()
    )
    roster = config.get("agents") or []
    agent = _resolve_checkpoint_agent(args, roster=roster)
    hook_payload = checkpoint_mod.read_hook_payload() if args.hook else {}
    payload = checkpoint_mod.build_checkpoint(
        store,
        agent,
        hook_payload=hook_payload,
        trigger=args.trigger,
        capacity_source="claude" if args.hook else "auto",
        session_scoped_context=args.hook,
    )
    return checkpoint_mod.save_checkpoint(store, agent, payload)


def _run_checkpoint_hook(
    args: argparse.Namespace,
    action: str,
    callback,
):
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            value, error = callback(), None
    except BaseException as exc:
        value, error = None, exc
    if error is not None:
        try:
            checkpoint_mod.log_hook_error(
                _checkpoint_root_hint(args),
                action,
                error,
            )
        except BaseException:
            return value
    return value


def cmd_checkpoint_save(args: argparse.Namespace) -> int:
    invalid = _checkpoint_fallback_requires_hook(args)
    if invalid is not None:
        return invalid
    if args.hook:
        _run_checkpoint_hook(args, "save", lambda: _do_checkpoint_save(args))
        return 0
    path = _do_checkpoint_save(args)
    print(f"checkpoint saved: {path}")
    return 0


def _read_checkpoint_for_args(args: argparse.Namespace) -> tuple[str, dict | None]:
    store = _get_store(args)
    config = (
        checkpoint_mod.read_checkpoint_config(store)
        if getattr(args, "hook", False)
        else store.load_config()
    )
    roster = config.get("agents") or []
    agent = _resolve_checkpoint_agent(args, roster=roster)
    return agent, checkpoint_mod.read_checkpoint(store, agent)


def cmd_checkpoint_resume(args: argparse.Namespace) -> int:
    invalid = _checkpoint_fallback_requires_hook(args)
    if invalid is not None:
        return invalid
    if args.hook:
        result = _run_checkpoint_hook(
            args,
            "resume",
            lambda: _read_checkpoint_for_args(args),
        )
        payload = result[1] if isinstance(result, tuple) and len(result) == 2 else None
        try:
            output = checkpoint_mod.session_start_output(payload)
        except BaseException:
            output = checkpoint_mod.EMPTY_SESSION_START_OUTPUT
        try:
            sys.stdout.write(output + "\n")
            sys.stdout.flush()
        except BaseException:
            return 0
        return 0
    agent, payload = _read_checkpoint_for_args(args)
    if payload is None:
        print(f"checkpoint resume: no checkpoint found for {agent}")
        return 0
    print(checkpoint_mod.render_resume_context(payload))
    return 0


def cmd_checkpoint_show(args: argparse.Namespace) -> int:
    agent, payload = _read_checkpoint_for_args(args)
    if payload is None:
        sys.stderr.write(
            f"agenttalk checkpoint show: no checkpoint found for {agent}\n"
        )
        return 1
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(checkpoint_mod.render_resume_context(payload))
    return 0


def cmd_request_restart(args: argparse.Namespace) -> int:
    """Queue a MANUAL restart of an agent (the supervisor relaunches + clears).

    Writes an atomic, request_id-scoped state/<agent>.restart-request marker.
    A protected agent (operator_facing / role=lead) needs --force-protected,
    and a fresh protected heartbeat also needs --acknowledge-live-protected-kill,
    enforced by the supervisor.
    """
    store = _get_store(args)
    roster = store.load_config().get("agents") or []
    agent = args.agent
    if agent not in roster:
        sys.stderr.write(f"agenttalk request-restart: {agent!r} is not in the "
                         f"roster {sorted(roster)}\n")
        return 2
    from agenttalk import supervisor as _sup
    requested_by = _resolve_self(args.sender, roster=roster)
    authority = _sup.resolve_restart_request_authority(
        store,
        requested_by,
        force_protected=bool(args.force_protected),
        acknowledge_live_protected_kill=bool(
            getattr(args, "acknowledge_live_protected_kill", False)),
    )
    if authority.get("authority_result") != "authorized":
        sys.stderr.write("agenttalk request-restart: requester is not authorized "
                         f"({authority.get('authority_reason')}).\n")
        return 2
    rid = "rr-" + uuid.uuid4().hex[:12]
    marker = {
        "agent": agent,
        "request_id": rid,
        "source": "manual",
        "requested_by": requested_by,
        "authorized_by": authority.get("authorized_by"),
        "authority_result": authority.get("authority_result"),
        "authority_reason": authority.get("authority_reason"),
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "at_epoch": time.time(),
        "force_protected": bool(args.force_protected),
        "force_protected_authorized": bool(authority.get("force_protected_authorized")),
        "force_protected_authorized_by": authority.get("force_protected_authorized_by"),
        "acknowledge_live_protected_kill": bool(
            getattr(args, "acknowledge_live_protected_kill", False)),
        "acknowledge_live_protected_kill_authorized": bool(
            authority.get("acknowledge_live_protected_kill_authorized")),
        "acknowledge_live_protected_kill_by": authority.get(
            "acknowledge_live_protected_kill_by"),
        "reason": args.reason or "",
    }
    store.write_restart_request(agent, marker)
    extra = " (force-protected)" if args.force_protected else ""
    blocked = False
    try:
        supervisor_state = _read_supervisor_state(store)
        row = (supervisor_state.get("agents") or {}).get(agent)
        tree = row.get("owned_process_tree") if isinstance(row, dict) else None
        blocked = bool(
            isinstance(tree, dict)
            and tree.get("status") in {"invalid", "truncated"}
        )
    except Exception:  # noqa: BLE001 - acknowledgement must not promise progress
        blocked = False
    if blocked:
        print(
            f"request-restart: recorded request for {agent!r} [{rid}]{extra}, "
            "but automatic recovery is currently refused by a process-tree "
            "HOLD. This request is blocked, not pending progress; see "
            "`agenttalk attention`."
        )
    else:
        print(
            f"request-restart: queued request for {agent!r} [{rid}]{extra} "
            "for supervisor assessment; recording the request does not establish "
            "that relaunch is currently admissible."
        )
    return 0


def cmd_request_launch(args: argparse.Namespace) -> int:
    """Queue an evidence-only ephemeral adversarial review launch."""
    from agenttalk import close as close_mod

    store = _get_store(args)
    cfg = store.load_config()
    roster = cfg.get("agents") or []
    requested_by = _resolve_self(args.sender, roster=roster)
    rid = args.request_id or eph.new_request_id()
    if not eph.is_safe_id(rid):
        sys.stderr.write(f"agenttalk request-launch: unsafe request_id {rid!r}\n")
        return 2
    try:
        revision, _kind = _resolve_revision(store.root, args.revision)
    except close_mod.CloseError as e:
        sys.stderr.write(f"agenttalk request-launch: {e}\n")
        return 2
    prompt = _read_body(args)
    if not prompt.strip():
        sys.stderr.write("agenttalk request-launch: prompt is required (-m/--file/stdin)\n")
        return 2
    marker = {
        "schema_version": eph.SCHEMA_VERSION,
        "kind": eph.REQUEST_KIND,
        "request_id": rid,
        "state": eph.STATE_QUEUED,
        "requested_by": requested_by,
        "at": _iso_now(),
        "at_epoch": time.time(),
        "profile": args.profile,
        "skill": args.skill,
        "prompt": prompt,
        "scope": {
            "revision": revision,
            "base_revision": args.base_revision,
            "paths": args.path or [],
            "summary": args.summary or "",
        },
    }
    if getattr(args, "lane_id", None):
        try:
            lane_id = lane_mod.validate_lane_id(args.lane_id)
        except lane_mod.LaneError as e:
            sys.stderr.write(f"agenttalk request-launch: {e}\n")
            return 2
        marker["lane_id"] = lane_id
        marker["scope"]["lane_id"] = lane_id
    if args.timeout_seconds is not None:
        marker["timeout_seconds"] = args.timeout_seconds
    if args.role:
        marker["role"] = args.role
    if args.group:
        marker["groups"] = args.group
    errors = eph.validate_marker(marker)
    if errors:
        sys.stderr.write("agenttalk request-launch: " + "; ".join(errors) + "\n")
        return 2
    try:
        store.write_launch_request(marker)
    except ValueError as e:
        sys.stderr.write(f"agenttalk request-launch: {e}\n")
        return 2
    print(f"request-launch: queued ephemeral review [{rid}] for {revision[:12]} "
          f"(profile={args.profile}, skill={args.skill})")
    return 0


def _dead_letter_notifier(store, agent: str):
    """Operator escalation for the wrapper dead-letter path: route a notice to the
    operator_facing liaison else the sole lead when a message is dead-lettered or hits the
    high-attempt backstop. Returns True iff it ROUTED (a target resolved + the send
    succeeded) so the loop can record whether the operator was actually signalled; when no
    target resolves it returns False (doctor surfaces the unrouted backstop LOUD).
    The notice mints an ``esc-`` request_id + needs_operator=true (mirroring `escalate`)
    so it THREADS and shows in the liaison's `sync` OPERATOR INPUT NEEDED bucket - not as
    an unread FYI (reviewer-1 blocker). NEVER crashes the loop."""
    def emit(info: dict, *, disposed: bool) -> bool:
        try:
            from agenttalk import attention as A
            target = store.operator_facing() or store.sole_lead()
            if not target or target == agent:
                return False
            mid = info.get("msg_id")
            ag = info.get("agent")
            if info.get("failure_class") == "config_blocked":
                summary = str(info.get("summary") or "deterministic exec/permission denial")
                body = (
                    f"[wrapper-config-blocked] agent {ag} is PARKED on message {mid} "
                    f"from {info.get('from')} (kind={info.get('kind')}, "
                    f"attempts={info.get('attempts')}). Cursor is unchanged; the message "
                    "was NOT committed or disposed. "
                    f"Command/error/remediation: {summary}. After repairing the child "
                    f"bus invocation/config, run: agenttalk request-restart --for {ag}"
                )
                store.send(sender=agent, recipient=target, kind="question",
                           subject="wrapper config-blocked", body=body,
                           meta={"needs_operator": "true", "dead_letter": "true",
                                 "config_blocked": "true",
                                 "dl_msg_id": str(mid),
                                 "dl_disposed": "false",
                                 "request_id": "esc-" + uuid.uuid4().hex[:12]})
                return True
            state = A.dead_letter_notice_state(info, disposed=disposed)
            generation = str(
                info.get("requeue_generation")
                or info.get("first_started_at")
                or info.get("first_at")
                or "unknown"
            )
            should_emit, notice = A.should_emit_dead_letter_notice(
                store, agent=str(ag), message_id=str(mid),
                generation=generation, state=state)
            if not should_emit:
                return True
            request_id = "esc-" + uuid.uuid4().hex[:12]
            verb = "DEAD-LETTERED" if disposed else "repeatedly FAILING (not yet dead-lettered)"
            # #202 D3 (review finding 9): carry the REAL reason/remedy, not only the
            # class - e.g. the interruption-budget escalation's concrete remedy.
            reason = str(info.get("summary") or "")
            body = (f"[dead-letter] agent {ag} {verb} message {mid} from "
                    f"{info.get('from')} (kind={info.get('kind')}, "
                    f"attempts={info.get('attempts')}, class={info.get('failure_class')}). "
                    + (f"Reason: {reason}  " if reason else "")
                    + f"Inspect: agenttalk dead-letter show --agent {ag} --id {mid}")
            if disposed:
                body += f"  Requeue: agenttalk dead-letter requeue --agent {ag} --id {mid}"
            store.send(sender=agent, recipient=target, kind="question",
                       subject="dead-letter notice", body=body,
                       meta={"needs_operator": "true", "dead_letter": "true",
                             "dl_msg_id": str(mid),
                             "dl_disposed": "true" if disposed else "false",
                             "request_id": request_id})
            with contextlib.suppress(Exception):
                A.append_notice_event(store, {
                    "schema_version": A.SCHEMA_VERSION,
                    "kind": A.NOTICE_DEAD_LETTER,
                    "notice_key": notice["notice_key"],
                    "request_id": request_id,
                    "agent": str(ag),
                    "message_id": str(mid),
                    "generation": generation,
                    "state": state,
                    "state_hash": notice["state_hash"],
                    "at": _attn_now_iso(),
                    "warnings": notice.get("warnings") or [],
                })
            return True
        except Exception:  # noqa: BLE001 - a notification must never crash the loop
            return False   # not routed; dead-letter + doctor visibility remain
    return emit


def _cadence_health_notifier(store, agent: str):
    """Operator escalation for a FAILING cadence sweep (WP3 condition 6): controller-HEALTH
    trouble, NOT message poison. Routes a notice to the operator_facing liaison else the
    sole lead when the controller's proactive sweep has failed repeatedly. Mirrors
    :func:`_dead_letter_notifier` (esc- request_id + needs_operator=true so it THREADS into
    the liaison's OPERATOR INPUT NEEDED bucket); the caller dedupes via the cadence state's
    ``health_escalated`` latch, so this fires ONCE per failure run. NEVER crashes the loop."""
    def emit(fails: int, reason: str) -> bool:
        try:
            target = store.operator_facing() or store.sole_lead()
            if not target or target == agent:
                return False
            body = (f"[controller-health] lead-loop controller {agent} cadence sweep has "
                    f"FAILED {fails} consecutive times ({reason}). This is a CONTROLLER "
                    f"health problem, not a poisoned message. Inspect: agenttalk doctor / "
                    f"agenttalk status --json; consider agenttalk request-restart "
                    f"--for {agent} if it does not recover.")
            store.send(sender=agent, recipient=target, kind="question",
                       subject="cadence controller-health notice", body=body,
                       meta={"needs_operator": "true", "cadence_health": "true",
                             "cadence_fails": str(fails),
                             "request_id": "esc-" + uuid.uuid4().hex[:12]})
            return True
        except Exception:  # noqa: BLE001 - a notification must never crash the loop
            return False
    return emit


def _launch_config_blocked_notifier(store, agent: str):
    """Operator escalation for a pre-loop wrapper launch preflight failure."""
    def emit(summary: str) -> bool:
        try:
            target = store.operator_facing() or store.sole_lead()
            if not target or target == agent:
                return False
            body = (
                f"[wrapper-launch-config-blocked] agent {agent} did not enter the "
                "wrapper loop because launch/runtime preflight failed before any "
                "message was consumed. Cursor, attempts, and dead-letter state are "
                f"unchanged. Command/error/remediation: {summary}. After repairing "
                f"the launch config, run: agenttalk request-restart --for {agent}"
            )
            store.send(
                sender=agent,
                recipient=target,
                kind="question",
                subject="wrapper launch config-blocked",
                body=body,
                meta={
                    "needs_operator": "true",
                    "config_blocked": "true",
                    "launch_config_blocked": "true",
                    "request_id": "esc-" + uuid.uuid4().hex[:12],
                },
            )
            return True
        except Exception:  # noqa: BLE001 - a launch notice must never crash the wrapper
            return False
    return emit


def _handle_launch_config_blocked(store, agent: str, cli_name: str, *,
                                  mode: str, min_interval: float,
                                  summary: str) -> int:
    from agenttalk import health as health_model
    from .wrapper import loop as wloop
    from .wrapper.health import WrapperHealthWriter

    raw = store.read_health_raw(agent) or {}
    already_reported = (
        raw.get("state") == health_model.STATE_ERRORED_AMBIGUOUS
        and raw.get("reason_code") == "config_blocked"
    )
    health_writer = WrapperHealthWriter(
        store, agent, cli_name, mode=mode, min_interval=min_interval)
    sig = {"error": summary, "config_blocked": True, "config_blocked_text": summary}
    store.write_config_blocked_hold(agent, summary=summary)
    health_writer.failure(sig, wloop.CLASS_CONFIG_BLOCKED)
    try:
        store.write_heartbeat(agent)
    except Exception as exc:  # noqa: BLE001 - health/escalation still carry the failure
        _ = exc
    if not already_reported:
        _launch_config_blocked_notifier(store, agent)(summary)
    sys.stderr.write(f"agenttalk wrap: launch config-blocked: {summary}\n")
    return 1


def _unquote_toml_scalar(value: str) -> str:
    """Strip ONE matching pair of surrounding quotes from a codex ``-c key="V"``
    TOML scalar so ``model="opus"`` and ``model=opus`` scan the same. Pure."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value


def scan_model_effort(argv: list[str], cli: str) -> dict:
    """Return the model/effort tokens PRESENT in ``argv`` for ``cli`` (v0.75.0),
    recognizing ALL forms. Pure; used for BOTH operator-tail conflict detection
    and EFFECTIVE-value extraction for the runtime fingerprint. Last occurrence
    wins (mirrors CLI last-flag-wins).

    All BUILD-CONFIRMED (codex clap 0.144.1) flag spellings are recognized so the
    operator-tail-wins contract holds for every one:
    codex model: ``-m V`` / ``-m=V`` / ``-mV`` / ``--model V`` / ``--model=V`` /
      ``-c model=V`` / ``-c model="V"`` / ``-c=model=V`` / ``-cmodel=V`` /
      ``--config model=V`` / ``--config=model=V``;
    codex effort: the same ``-c`` / ``--config`` families with key
      ``model_reasoning_effort`` (space / ``=``-attached / value-attached, quoted or not).
    claude: ``--model V`` / ``--model=V`` / ``--effort V`` / ``--effort=V``.
    """
    model: str | None = None
    effort: str | None = None
    i = 0
    n = len(argv)
    while i < n:
        tok = argv[i]
        nxt = argv[i + 1] if i + 1 < n else None
        if cli == "codex":
            # model: -m V | --model V
            if tok in ("-m", "--model") and nxt is not None:
                model = nxt
                i += 2
                continue
            # model: --model=V
            if tok.startswith("--model="):
                model = tok[len("--model="):]
                i += 1
                continue
            # model: -m=V | -mV (short attached, clap strips a single leading '=')
            if tok != "-m" and tok.startswith("-m") and not tok.startswith("--"):
                rest = tok[2:]
                model = rest[1:] if rest.startswith("=") else rest
                i += 1
                continue
            # config (model / model_reasoning_effort): extract the key=value token in
            # ANY form, then split on the FIRST '='.
            cfgval: str | None = None
            consumed = 1
            if tok in ("-c", "--config") and nxt is not None:
                cfgval = nxt
                consumed = 2
            elif tok.startswith("--config="):
                cfgval = tok[len("--config="):]
            elif tok != "-c" and tok.startswith("-c") and not tok.startswith("--"):
                rest = tok[2:]
                cfgval = rest[1:] if rest.startswith("=") else rest
            if cfgval is not None:
                key, sep, val = cfgval.partition("=")
                if sep:
                    k = key.strip()
                    if k == "model":
                        model = _unquote_toml_scalar(val)
                    elif k == "model_reasoning_effort":
                        effort = _unquote_toml_scalar(val)
                i += consumed
                continue
        elif cli == "claude":
            if tok == "--model" and nxt is not None:
                model = nxt
                i += 2
                continue
            if tok.startswith("--model="):
                model = tok[len("--model="):]
                i += 1
                continue
            if tok == "--effort" and nxt is not None:
                effort = nxt
                i += 2
                continue
            if tok.startswith("--effort="):
                effort = tok[len("--effort="):]
                i += 1
                continue
        i += 1
    return {"model": model, "effort": effort}


def inject_model_flags(argv: list[str], cli: str, model: str | None,
                       effort: str | None) -> tuple[list[str], list[str]]:
    """Append BARE model/effort tokens to a wrapped child's base argv (v0.75.0),
    unless the operator tail already sets that flag (in ANY form) — then no-op +
    warn (explicit tail wins). SELF-VALIDATING (D7): re-asserts the per-CLI
    preconditions and never emits a malformed or dash-shaped token, even though
    normal callers pre-validate. Pure + idempotent (a second call with the value
    already present no-ops). Returns ``(argv, warnings)``.

    Shell=False children (subprocess.Popen with a list): inject bare tokens —
    codex ``["-m", M]`` + ``["-c", f"model_reasoning_effort={E}"]`` (NO quotes);
    claude ``["--model", M]`` + ``["--effort", E]``.
    """
    from agenttalk import supervisor as _sup
    argv = list(argv)
    warnings: list[str] = []
    if cli not in ("codex", "claude"):
        return argv, warnings
    v_model, mw = _sup.validate_model(model, source="inject")
    if mw:
        warnings.append(mw)
    v_effort, ew = _sup.validate_reasoning_effort(effort, cli, source="inject")
    if ew:
        warnings.append(ew)
    present = scan_model_effort(argv, cli)
    if v_model is not None:
        if present["model"] is not None:
            warnings.append(
                f"model already set in the launch command ({present['model']!r}); "
                f"not injecting model={v_model!r} (explicit tail wins)")
        elif cli == "codex":
            argv += ["-m", v_model]
        else:
            argv += ["--model", v_model]
    if v_effort is not None:
        if present["effort"] is not None:
            warnings.append(
                f"reasoning_effort already set in the launch command "
                f"({present['effort']!r}); not injecting effort={v_effort!r} "
                "(explicit tail wins)")
        elif cli == "codex":
            argv += ["-c", f"model_reasoning_effort={v_effort}"]
        else:
            argv += ["--effort", v_effort]
    return argv, warnings


def _resolve_runtime_model_effort(cfg_agent, cli: str, flag_model: str | None,
                                  flag_effort: str | None
                                  ) -> tuple[str | None, str | None, list[str]]:
    """Resolve the effective (model, effort) for a wrapped loop child (v0.75.0):
    per-agent config first, then an explicit ``wrap --model/--effort`` flag OVERRIDES
    it (flag > per-agent config; NO global fallback). Every value is validated the
    same way; an invalid one is dropped with a warning (never bricks launch). An
    explicit launch-TAIL flag still wins over both — that layer is applied later by
    :func:`inject_model_flags`. Pure; returns ``(model, effort, warnings)``."""
    from agenttalk import supervisor as _sup
    warnings: list[str] = []
    model, w = _sup.resolve_model(cfg_agent)
    if w:
        warnings.append(w)
    effort, w = _sup.resolve_reasoning_effort(cfg_agent, cli)
    if w:
        warnings.append(w)
    if flag_model is not None:
        fm, w = _sup.validate_model(flag_model, source="--model")
        if w:
            warnings.append(w)
        if fm is not None:
            model = fm
    if flag_effort is not None:
        fe, w = _sup.validate_reasoning_effort(flag_effort, cli, source="--effort")
        if w:
            warnings.append(w)
        if fe is not None:
            effort = fe
    return model, effort, warnings


def _inject_claude_permission_mode(argv: list[str], cli: str,
                                   perm_mode: str | None) -> list[str]:
    """Ensure a wrapped Claude child receives the resolved ``--permission-mode``.

    A wrapped agent's supervisor ``session_args`` is empty, so the supervisor's
    ``{PERM_MODE}`` substitution never reaches the child — a supervised wrapped
    Claude would otherwise launch read-only (auto-denying every write). Apply the
    SAME resolved mode the supervisor uses for a non-wrapped Claude
    (``supervisor.claude_permission_mode``) to the child argv. No-op for a
    non-Claude CLI, an empty/None mode, or when ``--permission-mode`` is already
    present — an explicit operator tail always wins.
    """
    if cli != "claude" or not perm_mode:
        return argv
    # An explicit operator tail always wins — in EITHER form: the separated
    # `--permission-mode <mode>` or the GNU single-token `--permission-mode=<mode>`.
    # (Checking only exact-token membership would double-add on the equals form and,
    # if Claude is last-flag-wins, silently widen a narrower operator mode.)
    if any(a == "--permission-mode" or a.startswith("--permission-mode=") for a in argv):
        return argv
    return [*argv, "--permission-mode", perm_mode]


def _interruption_rejoin_for(store, agent: str, k_interrupted: int):
    """Build make_drive's #202 D4 rejoin provider (cli wiring, NOT the loop).

    Keyed per head id from the PERSISTED attempt ledger, so a rejoin can never leak
    across heads and survives relaunch. The one-shot loop has no ledger; it decorates
    the record in-memory (``interrupted_redelivery``) and is read here first. Returns
    the REJOIN CONTEXT block, or None on a first attempt / clean redelivery."""
    from agenttalk import reply_transport as _rt

    def _elapsed_text(last_at: object) -> str:
        if not isinstance(last_at, str) or not last_at.strip():
            return "an unknown time"
        try:
            at = datetime.fromisoformat(last_at.strip().replace("Z", "+00:00"))
        except ValueError:
            return "an unknown time"
        seconds = max(0.0, datetime.now(timezone.utc).timestamp() - at.timestamp())
        return f"~{seconds:.0f}s"

    def rejoin_for(record: dict) -> str | None:
        head_id = record.get("id")
        ctx = record.get("interrupted_redelivery")
        if isinstance(ctx, dict):
            kind = ctx.get("kind") or "unknown"
            count = ctx.get("consecutive") or 1
            last_at = ctx.get("last_failure_at")
        else:
            if not isinstance(head_id, str) or not head_id:
                return None
            rec = store.attempt_record(agent, head_id) or {}
            if not rec.get("last_interrupted"):
                return None
            kind = rec.get("last_interruption_kind") or "unknown"
            try:
                count = max(1, int(rec.get("interrupted_consecutive") or 1))
            except (TypeError, ValueError):
                count = 1
            last_at = rec.get("last_failure_at")
        lines = [
            f"Your previous turn on this message was INTERRUPTED ({kind}) "
            f"{_elapsed_text(last_at)} ago - the work was killed mid-turn, "
            "not rejected.",
        ]
        if kind == "turn_watchdog" and k_interrupted > 0:
            lines.append(
                f"This is interruption {count} of a budget of {k_interrupted}; "
                "at the budget the message is dead-lettered for the operator.")
        else:
            lines.append(f"Consecutive interruptions on this message: {count}.")
        if isinstance(head_id, str) and head_id:
            preserved = _rt.reply_draft_path(store, agent, head_id).with_suffix(
                ".interrupted.md")
            if preserved.is_file():
                lines.append(f"Your interrupted draft was preserved at: {preserved}")
        lines.append("Verify state before repeating side-effectful work; prefer "
                     "resuming over redoing.")
        return "\n".join(lines)

    return rejoin_for


def _wrap_loop_mode(store, agent: str, *, cli: str, base_argv: list[str],
                    sender: str, min_interval: float, render: bool,
                    one_shot_request_id: str | None = None,
                    k_poison: int = 3, k_escalate: int = 20,
                    k_interrupted: int = 3,
                    interruption_redrive_seconds: float = 60.0,
                    infra_exhaust_after_seconds: float = 14400.0,
                    infra_exhaust_min_attempts: int = 100,
                    noninfra_sub_ceiling: int | None = None,
                    lead_loop: bool = False,
                    supervisor_config: dict | None = None,
                    turn_watchdog: object | None = None,
                    work_heartbeat: object | None = None,
                    runtime_model: str | None = None,
                    runtime_effort: str | None = None,
                    runtime_fingerprint: str | None = None,
                    backend_profile: str | None = None,
                    profile_env: dict[str, str] | None = None,
                    supervisor_launch_nonce: str | None = None,
                    lifecycle_log: object | None = None) -> int:
    """The long-running supervised wrapper loop (design C): own the idle bus-wait +
    heartbeat, drive the CLI ONE turn per inbound message in structured-stream mode
    (session continuity owned here), then return to the wait. Runs until killed -
    the supervisor supervises THIS process. Manual /agenttalk.listen stays the
    default; this is the opt-in supervised mode.

    ``lead_loop`` (WP2): become the managed lead-loop CONTROLLER for ``agent`` - own
    the team mailbox via a renewable LEASE (acquired BEFORE the loop, renewed on every
    heartbeat) so an external consumer / a duplicate window cannot race the bus. Exit
    states the supervisor distinguishes via the exit MARKER: BLOCKED acquire (another
    live owner) -> ``_LEAD_LOOP_BLOCKED_EXIT`` + blocked marker (HOLD, no relaunch);
    a clean VALID human release/end -> release lease + ``_LEAD_LOOP_STOOD_DOWN_EXIT`` +
    stood-down marker (no relaunch, v0.39 authority sticks); a crash -> best-effort
    release + re-raise + NO marker (the supervisor relaunches and re-acquires)."""
    from .wrapper import loop as wloop
    from .wrapper import run as wrapper_run
    from .wrapper.health import WrapperHealthWriter
    from .wrapper import session as wsession
    from .wrapper_runtime import WrapperRuntimeWriter
    from .wrapper_logs import WrapperLifecycleLog

    # One observational generation spans this wrapper process.  It is safe to
    # expose to the child (unlike the lead-loop lease id): it grants no mailbox
    # authority and only binds body-free --await-reply markers to this loop.
    wrapper_generation = uuid.uuid4().hex
    if lifecycle_log is None:
        lifecycle_log = WrapperLifecycleLog.from_environment(
            agent,
            expected_nonce=supervisor_launch_nonce,
        )

    def _wrapper_exit(code: int, reason: str) -> int:
        lifecycle_log.wrapper_exited(code, reason=reason)
        return code

    # --- managed lead-loop CONTROLLER lease lifecycle (WP2) -------------------
    lease_id: str | None = None
    heartbeat = None     # Callable[[], None] | None - combined stamp (lead-loop)
    pre_commit = None    # Callable[[], None] | None - ownership gate at consume boundaries
    if lead_loop:
        if not store.is_managed_lead_loop(agent):
            sys.stderr.write(
                f"agenttalk wrap --lead-loop: {agent!r} is not a configured managed "
                f"lead-loop identity (run `agenttalk managed-lead-loop set {agent}`)\n")
            return _wrapper_exit(2, "managed_lead_loop_not_configured")
        timing = lead_loop_runtime.resolve_timing(
            store, agent, supervisor_config=supervisor_config)
        ttl = timing["ttl_seconds"]
        # Thread the RESOLVED heartbeat window into acquire (WP2 residual #3): a
        # duplicate controller must not steal earlier than the supervisor would call
        # the owner stuck - and the steal threshold must match the visibility paths.
        lease = store.acquire_lead_loop_lease(
            agent, owner_pid=os.getpid(), ttl_seconds=ttl,
            wrapper_generation=wrapper_generation,
            heartbeat_stale_after=timing["heartbeat_stale_after"])
        if lease is None:
            existing = store.read_lead_loop_lease(agent) or {}
            store.write_lead_loop_exit(
                agent, state=store.LEAD_LOOP_EXIT_BLOCKED,
                owner_pid=existing.get("owner_pid"),
                reason="acquire blocked: another live managed lead-loop owner holds the lease")
            sys.stderr.write(
                f"agenttalk wrap --lead-loop: {agent!r} mailbox is already owned by a live "
                f"controller (PID {existing.get('owner_pid')}); standing down "
                f"(supervisor HOLD, no relaunch).\n")
            return _wrapper_exit(
                _LEAD_LOOP_BLOCKED_EXIT,
                "managed_lead_loop_acquire_blocked",
            )
        lease_id = lease["lease_id"]
        store.clear_lead_loop_exit(agent)  # a live controller makes any prior exit state moot

        def _renew_or_lost() -> None:
            # Renew the lease; a None result means we have LOST ownership (stolen / torn /
            # force-released). That is a HARD signal: RAISE so the loop STOPS at once - a
            # controller that no longer owns the mailbox must NOT keep consuming it
            # unguarded (else, in the stolen-owner case, both consume = the duplicate-
            # consumer race the lease prevents). Caught in _wrap_loop_mode -> release +
            # exit with NO marker -> supervisor relaunches (re-acquire, or HOLD if another
            # owner is now live). lease_id lives in this LOCAL closure - never os.environ,
            # so it is never leaked to the model child (codex WP2 blockers).
            if store.renew_lead_loop_lease(agent, lease_id=lease_id, ttl_seconds=ttl) is None:
                raise _LeadLoopLeaseLost(agent)

        def heartbeat() -> None:
            # Combined stamp on BOTH the idle stamp and make_drive streaming (WP2 cond 4):
            # renew the lease (hard-fail on loss) THEN refresh the supervisor heartbeat.
            _renew_or_lost()
            store.write_heartbeat(agent)

        # the SAME hard ownership check guards every cursor-advance boundary in the loop
        # (commit on success/control/invalid + dead-letter dispose), so a lost lease can
        # never advance the cursor / consume a record unguarded (codex WP2 blocker).
        pre_commit = _renew_or_lost

    def _release() -> None:
        if lease_id is not None:
            store.release_lead_loop_lease(agent, lease_id=lease_id)

    state = wsession.load_session(store, agent, cli)
    # v0.75.0 runtime fingerprint: reconcile the EFFECTIVE launch (model, effort)
    # against the persisted baseline AFTER load, then PERSIST immediately — BEFORE
    # make_drive — so the early-return path (a make_drive ValueError) cannot lose
    # the stamp and cause a reset on every relaunch (spec §3.4, P3). Absent baseline
    # is adopted silently; a present-but-different fingerprint forces a fresh
    # session (runtime_config_changed). Loop-path only.
    if runtime_fingerprint is not None:
        wsession.reconcile_runtime_fingerprint(state, runtime_fingerprint)
        state.model = runtime_model
        state.reasoning_effort = runtime_effort
        wsession.save_session(store, agent, state)
    health_mode = (
        "lead-loop" if lead_loop else
        "wrapper-one-shot" if one_shot_request_id else
        "wrapper-loop"
    )
    health_writer = WrapperHealthWriter(
        store, agent, cli, mode=health_mode, min_interval=min_interval)
    runtime_writer = WrapperRuntimeWriter(
        store.state_dir,
        agent,
        wrapper_generation,
        on_transition=lifecycle_log.runtime_transition,
    )
    try:
        drive = wrapper_run.make_drive(
            store, agent, cli, state, base_argv, sender=sender,
            min_interval=min_interval, render=render, heartbeat=heartbeat,
            persist=lambda st: wsession.save_session(store, agent, st),
            turn_watchdog=turn_watchdog,
            health_writer=health_writer,
            runtime_writer=runtime_writer,
            work_heartbeat=work_heartbeat,
            wrapper_generation=wrapper_generation,
            backend_profile=backend_profile,
            profile_env=profile_env,
            lifecycle_log=lifecycle_log,
            # the lead-loop combined stamp raises _LeadLoopLeaseLost on a lost lease:
            # the ticker treats it as TYPED loss (permanent stop for the turn, status
            # lost_lease, never a bus heartbeat without the lease), while the
            # pre_commit gate remains the consume-boundary authority.
            lease_lost_exceptions=(_LeadLoopLeaseLost,) if lead_loop else (),
            # #202 D4: tell the child it was interrupted - built here in the cli
            # wiring (review finding 7), reading the persisted attempt ledger.
            rejoin_for=_interruption_rejoin_for(store, agent, k_interrupted),
        )
    except ValueError as e:
        _release()
        sys.stderr.write(f"agenttalk wrap: {e}\n")
        return _wrapper_exit(2, "drive_configuration_rejected")
    capacity_refresh = None
    if one_shot_request_id is None:

        def capacity_refresh() -> None:
            if cli == "codex":
                codex_home = os.environ.get("CODEX_HOME")
                if not codex_home:
                    snap = capmod.CapacitySnapshot.unknown(
                        agent, reason="codex_home_missing")
                else:
                    snap = capmod.read_local(
                        agent, source="codex",
                        sessions_dir=Path(codex_home) / "sessions",
                        thread_id=state.codex_thread_id,
                    )
            else:
                snap = capmod.read_local(agent, source="claude")
            store.write_capacity(agent, snap.to_dict())
    if lead_loop:
        # OWNERSHIP GATE: re-verify the lease BEFORE consuming EACH record, so a lost
        # lease stops consumption IMMEDIATELY (not after the supervisor's stale
        # threshold) - the hard loss-of-ownership signal codex required.
        _model_drive = drive

        def _lead_loop_drive(record):
            _renew_or_lost()
            return _model_drive(record)
        drive = _lead_loop_drive
    wsession.save_session(store, agent, state)   # persist the (possibly minted) id
    # ONE-SHOT: bound the wait in-process so an ephemeral reviewer whose request
    # never arrives (or is closed/superseded) exits NONZERO with a diagnostic well
    # before the supervisor's deadline kill, instead of waiting idle to the kill.
    # The bound mirrors the launch marker's timeout_seconds when we can still read
    # it (it may already be archived after the claim), else a conservative default.
    max_wall: float | None = None
    if one_shot_request_id:
        marker = store.read_launch_request(one_shot_request_id)
        try:
            max_wall = float(int((marker or {}).get("timeout_seconds") or 1800))
        except (TypeError, ValueError):
            max_wall = 1800.0
    # dead-letter (CONTINUOUS loop only; the one-shot path ignores these). On
    # dead-letter / high-attempt backstop the wrapper escalates to the operator.
    notifier = _dead_letter_notifier(store, agent)

    # --- managed lead-loop CADENCE TICK (WP3): the proactive sweep ----------------
    # Built ONLY for the lead-loop controller (not the plain wrapper / one-shot). The
    # hook is consulted by run_loop on each IDLE poll; it gates due-ness itself and, when
    # due, drives at most ONE synthetic snapshot turn - never advancing the cursor /
    # recording an attempt / dead-lettering (condition 1). A failed sweep is controller-
    # HEALTH: back off + (past the threshold, once) escalate to the operator.
    cadence_hook = None
    if lead_loop and not one_shot_request_id:
        from . import lead_loop_cadence as _cad
        cadence_drive = wrapper_run.make_cadence_drive(
            store, agent, cli, state, base_argv, sender=sender,
            min_interval=min_interval, render=render, heartbeat=heartbeat,
            persist=lambda st: wsession.save_session(store, agent, st),
            runtime_writer=runtime_writer,
            work_heartbeat=work_heartbeat,
            lifecycle_log=lifecycle_log,
            lease_lost_exceptions=(_LeadLoopLeaseLost,))
        cadence_health = _cadence_health_notifier(store, agent)

        def _cadence() -> "wloop.CadenceResult":
            now_epoch = time.time()   # WALL clock: cadence state persists across restarts
            cstate = store.read_lead_loop_cadence(agent)
            timing = lead_loop_runtime.resolve_timing(
                store, agent, supervisor_config=supervisor_config)
            if not _cad.cadence_due(cstate, now_epoch=now_epoch,
                                    cadence_seconds=timing["cadence_seconds"]):
                return wloop.CadenceResult(ran=False)
            # DUE. Ownership gate first - a lost-lease controller must not sweep/send; a
            # raise propagates out (run_loop -> _wrap_loop_mode handles lease-loss exit).
            _renew_or_lost()

            def _fail() -> "wloop.CadenceResult":
                new, esc = _cad.apply_tick_failure(
                    cstate, now_epoch=now_epoch,
                    base=LEAD_LOOP_CADENCE_FAIL_BACKOFF_BASE,
                    max_backoff=LEAD_LOOP_CADENCE_FAIL_BACKOFF_MAX,
                    health_threshold=LEAD_LOOP_CADENCE_HEALTH_THRESHOLD)
                if esc and cadence_health(new["cadence_fails"], "snapshot/sweep failure"):
                    # latch the controller-health escalation ONLY after it ROUTED. An
                    # unrouted notice (no operator-facing / sole-lead target) leaves
                    # health_escalated False so the NEXT failure RETRIES it - never
                    # silently dropping the durable operator signal (codex WP3 MAJOR;
                    # mirrors the dead-letter escalation_routed discipline).
                    new["health_escalated"] = True
                store.write_lead_loop_cadence(agent, new)
                return wloop.CadenceResult(ran=True, ok=False)

            try:
                snap = _cad.build_cadence_snapshot(
                    store, agent, now_epoch=now_epoch, supervisor_config=supervisor_config)
                items = _cad.cadence_actionable(
                    snap, cstate, now_epoch=now_epoch,
                    reminder_after_seconds=LEAD_LOOP_REMINDER_AFTER_DEFAULT)
            except _LeadLoopLeaseLost:
                raise
            except Exception:  # noqa: BLE001 - snapshot/actionability failure == cadence failure
                return _fail()
            if not items:
                # due but nothing actionable: record the sweep (no model turn spent).
                store.write_lead_loop_cadence(agent, _cad.apply_tick_success(
                    cstate, now_epoch=now_epoch, reminded_keys=[], escalation_keys=[]))
                return wloop.CadenceResult(ran=True, ok=True, drove_turn=False)
            try:
                ok = bool(cadence_drive(snap, items))
            except _LeadLoopLeaseLost:
                raise
            except Exception:  # noqa: BLE001 - any drive error is a cadence failure
                ok = False
            if not ok:
                return _fail()
            reminded_keys = [(it["request_id"], it.get("last_msg_id"))
                             for it in items if it.get("type") == "outbound_reminder"]
            escalation_keys = [it["key"] for it in items
                               if it.get("type") in ("dead_letter", "unrouted_escalation")]
            store.write_lead_loop_cadence(agent, _cad.apply_tick_success(
                cstate, now_epoch=now_epoch, reminded_keys=reminded_keys,
                escalation_keys=escalation_keys))
            runtime_writer.idle()
            return wloop.CadenceResult(ran=True, ok=True, drove_turn=True)

        cadence_hook = _cadence
    try:
        from .wrapper.obligations import DetectionCommitGate

        commit_gate = DetectionCommitGate.from_environment(
            store,
            agent,
            fence=wrapper_generation,
        )
        turns = wloop.run_loop(
            store, agent, drive,
            max_turns=1 if one_shot_request_id else None,
            only_request_id=one_shot_request_id,
            max_wall=max_wall,
            k_poison=k_poison, k_escalate=k_escalate,
            k_interrupted=k_interrupted,
            interruption_redrive_seconds=interruption_redrive_seconds,
            # for the escalation's remedy text only: the watchdog budget each
            # killed turn burned (None when the watchdog is not live).
            interruption_budget_seconds=(
                getattr(turn_watchdog, "turn_elapsed_seconds", None)
                if getattr(turn_watchdog, "enabled", False) else None),
            infra_exhaust_after_seconds=infra_exhaust_after_seconds,
            infra_exhaust_min_attempts=infra_exhaust_min_attempts,
            noninfra_sub_ceiling=noninfra_sub_ceiling,
            on_dead_letter=lambda info: notifier(info, disposed=True),
            on_escalate=lambda info: notifier(info, disposed=False),
            heartbeat=heartbeat,
            pre_commit=pre_commit,  # lead-loop ownership gate at every cursor advance
            manage_waiting=not lead_loop,  # the lead-loop LEASE owns the .waiting mirror
            cadence=cadence_hook,  # WP3 proactive sweep (lead-loop only)
            on_health_idle=health_writer.idle,
            on_health_parked=health_writer.parked,  # #58: config-blocked park is visible, not a frozen 'idle'
            on_runtime_idle=runtime_writer.idle,
            on_runtime_dead_letter=(
                lambda record: runtime_writer.dead_letter(
                    message_id=record.get("id")
                )
            ),
            capacity_refresh=capacity_refresh,
            wrapper_generation=wrapper_generation,
            commit_gate=commit_gate,
        )
    except _LeadLoopLeaseLost:
        # LOST the lease mid-run (stolen / torn / force-released): the ownership gate /
        # heartbeat raised to stop consuming AT ONCE. Release is lease_id-guarded (a no-op
        # if another owner now holds it). NO exit marker -> the supervisor relaunches and
        # the relaunch re-acquires (or HOLDS if the new owner is live). Clean nonzero exit
        # (not a traceback) - the supervisor recovers via the stale heartbeat, not the code.
        _release()
        sys.stderr.write(f"agenttalk wrap --lead-loop: {agent!r} lost the mailbox lease "
                         f"(stolen / torn / force-released); exiting for supervisor recovery.\n")
        return _wrapper_exit(
            _LEAD_LOOP_LEASE_LOST_EXIT,
            "managed_lead_loop_lease_lost",
        )
    except BaseException:
        # CRASH / interruption: best-effort release (fast handoff; a SIGKILL skips this
        # but the owner pid is then CONFIRMED-dead -> immediately stealable, D-12). NO
        # exit marker is written -> the supervisor relaunches + the relaunch re-acquires.
        _release()
        raise
    if one_shot_request_id and turns < 1:
        # Distinguish a dead thread from a never-arriving request for the diagnostic
        # (read-only scoped poll; either way it is a nonzero one-shot exit).
        from .wrapper import recv_api as _recv_api
        env = _recv_api.poll(store, agent, scoped_request_id=one_shot_request_id)
        sc = env.get("scoped") or {}
        why = ("thread closed/superseded" if (sc.get("closed") or sc.get("superseded"))
               else f"no message on request {one_shot_request_id!r} within the one-shot bound")
        sys.stderr.write(f"agenttalk wrap --one-shot: no turn driven ({why}).\n")
        return _wrapper_exit(1, "one_shot_request_not_driven")
    if lead_loop:
        # A CLEAN return from the unbounded continuous lead-loop == a VALID human
        # release/end (classify_loop_control "stop"): a DELIBERATE stand-down. Release
        # the lease + record it so the supervisor does NOT relaunch (the v0.39 stand-down
        # sticks until an operator request-restart re-arms - which overrides the HOLD and
        # clears the marker on the confirmed relaunch).
        _release()
        store.write_lead_loop_exit(agent, state=store.LEAD_LOOP_EXIT_STOOD_DOWN,
                                   owner_pid=os.getpid(),
                                   reason="valid human release/end (deliberate stand-down)")
        return _wrapper_exit(
            _LEAD_LOOP_STOOD_DOWN_EXIT,
            "managed_lead_loop_stood_down",
        )
    return _wrapper_exit(0, "loop_returned")


def _resolves_to_cmd_wrap(argv: list[str]) -> bool:
    """True iff parsing argv through this SAME parser resolves to cmd_wrap
    without argparse exiting first. A terminal option (bare -h/--help
    anywhere before the wrap subcommand's own `cmd` REMAINDER capture
    starts, e.g. `wrap --help`) or a parse-invalid tail (a missing
    required value, an unrecognized flag) both call sys.exit before
    cmd_wrap - hence before the wrap subcommand's bounded logging would
    ever install - so neither should count as reaching it.

    argparse itself prints usage/help or an "error: ..." line for exactly
    those two cases as part of raising SystemExit - suppressed here so
    probing an arbitrary candidate argv (untrusted launch config, not a
    real invocation) never writes anything to this process's own
    stdout/stderr as a side effect of merely checking it.
    """
    parser = build_parser()
    try:
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            args = parser.parse_args(argv)
    except SystemExit:
        return False
    if getattr(args, "func", None) is not cmd_wrap:
        return False
    # The actual parser now uses launch_admission's canonical wrap grammar.
    # Normalize the namespace through the same typed boundary as supervisor
    # admission, but deliberately retain this probe's narrower contract: it
    # answers whether argparse DISPATCHES to cmd_wrap, before runtime-shape
    # validation performed inside that command.
    launch_admission.wrap_invocation_from_namespace(args)
    return True


def cmd_internal_check_wrap_dispatch(args: argparse.Namespace) -> int:
    """Supervisor-only probe, never documented as a stable CLI surface: exit
    0 iff the given argv would dispatch to cmd_wrap, 1 otherwise. No
    stdout/stderr output and no side effects - parsing argv does not call
    args.func, only inspects what it resolves to.

    Exists so the supervisor can ask this parser directly whether a
    candidate launch argv actually reaches the wrap subcommand, instead of
    re-deriving argparse's own grammar (nargs=REMAINDER's auto --help
    recognition, required-value errors, subparser dispatch) in PowerShell -
    the same class of leak this project has already paid for once at the
    interpreter layer (module_args_from), and the reason to prefer asking
    the real parser here rather than repeating that.
    """
    tail = list(args.argv or [])
    if tail and tail[0] == "--":
        tail = tail[1:]
    return 0 if _resolves_to_cmd_wrap(tail) else 1


def cmd_wrap(args: argparse.Namespace) -> int:
    """Install supervisor-authenticated logging before wrapper setup begins."""
    from .wrapper_logs import (
        WrapperLifecycleLog,
        capture_termination_signals,
        installed_standard_streams_from_environment,
    )

    with installed_standard_streams_from_environment(
        expected_nonce=getattr(args, "supervisor_launch_nonce", None),
    ):
        agent = (
            getattr(args, "agent", None)
            or os.environ.get("AGENTTALK_SELF")
            or "unknown"
        )
        lifecycle_log = WrapperLifecycleLog.from_environment(
            str(agent),
            expected_nonce=getattr(args, "supervisor_launch_nonce", None),
        )
        args._wrapper_lifecycle_log = lifecycle_log
        try:
            with capture_termination_signals(lifecycle_log):
                result = _cmd_wrap_with_logging(args)
        except BaseException as exc:
            # A SystemExit reaching here was ALREADY a deliberate exit with
            # its own diagnostic already written (e.g. _get_store's "not
            # initialized" message before sys.exit(2)), or a termination
            # signal already recorded structurally via lifecycle.defer_signal
            # before capture_termination_signals raised it. Recording it
            # again via wrapper_exception and printing a synthetic Python
            # traceback on top is not a crash report, it is noise over an
            # already-explained, intentional exit - exactly the regression
            # #117 exists to prevent. Handle it BEFORE the crash-reporting
            # path below, not after, so it never reaches it.
            #
            # But skipping wrapper_exception must not also skip EVERY
            # lifecycle fact: a signal-driven SystemExit already recorded
            # wrapper_signal_received (terminal_emitted is already True by
            # the time it gets here), but a non-signal SystemExit - like
            # _get_store's - has no deferred signal for anything to have
            # recorded, so terminal_emitted is still False. Without an
            # explicit wrapper_exited here, the trail ends with no
            # termination fact at all, and a cleanly explained
            # configuration error becomes indistinguishable from an OOM or
            # a hard kill when reading the JSON lines - a worse defect than
            # the traceback this branch exists to suppress. Every
            # termination path must emit exactly one termination fact.
            if isinstance(exc, SystemExit):
                if not lifecycle_log.terminal_emitted:
                    code = exc.code if isinstance(exc.code, int) else (
                        0 if exc.code is None else 1
                    )
                    lifecycle_log.wrapper_exited(code, reason="system_exit")
                raise
            # Same shape, one layer up (PR 98 connector, round 9): the
            # crash-reporting block below must run ONLY for genuinely
            # unexpected exceptions. KeyboardInterrupt and (ValueError,
            # FileNotFoundError, OSError) are NOT unexpected - main() (see
            # its own except clauses) already has a concise, actionable
            # one-line diagnostic for exactly these, and this block used to
            # convert them to the SAME SystemExit codes further down, AFTER
            # unconditionally recording wrapper_exception and printing a
            # full Python traceback first. That turned a routine OSError
            # into crash-report noise ahead of the one line anyone actually
            # wants - the exact regression #117 exists to prevent, just for
            # a wider set of types than SystemExit alone.
            #
            # Enumerated as a set, not patched type by type: anything NOT
            # in this set falls through to the crash path below BY
            # CONSTRUCTION, not by omission - a future exception type that
            # gains a concise diagnostic elsewhere and is not added here
            # must be a visible test failure, not a silent traceback. See
            # test_cmd_wrap_routine_exception_types_skip_crash_reporting
            # (this set) and
            # test_cmd_wrap_unclassified_exception_still_gets_crash_reporting
            # (the other half of the property).
            # Round 17 connector finding: raising SystemExit here for a
            # class main() itself handles bypasses main()'s own handler for
            # it - main() never has a chance to run its except clause,
            # because SystemExit is not a subclass of Exception and simply
            # propagates through main()'s try/except untouched, all the way
            # out of main() itself. main() previously RETURNED an int for
            # exactly these two classes (see the contract table in
            # test_cmd_wrap_and_main_exception_contract); that RETURN is
            # the actual contract an embedder or a test runner calling
            # cli.main([...]) programmatically depends on, not a
            # SystemExit with a matching code - the two look the same at a
            # console (the process exits N either way) but are NOT the
            # same to anything catching Exception around a call to main().
            # Return the SAME int main() would have, so main()'s contract
            # holds regardless of whether cmd_wrap's own exception handling
            # sits in front of it.
            if isinstance(exc, KeyboardInterrupt):
                sys.stderr.write("\nagenttalk: interrupted\n")
                if not lifecycle_log.terminal_emitted:
                    lifecycle_log.wrapper_exited(130, reason="keyboard_interrupt")
                return 130
            if isinstance(exc, (ValueError, FileNotFoundError, OSError)):
                sys.stderr.write(f"agenttalk: {exc}\n")
                if not lifecycle_log.terminal_emitted:
                    lifecycle_log.wrapper_exited(2, reason="mapped_cli_exception")
                return 2
            # Genuinely unexpected: record the crash fact and print the
            # traceback here, while sys.stderr is still the bounded tee
            # installed above - the "finally" of that context manager
            # restores the raw stream before this exception reaches
            # whatever eventually reports it, which would otherwise let
            # the one diagnostic anyone actually wants bypass the cap and
            # the tail rotation #117 exists to provide.
            #
            # Round 18: propagate the ORIGINAL exception, not a converted
            # SystemExit(1) - the same argument that made the two branches
            # above wrong, one row down. Before this PR, an unexpected
            # exception left main() with its original type; an embedder
            # or test runner calling cli.main([...]) could catch it,
            # inspect it, log it, retry. Converting to SystemExit(1) threw
            # that away twice: it destroys the type information a caller
            # needs, and substitutes an exception class that terminates
            # an unhandled process instead of being caught by an ordinary
            # except Exception. The "one known exit code for any crash"
            # goal is a CONSOLE concern the console already gets for
            # free - Python exits 1 on any uncaught exception regardless
            # - so propagating the original preserves console behavior
            # exactly while restoring the contract for embedders. main()
            # never had a special RETURN contract for this class either
            # (it would have let the original type propagate uncaught),
            # so this is not a new divergence to justify - it is the
            # STATUS QUO main() always had, kept intact.
            if not lifecycle_log.terminal_emitted:
                lifecycle_log.wrapper_exception(exc)
                traceback.print_exc(file=sys.stderr)
            raise
        finally:
            del args._wrapper_lifecycle_log
        if not lifecycle_log.terminal_emitted:
            lifecycle_log.wrapper_exited(
                int(result),
                reason="wrap_command_returned",
            )
        return result


def _cmd_wrap_with_logging(args: argparse.Namespace) -> int:
    """Run an agent CLI under the progress-adapter wrapper (0.30.0).

    Default (one-shot ``-- <argv>``): launch the CLI in structured-stream mode,
    stamp heartbeat on progress (throttled), render, run the degraded detector.
    ``--loop``: become the long-running SUPERVISED wrapper that owns the idle
    bus-wait + heartbeat and drives the CLI one turn per inbound message (design C).
    The supervisor stays dumb (heartbeat/backoff/kill).
    """
    from .wrapper import run as wrapper_run

    # (stdout is already reconfigured to utf-8/replace for every command in main(),
    # so the wrapper's render of the child's UTF-8 progress is clean without a
    # per-command reconfigure here.)
    store = _get_store(args)
    roster = store.load_config().get("agents") or []
    agent = _resolve_self(args.agent, roster=roster)
    lifecycle_log = getattr(args, "_wrapper_lifecycle_log", None)
    if lifecycle_log is not None:
        lifecycle_log.agent = agent
    parsed_wrap = launch_admission.validate_standalone_wrap(args)
    if isinstance(parsed_wrap, launch_admission.WrapRefusal):
        sys.stderr.write(f"{parsed_wrap.message}\n")
        return 2
    argv = list(parsed_wrap.child_argv)
    lead_loop = parsed_wrap.lead_loop
    # v0.75.0: model/effort injection + fingerprinting live in the --loop path only
    # (D2). Warn (don't fail) so an operator's --model/--effort isn't silently dropped
    # on the non-loop one-shot `wrap` path.
    if not getattr(args, "loop", False) and (
            getattr(args, "model", None) or getattr(args, "effort", None)):
        sys.stderr.write(
            "agenttalk wrap: --model/--effort only apply with --loop; ignoring\n")
    sender = (_resolve_self(args.sender, roster=roster)
              if getattr(args, "sender", None) else agent)
    launch_cwd = store.root
    if getattr(args, "lane_id", None):
        try:
            lane_id = lane_mod.validate_lane_id(args.lane_id)
            data = lane_mod.load_lanes(store)
            lane = (data.get("lanes") or {}).get(lane_id)
            if not isinstance(lane, dict):
                raise lane_mod.LaneError(f"lane {lane_id!r} has no active provisioned worktree")
            if lane.get("status") != lane_mod.STATUS_ACTIVE:
                raise lane_mod.LaneError(f"lane {lane_id!r} is not active for launch")
            if not lane.get("worktree_path"):
                raise lane_mod.LaneError(f"lane {lane_id!r} has no provisioned worktree")
            launch_cwd = Path(lane["worktree_path"])
            if args.cli == "codex" and not any(
                    argv[i] == "--add-dir" and i + 1 < len(argv)
                    and str(argv[i + 1]) == str(launch_cwd) for i in range(len(argv))):
                argv = ["--add-dir", str(launch_cwd), *argv]
        except lane_mod.LaneError as e:
            sys.stderr.write(f"agenttalk wrap: {e}\n")
            return 2
    sup_cfg: dict = {}
    cfg_agent: dict = {}
    backend_profile: str | None = None
    profile_env: dict[str, str] | None = None
    if getattr(args, "loop", False):
        sup_cfg = _load_supervisor_config(store)
        _agents = sup_cfg.get("agents")
        raw_agent = _agents.get(agent) if isinstance(_agents, dict) else None
        cfg_agent = raw_agent if isinstance(raw_agent, dict) else {}
        raw_profile = cfg_agent.get("backend_profile")
        if raw_profile is not None and not isinstance(raw_profile, str):
            sys.stderr.write("agenttalk wrap: backend_profile must be a string\n")
            return 2
        backend_profile = raw_profile
        if backend_profile == "ovh-qwen":
            if lead_loop:
                sys.stderr.write(
                    "agenttalk wrap: ovh-qwen does not support --lead-loop; "
                    "cadence turns have no immutable inbound budget scope\n"
                )
                return 2
            from agenttalk.ovh_gateway import (
                EXTERNAL_WORKER,
                GatewayConfigError,
                MODEL_ALIAS,
                default_front_token_path,
                read_secret_file,
            )
            from agenttalk.ovh_gateway_service import gateway_status

            if args.cli != "claude":
                sys.stderr.write("agenttalk wrap: ovh-qwen requires cli=claude\n")
                return 2
            ambient = [key for key in ("OVH_KEY", "ANTHROPIC_API_KEY") if os.environ.get(key)]
            if ambient:
                sys.stderr.write(
                    "agenttalk wrap: ovh-qwen refuses supervisor ambient provider keys: "
                    + ", ".join(ambient)
                    + "\n"
                )
                return 2
            if cfg_agent.get("trust_class") != EXTERNAL_WORKER:
                sys.stderr.write(
                    "agenttalk wrap: ovh-qwen requires supervisor trust_class=external-worker\n"
                )
                return 2
            if cfg_agent.get("model") != MODEL_ALIAS:
                sys.stderr.write(
                    f"agenttalk wrap: ovh-qwen requires model={MODEL_ALIAS}\n"
                )
                return 2
            store_trust = getattr(store, "trust_class", lambda _name: None)(agent)
            if store_trust != EXTERNAL_WORKER:
                sys.stderr.write(
                    "agenttalk wrap: ovh-qwen requires roster trust_class=external-worker\n"
                )
                return 2
            if cfg_agent.get("env"):
                sys.stderr.write("agenttalk wrap: ovh-qwen forbids literal per-agent env config\n")
                return 2
            try:
                gateway = gateway_status(store.root)
            except (GatewayConfigError, OSError) as exc:
                gateway = {
                    "ready": False,
                    "operational_ready": False,
                    "worker_spend_ready": False,
                    "errors": [type(exc).__name__],
                    "worker_spend_errors": ["worker_spend_readiness_unavailable"],
                }
            operational_ready = (
                gateway.get("operational_ready", gateway.get("ready")) is True
            )
            worker_spend_ready = gateway.get("worker_spend_ready") is True
            if not operational_ready or not worker_spend_ready:
                reasons: list[str] = []
                if not operational_ready:
                    operational_errors = gateway.get("errors")
                    if isinstance(operational_errors, list):
                        reasons.extend(
                            reason
                            for reason in operational_errors
                            if isinstance(reason, str)
                        )
                if not worker_spend_ready:
                    worker_errors = gateway.get("worker_spend_errors")
                    safe_worker_errors = (
                        [reason for reason in worker_errors if isinstance(reason, str)]
                        if isinstance(worker_errors, list)
                        else []
                    )
                    if safe_worker_errors:
                        reasons.extend(safe_worker_errors)
                    else:
                        reasons.append("worker_spend_readiness_unavailable")
                if not reasons:
                    reasons.append("readiness_unavailable")
                safe_reasons = ",".join(dict.fromkeys(reasons))
                summary = f"ovh-qwen gateway is not ready ({safe_reasons})"
                mode = (
                    "lead-loop" if lead_loop else
                    "wrapper-one-shot" if args.one_shot else
                    "wrapper-loop"
                )
                return _handle_launch_config_blocked(
                    store,
                    agent,
                    args.cli,
                    mode=mode,
                    min_interval=args.min_interval,
                    summary=summary,
                )
            try:
                front_token = read_secret_file(default_front_token_path())
            except GatewayConfigError as exc:
                sys.stderr.write(f"agenttalk wrap: {exc}\n")
                return 2
            profile_env = {
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
                "ANTHROPIC_AUTH_TOKEN": front_token,
                "ANTHROPIC_MODEL": MODEL_ALIAS,
            }
        elif backend_profile is not None:
            sys.stderr.write(f"agenttalk wrap: unsupported backend_profile {backend_profile!r}\n")
            return 2
    child_env = wrapper_run._child_env(
        store.root,
        backend_profile=backend_profile,
        profile_env=profile_env,
    )
    if backend_profile == "ovh-qwen":
        Path(child_env["CLAUDE_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)
    launch = wrapper_run.preflight_launch_runtime(
        argv, args.cli, launch_cwd, child_env)
    if launch.blocked:
        mode = (
            "lead-loop" if lead_loop else
            "wrapper-one-shot" if args.one_shot else
            "wrapper-loop" if getattr(args, "loop", False) else
            "wrapper"
        )
        return _handle_launch_config_blocked(
            store, agent, args.cli, mode=mode, min_interval=args.min_interval,
            summary=launch.blocked)
    store.clear_config_blocked_hold(agent)
    argv = launch.argv
    if launch_cwd != store.root:
        os.chdir(launch_cwd)
    if getattr(args, "loop", False):
        # Dead-letter caps: an explicit --dead-letter-* flag wins; otherwise resolve from
        # supervisor.json (per-agent -> global -> default) so a supervised wrapped agent's
        # dead_letter:{...} config actually takes effect (codex P1 / lead F3). This makes
        # supervisor.resolve_dead_letter_caps the single source of truth.
        from agenttalk import supervisor as _sup
        # sup_cfg/cfg_agent were loaded before preflight so a backend profile can
        # constrain the exact child environment used by that preflight and launch.
        # Wrapped Claude write-grant fix: session_args is empty for a wrapped agent,
        # so the supervisor never substitutes {PERM_MODE} into the child tail — a
        # supervised wrapped Claude would launch read-only. Apply the same resolved
        # mode here (no-op if an explicit --permission-mode is already in the tail).
        argv = _inject_claude_permission_mode(
            argv, args.cli, _sup.claude_permission_mode(sup_cfg, cfg_agent))
        # v0.75.0 runtime ergonomics: resolve per-agent model + reasoning_effort
        # (flag > per-agent config; NO global fallback), then inject BARE tokens into
        # the child argv (no-op + warn when the operator tail already sets the flag —
        # explicit tail wins). A bad value is dropped with a warning, never bricks
        # launch. The runtime fingerprint is computed from the EFFECTIVE model/effort
        # scanned from the FINAL argv (so a hand-written tail flag, which wins, is
        # tracked correctly) and threaded into the loop for restart-safe reconcile.
        from agenttalk.wrapper import session as _wsession_mod
        eff_model, eff_effort, _rt_warnings = _resolve_runtime_model_effort(
            cfg_agent, args.cli, getattr(args, "model", None),
            getattr(args, "effort", None))
        for _w in _rt_warnings:
            sys.stderr.write(f"agenttalk wrap: {_w}\n")
        argv, _inj_warnings = inject_model_flags(argv, args.cli, eff_model, eff_effort)
        for _w in _inj_warnings:
            sys.stderr.write(f"agenttalk wrap: {_w}\n")
        _scanned = scan_model_effort(argv, args.cli)
        runtime_model = _scanned["model"]
        runtime_effort = _scanned["effort"]
        runtime_fingerprint = _wsession_mod.compute_runtime_fingerprint(
            runtime_model, runtime_effort)
        res_poison, res_escalate = _sup.resolve_dead_letter_caps(sup_cfg, cfg_agent)
        flag_poison = getattr(args, "dead_letter_max_attempts", None)
        flag_escalate = getattr(args, "dead_letter_escalate_after", None)
        k_poison = flag_poison if flag_poison is not None else res_poison
        k_escalate = flag_escalate if flag_escalate is not None else res_escalate
        infra_ceiling = _sup.resolve_infra_retry_exhaustion(
            sup_cfg, cfg_agent, k_escalate)
        # Per-turn watchdog (wrapped-codex hung-tool-descendant hang). DEFAULT-ON for a
        # CONTINUOUS wrapped-codex loop only (not one-shot, not claude); config can flip it.
        from agenttalk.wrapper import turn_watchdog as _twd
        default_wd_on = (args.cli == "codex" and not args.one_shot)
        watchdog_cfg = _twd.resolve_turn_watchdog(
            sup_cfg, cfg_agent, default_enabled=default_wd_on)
        # Low-floor guard (mirrors allow_low_stuck_after): refuse an unsafe-low turn_elapsed
        # unless explicitly opted in - never silently coerce. Routed through the SHARED
        # watchdog_effectively_live predicate so the supervisor planner makes the SAME
        # live/disabled decision (else the wrapper disables it but the supervisor still
        # refuses restart-on-stale -> a wedge with no recovery).
        if watchdog_cfg.enabled and not _twd.watchdog_effectively_live(watchdog_cfg):
            sys.stderr.write(
                f"agenttalk wrap: turn_watchdog.turn_elapsed_seconds="
                f"{watchdog_cfg.turn_elapsed_seconds:.0f}s is below the "
                f"{_twd.SAFE_TURN_ELAPSED_FLOOR:.0f}s floor; set allow_low_turn_elapsed=true "
                f"to opt in. Disabling the turn watchdog for this run.\n")
            watchdog_cfg = dataclasses.replace(watchdog_cfg, enabled=False)
        # Bounded in-turn work heartbeat (wrapped-Claude false-STUCK fix). Default-ON
        # only for wrapped CLAUDE continuous loop + managed lead-loop; codex and
        # one-shot default-OFF (no codex stuck_after / watchdog-preemption change).
        # An ENABLED-but-invalid config FAILS VISIBLY through the launch config-blocked
        # path (durable hold + escalation, no silent coercion, no churn).
        from agenttalk.wrapper import work_heartbeat as _whb
        whb_mode = (
            "lead-loop" if lead_loop else
            "wrapper-one-shot" if args.one_shot else
            "wrapper-loop"
        )
        whb_cfg = _whb.resolve_work_heartbeat(
            sup_cfg, cfg_agent, cli=args.cli, mode=whb_mode)
        if whb_cfg.enabled:
            problems = list(whb_cfg.config_errors)
            violation = _whb.interval_violation(
                whb_cfg, stuck_after_seconds=_sup.resolve_stuck_after(sup_cfg, cfg_agent))
            if violation:
                problems.append(violation)
            if problems:
                summary = ("invalid work_heartbeat config (never silently coerced): "
                           + "; ".join(problems))
                sys.stderr.write(f"agenttalk wrap: {summary}\n")
                return _handle_launch_config_blocked(
                    store, agent, args.cli, mode=whb_mode,
                    min_interval=args.min_interval, summary=summary)
        # #202 D2: interruption-aware redelivery knobs. A present-but-corrupt value
        # refuses visibly through the launch config-blocked path (never silently
        # clamped) - same discipline as work_heartbeat above.
        interruption_redrive, k_interrupted, _int_errors = (
            _sup.resolve_interruption_policy(sup_cfg, cfg_agent))
        if _int_errors:
            summary = ("invalid interruption redelivery config (never silently "
                       "coerced): " + "; ".join(_int_errors))
            sys.stderr.write(f"agenttalk wrap: {summary}\n")
            return _handle_launch_config_blocked(
                store, agent, args.cli, mode=whb_mode,
                min_interval=args.min_interval, summary=summary)
        # #202 D2 launch validation (rev 3): the chunked backoff stamps once per
        # heartbeat_interval slice, keeping heartbeat age <= heartbeat_interval no
        # matter how long the total backoff - so the ONE load-bearing invariant is
        # heartbeat_interval < stuck_after. Refuse with both numbers.
        from agenttalk.wrapper.loop import HEARTBEAT_INTERVAL_SECONDS as _hb_interval
        _stuck_after = _sup.resolve_stuck_after(sup_cfg, cfg_agent)
        if _hb_interval >= _stuck_after:
            summary = (
                f"wrapper heartbeat_interval {_hb_interval:.0f}s must be below "
                f"stuck_after {_stuck_after:.0f}s: the interruption backoff stamps "
                f"the heartbeat once per {_hb_interval:.0f}s chunk, so a stuck_after "
                "at/below it would let the supervisor kill a deliberately "
                "backing-off wrapper. Raise stuck_after_seconds.")
            sys.stderr.write(f"agenttalk wrap: {summary}\n")
            return _handle_launch_config_blocked(
                store, agent, args.cli, mode=whb_mode,
                min_interval=args.min_interval, summary=summary)
        return _wrap_loop_mode(
            store,
            agent,
            cli=args.cli,
            base_argv=argv,
            sender=sender,
            min_interval=args.min_interval,
            render=not args.no_render,
            one_shot_request_id=args.to_request if args.one_shot else None,
            k_poison=k_poison,
            k_escalate=k_escalate,
            k_interrupted=k_interrupted,
            interruption_redrive_seconds=interruption_redrive,
            infra_exhaust_after_seconds=infra_ceiling[
                "infra_exhaust_after_seconds"
            ],
            infra_exhaust_min_attempts=infra_ceiling[
                "infra_exhaust_min_attempts"
            ],
            noninfra_sub_ceiling=infra_ceiling["noninfra_sub_ceiling"],
            lead_loop=lead_loop,
            supervisor_config=sup_cfg,
            turn_watchdog=watchdog_cfg,
            work_heartbeat=whb_cfg,
            runtime_model=runtime_model,
            runtime_effort=runtime_effort,
            runtime_fingerprint=runtime_fingerprint,
            backend_profile=backend_profile,
            profile_env=profile_env,
            supervisor_launch_nonce=getattr(
                args,
                "supervisor_launch_nonce",
                None,
            ),
            lifecycle_log=getattr(
                args,
                "_wrapper_lifecycle_log",
                None,
            ),
        )
    try:
        return wrapper_run.run_wrapper(
            cli=args.cli,
            agent=agent,
            argv=argv,
            store=store,
            sender=sender,
            min_interval=args.min_interval,
            render=not args.no_render,
        )
    except ValueError as e:
        sys.stderr.write(f"agenttalk wrap: {e}\n")
        return 2


def cmd_managed_lead_loop(args: argparse.Namespace) -> int:
    """Configure / inspect managed lead-loop identities (lead-loop Slice 1).

    ``set <agent>`` marks an agent a managed lead-loop (its team mailbox is owned
    by a wrapped controller that cannot silently un-arm); ``clear`` unmarks it;
    ``list`` shows configured identities + their current armed/lease state. Generic
    by agent NAME - a codex identity is managed exactly as a claude one (never cli)."""
    store = _get_store(args)
    cmd = getattr(args, "managed_cmd", None)
    if cmd == "set":
        try:
            store.set_managed_lead_loop(
                args.agent, enabled=True,
                ttl_seconds=getattr(args, "ttl", None),
                cadence_seconds=getattr(args, "cadence", None))
        except ValueError as e:
            sys.stderr.write(f"agenttalk managed-lead-loop set: {e}\n")
            return 2
        spec = store.managed_lead_loop_spec(args.agent) or {}
        print(f"managed-lead-loop: {args.agent} ENABLED "
              f"(ttl={spec.get('ttl_seconds'):g}s, cadence={spec.get('cadence_seconds'):g}s)")
        return 0
    if cmd == "clear":
        try:
            store.set_managed_lead_loop(args.agent, enabled=False)
        except ValueError as e:
            sys.stderr.write(f"agenttalk managed-lead-loop clear: {e}\n")
            return 2
        print(f"managed-lead-loop: {args.agent} cleared")
        return 0
    managed = store.managed_lead_loop_agents()
    # Resolve each agent's heartbeat window from supervisor.json (if present) so the
    # listed armed/state matches the steal path for wrapped agents (WP1 contract).
    sup_cfg = _load_supervisor_config(store)

    def _ll_state(a: str) -> dict:
        hsa = lead_loop_runtime.resolve_timing(
            store, a, supervisor_config=sup_cfg or None)["heartbeat_stale_after"]
        return store.lead_loop_state(a, heartbeat_stale_after=hsa)

    if getattr(args, "json", False):
        out = {a: {**(store.managed_lead_loop_spec(a) or {}),
                   "state": _ll_state(a)} for a in managed}
        print(json.dumps(out, indent=2))
        return 0
    if not managed:
        print("no managed lead-loop identities configured")
        return 0
    for a in sorted(managed):
        spec = store.managed_lead_loop_spec(a) or {}
        st = _ll_state(a)
        flag = "ARMED" if st["armed"] else f"NOT-ARMED ({st['reason']})"
        print(f"  {a}: {flag}  ttl={spec.get('ttl_seconds')}s "
              f"cadence={spec.get('cadence_seconds')}s")
    return 0


def _dead_letter_resolution_state(store: Store) -> dict[tuple[str, str], str]:
    """{(agent, message_id): 'resolved'|'requeued'} from the central disposition log
    (latest dead_letter_resolution event per item wins). A resolve_dead_letter marks it
    resolved; a later requeued_after_resolve reopens it. Central log is authoritative."""
    from agenttalk import attention as A
    live_hashes: dict[tuple[str, str], str] = {}
    try:
        for entry in store.list_dead_letters():
            key = (str(entry.get("agent") or ""), str(entry.get("message_id") or ""))
            live_hashes[key] = A.dead_letter_entry_source_hash(entry)
    except Exception:
        live_hashes = {}
    events, _ = A.read_dispositions(store)
    folded = A.fold_dispositions(events)
    out: dict[tuple[str, str], str] = {}
    for iid, fams in folded.items():
        dl = fams.get("dead_letter_resolution")
        if not dl or not iid.startswith(A.SOURCE_DEAD_LETTER + ":"):
            continue
        parts = iid.split(":", 2)
        if len(parts) != 3:
            continue
        key = (parts[1], parts[2])
        snap_hash = dl.get("source_snapshot", {}).get("source_hash")
        if snap_hash != live_hashes.get(key):
            continue
        out[key] = "resolved" if dl["action"] == A.ACTION_RESOLVE_DEAD_LETTER else "requeued"
    return out


def _unresolved_dead_letter_entries(store: Store, agent: str | None = None) -> list[dict]:
    """User-facing unresolved dead-letter projection.

    ``Store.dead_lettered_count`` deliberately counts payload files in the sink. Status and
    dashboard-style surfaces need the attention count instead: resolved items no longer need
    an operator. Fail safe by returning the raw entries if disposition state is unreadable.
    """
    items = store.list_dead_letters(agent)
    try:
        res = _dead_letter_resolution_state(store)
    except Exception:  # noqa: BLE001 - a broken disposition log must not hide poison messages
        return items
    return [
        m for m in items
        if res.get((str(m.get("agent") or ""), str(m.get("message_id") or ""))) != "resolved"
    ]


def _unresolved_dead_letter_count(store: Store, agent: str | None = None) -> int:
    try:
        return len(_unresolved_dead_letter_entries(store, agent))
    except Exception:  # noqa: BLE001 - preserve the older raw-count warning on read failures
        return store.dead_lettered_count(agent)


def _is_listed_dead_letter(store: Store, agent: str, msg_id: str) -> bool:
    """SECURITY guard (reviewer-2 F5): the --id must be an EXACT message_id currently in the
    agent's sink. Blocks a path-traversal id (e.g. ..\\..\\config) from reaching ANY payload
    read or sidecar write, and gives a clean 'no such dead-letter' for a stale/typo id. Used
    as the single choke point by resolve / show / requeue before touching the filesystem."""
    if not (agent and msg_id):
        return False
    try:
        return any(m.get("message_id") == msg_id for m in store.list_dead_letters(agent))
    except (OSError, ValueError):
        return False


def _pending_dead_letter_notice_request_ids(
    store: Store,
    *,
    actor: str,
    agent: str,
    msg_id: str,
) -> list[str]:
    msgs = sorted(store.valid_messages(), key=lambda m: m.id)
    rows = {
        t.request_id: t
        for t in th.derive_threads(
            msgs,
            agent=actor,
            cursor=store.cursor(actor) or "",
            closed_rids=_closed_rids(store, actor),
            retired=set(store.retired_agents()),
        )
    }
    request_ids: list[str] = []
    for m in msgs:
        meta = m.meta or {}
        rid = meta.get("request_id")
        if not (isinstance(rid, str) and rid):
            continue
        if m.sender != agent or m.recipient != actor or m.kind != "question":
            continue
        if str(meta.get("needs_operator", "")).lower() != "true":
            continue
        if str(meta.get("dead_letter", "")).lower() != "true":
            continue
        if str(meta.get("dl_disposed", "")).lower() != "true":
            continue
        if str(meta.get("dl_msg_id") or "") != msg_id:
            continue
        row = rows.get(rid)
        if row is None or row.operator_state != "pending":
            continue
        request_ids.append(rid)
    return sorted(set(request_ids))


def _close_dead_letter_notice_threads(
    store: Store,
    *,
    actor: str,
    agent: str,
    msg_id: str,
    reason: str,
    evidence: str | None = None,
) -> int:
    """Best-effort close of wrapper escalation twins for a resolved sink row.

    The normal operator-answer resolver intentionally rejects these twins as
    ``superseded_by_canonical`` and tells the operator to resolve the canonical
    dead-letter. This helper is that canonical path: after the sink row is resolved, answer
    only the matching pending wrapper notices so their thread projections stop looking like
    current work.
    """
    closed = 0
    for rid in _pending_dead_letter_notice_request_ids(
        store, actor=actor, agent=agent, msg_id=msg_id,
    ):
        meta = {
            "request_id": rid,
            "operator_answer": "true",
            "operator_origin": actor,
            "dead_letter_resolved": "true",
            "dead_letter_agent": agent,
            "dead_letter_msg_id": msg_id,
        }
        if evidence:
            meta["dead_letter_evidence"] = evidence
        try:
            store.send(
                sender=actor,
                recipient=agent,
                kind="message",
                subject=f"dead-letter resolved ({msg_id})",
                body=f"Dead-letter {agent}/{msg_id} was resolved by {actor}: {reason}",
                meta=meta,
            )
            closed += 1
        except (OSError, ValueError):
            continue
    return closed


def _cmd_dead_letter_resolve(store: Store, args: argparse.Namespace) -> int:
    """dead-letter resolve: an operator decision distinct from requeue. Preserves the
    payload + sidecar; appends an AUTHORITATIVE resolve_dead_letter event to the central
    disposition log, then best-effort writes a .resolved.json sidecar for copied-sink
    readability (central wins on conflict - gate 6). Actor = liaison/sole-lead (no --by)."""
    from agenttalk import attention as A
    if not (getattr(args, "agent", None) and getattr(args, "id", None)):
        sys.stderr.write("agenttalk dead-letter resolve: --agent and --id are required.\n")
        return 2
    reason = getattr(args, "reason", None)
    if not reason or not reason.strip():
        sys.stderr.write("agenttalk dead-letter resolve: --reason is required.\n")
        return 2
    actor = _resolve_disposition_actor(store, args)
    if actor is None:
        sys.stderr.write("agenttalk dead-letter resolve: only the operator-facing liaison "
                         "(or the sole lead) may resolve; resolve --from/$AGENTTALK_SELF.\n")
        return 2
    if not _is_listed_dead_letter(store, args.agent, args.id):
        sys.stderr.write(f"agenttalk dead-letter resolve: no dead-letter "
                         f"{args.agent}/{args.id}.\n")
        return 2
    entry = next((m for m in store.list_dead_letters(args.agent)
                  if m.get("message_id") == args.id), None)
    if entry is None:
        sys.stderr.write(f"agenttalk dead-letter resolve: no dead-letter "
                         f"{args.agent}/{args.id}.\n")
        return 2
    src_hash = A.dead_letter_entry_source_hash(entry)
    event_id = "att-" + uuid.uuid4().hex[:12]
    A.append_disposition(store, {
        "schema_version": A.SCHEMA_VERSION, "event_id": event_id,
        "item_id": A.item_id(A.SOURCE_DEAD_LETTER, args.agent, args.id),
        "source": A.SOURCE_DEAD_LETTER, "action": A.ACTION_RESOLVE_DEAD_LETTER,
        "actor": actor, "reason": reason, "at": _attn_now_iso(),
        "evidence": getattr(args, "evidence", None),
        "source_snapshot": {"source_hash": src_hash,
                            "refs": [{"kind": "dead_letter", "agent": args.agent,
                                      "message_id": args.id}]}})
    # best-effort sidecar (central log already durable + authoritative)
    try:
        side = store.dead_letter_dir / args.agent / f"{args.id}.resolved.json"
        side.write_text(json.dumps({"event_id": event_id, "actor": actor, "reason": reason,
                                    "evidence": getattr(args, "evidence", None),
                                    "at": _attn_now_iso(), "source_hash": src_hash},
                                   ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    closed = _close_dead_letter_notice_threads(
        store,
        actor=actor,
        agent=args.agent,
        msg_id=args.id,
        reason=reason.strip(),
        evidence=getattr(args, "evidence", None),
    )
    extra = f"; closed {closed} related escalation thread(s)" if closed else ""
    print(f"resolved dead-letter {args.agent}/{args.id} by {actor} "
          f"(payload preserved; requeue with --force-resolved --reason to reopen{extra})")
    return 0


def _cmd_dead_letter_purge(store: Store, args: argparse.Namespace) -> int:
    if not getattr(args, "resolved", False):
        sys.stderr.write("agenttalk dead-letter purge: pass --resolved (only resolved "
                         "dead-letters can be purged).\n")
        return 2
    actor = _resolve_disposition_actor(store, args)
    if actor is None:
        sys.stderr.write("agenttalk dead-letter purge: only the operator-facing liaison "
                         "(or the sole lead) may purge; set --from/$AGENTTALK_SELF.\n")
        return 2
    agent_filter = getattr(args, "agent", None)
    items = store.list_dead_letters(agent_filter)
    res = _dead_letter_resolution_state(store)
    candidates = [
        m for m in items
        if res.get((str(m.get("agent") or ""), str(m.get("message_id") or ""))) == "resolved"
    ]
    if getattr(args, "json", False) and getattr(args, "dry_run", False):
        preview = []
        for entry in candidates:
            ag = str(entry.get("agent") or "")
            mid = str(entry.get("message_id") or "")
            preview.append({
                **entry,
                "pending_notice_request_ids": _pending_dead_letter_notice_request_ids(
                    store, actor=actor, agent=ag, msg_id=mid,
                ),
            })
        print(json.dumps({"dry_run": True, "count": len(preview), "items": preview},
                         indent=2))
        return 0
    if not candidates:
        if getattr(args, "json", False):
            print(json.dumps({"dry_run": bool(getattr(args, "dry_run", False)),
                              "count": 0, "archive_dir": None, "items": []}, indent=2))
        else:
            print("dead-letter purge: no resolved dead-letters")
        return 0

    if not getattr(args, "dry_run", False):
        blocked: list[str] = []
        for entry in candidates:
            ag = str(entry.get("agent") or "")
            mid = str(entry.get("message_id") or "")
            if _pending_dead_letter_notice_request_ids(store, actor=actor, agent=ag, msg_id=mid):
                _close_dead_letter_notice_threads(
                    store, actor=actor, agent=ag, msg_id=mid,
                    reason="dead-letter purge preflight",
                )
            pending = _pending_dead_letter_notice_request_ids(
                store, actor=actor, agent=ag, msg_id=mid,
            )
            if pending:
                blocked.append(f"{ag}/{mid} ({', '.join(pending)})")
        if blocked:
            sys.stderr.write(
                "agenttalk dead-letter purge: refusing to archive resolved item(s) "
                "with pending wrapper notice thread(s): "
                + "; ".join(blocked)
                + "\n"
            )
            return 2

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_root = store.dir / "dead-letter-archive" / stamp
    moved: list[dict] = []
    for entry in candidates:
        agent = str(entry.get("agent") or "")
        msg_id = str(entry.get("message_id") or "")
        try:
            safe_agent = validate_agent_name(agent)
        except ValueError:
            continue
        src_dir = store.dead_letter_dir / safe_agent
        dst_dir = archive_root / safe_agent
        names = [
            f"{msg_id}.json",
            f"{msg_id}.deadletter.json",
            f"{msg_id}.resolved.json",
        ]
        selected = [src_dir / name for name in names if (src_dir / name).is_file()]
        if not selected:
            continue
        if getattr(args, "dry_run", False):
            moved.append({"agent": agent, "message_id": msg_id,
                          "files": [p.name for p in selected]})
            continue
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst_files = []
        for src in selected:
            dst = dst_dir / src.name
            if dst.exists():
                dst = dst_dir / f"{src.stem}.{uuid.uuid4().hex[:8]}{src.suffix}"
            shutil.move(str(src), str(dst))
            dst_files.append(str(dst))
        moved.append({"agent": agent, "message_id": msg_id, "files": dst_files})

    if getattr(args, "json", False):
        print(json.dumps({
            "dry_run": bool(getattr(args, "dry_run", False)),
            "count": len(moved),
            "archive_dir": str(archive_root),
            "items": moved,
        }, indent=2))
        return 0
    verb = "would archive" if getattr(args, "dry_run", False) else "archived"
    print(f"dead-letter purge: {verb} {len(moved)} resolved item(s) "
          f"to {archive_root}")
    return 0


def cmd_dead_letter(args: argparse.Namespace) -> int:
    """Inspect + recover dead-lettered (poison) messages. SEPARATE from `prune
    --invalid` (that quarantines INVALID/forged files = a trust failure; this handles
    VALID files the model could not process = a delivery failure). Move-only +
    recoverable: `requeue` re-injects a FRESH message (new id, own fresh attempt
    count) - it never rewinds the cursor (would re-poison the loop)."""
    store = _get_store(args)
    action = getattr(args, "dead_letter_cmd", None)
    if action == "resolve":
        return _cmd_dead_letter_resolve(store, args)
    if action == "purge":
        return _cmd_dead_letter_purge(store, args)
    if action == "list":
        items = store.list_dead_letters(getattr(args, "agent", None))
        # resolved-aware (0.56.0): default shows UNRESOLVED only; --resolved / --all audit.
        res = _dead_letter_resolution_state(store)
        show_resolved = getattr(args, "resolved", False)
        show_all = getattr(args, "all", False)
        if not show_all:
            items = [m for m in items
                     if (res.get((m.get("agent"), m.get("message_id"))) == "resolved") == show_resolved]
        if getattr(args, "json", False):
            print(json.dumps(items, indent=2))
            return 0
        if not items:
            print("dead-letter: none")
            return 0
        print(f"dead-letter ({len(items)}):")
        for m in items:
            print(f"  {m.get('agent')}/{m.get('message_id')}  from={m.get('from')} "
                  f"kind={m.get('kind')} attempts={m.get('attempts')} "
                  f"class={m.get('class')} reason={(m.get('last_reason') or '')[:60]}")
        # A `requeue` re-injects a FRESH copy but PRESERVES the original here, so a handled
        # dead-letter keeps showing until you `resolve` it. We deliberately do NOT auto-quiet
        # (that could hide a real unhandled poison) - we point at the flow instead (fable-max #2).
        if not show_resolved:
            print("  tip: `requeue` leaves the original listed; run `agenttalk dead-letter "
                  "resolve --agent A --id ID --reason ...` once handled to quiet it.")
        return 0
    if action == "show":
        if not (getattr(args, "agent", None) and getattr(args, "id", None)):
            sys.stderr.write("agenttalk dead-letter show: --agent and --id are required.\n")
            return 2
        meta = next((m for m in store.list_dead_letters(args.agent)
                     if m.get("message_id") == args.id), None)
        raw = store.read_dead_letter_payload(args.agent, args.id)
        if meta is None and raw is None:
            sys.stderr.write(
                f"agenttalk dead-letter show: no dead-letter {args.agent}/{args.id}.\n")
            return 2
        body = ""
        if raw is not None:
            try:
                body = (json.loads(raw.decode("utf-8")) or {}).get("body", "")
            except (ValueError, UnicodeDecodeError):
                body = "<unreadable payload>"
        child_output_tail = (
            meta.get("child_output_tail")
            if isinstance(meta, dict) and isinstance(meta.get("child_output_tail"), dict)
            else None
        )
        out = {"metadata": meta, "body": body, "child_output_tail": child_output_tail}
        if getattr(args, "json", False):
            print(json.dumps(out, indent=2))
        else:
            print(json.dumps(meta, indent=2))
            print("---- original body (untrusted data) ----")
            print(body)
            if child_output_tail is not None:
                lines = child_output_tail.get("lines")
                if isinstance(lines, list) and lines:
                    print("---- child output tail (redacted) ----")
                    for line in lines:
                        if not isinstance(line, dict):
                            continue
                        stream = line.get("stream")
                        text = line.get("text")
                        if stream not in {"stdout", "stderr"} or not isinstance(text, str):
                            continue
                        print(f"[{stream}] {text}")
                    if child_output_tail.get("truncated") is True:
                        print(
                            "---- child output tail truncated "
                            f"(last {child_output_tail.get('max_lines')} lines / "
                            f"{child_output_tail.get('max_bytes')} bytes) ----"
                        )
        return 0
    if action == "requeue":
        if not (getattr(args, "agent", None) and getattr(args, "id", None)):
            sys.stderr.write("agenttalk dead-letter requeue: --agent and --id are required.\n")
            return 2
        if not _is_listed_dead_letter(store, args.agent, args.id):   # F5 traversal/typo guard
            sys.stderr.write(
                f"agenttalk dead-letter requeue: no dead-letter {args.agent}/{args.id}.\n")
            return 2
        raw = store.read_dead_letter_payload(args.agent, args.id)
        if raw is None:
            sys.stderr.write(
                f"agenttalk dead-letter requeue: no dead-letter {args.agent}/{args.id}.\n")
            return 2
        # A RESOLVED dead-letter requires an explicit --force-resolved + a non-empty --reason
        # to requeue. We VALIDATE + resolve authority here, but BUILD the reopen audit event
        # without appending it yet - it is appended only AFTER the payload parse + send SUCCEED
        # (fable-max #6), so a corrupt-payload / send failure can never leave the item
        # audit-reopened with no requeued message.
        reopen_event = None
        if _dead_letter_resolution_state(store).get((args.agent, args.id)) == "resolved":
            reason = getattr(args, "reason", None)
            # reason must be NON-EMPTY AFTER STRIP, exactly like resolve + attention
            # dispositions (codex F7): a whitespace-only reason folds to an INVALID disposition
            # line, so it must exit 2 and SEND NOTHING rather than requeue with a blank audit.
            if not getattr(args, "force_resolved", False) or not reason or not reason.strip():
                sys.stderr.write("agenttalk dead-letter requeue: this item is RESOLVED; "
                                 "pass --force-resolved --reason TEXT (non-empty) to requeue it.\n")
                return 2
            # Reopening a resolved dead-letter is an operator-authority disposition write, so
            # it MUST go through the SAME liaison/sole-lead resolver as resolve (codex F1) -
            # NOT _resolve_self, which any roster identity satisfies (authority bypass, gate 5).
            reopen_actor = _resolve_disposition_actor(store, args)
            if reopen_actor is None:
                sys.stderr.write("agenttalk dead-letter requeue: only the operator-facing "
                                 "liaison (or the sole lead) may reopen a RESOLVED "
                                 "dead-letter; set --from/$AGENTTALK_SELF to that identity.\n")
                return 2
            from agenttalk import attention as A
            entry = next((m for m in store.list_dead_letters(args.agent)
                          if m.get("message_id") == args.id), None)
            if entry is None:
                sys.stderr.write(
                    f"agenttalk dead-letter requeue: no dead-letter {args.agent}/{args.id}.\n")
                return 2
            reopen_event = {
                "schema_version": A.SCHEMA_VERSION, "event_id": "att-" + uuid.uuid4().hex[:12],
                "item_id": A.item_id(A.SOURCE_DEAD_LETTER, args.agent, args.id),
                "source": A.SOURCE_DEAD_LETTER, "action": A.ACTION_REQUEUED_AFTER_RESOLVE,
                "actor": reopen_actor,
                "reason": reason, "at": _attn_now_iso(),
                "source_snapshot": {"source_hash": A.dead_letter_entry_source_hash(entry),
                                    "refs": []}}
        try:
            orig = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            sys.stderr.write(f"agenttalk dead-letter requeue: corrupt payload ({e}).\n")
            return 2
        meta = dict(orig.get("meta") or {})
        meta["requeued_from"] = args.id   # provenance; preserves request_id/broadcast_id
        try:
            new = store.send(
                sender=orig.get("from"), recipient=orig.get("to"),
                body=orig.get("body", ""), kind=orig.get("kind", "message"),
                subject=orig.get("subject", ""), meta=meta)
        except (ValueError, FileNotFoundError) as e:
            sys.stderr.write(f"agenttalk dead-letter requeue: {e}\n")
            return 2
        # Send SUCCEEDED - NOW record the reopen audit (ordering closes fable-max #6).
        if reopen_event is not None:
            from agenttalk import attention as A
            A.append_disposition(store, reopen_event)
        print(f"requeued dead-letter {args.id} as fresh message {new.id} "
              "(new id, own fresh attempt count; original evidence preserved in the sink)")
        return 0
    sys.stderr.write("agenttalk dead-letter: expected list, show, requeue, resolve, or purge.\n")
    return 2


def cmd_supervise(args: argparse.Namespace) -> int:
    """Supervisor support (thin): --init scaffolds config+scripts; --report
    emits the read-only liveness JSON; --plan emits the action plan (the shared
    decision table); --clear-restart clears a restart marker by request_id."""
    store = _get_store(args)
    supervisor_mutations = (
        "archive_launch_request",
        "claim_instance",
        "clear_restart",
        "drain_intents",
        "janitor_ephemeral",
        "prepare_launch_request",
        "record_ephemeral_launch",
        "record_launch",
        "refresh_scripts",
        "seed_claude_settings",
        "seed_codex_config",
        "select_pwsh",
        "prepare_task_install",
        "commit_task_install",
        "validate_task_start",
    )
    if (
        any(bool(getattr(args, name, False)) for name in supervisor_mutations)
        and (store.dir / "supervisor.kill").exists()
    ):
        sys.stderr.write(
            "agenttalk supervise: supervisor.kill is present; refusing "
            "script-use supervisor mutation\n"
        )
        return 3
    if args.init:
        try:
            res = sup.init(store, force=args.force)
        except (OSError, sup.ArtifactValidationError,
                supervisor_lifecycle.SupervisorLifecycleError) as e:
            sys.stderr.write(f"agenttalk supervise --init: {e}\n")
            return 3
        for path, status in res.items():
            print(f"  {status}: {path}")
        wrote = [p for p, s in res.items() if s == "written"]
        missing = [p for p, s in res.items() if s == "missing"]
        if missing:
            print(
                "supervise --init: generated artifacts are partially scaffolded; "
                "run `agenttalk supervise --refresh-scripts` or rerun "
                "`agenttalk supervise --init --force`."
            )
        elif not wrote:
            print("supervise --init: all files already exist (use --force to "
                  "regenerate).")
        else:
            print("supervise --init: fill in each agent's launch command in "
                  "supervisor.json, run `agenttalk supervise --select-pwsh`, "
                  "then launch supervisor.ps1 with the returned absolute host. "
                  "(PowerShell Core 7+ is required; a POSIX bash supervisor "
                  "is a follow-up — the Python core is already cross-platform.)")
        print("\nManaged hooks (the activity hook unlocks stuck-recovery after "
              "activity_hook=true; Claude also gets fail-soft checkpoint hooks):\n"
              "  agenttalk supervise --install-activity-hook   # merges project "
              ".claude/settings.json (add --codex for .codex/hooks.json)\n"
              "Or paste these hooks into your project .claude/settings.json:\n"
              f"{sup.claude_hook_snippet()}")
        return 0
    if args.refresh_scripts:
        try:
            if args.pwsh:
                supervisor_lifecycle.select_powershell_host(
                    store, explicit_path=args.pwsh,
                )
            res = sup.refresh_artifacts(store)
            sup.validate_artifact_bundle(store)
        except (OSError, sup.ArtifactValidationError,
                supervisor_lifecycle.SupervisorLifecycleError) as e:
            sys.stderr.write(f"agenttalk supervise --refresh-scripts: {e}\n")
            return 3
        for path, status in res.items():
            print(f"  {status}: {path}")
        return 0
    if args.select_pwsh:
        try:
            record, attempts = supervisor_lifecycle.select_powershell_host(
                store, explicit_path=args.pwsh,
            )
        except (OSError, supervisor_lifecycle.SupervisorLifecycleError) as e:
            sys.stderr.write(f"agenttalk supervise --select-pwsh: {e}\n")
            return 3
        payload = dict(record)
        payload["attempts"] = [attempt.to_dict() for attempt in attempts]
        print(json.dumps(payload, indent=2))
        if payload.get("version"):
            version = psh.PowerShellVersion(**payload["version"])
            warning = psh.host_warning(payload.get("edition"), version)
            if warning:
                sys.stderr.write(f"agenttalk supervise --select-pwsh: WARN: {warning}\n")
        return 0
    if args.repair_instance_marker:
        if not args.quarantine or not args.acknowledge_no_live_supervisor:
            sys.stderr.write(
                "agenttalk supervise --repair-instance-marker requires "
                "--quarantine --acknowledge-no-live-supervisor\n"
            )
            return 2
        try:
            path = supervisor_lifecycle.repair_invalid_instance_marker(store)
        except (OSError, ValueError) as e:
            sys.stderr.write(f"agenttalk supervise --repair-instance-marker: {e}\n")
            return 3
        print("no instance marker present" if path is None else f"quarantined: {path}")
        return 0
    if args.validate_current_pwsh:
        try:
            sup.validate_artifact_bundle(store, boundary=args.artifact_boundary)
            record = supervisor_lifecycle.validate_current_powershell(
                store,
                pid=args.pid if args.pid is not None else os.getpid(),
                pid_start=args.pid_start,
            )
        except (OSError, sup.ArtifactValidationError,
                supervisor_lifecycle.SupervisorLifecycleError) as e:
            sys.stderr.write(f"agenttalk supervise --validate-current-pwsh: {e}\n")
            return 3
        print(json.dumps(psh.selection_public_view(record), separators=(",", ":")))
        return 0
    if args.prepare_task_install:
        try:
            payload = supervisor_lifecycle.prepare_task_install(
                store,
                pid=args.pid if args.pid is not None else os.getpid(),
                pid_start=args.pid_start,
                task_name=args.task_name or "agenttalk-supervisor",
                validate_artifacts=lambda: sup.validate_artifact_bundle(
                    store, boundary="task"
                ),
            )
        except (OSError, sup.ArtifactValidationError,
                supervisor_lifecycle.SupervisorLifecycleError) as e:
            sys.stderr.write(f"agenttalk supervise --prepare-task-install: {e}\n")
            return 3
        print(json.dumps(payload, separators=(",", ":")))
        return 0
    if args.commit_task_install:
        try:
            payload = supervisor_lifecycle.commit_task_install(
                store,
                pid=args.pid if args.pid is not None else os.getpid(),
                pid_start=args.pid_start,
                task_name=args.task_name or "agenttalk-supervisor",
                expected_revision=args.selection_revision,
                expected_fingerprint=args.selection_fingerprint or "",
                validate_artifacts=lambda: sup.validate_artifact_bundle(
                    store, boundary="task"
                ),
            )
        except (OSError, sup.ArtifactValidationError,
                supervisor_lifecycle.SupervisorLifecycleError) as e:
            sys.stderr.write(f"agenttalk supervise --commit-task-install: {e}\n")
            return 3
        print(json.dumps(payload, separators=(",", ":")))
        return 0
    if args.clear_task_binding:
        try:
            payload = supervisor_lifecycle.clear_task_binding(
                store,
                task_name=args.task_name or "agenttalk-supervisor",
            )
        except (OSError, supervisor_lifecycle.SupervisorLifecycleError) as e:
            sys.stderr.write(f"agenttalk supervise --clear-task-binding: {e}\n")
            return 3
        print(json.dumps(payload, separators=(",", ":")))
        return 0
    if args.validate_task_start:
        try:
            sup.validate_artifact_bundle(store, boundary="task")
            record = supervisor_lifecycle.validate_current_powershell(
                store,
                pid=args.pid if args.pid is not None else os.getpid(),
                pid_start=args.pid_start,
            )
            expected = sup.expected_task_action(store)
            mismatches = []
            if os.path.normcase(os.path.normpath(args.task_execute or "")) != os.path.normcase(
                os.path.normpath(str(record["path"]))
            ):
                mismatches.append("Execute")
            if (args.task_arguments or "") != expected["arguments"]:
                mismatches.append("Arguments")
            if os.path.normcase(os.path.normpath(args.task_working_directory or "")) != os.path.normcase(
                os.path.normpath(expected["working_directory"])
            ):
                mismatches.append("WorkingDirectory")
            if record.get("task_name") != (args.task_name or "agenttalk-supervisor"):
                mismatches.append("TaskName")
            if mismatches:
                raise supervisor_lifecycle.SupervisorLifecycleError(
                    "registered task mismatch: " + ", ".join(mismatches)
                    + "; " + sup.task_recovery_remediation(
                        store, str(record["path"]), args.task_name or "agenttalk-supervisor"
                    )
                )
        except (OSError, sup.ArtifactValidationError,
                supervisor_lifecycle.SupervisorLifecycleError) as e:
            sys.stderr.write(f"agenttalk supervise --validate-task-start: {e}\n")
            return 3
        print("task action matches selected PowerShell host and checkout")
        return 0
    if args.install_activity_hook:
        interactive_for = getattr(args, "interactive_for", None)
        if interactive_for and (args.codex or args.codex_only):
            sys.stderr.write(
                "agenttalk supervise --install-activity-hook: --interactive-for "
                "cannot be combined with --codex or --codex-only\n"
            )
            return 2
        if interactive_for:
            try:
                interactive_for = sup.resolve_interactive_activity_hook_target(
                    store, interactive_for)
            except ValueError as e:
                sys.stderr.write(
                    f"agenttalk supervise --install-activity-hook: {e}\n")
                return 2
        res = sup.install_activity_hook(
            store,
            claude=True if interactive_for else not args.codex_only,
            codex=False if interactive_for else args.codex or args.codex_only,
            interactive_for=interactive_for,
        )
        for path, event_statuses in res.items():
            for event, status in event_statuses.items():
                print(f"  {status}: {path} [{event}]")
        print("install-activity-hook: PROJECT config only (never global, never "
              "clobbered); the per-hook results above are authoritative. "
              "Now set activity_hook=true for the instrumented agents in "
              "supervisor.json to enable stuck-recovery.")
        return 0
    if args.seed_codex_config:
        # Overlay the unattended-auto-mode keys onto a (already-COPIED) config.toml
        # in the isolated CODEX_HOME. --home is the isolated home; the repo abs
        # path (writable_roots) defaults to the store root.
        if not args.home:
            sys.stderr.write("agenttalk supervise --seed-codex-config: need --home <dir>\n")
            return 2
        cfg_p = Path(args.home) / "config.toml"
        repo = str(Path(args.repo).resolve() if args.repo else store.root.resolve())
        sandbox = args.sandbox or "unelevated"
        # Heal a pre-0.75.3 BOM-corrupted config: collapse duplicate [projects.<repo>]
        # tables (SEMANTIC key match, project-scoped) BEFORE overlaying, so the launch
        # seed emits valid TOML the external codex CLI can parse (D-26).
        if cfg_p.exists():
            cxc.repair_duplicate_project_tables(cfg_p, Path(repo))
        existing = cfg_p.read_text(encoding="utf-8-sig") if cfg_p.exists() else ""
        cfg_p.parent.mkdir(parents=True, exist_ok=True)
        cfg_p.write_text(sup.codex_config_overlay(existing, repo_path=repo,
                                                  windows_sandbox=sandbox), encoding="utf-8")
        print(f"seeded codex config.toml (sandbox={sandbox}): {cfg_p}")
        return 0
    if args.seed_claude_settings:
        # Merge {"defaultMode": <mode>} into <dir>/.claude/settings.json.
        if not args.dir:
            sys.stderr.write("agenttalk supervise --seed-claude-settings: need --dir <dir>\n")
            return 2
        sp = Path(args.dir) / ".claude" / "settings.json"
        existing = sp.read_text(encoding="utf-8-sig") if sp.exists() else None
        sp.parent.mkdir(parents=True, exist_ok=True)
        mode = args.mode or "bypassPermissions"
        sp.write_text(sup.seed_claude_settings(existing, mode=mode), encoding="utf-8")
        print(f"seeded .claude/settings.json (defaultMode={mode}): {sp}")
        return 0
    if args.claim_instance:
        pid = args.pid if args.pid is not None else os.getpid()
        try:
            rec = supervisor_lifecycle.claim_powershell_supervisor(
                store,
                pid=pid,
                pid_start=args.pid_start,
                validate_artifacts=lambda: sup.validate_artifact_bundle(
                    store, boundary="supervisor"
                ),
            )
        except (OSError, sup.ArtifactValidationError,
                supervisor_lifecycle.SupervisorLifecycleError) as e:
            sys.stderr.write(f"agenttalk supervise --claim-instance: {e}\n")
            return 3
        if rec is None:
            sys.stderr.write("agenttalk supervise --claim-instance: another live supervisor instance owns this root\n")
            return 3
        print(json.dumps(rec, indent=2))
        return 0
    if args.release_instance:
        ok = store.release_supervisor_instance(
            token=args.instance_token or "", pid=args.pid, pid_start=args.pid_start)
        if not ok:
            sys.stderr.write("agenttalk supervise --release-instance: token/pid did not match the live instance\n")
            return 3
        print("released supervisor instance")
        return 0
    if args.drain_intents:
        from agenttalk import intents as intent_mod
        pid = args.pid if args.pid is not None else os.getpid()
        owned = False
        if args.instance_token:
            rec = store.read_supervisor_instance()
            if not rec or rec.get("token") != args.instance_token:
                sys.stderr.write("agenttalk supervise --drain-intents: invalid or missing supervisor instance token\n")
                return 3
            if args.pid is not None and rec.get("pid") != args.pid:
                sys.stderr.write("agenttalk supervise --drain-intents: instance pid mismatch\n")
                return 3
            if args.pid_start is not None and rec.get("pid_start") != args.pid_start:
                sys.stderr.write("agenttalk supervise --drain-intents: instance pid-start mismatch\n")
                return 3
        else:
            rec = store.claim_supervisor_instance(pid=pid, pid_start=args.pid_start)
            if rec is None:
                sys.stderr.write(
                    "agenttalk supervise --drain-intents: another live "
                    "supervisor instance owns this root\n")
                return 3
            args.instance_token = rec.get("token")
            owned = True
        try:
            summary = intent_mod.drain_intents(
                store, pid=pid, pid_start=args.pid_start,
                max_per_tick=args.max_per_tick)
            print(json.dumps(summary, indent=2))
            return 0
        finally:
            if owned:
                store.release_supervisor_instance(
                    token=args.instance_token or "", pid=pid, pid_start=args.pid_start)

    def _read_state() -> dict:
        if not args.state_file:
            return {"agents": {}}
        return sup.load_supervisor_state(Path(args.state_file))

    def _write_state(state: dict) -> None:
        if not args.state_file:
            raise ValueError("need --state-file <path>")
        sup.save_supervisor_state(Path(args.state_file), state)

    def _read_snapshot_file(path_value: str | None) -> list[dict] | None:
        if path_value and Path(path_value).exists():
            try:
                raw = json.loads(Path(path_value).read_text(encoding="utf-8-sig"))
                return raw if isinstance(raw, list) else None
            except (ValueError, OSError):
                return None
        return None

    if args.reset_process_tree_ownership:
        configured_reset = bool(args.agent)
        ephemeral_reset = bool(args.request_id)
        if (
            configured_reset == ephemeral_reset
            or not args.hold_source_hash
            or (configured_reset and not args.verified_launch_nonce)
            or not args.reason
            or not args.acknowledge_no_live_supervisor
            or not args.acknowledge_owned_processes_stopped
        ):
            sys.stderr.write(
                "agenttalk supervise --reset-process-tree-ownership requires "
                "exactly one of --for or --request-id, plus "
                "--hold-source-hash, --reason, "
                "--acknowledge-no-live-supervisor, and "
                "--acknowledge-owned-processes-stopped; configured-agent "
                "resets also require --verified-launch-nonce\n"
            )
            return 2
        attended_reason = args.reason.strip()
        if not eph.is_safe_reason(attended_reason):
            sys.stderr.write(
                "agenttalk supervise --reset-process-tree-ownership: "
                "--reason must be a non-empty single line of at most "
                "500 characters\n"
            )
            return 2
        state_path = store.dir / "supervisor-state.json"
        if (
            args.state_file
            and Path(args.state_file).resolve() != state_path.resolve()
        ):
            sys.stderr.write(
                "agenttalk supervise --reset-process-tree-ownership: "
                "--state-file must be the official .agenttalk/supervisor-state.json\n"
            )
            return 2
        now = args.now if args.now is not None else time.time()
        output_record: dict | None = None
        try:
            with store._supervisor_lifecycle_lock():
                marker_status, _marker, marker_detail = (
                    store._read_supervisor_instance_strict_locked()
                )
                if marker_status != "absent":
                    raise ValueError(
                        "supervisor instance marker is "
                        f"{marker_status}: {marker_detail or 'a supervisor may be live'}"
                    )
                if store.supervisor_kill_switch() is not True:
                    raise ValueError("supervisor.kill must remain present")
                with store._config_lock():
                    actor = _resolve_disposition_actor(store, args)
                    if actor is None:
                        raise ValueError(
                            "--from must resolve to the operator-facing liaison "
                            "or sole lead"
                        )
                    marker_status, _marker, marker_detail = (
                        store._read_supervisor_instance_strict_locked()
                    )
                    if marker_status != "absent":
                        raise ValueError(
                            "supervisor instance marker changed to "
                            f"{marker_status}: "
                            f"{marker_detail or 'a supervisor may be live'}"
                        )
                    if store.supervisor_kill_switch() is not True:
                        raise ValueError("supervisor.kill was removed during reset")

                    state = sup.load_supervisor_state(state_path)
                    from agenttalk import attention as A

                    try:
                        supervisor_config = _load_supervisor_config(store)
                    except Exception:  # noqa: BLE001 - match attention fail-closed view
                        supervisor_config = None
                    try:
                        store_config = store.load_config()
                    except Exception:  # noqa: BLE001 - match attention fail-closed view
                        store_config = None
                    restart_requests: dict[str, dict] = {}
                    if configured_reset:
                        try:
                            restart_marker = store.read_restart_request(args.agent)
                        except Exception:  # noqa: BLE001 - optional projection context
                            restart_marker = None
                        if isinstance(restart_marker, dict):
                            restart_requests[args.agent] = restart_marker
                    reset_admissions = sup.evaluate_process_tree_reset_admissions(
                        store,
                        state,
                        actor=actor,
                        now_epoch=now,
                        identity_gone=_owner_identity_gone,
                    )
                    launch_requests = sup.active_ephemeral_launch_markers(
                        store,
                        state,
                    )
                    launch_deliveries = sup.active_ephemeral_one_shot_deliveries(
                        store,
                        state,
                        launch_requests,
                    )
                    lane_workspaces = sup.active_ephemeral_lane_workspaces(store)
                    current_items = []
                    for item in A.process_tree_hold_items(
                        state,
                        supervisor_config=supervisor_config,
                        store_config=store_config,
                        root=store.root,
                        restart_requests=restart_requests,
                        launch_requests=launch_requests,
                        launch_deliveries=launch_deliveries,
                        lane_workspaces=lane_workspaces,
                        reset_admissions=reset_admissions,
                        now_epoch=now,
                    ):
                        refs = item.get("source_refs")
                        if not (
                            isinstance(refs, list)
                            and len(refs) == 1
                            and isinstance(refs[0], dict)
                        ):
                            continue
                        ref = refs[0]
                        if configured_reset:
                            matches = (
                                ref.get("kind") == "supervisor_state"
                                and ref.get("agent") == args.agent
                            )
                        else:
                            matches = (
                                ref.get("kind")
                                == "supervisor_ephemeral_state"
                                and ref.get("request_id") == args.request_id
                            )
                        if matches:
                            current_items.append(item)
                    if len(current_items) != 1:
                        target = (
                            f"configured agent {args.agent!r}"
                            if configured_reset
                            else f"ephemeral request {args.request_id!r}"
                        )
                        raise ValueError(
                            "no unique process-tree HOLD exists for " + target
                        )
                    current_item = current_items[0]
                    if current_item.get("source_hash") != args.hold_source_hash:
                        raise ValueError(
                            "HOLD source hash is stale; read `agenttalk attention` again"
                        )

                    if configured_reset:
                        target_agent = args.agent
                        runtime_view = runtime_obs.read_runtime(
                            store.state_dir,
                            target_agent,
                            now_epoch=now,
                        )
                        if runtime_view.get("status") != runtime_obs.STATUS_VALID:
                            raise ValueError(
                                "strict wrapper runtime record is not valid: "
                                f"{runtime_view.get('error') or runtime_view.get('status')}"
                            )
                        evidence = sup.process_tree_ownership_reset_evidence(
                            state,
                            target_agent,
                            expected_root=store.root,
                            verified_launch_nonce=args.verified_launch_nonce,
                            runtime_record=runtime_view["record"],
                            now_epoch=now,
                        )
                        live_identities = [
                            row
                            for row in evidence["identities"]
                            if not _owner_identity_gone(
                                row["pid"],
                                row["start"],
                                row.get("start_filetime"),
                            )
                        ]
                        if live_identities:
                            raise ValueError(
                                "recorded process identities are still live or "
                                "cannot be distinguished from pid reuse: "
                                + ", ".join(
                                    f"{row['pid']}/{row['start']}"
                                    for row in live_identities[:8]
                                )
                            )
                        marker_status, _marker, marker_detail = (
                            store._read_supervisor_instance_strict_locked()
                        )
                        if marker_status != "absent":
                            raise ValueError(
                                "supervisor instance marker changed before commit: "
                                f"{marker_status}: "
                                f"{marker_detail or 'a supervisor may be live'}"
                            )
                        if store.supervisor_kill_switch() is not True:
                            raise ValueError(
                                "supervisor.kill was removed before reset commit"
                            )
                        sup.reset_process_tree_ownership_after_attended_teardown(
                            state,
                            target_agent,
                            hold_source_hash=args.hold_source_hash,
                            acknowledged_by=actor,
                            verified_launch_nonce=args.verified_launch_nonce,
                            expected_root=store.root,
                            runtime_record=runtime_view["record"],
                            recorded_identities_gone=True,
                            reason=attended_reason,
                            now_epoch=now,
                        )
                        sup.save_supervisor_state(state_path, state)
                        output_record = state["process_tree_resets"][-1]
                    else:
                        if not eph.is_safe_id(args.request_id):
                            raise ValueError(
                                "ephemeral request id must be a safe path token"
                            )
                        pending = sup.attended_ephemeral_archive_pending(
                            state,
                            args.request_id,
                        )
                        if pending is not None:
                            # ``actor`` is authorized against the current
                            # liaison configuration above. Do not require it
                            # to equal the original acknowledger: a durable
                            # retry must survive legitimate liaison turnover,
                            # while the journal keeps the original audit fact.
                            # The current item hash already binds the current
                            # actor; only immutable staged arguments must still
                            # equal the journal.
                            if (
                                pending["reason"] != attended_reason
                                or pending["verified_launch_nonce"]
                                != args.verified_launch_nonce
                            ):
                                raise ValueError(
                                    "retry arguments do not match the durable "
                                    "attended archive journal"
                                )
                        else:
                            ref = current_item["source_refs"][0]
                            target_agent = validate_agent_name(ref.get("agent"))
                            eph_root = state.get("ephemeral_reviewers")
                            active = (
                                eph_root.get("active")
                                if isinstance(eph_root, dict)
                                else None
                            )
                            entry = (
                                active.get(args.request_id)
                                if isinstance(active, dict)
                                else None
                            )
                            if (
                                not isinstance(entry, dict)
                                or entry.get("request_id") != args.request_id
                                or entry.get("agent") != target_agent
                            ):
                                raise ValueError(
                                    "ephemeral HOLD does not match one exact "
                                    "active request and temporary identity"
                                )
                            held_terminal = eph.validate_held_terminal(
                                entry.get("held_terminal")
                            )
                            if held_terminal is None:
                                raise ValueError(
                                    "ephemeral HOLD has no valid persisted "
                                    "terminal disposition to archive"
                                )
                            launch_marker = store.read_launch_request(
                                args.request_id
                            )
                            marker_errors = eph.validate_marker(launch_marker)
                            if (
                                marker_errors
                                or not isinstance(launch_marker, dict)
                                or launch_marker.get("request_id")
                                != args.request_id
                                or launch_marker.get("agent") != target_agent
                                or launch_marker.get("state")
                                not in eph.ACTIVE_STATES
                            ):
                                raise ValueError(
                                    "active launch marker does not match the "
                                    "exact ephemeral HOLD"
                                )
                            verification_mode = current_item.get(
                                "attended_disposition_mode"
                            )
                            if verification_mode != "operator_attested":
                                raise ValueError(
                                    "this ephemeral HOLD has no attended archive "
                                    "command; read `agenttalk attention` again"
                                )
                            if args.verified_launch_nonce:
                                raise ValueError(
                                    "terminal ephemeral archives use the "
                                    "request-bound operator-attested command; "
                                    "omit --verified-launch-nonce"
                                )
                            verified_nonce = None
                            verified_identity_count = 0
                            marker_status, _marker, marker_detail = (
                                store._read_supervisor_instance_strict_locked()
                            )
                            if marker_status != "absent":
                                raise ValueError(
                                    "supervisor instance marker changed before "
                                    "archive staging: "
                                    f"{marker_status}: "
                                    f"{marker_detail or 'a supervisor may be live'}"
                                )
                            if store.supervisor_kill_switch() is not True:
                                raise ValueError(
                                    "supervisor.kill was removed before archive "
                                    "staging"
                                )
                            sup.stage_attended_ephemeral_archive(
                                state,
                                args.request_id,
                                agent=target_agent,
                                launch_marker=launch_marker,
                                held_terminal=held_terminal,
                                hold_source_hash=args.hold_source_hash,
                                acknowledged_by=actor,
                                verification_mode=verification_mode,
                                verified_launch_nonce=verified_nonce,
                                verified_identity_count=(
                                    verified_identity_count
                                ),
                                reason=attended_reason,
                                now_epoch=now,
                            )
                            # This durable journal must land before the launch
                            # marker or temporary identity can be changed.
                            sup.save_supervisor_state(state_path, state)

                if ephemeral_reset:
                    # The lifecycle lock fences the full transaction. The
                    # durable journal makes marker/archive/config effects
                    # idempotently recoverable after any later failure.
                    marker_status, _marker, marker_detail = (
                        store._read_supervisor_instance_strict_locked()
                    )
                    if marker_status != "absent":
                        raise ValueError(
                            "supervisor instance marker changed before archive: "
                            f"{marker_status}: "
                            f"{marker_detail or 'a supervisor may be live'}"
                        )
                    if store.supervisor_kill_switch() is not True:
                        raise ValueError(
                            "supervisor.kill was removed before request archive"
                        )
                    output_record = sup.finish_attended_ephemeral_archive(
                        store,
                        state,
                        args.request_id,
                    )
                    sup.save_supervisor_state(state_path, state)
        except (
            OSError,
            ValueError,
            sup.SupervisorPersistenceError,
            runtime_obs.RuntimeRecordError,
        ) as exc:
            sys.stderr.write(
                "agenttalk supervise --reset-process-tree-ownership: "
                f"{exc}\n"
            )
            return 3
        print(json.dumps(output_record, indent=2))
        return 0

    if args.prepare_launch_request:
        if (
            not args.request_id
            or not args.state_file
            or args.launch_agenttalk_python is None
            or args.launch_src_on_pythonpath is None
            or args.supervisor_config_sha256 is None
        ):
            sys.stderr.write("agenttalk supervise --prepare-launch-request: need "
                             "--request-id <rid>, --state-file <path>, "
                             "--launch-agenttalk-python <path>, and "
                             "--launch-src-on-pythonpath true|false, and a "
                             "PowerShell-accepted supervisor config SHA-256\n")
            return 2
        state_path = store.dir / "supervisor-state.json"
        try:
            selected_state_path = Path(args.state_file).resolve()
            official_state_path = state_path.resolve()
        except (OSError, RuntimeError) as exc:
            sys.stderr.write(
                "agenttalk supervise --prepare-launch-request: state path "
                f"could not be resolved: {exc}\n"
            )
            return 2
        if selected_state_path != official_state_path:
            sys.stderr.write(
                "agenttalk supervise --prepare-launch-request: --state-file "
                "must be the official .agenttalk/supervisor-state.json\n"
            )
            return 2
        try:
            config = _load_supervisor_config(
                store,
                expected_sha256=args.supervisor_config_sha256,
                require_powershell_transport=True,
            )
        except sup.SupervisorPersistenceError as exc:
            sys.stderr.write(
                "agenttalk supervise --prepare-launch-request: "
                f"{exc}\n"
            )
            return 3
        state = sup.load_supervisor_state(state_path)
        now = args.now if args.now is not None else time.time()
        try:
            spec = sup.prepare_launch_request(
                store,
                state,
                config,
                args.request_id,
                now_epoch=now,
                launch_agenttalk_python=args.launch_agenttalk_python,
                launch_src_on_pythonpath=(
                    args.launch_src_on_pythonpath == "true"
                ),
            )
        except eph.EphemeralError as e:
            sys.stderr.write(f"agenttalk supervise --prepare-launch-request: {e}\n")
            return 3
        sup.save_supervisor_state(state_path, state)
        print(json.dumps(spec, indent=2))
        return 0

    if args.record_ephemeral_launch:
        if not args.request_id or not args.state_file:
            sys.stderr.write("agenttalk supervise --record-ephemeral-launch: need "
                             "--request-id <rid> and --state-file <path>\n")
            return 2
        state = _read_state()
        root_key = sup._root_key(str(store.root.resolve()))
        sup.record_ephemeral_launch(
            state, args.request_id, pid=args.pid, pid_start=args.pid_start,
            now_epoch=(args.now if args.now is not None else time.time()),
            timeout_seconds=args.timeout_seconds,
            pre_snapshot=_read_snapshot_file(args.pre_snapshot_file),
            post_snapshot=_read_snapshot_file(args.post_snapshot_file),
            root_key=root_key,
            launcher_nonce=args.launcher_nonce,
            launcher_nonce_injected=bool(args.launcher_nonce_injected),
            launcher_nonce_source=args.launcher_nonce_source,
            launcher_nonce_missing_reason=args.launcher_nonce_missing_reason,
        )
        _write_state(state)
        return 0

    if args.archive_launch_request:
        if not args.request_id or not args.state_file or not args.terminal_state:
            sys.stderr.write("agenttalk supervise --archive-launch-request: need "
                             "--request-id <rid>, --terminal-state <state>, and "
                             "--state-file <path>\n")
            return 2
        state_path = store.dir / "supervisor-state.json"
        try:
            selected_state_path = Path(args.state_file).resolve()
            official_state_path = state_path.resolve()
        except (OSError, RuntimeError) as exc:
            sys.stderr.write(
                "agenttalk supervise --archive-launch-request: state path "
                f"could not be resolved: {exc}\n"
            )
            return 2
        if selected_state_path != official_state_path:
            sys.stderr.write(
                "agenttalk supervise --archive-launch-request: --state-file "
                "must be the official .agenttalk/supervisor-state.json\n"
            )
            return 2
        if not args.instance_token or args.pid is None:
            sys.stderr.write(
                "agenttalk supervise --archive-launch-request: "
                "supervisor_owner_identity_missing: need the live supervisor "
                "--instance-token and --pid identity\n"
            )
            return 3
        completion = None
        if args.completion_json:
            try:
                completion = json.loads(args.completion_json)
            except ValueError:
                sys.stderr.write("agenttalk supervise --archive-launch-request: "
                                 "--completion-json must be a JSON object\n")
                return 2
            if not isinstance(completion, dict):
                sys.stderr.write("agenttalk supervise --archive-launch-request: "
                                 "--completion-json must be a JSON object\n")
                return 2
        try:
            with store._supervisor_lifecycle_lock():
                marker_status, instance, marker_detail = (
                    store._read_supervisor_instance_strict_locked()
                )
                if marker_status != "valid" or not isinstance(instance, dict):
                    sys.stderr.write(
                        "agenttalk supervise --archive-launch-request: "
                        f"supervisor_owner_marker_{marker_status}: "
                        f"{marker_detail or 'no live supervisor identity is available'}\n"
                    )
                    return 3
                if (
                    instance.get("token") != args.instance_token
                    or instance.get("pid") != args.pid
                    or instance.get("pid_start") != args.pid_start
                ):
                    sys.stderr.write(
                        "agenttalk supervise --archive-launch-request: "
                        "supervisor_owner_identity_mismatch: supplied "
                        "token/pid/start did not match the supervisor marker\n"
                    )
                    return 3
                owner_status = store_mod._probe_owner_identity(
                    instance.get("pid"),
                    instance.get("pid_start"),
                )
                refusal_code = {
                    store_mod.OWNER_IDENTITY_DEAD: "supervisor_owner_dead",
                    store_mod.OWNER_IDENTITY_PID_REUSED: "supervisor_owner_pid_reused",
                    store_mod.OWNER_IDENTITY_UNKNOWN: "supervisor_owner_liveness_unknown",
                    store_mod.OWNER_IDENTITY_START_UNMATCHABLE:
                        "supervisor_owner_start_unmatchable",
                }.get(owner_status)
                if owner_status != store_mod.OWNER_IDENTITY_ALIVE:
                    sys.stderr.write(
                        "agenttalk supervise --archive-launch-request: "
                        f"{refusal_code or 'supervisor_owner_probe_invalid'}: "
                        "positive supervisor owner identity was not proven\n"
                    )
                    return 3
                marker_status_after, instance_after, marker_detail_after = (
                    store._read_supervisor_instance_strict_locked()
                )
                if marker_status_after != "valid" or instance_after != instance:
                    detail_suffix = (
                        f": {marker_detail_after}" if marker_detail_after else ""
                    )
                    sys.stderr.write(
                        "agenttalk supervise --archive-launch-request: "
                        "supervisor_owner_marker_changed: marker changed during "
                        f"owner verification{detail_suffix}\n"
                    )
                    return 3
                state = sup.load_supervisor_state(state_path)
                sup.archive_ephemeral_request(
                    store, state, args.request_id,
                    terminal_state=args.terminal_state,
                    reason=args.reason or "",
                    now_epoch=(args.now if args.now is not None else time.time()),
                    completion=completion,
                )
                sup.save_supervisor_state(state_path, state)
        except (OSError, ValueError, eph.EphemeralError,
                sup.SupervisorPersistenceError) as exc:
            sys.stderr.write(
                f"agenttalk supervise --archive-launch-request: {exc}\n"
            )
            return 3
        return 0

    if args.launch_barrier:
        if not args.agent or not args.state_file:
            sys.stderr.write("agenttalk supervise --launch-barrier: need --for "
                             "<agent> and --state-file <path>\n")
            return 2
        try:
            config = _load_supervisor_config(
                store,
                expected_sha256=args.supervisor_config_sha256,
            )
        except sup.SupervisorPersistenceError as exc:
            sys.stderr.write(f"agenttalk supervise --launch-barrier: {exc}\n")
            return 3
        state = _read_state()
        now = args.now if args.now is not None else time.time()
        result = sup.evaluate_launch_barrier(
            _read_snapshot_file(args.snapshot_file),
            state,
            config,
            args.agent,
            root_key=sup._root_key(str(store.root.resolve())),
            request_id=args.request_id,
        )
        if result.get("blocked") and getattr(args, "record_events", False):
            with contextlib.suppress(Exception):
                sup.record_supervisor_launch_barrier_event(
                    store, args.agent,
                    reason_code=str(result.get("reason") or "launch_barrier"),
                    now_epoch=now,
                )
        if getattr(args, "record_events", False):
            with contextlib.suppress(Exception):
                sup.record_supervisor_launch_barrier_observation(
                    store,
                    args.agent,
                    result,
                    now_epoch=now,
                )
        print(json.dumps(result, indent=2))
        return 0

    if args.janitor_ephemeral:
        if not args.agent:
            sys.stderr.write("agenttalk supervise --janitor-ephemeral: need --for <agent>\n")
            return 2
        ok = sup.janitor_retire_ephemeral_orphan(store, args.agent)
        print(f"janitor-ephemeral: retired {args.agent!r}" if ok
              else f"janitor-ephemeral: no active adversary orphan {args.agent!r}")
        return 0

    if args.record_launch:
        if not args.agent or not args.state_file:
            sys.stderr.write("agenttalk supervise --record-launch: need --for "
                             "<agent> and --state-file <path>\n")
            return 2
        state = _read_state()
        state_agents = state.get("agents")
        state_agent = (
            state_agents.get(args.agent)
            if isinstance(state_agents, dict)
            else None
        )
        try:
            grace, cfg_agent = sup.decode_launch_record_context(
                state_agent.get("pending_launch_record")
                if isinstance(state_agent, dict)
                else None,
                agent=args.agent,
                cli=args.cli or "claude",
            )
        except ValueError as exc:
            sys.stderr.write(f"agenttalk supervise --record-launch: {exc}\n")
            return 3
        sup.record_launch(state, args.agent, cli=args.cli or "claude",
                          pid=args.pid, pid_start=args.pid_start,
                          now_epoch=(args.now if args.now is not None else time.time()),
                          grace_seconds=grace, session_id=args.session_id,
                          pre_snapshot=_read_snapshot_file(args.pre_snapshot_file),
                          post_snapshot=_read_snapshot_file(args.post_snapshot_file),
                          cfg_agent=cfg_agent,
                          root_key=sup._root_key(str(store.root.resolve())),
                          launcher_nonce=args.launcher_nonce,
                          launcher_nonce_injected=bool(args.launcher_nonce_injected),
                          launcher_nonce_source=args.launcher_nonce_source,
                          launcher_nonce_missing_reason=args.launcher_nonce_missing_reason)
        state["agents"][args.agent].pop("pending_launch_record", None)
        _write_state(state)
        return 0
    if args.clear_restart:
        if not args.agent or not args.request_id:
            sys.stderr.write("agenttalk supervise --clear-restart: need --for "
                             "<agent> and --request-id <rid>\n")
            return 2
        cleared = store.clear_restart_request(args.agent, args.request_id)
        # ONLY a CONFIRMED restart (a marker actually matched + was cleared) supersedes
        # the lead-loop exit marker - so an operator re-arm is not defeated by a stale
        # stand-down/blocked marker if the relaunched child fails before acquire (the
        # .ps1 calls this right after a confirmed Start-Process). A clear-restart that
        # matched NOTHING (stale/typo rid) must NOT delete a deliberate stand-down
        # marker (codex: no re-arm without a confirmed restart).
        if cleared:
            store.clear_lead_loop_exit(args.agent)
        print(f"cleared restart-request for {args.agent!r}" if cleared
              else f"no matching restart-request for {args.agent!r} "
                   f"[{args.request_id}] (already cleared or superseded)")
        return 0
    try:
        config = _load_supervisor_config(
            store,
            expected_sha256=args.supervisor_config_sha256,
        )
    except sup.SupervisorPersistenceError as exc:
        if args.supervisor_config_sha256 is None:
            # Preserve the public usage-error contract for a corrupt project
            # config.  SupervisorPersistenceError is a ValueError, so the
            # top-level CLI reports this as exit 2 just as it did before the
            # optional PowerShell byte binding was added.  Only a supplied
            # binding that cannot be honored is a runtime HOLD (exit 3).
            raise
        sys.stderr.write(f"agenttalk supervise: {exc}\n")
        return 3
    now = args.now if args.now is not None else time.time()
    stuck = config.get("stuck_after_seconds")
    stuck = float(stuck) if isinstance(stuck, (int, float)) else None

    if args.report:
        print(json.dumps(sup.build_report(store, now_epoch=now,
                                          stuck_after_seconds=stuck,
                                          state=_read_state() or None,
                                          supervisor_config=config), indent=2))
        return 0
    if args.bootstrap_check:
        payload = sup.bootstrap_check(
            store, now_epoch=now, supervisor_config=config,
            state=_read_state() or None,
        )
        print(json.dumps(payload, indent=2))
        return 2 if payload.get("verdict") == "error" else 0
    if args.plan:
        if args.report_file:
            report = json.loads(Path(args.report_file).read_text(encoding="utf-8-sig"))
        else:
            report = sup.build_report(store, now_epoch=now, stuck_after_seconds=stuck,
                                      supervisor_config=config)
        # The executor's process snapshot (a JSON list of rows). A dict marker
        # {"unavailable": true}, a missing/unreadable file, or no --snapshot-file
        # => UNAVAILABLE (None): a brain-required CLI then fails closed. utf-8-sig
        # tolerates the PowerShell 5.1 Set-Content BOM.
        snapshot = None
        if args.snapshot_file and Path(args.snapshot_file).exists():
            snapshot = _read_snapshot_file(args.snapshot_file)
        plan = sup.plan_actions(report, _read_state(), config,
                                now_epoch=now, snapshot=snapshot)
        sup.attach_regular_launch_admissions(
            plan,
            config,
            root=store.root,
        )
        print(json.dumps(plan, indent=2))
        if getattr(args, "record_events", False):
            with contextlib.suppress(Exception):
                sup.record_coordination_availability_observation(
                    store,
                    report,
                    plan,
                    config,
                    now_epoch=now,
                )
            with contextlib.suppress(Exception):
                sup.record_supervisor_plan_events(store, plan, now_epoch=now)
        return 0
    sys.stderr.write("agenttalk supervise: choose --init, --report, --plan, "
                     "--bootstrap-check, --install-activity-hook, --launch-barrier, "
                     "or --clear-restart\n")
    return 2


def cmd_deadman(args: argparse.Namespace) -> int:
    store = _get_store(args)
    if args.threshold_seconds is not None and args.threshold_seconds <= 0:
        sys.stderr.write("agenttalk deadman: --threshold-seconds must be positive\n")
        return 2
    now = (
        datetime.fromtimestamp(args.now, timezone.utc)
        if getattr(args, "now", None) is not None else None
    )
    try:
        rc, report = deadman_mod.check(
            store,
            threshold_seconds=args.threshold_seconds,
            alarm_unread_response=args.alarm_unread_response,
            now=now,
        )
    except ValueError as e:
        sys.stderr.write(f"agenttalk deadman: {e}\n")
        return 2
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        status = report.get("status", "unknown")
        counts = report.get("counts") or {}
        print(
            "deadman: "
            f"{status} "
            f"stale_obligation={counts.get('stale_obligation', 0)} "
            f"stale_unread_response={counts.get('stale_unread_response', 0)} "
            f"stale_control={counts.get('stale_control', 0)}"
        )
        for err in report.get("errors") or []:
            print(f"  error: {err.get('class', 'unknown')}")
    return rc


def cmd_end(args: argparse.Namespace) -> int:
    store = _get_store(args)
    cfg = store.load_config()
    sender = _resolve_self(args.sender, roster=cfg.get("agents") or [])
    others = [a for a in cfg.get("agents", []) if a != sender]
    if not others:
        sys.stderr.write("agenttalk end: no other agents registered\n")
        return 2
    body = args.reason or "session ended"
    for other in others:
        store.send(
            sender=sender,
            recipient=other,
            body=body,
            kind="end",
        )
    out = tx.export(store, fmt="md")
    print(f"agenttalk: ended session, transcript at {out}")
    return 0


def _dev_gate_forward_argv(args: argparse.Namespace) -> list[str]:
    argv = ["--profile", args.profile]
    if args.ci_leg:
        argv.extend(["--ci-leg", args.ci_leg])
    if args.aggregate:
        argv.extend(["--aggregate", str(Path(args.aggregate).resolve())])
    if args.evidence:
        argv.extend(["--evidence", str(Path(args.evidence).resolve())])
    if args.temp_root:
        argv.extend(["--temp-root", str(Path(args.temp_root).resolve())])
    for mapping in args.python:
        argv.extend(["--python", mapping])
    return argv


def cmd_dev_gate(args: argparse.Namespace) -> int:
    """Run the committed, SHA-bound repository development gate."""

    from agenttalk import dev_gate as dev_gate_mod

    root = Path.cwd().resolve()
    try:
        if args.root:
            raise dev_gate_mod.GateBlock(
                "candidate_root_override_forbidden",
                "use the Git worktree CWD",
            )
        root = dev_gate_mod.discover_repo_root()
        forward_argv = _dev_gate_forward_argv(args)
        reentered = dev_gate_mod.reenter_candidate_source(root, forward_argv)
        if reentered is not None:
            return reentered
        if args.aggregate is not None:
            if args.python:
                raise dev_gate_mod.GateBlock(
                    "aggregate_argument_invalid", "--python is not valid with --aggregate"
                )
            result = dev_gate_mod.execute_aggregate(
                root=root,
                input_root=Path(args.aggregate),
                profile=args.profile,
                evidence_path=Path(args.evidence) if args.evidence else None,
                temp_base=Path(args.temp_root) if args.temp_root else None,
            )
        else:
            result = dev_gate_mod.execute_gate(
                root=root,
                profile=args.profile,
                ci_leg=args.ci_leg,
                evidence_path=Path(args.evidence) if args.evidence else None,
                temp_base=Path(args.temp_root) if args.temp_root else None,
                python_overrides=dev_gate_mod.parse_python_overrides(args.python),
            )
    except Exception as caught:
        exc = (
            caught
            if isinstance(caught, dev_gate_mod.GateBlock)
            else dev_gate_mod.GateBlock(
                "gate_internal_error",
                f"{type(caught).__name__}: {caught}",
            )
        )
        evidence_note = ""
        try:
            evidence_path, evidence_sha256, preflight_artifact = (
                dev_gate_mod.write_preflight_block_evidence(
                    root=root,
                    profile=args.profile,
                    ci_leg=args.ci_leg,
                    aggregate=Path(args.aggregate) if args.aggregate is not None else None,
                    evidence_path=Path(args.evidence) if args.evidence else None,
                    temp_base=Path(args.temp_root) if args.temp_root else None,
                    problem=exc,
                )
            )
        except (dev_gate_mod.GateBlock, OSError) as evidence_exc:
            evidence_note = f"; preflight evidence unavailable: {evidence_exc}"
        else:
            print(
                json.dumps(
                    {
                        "verdict": "block",
                        "complete": False,
                        "evidence": str(evidence_path),
                        "evidence_sha256": evidence_sha256,
                        "candidate_sha": preflight_artifact["subject"]["candidate_sha"],
                    },
                    sort_keys=True,
                )
            )
        sys.stderr.write(f"agenttalk dev-gate: BLOCK [{exc.code}] {exc.detail}\n")
        if evidence_note:
            sys.stderr.write(f"agenttalk dev-gate: BLOCK [evidence_write_failed]{evidence_note}\n")
        return 2
    print(
        json.dumps(
            {
                "verdict": result.artifact["verdict"],
                "complete": result.artifact["complete"],
                "evidence": str(result.evidence_path),
                "evidence_sha256": result.evidence_sha256,
                "candidate_sha": result.artifact["subject"]["candidate_sha"],
            },
            sort_keys=True,
        )
    )
    return result.exit_code


# --------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agenttalk",
        description="File-backed message bus for two agent CLIs.",
    )
    p.add_argument("--version", action="version", version=f"agenttalk {__version__}")
    launch_admission.add_agenttalk_launch_arguments(p)
    sub = p.add_subparsers(dest="cmd", required=True)

    pdev = sub.add_parser(
        "dev-gate",
        help="Run the committed hermetic source+wheel development gate and emit SHA-bound JSON evidence.",
    )
    pdev.add_argument("--profile", choices=["release"], default="release")
    dev_scope = pdev.add_mutually_exclusive_group()
    dev_scope.add_argument("--ci-leg", help="Emit incomplete evidence for one declared <os>/<python> CI leg.")
    dev_scope.add_argument(
        "--aggregate",
        type=Path,
        help="Aggregate a directory of exact CI-leg JSON artifacts into authoritative evidence.",
    )
    pdev.add_argument(
        "--evidence",
        type=Path,
        help="Output JSON path; must be outside the candidate worktree and any AgentTalk store.",
    )
    pdev.add_argument(
        "--temp-root",
        type=Path,
        help="External temp parent for isolated source, wheel, logs, and pytest basetemps.",
    )
    pdev.add_argument(
        "--python",
        action="append",
        default=[],
        metavar="MINOR=ABSOLUTE_EXE",
        help="Bind a required Python minor to a direct interpreter (repeatable).",
    )
    pdev.set_defaults(func=cmd_dev_gate)

    pi = sub.add_parser("init", help="Initialize a fresh .agenttalk/ store in the current dir.")
    pi.add_argument("--agents", default="claude,codex", help="Comma-separated agent names (default: claude,codex)")
    pi.add_argument("--path", help="Directory to init (default: CWD)")
    pi.add_argument("--here", action="store_true", help="(alias for --path .)")
    pi.add_argument("--force", action="store_true",
                    help="Overwrite existing config.json (roster, session_id). "
                         "Does NOT clear messages/cursors/heartbeats — use "
                         "`agenttalk reset` for a clean slate.")
    pi.set_defaults(func=cmd_init)

    ps = sub.add_parser("status", help="Show roster, message count, per-agent cursor + unread.")
    ps.add_argument("--json", action="store_true",
                    help="Emit structured JSON instead of human-readable text.")
    ps.set_defaults(func=cmd_status)

    psview = sub.add_parser(
        "supervisor",
        help="Read-only supervisor assessment: report, exact plan decision, and event ring.",
    )
    psview.add_argument("--json", action="store_true",
                        help="Emit structured JSON instead of human-readable text.")
    psview.add_argument("--state-file", dest="state_file",
                        help="Supervisor state JSON (default .agenttalk/supervisor-state.json).")
    psview.add_argument("--snapshot-file", dest="snapshot_file",
                        help="Optional process snapshot JSON for exact plan parity.")
    psview.add_argument("--events", type=int, default=10,
                        help="Number of recent supervisor events to show (default 10).")
    psview.add_argument("--now", type=_finite_float_arg, default=None,
                        help="Override 'now' (epoch seconds) for deterministic tests.")
    psview.set_defaults(func=cmd_supervisor)

    pth = sub.add_parser(
        "threads",
        help="Show open request/reply threads from an agent's perspective — "
             "the 'did the reviewer ever respond?' answer. Each thread gets "
             "one state: reply-waiting (unconsumed response in your inbox), "
             "owed-inbound (peer is waiting on you), open-outbound (you're "
             "waiting on the peer), or closed. Run it before declaring work "
             "done or going idle.",
    )
    pth.add_argument("--for", dest="agent",
                     help="Agent name (default: $AGENTTALK_SELF)")
    pth.add_argument("--all", action="store_true",
                     help="Include closed threads (default: actionable only).")
    pth.add_argument("--json", action="store_true",
                     help="Emit the structured contract for skills to parse.")
    pth.set_defaults(func=cmd_threads)

    proster = sub.add_parser(
        "roster",
        help="View or manage the agent roster, roles, and groups. With no "
             "subcommand, shows the team (roles + group memberships + who "
             "you are). `add`/`remove`/`set-role`/`set-group` are deliberate "
             "local admin ops.",
    )
    proster.add_argument("--json", action="store_true",
                         help="(show) machine-readable roster/roles/groups.")
    proster.add_argument("--expertise", action="store_true",
                         help="(show) per-domain expertise derived from domains.json "
                              "owners/reviewers/curators + lane-delivery history + "
                              "curated note authors (no expertise registry).")
    proster.set_defaults(func=cmd_roster, roster_cmd=None)
    rsub = proster.add_subparsers(dest="roster_cmd")
    r_add = rsub.add_parser("add", help="Add an agent (idempotent).")
    r_add.add_argument("name")
    r_add.add_argument("--role", help="Role label (e.g. implementer, reviewer, lead).")
    r_add.add_argument("--group", action="append",
                       help="Add the agent to this group (repeatable).")
    r_add.add_argument("--trust-class", choices=("external-worker",),
                       help="Set opt-in non-authority model trust metadata.")
    r_add.add_argument("--unique", action="store_true",
                       help="Self-join guard: REFUSE (exit 3) if <name> is an "
                            "ACTIVE identity (fresh heartbeat or a live waiter), "
                            "printing a free variant to adopt instead. Use this on "
                            "a FRESH self-join so two agents never share one name; "
                            "plain `add` stays idempotent for rejoin/re-init.")
    r_add.add_argument("--json", action="store_true",
                       help="(add --unique) machine-readable refusal "
                            '{"refused", "active_holder", "suggested"}.')
    r_add.set_defaults(func=cmd_roster)
    r_rm = rsub.add_parser(
        "remove",
        help="Remove an agent (refused by default — use `retire` to keep "
             "history valid; --force removes anyway and breaks historical "
             "readability for that agent's messages).")
    r_rm.add_argument("name")
    r_rm.add_argument("--force", action="store_true",
                      help="Remove despite history-read breakage (no tombstone; "
                           "the name stays re-addable).")
    r_rm.set_defaults(func=cmd_roster)
    r_ret = rsub.add_parser(
        "retire",
        help="Retire an agent to a PERMANENT tombstone (#19): it can no longer "
             "send, its name can never be re-bound, but its history stays valid. "
             "The safe alternative to `remove`.")
    r_ret.add_argument("name")
    r_ret.add_argument("--reason", help="Optional audit note.")
    r_ret.add_argument("--json", action="store_true",
                       help='Emit the updated registry slice {"retired": [...]}.')
    r_ret.set_defaults(func=cmd_roster)
    r_ren = rsub.add_parser(
        "rename",
        help="Safe rename = retire <old> (tombstone -> <new>) + add <new>, "
             "carrying over role/groups/operator-facing. History stays valid; "
             "<old> is non-rebindable.")
    r_ren.add_argument("old")
    r_ren.add_argument("new")
    r_ren.add_argument("--drain-check", action="store_true",
                       help="Refuse if any open thread is owed to/from <old>.")
    r_ren.add_argument("--reason", help="Optional audit note.")
    r_ren.set_defaults(func=cmd_roster)
    r_fwd = rsub.add_parser(
        "forward",
        help="Forward a specific owed request from a retired identity to a live "
             "agent (single hop; transcript-visible).")
    r_fwd.add_argument("name", help="The retired identity to forward from.")
    r_fwd.add_argument("--to", required=True, help="The live agent to forward to.")
    r_fwd.add_argument("--to-request", required=True,
                       help="The request_id owed to/from the retired identity.")
    r_fwd.add_argument("--from", dest="from_agent",
                       help="Sender of the forward note (active; defaults to the "
                            "operator-facing identity). Never the target.")
    r_fwd.add_argument("--reason", help="Optional audit note / body.")
    r_fwd.set_defaults(func=cmd_roster)
    r_sr = rsub.add_parser("set-role", help="Set an agent's role.")
    r_sr.add_argument("name")
    r_sr.add_argument("role")
    r_sr.set_defaults(func=cmd_roster)
    r_sg = rsub.add_parser("set-group", help="Define a group's membership.")
    r_sg.add_argument("group")
    r_sg.add_argument("members", help="Comma-separated agent names.")
    r_sg.set_defaults(func=cmd_roster)
    r_tc = rsub.add_parser(
        "set-trust-class",
        help="Set or clear opt-in non-authority model trust metadata.",
    )
    r_tc.add_argument("name")
    r_tc.add_argument("trust_class", nargs="?", choices=("external-worker",))
    r_tc.add_argument("--clear", action="store_true")
    r_tc.set_defaults(func=cmd_roster)
    r_of = rsub.add_parser(
        "set-operator-facing",
        help="Designate the ONE agent the human operator talks to directly "
             "(the liaison). Workers route operator questions to it via "
             "`agenttalk escalate`. Advisory routing metadata, not an "
             "authorization boundary. Single slot: setting it replaces the "
             "previous designation; --clear removes it.",
    )
    r_of.add_argument("name", nargs="?", help="Agent name (must be in the roster).")
    r_of.add_argument("--clear", action="store_true",
                      help="Remove the operator-facing designation.")
    r_of.set_defaults(func=cmd_roster)

    pav = sub.add_parser("avatar", help="List and set display-avatar preferences.")
    pav.set_defaults(func=cmd_avatar, avatar_cmd=None)
    avsub = pav.add_subparsers(dest="avatar_cmd", required=True)
    av_list = avsub.add_parser("list", help="List allowlisted avatar ids.")
    av_list.add_argument("--json", action="store_true")
    av_list.set_defaults(func=cmd_avatar)
    av_set = avsub.add_parser("set", help="Set your own avatar preference.")
    av_set.add_argument("avatar_id", help="Allowlisted avatar id, e.g. claude-dev.")
    av_set.add_argument("--from", dest="from_agent",
                        help="Your active roster identity (or $AGENTTALK_SELF).")
    av_set.set_defaults(func=cmd_avatar)
    av_clear = avsub.add_parser("clear", help="Clear your own avatar preference.")
    av_clear.add_argument("--from", dest="from_agent",
                          help="Your active roster identity (or $AGENTTALK_SELF).")
    av_clear.set_defaults(func=cmd_avatar)
    av_op = avsub.add_parser("set-operator", help="Set the operator avatar preference.")
    av_op.add_argument("avatar_id", help="Allowlisted avatar id.")
    av_op.set_defaults(func=cmd_avatar)
    av_op_clear = avsub.add_parser("clear-operator", help="Clear the operator avatar preference.")
    av_op_clear.set_defaults(func=cmd_avatar)

    pdom = sub.add_parser(
        "domain",
        help="View and validate the durable domain registry used by native "
             "lanes and knowledge. Phase 0 is read-only: no lane/knowledge "
             "state is created.",
    )
    pdom.add_argument("--json", action="store_true",
                      help="(default list) machine-readable domain registry summary.")
    pdom.set_defaults(func=cmd_domain, domain_cmd="list")
    dsub = pdom.add_subparsers(dest="domain_cmd")
    d_list = dsub.add_parser("list", help="List domains in .agenttalk/domains.json.")
    d_list.add_argument("--json", action="store_true",
                        default=argparse.SUPPRESS,
                        help="Emit structured JSON instead of human-readable text.")
    d_list.set_defaults(func=cmd_domain)
    d_show = dsub.add_parser("show", help="Show one domain and resolved refs.")
    d_show.add_argument("domain_id")
    d_show.add_argument("--json", action="store_true",
                        default=argparse.SUPPRESS,
                        help="Emit structured JSON instead of human-readable text.")
    d_show.set_defaults(func=cmd_domain)
    d_check = dsub.add_parser(
        "check-path",
        help="Classify repo-relative paths against domain owned_globs and shared_paths.",
    )
    d_check.add_argument("paths", nargs="+")
    d_check.add_argument("--json", action="store_true",
                         default=argparse.SUPPRESS,
                         help="Emit structured JSON instead of human-readable text.")
    d_case = d_check.add_mutually_exclusive_group()
    d_case.add_argument("--case-sensitive", dest="casefold_paths", action="store_false",
                        help="Match paths case-sensitively.")
    d_case.add_argument("--case-insensitive", dest="casefold_paths", action="store_true",
                        help="Case-fold paths before matching.")
    d_check.set_defaults(func=cmd_domain, casefold_paths=None)
    d_val = dsub.add_parser("validate", help="Validate .agenttalk/domains.json.")
    d_val.add_argument("--json", action="store_true",
                       default=argparse.SUPPRESS,
                       help="Emit structured JSON instead of human-readable text.")
    d_val.set_defaults(func=cmd_domain)

    pse = sub.add_parser("send", help="Send a message from one agent to another.")
    pse.add_argument("--from", dest="sender", help="Sender agent name (default: $AGENTTALK_SELF)")
    pse.add_argument("--to", dest="recipient",
                     help="Recipient agent name "
                          "(default: $AGENTTALK_PEER, or the single other agent in the roster)")
    pse.add_argument("--kind", default="message",
                     help="Message kind. Known: message, note, question, "
                          "review-request, review-result, proposal, "
                          "proposal-response, wake, end, composing. "
                          "Unknown kinds are rejected at write time. Prefer the "
                          "`agenttalk propose`/`composing` subcommands over "
                          "`send --kind proposal`/`composing`.")
    pse.add_argument("--subject", help="One-line summary")
    pse.add_argument("--meta", action="append", help="key=value (repeatable)")
    pse.add_argument("-m", "--message", help="Body text (else --file or stdin)")
    pse.add_argument("--file", help="Read body from this file path ('-' = stdin)")
    pse.add_argument("--allow-empty", action="store_true")
    pse.add_argument(
        "--await-reply",
        action="store_true",
        help="Record an explicit across-turn reply wait for this opener. "
             "Managed-wrapper turns only; consult/handoff use this and return "
             "to the wrapper instead of owning the inbox cursor.",
    )
    pse.add_argument("--print-id", action="store_true", help="Print the new message id on its own line")
    pse.add_argument("--quiet", action="store_true")
    pse.set_defaults(func=cmd_send)

    pac = sub.add_parser(
        "await-cancel",
        help="Cancel one wrapped --await-reply marker by its opaque token.",
    )
    pac.add_argument("--from", dest="sender",
                     help="Waiting agent name (default: $AGENTTALK_SELF)")
    pac.add_argument("--token", required=True,
                     help="Exact await_reply_token printed by send/reply")
    pac.add_argument("--quiet", action="store_true")
    pac.set_defaults(func=cmd_await_cancel)

    pcomp = sub.add_parser(
        "composing",
        help="Send a 'composing' ping to the peer to extend their `wait` "
             "deadline (the default `wait --timeout` is 120s; each fresh "
             "ping extends by --composing-extend, capped at 30 min total). "
             "Use periodically while drafting a long reply.",
    )
    pcomp.add_argument("--from", dest="sender",
                       help="Sender agent name (default: $AGENTTALK_SELF)")
    pcomp.add_argument("--to", dest="recipient",
                       help="Recipient agent name (default: $AGENTTALK_PEER, "
                            "or the single other agent in the roster)")
    pcomp.add_argument("--to-request", dest="to_request",
                       help="Bind the ping to one open thread: sets "
                            "meta.request_id (so the peer's scoped "
                            "`wait --to-request` extends) AND records the "
                            "reply-in-flight marker that `threads`/`sync` "
                            "show as '(reply in flight)'. Validates the id "
                            "is a live thread of yours.")
    pcomp.add_argument("--operation-nonce", dest="operation_nonce",
                       help=argparse.SUPPRESS)
    pcomp.add_argument("--subject",
                       help="One-line summary (default: 'composing')")
    pcomp.add_argument("--meta", action="append",
                       help="key=value (repeatable); prefer --to-request over "
                            "a hand-built request_id=<id>")
    pcomp.add_argument("-m", "--message",
                       help="Body text (default: 'still drafting — please hold "
                            "the line')")
    pcomp.add_argument("--quiet", action="store_true")
    pcomp.set_defaults(func=cmd_composing)

    presc = sub.add_parser(
        "rescind",
        help="Mark one of your own tracked requests as no-longer-current. "
             "Transcript-visible; the thread becomes closed-superseded, a "
             "peer blocked in `wait --to-request` wakes with exit 3, and "
             "`check --to-request` reports superseded. Requester-only. "
             "Prefer this over a prose 'ignore my last message' — prose "
             "moves no thread state.",
    )
    presc.add_argument("--from", dest="sender",
                       help="Sender agent name (default: $AGENTTALK_SELF)")
    presc.add_argument("--to-request", dest="to_request", required=True,
                       help="request_id of the thread to rescind (you must "
                            "be its requester).")
    presc.add_argument("--to-id", dest="to_id",
                       help="Pin a specific message id as the superseded "
                            "anchor (default: the thread opener).")
    presc.add_argument("--subject", help="One-line summary (default: 'rescind: <id>')")
    presc.add_argument("-m", "--message",
                       help="Optional reason (else --file or stdin; empty is allowed)")
    presc.add_argument("--file", help="Read the reason from this file path ('-' = stdin)")
    presc.add_argument("--quiet", action="store_true")
    presc.set_defaults(func=cmd_rescind)

    pchk = sub.add_parser(
        "check",
        help="Pre-action currentness gate: is this request still current? "
             "Prints current|superseded|unknown; exit 0/3/4. Run it "
             "immediately before any irreversible action tied to a request "
             "— exit 3 is a hard stop. Read-only; a local ack never masks "
             "a rescind.",
    )
    pchk.add_argument("--for", dest="agent",
                      help="Agent name (default: $AGENTTALK_SELF)")
    pchk.add_argument("--to-request", dest="to_request", required=True,
                      help="request_id to check.")
    pchk.add_argument("--epoch", action="store_true",
                      help="Also check the global epoch (#19): exit 3 if the "
                           "request predates the latest barrier (stale / "
                           "previous-epoch / pre-epoch). Run before irreversible "
                           "actions after an epoch boundary.")
    pchk.add_argument("--gates", action="store_true",
                      help="Also check assurance gates: exit 3 when gate state is HOLD.")
    pchk.add_argument("--json", action="store_true",
                      help='{"request_id", "state", "rescind"} — stable contract.')
    pchk.set_defaults(func=cmd_check)

    pgate = sub.add_parser(
        "gate",
        help="Manage lightweight assurance gates. Blocker gates feed check --gates.",
    )
    pgate.set_defaults(func=cmd_gate, gate_cmd=None)
    gatesub = pgate.add_subparsers(dest="gate_cmd")
    gset = gatesub.add_parser("set", help="Set or update one gate.")
    gset.add_argument("--from", dest="actor", help="Agent recording this gate update.")
    gset.add_argument("--name", required=True, help="Gate id, e.g. connected-l1.")
    gset.add_argument("--status", required=True, choices=sorted(gate_mod.VALID_STATUSES))
    gset.add_argument("--severity", default="blocker", choices=sorted(gate_mod.VALID_SEVERITIES))
    gset.add_argument("--scope", default="global", help="Gate scope (default: global).")
    gset.add_argument("--reason", help="Human-readable reason or summary.")
    gset.add_argument("--evidence", action="append", help="Evidence artifact path/id (repeatable).")
    gset.add_argument("--evidence-source", default="manual_review",
                      choices=sorted(gate_mod.VALID_EVIDENCE_SOURCES))
    gset.add_argument("--revision", help="Revision/head this evidence applies to.")
    req_group = gset.add_mutually_exclusive_group()
    req_group.add_argument("--required", action="store_true", help="Declare this gate required.")
    req_group.add_argument("--optional", action="store_true", help="Remove this gate from required gates.")
    gset.add_argument("--json", action="store_true", help="Emit the stored gate object.")
    gset.set_defaults(func=cmd_gate)
    glist = gatesub.add_parser("list", help="List known gates.")
    glist.add_argument("--json", action="store_true", help="Emit full gate state.")
    glist.set_defaults(func=cmd_gate)
    gcheck = gatesub.add_parser("check", help="Print GO or HOLD from current gate state.")
    gscope = gcheck.add_mutually_exclusive_group()
    gscope.add_argument("--release", action="store_true", help="Check release-scoped gates.")
    gscope.add_argument("--scope", help="Check this scope plus global gates.")
    gcheck.add_argument("--json", action="store_true", help="Emit structured verdict.")
    gcheck.set_defaults(func=cmd_gate)
    gwaive = gatesub.add_parser("waive", help="Record an operator waiver for one gate.")
    gwaive.add_argument("--from", dest="actor", help="Agent recording the waiver.")
    gwaive.add_argument("--name", required=True, help="Gate id to waive.")
    gwaive.add_argument("--operator", required=True, help="Operator accepting the risk.")
    gwaive.add_argument("--reason", required=True, help="Reason for the waiver.")
    gwaive.add_argument("--scope", required=True, help="Waiver scope.")
    gwaive.add_argument("--expires", required=True, help="Expiration date/time, e.g. 2026-07-01.")
    gwaive.add_argument("--date", help="Decision date (defaults to now).")
    gwaive.add_argument("--json", action="store_true", help="Emit the stored gate object.")
    gwaive.set_defaults(func=cmd_gate)

    # ----- close (assurance P2 milestone/release close; advisory, opt-in) -----
    pclose = sub.add_parser(
        "close",
        help="Aggregate gates + review lenses + remediation into one auditable "
             "HOLD/GO release verdict for a frozen revision (advisory).",
    )
    pclose.set_defaults(func=cmd_close, close_cmd=None)
    csub = pclose.add_subparsers(dest="close_cmd")

    copen = csub.add_parser("open", help="Open a close on a frozen revision.")
    copen.add_argument("--id", required=True, help="Close id (safe identifier).")
    copen.add_argument("--from", dest="actor", help="Agent opening the close.")
    copen.add_argument("--scope", required=True, help="Close scope, e.g. release.")
    copen.add_argument("--gate-scope", help="Gate scope to check (default: --scope).")
    copen.add_argument("--revision", required=True, help="Ref or SHA; frozen to a full SHA via git.")
    copen.add_argument("--lens", action="append", help="Required lens id (repeatable).")
    copen.add_argument("--optional-lens", action="append", help="Optional lens id (repeatable).")
    copen.add_argument("--allow", action="append",
                       help="Authorize a lens ack: LENS:AGENT or LENS:@ROLE (repeatable).")
    copen.add_argument("--dirty-artifact", help="Pointer to a recorded diff when the worktree is dirty.")
    copen.add_argument("--allow-dirty", action="store_true", help="Proceed on a dirty worktree (records dirty).")
    copen.add_argument("--lane-artifact",
                       help="Path to a lane delivery artifact proving worktree isolation.")
    copen.add_argument("--non-lane-isolation-not-asserted", action="store_true",
                       help="Declare this release-class close does not assert lane isolation.")
    copen.add_argument("--force", action="store_true", help="Overwrite an existing close with this id.")
    copen.add_argument("--derive-signoffs", action="store_true",
                       help="P3: derive required signoffs from the risk inventory + signoffs.json.")
    copen.add_argument("--risk-class", action="append",
                       help="P3: a risk class in play (repeatable; needs --derive-signoffs).")
    copen.add_argument("--risk-na", action="append",
                       help="P3: dispositioned-N/A risk CLASS=REASON (repeatable).")
    copen.add_argument("--changed-path", action="append",
                       help="P3: changed path (repeatable; default = git diff base..revision).")
    copen.add_argument("--base", help="P3: diff base for changed paths (default revision^).")
    copen.add_argument("--json", action="store_true", help="Emit the opened record.")
    copen.set_defaults(func=cmd_close)

    csign = csub.add_parser("signoffs", help="P3: derive/inspect specialist sign-offs.")
    csignsub = csign.add_subparsers(dest="signoffs_cmd")
    csplan = csignsub.add_parser("plan", help="READ-ONLY: preview derived signoffs + candidates.")
    csplan.add_argument("--id", required=True)
    csplan.add_argument("--risk-class", action="append", help="Risk class in play (repeatable).")
    csplan.add_argument("--risk-na", action="append", help="Dispositioned-N/A risk CLASS=REASON.")
    csplan.add_argument("--changed-path", action="append", help="Changed path (default git diff).")
    csplan.add_argument("--base", help="Diff base (default revision^).")
    csplan.add_argument("--json", action="store_true")
    csplan.set_defaults(func=cmd_close)
    csapply = csignsub.add_parser("apply", help="The ONLY mutating derivation: freeze the route.")
    csapply.add_argument("--id", required=True)
    csapply.add_argument("--from", dest="actor", help="Close lead applying the derivation.")
    csapply.add_argument("--risk-class", action="append", help="Risk class in play (repeatable).")
    csapply.add_argument("--risk-na", action="append", help="Dispositioned-N/A risk CLASS=REASON.")
    csapply.add_argument("--changed-path", action="append", help="Changed path (default git diff).")
    csapply.add_argument("--base", help="Diff base (default revision^).")
    csapply.set_defaults(func=cmd_close)
    csov = csignsub.add_parser("override", help="Lead escape for an unroutable/blocked signoff.")
    csov.add_argument("--id", required=True)
    csov.add_argument("--set", required=True, help="Required signoff set id to override.")
    csov.add_argument("--from", dest="actor", help="Close lead recording the override.")
    csov.add_argument("--reason", required=True, help="Why the signoff is being overridden.")
    csov.set_defaults(func=cmd_close)
    csign.set_defaults(func=cmd_close, signoffs_cmd=None)

    cack = csub.add_parser("ack", help="Record a lens ack (accept / counter / na).")
    cack.add_argument("--id", required=True)
    cack.add_argument("--lens", required=True, help="Lens id being acked.")
    cack.add_argument("--status", required=True, choices=["accept", "counter", "na"])
    cack.add_argument("--from", dest="actor", help="Acking agent.")
    cack.add_argument("--reason", help="Reason (required for na).")
    cack.add_argument("--override", action="store_true",
                      help="Record a lead/operator authorization override.")
    cack.add_argument("--counter", help="Counter id (for status=counter; auto if omitted).")
    cack.add_argument("--finding", help="Counter finding summary or pointer.")
    cack.add_argument("--request-id", help="Pointer to the review-result/request msg id.")
    cack.add_argument("--risk-class", help="ACCEPT: typed risk_class (reuses 0.32.0 evidence).")
    cack.add_argument("--release-blocker", help="ACCEPT: yes|no|unknown.")
    cack.add_argument("--tests-referenced", help="ACCEPT: tests referenced.")
    cack.add_argument("--tests-executed", help="ACCEPT: tests executed.")
    cack.add_argument("--residual-risk", help="ACCEPT: residual risk note.")
    cack.add_argument("--na-reason", help="ACCEPT: reason a field is n/a (per typed-evidence rules).")
    cack.add_argument("--evidence", action="append", help="ACCEPT: evidence artifact (repeatable).")
    cack.set_defaults(func=cmd_close)

    cdraft = csub.add_parser("draft", help="Record/replace the merged human draft (lead).")
    cdraft.add_argument("--id", required=True)
    cdraft.add_argument("--from", dest="actor", help="Lead recording the draft.")
    cdraft.add_argument("-m", "--message", help="Draft body.")
    cdraft.set_defaults(func=cmd_close)

    cctr = csub.add_parser("counter", help="Decide a raised counter (lead).")
    cctrsub = cctr.add_subparsers(dest="counter_cmd")
    cdec = cctrsub.add_parser("decide", help="Accept or reject a counter.")
    cdec.add_argument("--id", required=True)
    cdec.add_argument("--counter", required=True, help="Counter id to decide.")
    cdec.add_argument("--decision", required=True, choices=["accept", "reject"])
    cdec.add_argument("--from", dest="actor", help="Deciding lead.")
    cdec.add_argument("--reason", required=True, help="Reason for the decision.")
    cdec.add_argument("--rem-id", help="ACCEPT: remediation item id (auto if omitted).")
    cdec.add_argument("--rem-owner", help="ACCEPT: remediation owner.")
    cdec.add_argument("--rem-severity", default="unknown", help="ACCEPT: severity.")
    cdec.add_argument("--rem-fix", help="ACCEPT: the fix.")
    cdec.add_argument("--rem-verification", help="ACCEPT: how it is verified.")
    cdec.add_argument("--blocker", action="store_true", help="ACCEPT: this is a release blocker.")
    cdec.add_argument("--gate", help="ACCEPT: gate id that resolves a blocker remediation.")
    cdec.add_argument("--affected", action="append", help="ACCEPT: affected items (repeatable).")
    cdec.add_argument("--regression-test", help="ACCEPT: regression test reference.")
    cdec.add_argument("--target", help="ACCEPT: target milestone/close.")
    cdec.set_defaults(func=cmd_close)
    cctr.set_defaults(func=cmd_close, counter_cmd=None)

    ccheck = csub.add_parser("check", help="Print HOLD/GO + hold codes (exit 0=GO, 3=HOLD).")
    ccheck.add_argument("--id", required=True)
    ccheck.add_argument("--json", action="store_true", help="Emit the structured verdict.")
    ccheck.set_defaults(func=cmd_close)

    cpub = csub.add_parser("publish", help="Publish the final HOLD/GO (lead).")
    cpub.add_argument("--id", required=True)
    cpub.add_argument("--from", dest="actor", help="Publishing lead.")
    cpub.add_argument("--verdict", required=True, choices=["hold", "go"])
    cpub.add_argument("--reason", help="Publish reason / release note.")
    cpub.add_argument("--residual-risk", help="Recorded residual risk.")
    cpub.add_argument("--bump-barrier", action="store_true",
                      help="GO only: fire the release barrier AFTER recording GO.")
    cpub.set_defaults(func=cmd_close)

    creopen = csub.add_parser("reopen", help="Reopen a published close (lead/operator).")
    creopen.add_argument("--id", required=True)
    creopen.add_argument("--from", dest="actor", help="Agent reopening.")
    creopen.add_argument("--revision", help="New ref/SHA (changing it stales prior lens acks).")
    creopen.set_defaults(func=cmd_close)

    clist = csub.add_parser("list", help="List closes.")
    clist.add_argument("--json", action="store_true")
    clist.set_defaults(func=cmd_close)

    cshow = csub.add_parser("show", help="Show one close record (JSON).")
    cshow.add_argument("--id", required=True)
    cshow.set_defaults(func=cmd_close)

    # ----- lane (deliver-gate + default-on isolated worktree provisioning) -----
    plane = sub.add_parser(
        "lane",
        help="Scoped work lanes with a deliver-gate: segment-aware path bounds vs the "
             "domain registry + active-lane overlap + merge-tree + gates -> HOLD/GO.",
    )
    plane.set_defaults(func=cmd_lane, lane_cmd=None)
    lsub = plane.add_subparsers(dest="lane_cmd")

    lassign = lsub.add_parser(
        "assign", help="Assign a lane and provision an isolated worktree by default.")
    lassign.add_argument("--id", required=True, help="Lane id (safe identifier).")
    lassign.add_argument("--from", dest="actor", help="Agent assigning the lane.")
    lassign.add_argument("--assignee", required=True, help="Agent who works the lane.")
    lassign.add_argument("--domain", required=True, help="Domain id (must exist in domains.json).")
    lassign.add_argument("--base", required=True, help="Base ref/SHA (frozen to a full SHA).")
    lassign.add_argument("--target", required=True, help="Target ref to deliver toward (e.g. main).")
    lassign.add_argument("--path", action="append",
                         help="Repo-relative path PREFIX in scope (repeatable; omit = whole domain).")
    lassign.add_argument("--notes", help="Free-text notes.")
    lassign.add_argument("--force", action="store_true", help="Overwrite an existing lane id.")
    lassign.add_argument("--worktrees-root",
                         help="Managed worktree root (default: <repo>/.worktrees).")
    lassign.add_argument(
        "--advisory", action="store_true",
        help="Non-release coordination lane; may use --no-worktree but can never "
             "satisfy release isolation.",
    )
    lassign.add_argument("--no-worktree", action="store_true",
                         help="Assign an --advisory lane without isolation (requires reason).")
    lassign.add_argument("--worktree-waiver-reason",
                         help="Required human-readable reason when --no-worktree is used.")
    lassign.set_defaults(func=cmd_lane)

    lcheck = lsub.add_parser("check", help="READ-ONLY deliver-gate verdict (exit 0=GO/3=HOLD).")
    lcheck.add_argument("--id", required=True)
    lcheck.add_argument("--head", help="Head ref/SHA to evaluate (default: HEAD).")
    lcheck.add_argument("--gate-scope", help="Gate scope to check (default: lane:<id>).")
    lcheck.add_argument("--json", action="store_true")
    lcheck.set_defaults(func=cmd_lane)

    ldeliver = lsub.add_parser("deliver", help="Gate + (on GO) write evidence and clear the lane.")
    ldeliver.add_argument("--id", required=True)
    ldeliver.add_argument("--from", dest="actor", help="Delivering agent.")
    ldeliver.add_argument("--head", help="Head ref/SHA to deliver (default: HEAD).")
    ldeliver.add_argument("--gate-scope", help="Gate scope (default: lane:<id>).")
    ldeliver.set_defaults(func=cmd_lane)

    lrecover = lsub.add_parser(
        "recover",
        help="Restore an incomplete/corrupt delivered transaction to ACTIVE safely.",
    )
    lrecover.add_argument("--id", required=True)
    lrecover.add_argument(
        "--reason", required=True,
        help="Audit reason for discarding the incomplete publication attempt.",
    )
    lrecover.set_defaults(func=cmd_lane)

    lworkspace = lsub.add_parser("workspace", help="Show the provisioned workspace for a lane.")
    lworkspace.add_argument("--id", required=True)
    lworkspace.add_argument("--json", action="store_true")
    lworkspace.set_defaults(func=cmd_lane)

    labandon = lsub.add_parser("abandon", help="Mark a lane abandoned and clean up its worktree.")
    labandon.add_argument("--id", required=True)
    labandon.add_argument("--target", default="HEAD",
                          help="Target ref used to prove branch ancestry before deletion.")
    labandon.add_argument("--delete-branch", action="store_true",
                          help="Delete lane branch only when merge-base --is-ancestor proves it safe.")
    labandon.set_defaults(func=cmd_lane)

    lgc = lsub.add_parser("gc", help="Discover/clean managed lane worktree leftovers.")
    lgc.add_argument("--target", default="HEAD",
                     help="Target ref used to prove branch ancestry before deletion.")
    lgc.add_argument("--delete", action="store_true",
                     help="Perform proven cleanups; default is dry-run only.")
    lgc.add_argument("--json", action="store_true")
    lgc.set_defaults(func=cmd_lane)

    lstatus = lsub.add_parser("status", help="List active lanes with staleness indicators.")
    lstatus.add_argument("--json", action="store_true")
    lstatus.set_defaults(func=cmd_lane)

    lappr = lsub.add_parser("approve-shared", help="Record a shared-path approval (lead/approver).")
    lappr.add_argument("--id", required=True)
    lappr.add_argument("--path", required=True, help="Shared path/glob being approved.")
    lappr.add_argument("--from", dest="actor", help="Approving agent.")
    lappr.add_argument("--reason", required=True, help="Why the shared path is approved.")
    lappr.set_defaults(func=cmd_lane)

    # ----- relay (mechanical liaison relay, lead-loop Slice 2 WP4) -----
    prelay = sub.add_parser(
        "relay",
        help="Mechanical liaison relay: carry the operator's words across the "
             "human<->bus boundary with an audit stamp (operator-answer / "
             "operator-command). A thin typed wrapper over reply/send - no new kind.",
    )
    prelay.set_defaults(func=cmd_relay, relay_cmd=None)
    relaysub = prelay.add_subparsers(dest="relay_cmd")

    roa = relaysub.add_parser(
        "operator-answer",
        help="Relay the operator's answer to a PENDING needs_operator escalation back "
             "to the asking lead-loop (validated reply on the thread).")
    roa.add_argument("--from", dest="sender", help="Relaying liaison (default: resolved self).")
    roa.add_argument("--to-request", dest="to_request", required=True,
                     help="The pending escalation request_id to answer.")
    roa.add_argument("--subject")
    roa.add_argument("-m", "--message", dest="message", help="The operator's answer text.")
    roa.add_argument("--file", help="Read the answer body from a file (`-` = stdin).")
    roa.add_argument("--meta", action="append", help="Extra meta key=value (repeatable).")
    roa.add_argument("--quiet", action="store_true")
    roa.set_defaults(func=cmd_relay)

    roc = relaysub.add_parser(
        "operator-command",
        help="Relay a SPONTANEOUS operator instruction to a managed lead-loop "
             "(fail-closed to the operator-facing liaison).")
    roc.add_argument("--from", dest="sender", help="Relaying liaison (default: resolved self).")
    roc.add_argument("--to", dest="to",
                     help="Target agent: an EXPLICIT --to may be ANY roster agent; --to is "
                          "INFERRED only when exactly one MANAGED lead-loop exists, else required.")
    roc.add_argument("--kind", choices=["question", "message"], default=None,
                     help="Message kind (default question; a question mints a request_id).")
    roc.add_argument("--subject")
    roc.add_argument("-m", "--message", dest="message", help="The operator's command text.")
    roc.add_argument("--file", help="Read the command body from a file (`-` = stdin).")
    roc.add_argument("--meta", action="append", help="Extra meta key=value (repeatable).")
    roc.add_argument("--override", action="store_true",
                     help="Audited exception: relay even if you are not the configured "
                          "liaison (requires --reason).")
    roc.add_argument("--reason", help="Reason for --override (audited).")
    roc.add_argument("--quiet", action="store_true")
    roc.set_defaults(func=cmd_relay)

    # ----- knowledge (mixed pointer notes and lessons; capture-open, curate-gated) -----
    pkn = sub.add_parser(
        "knowledge",
        help="Durable pointer notes and advisory process lessons: publish/curate, "
             "then pull/search/onboard a scoped mixed view.",
    )
    pkn.set_defaults(func=cmd_knowledge, knowledge_cmd=None)
    knsub = pkn.add_subparsers(dest="knowledge_cmd")
    lesson_scope_choices = sorted([
        "process", "craft", "review", "test", "release", "ops", "docs", "security",
    ])

    def _add_anchor_args(p):
        p.add_argument("--anchor-kind", choices=sorted(["path", "symbol", "request", "wp", "sha"]))
        p.add_argument("--path", help="anchor path (path/symbol/wp).")
        p.add_argument("--symbol", help="anchor symbol (symbol).")
        p.add_argument("--request-id", dest="request_id", help="anchor request id (request).")
        p.add_argument("--msg-id", dest="msg_id", help="anchor message id (request, optional).")
        p.add_argument("--mission", help="anchor mission (wp).")
        p.add_argument("--wp-id", dest="wp_id", help="anchor wp id (wp).")
        p.add_argument("--sha", help="anchor sha (sha).")

    knpub = knsub.add_parser(
        "publish",
        help="Capture an uncurated pointer note or advisory lesson; lessons default "
             "to the virtual process domain.")
    knpub.add_argument("--from", dest="actor", help="Publishing agent.")
    knpub.add_argument("--domain", help="Domain id (lessons default to process).")
    knpub.add_argument("--type", required=True,
                       choices=sorted(["seam", "gotcha", "decision", "pointer", "lesson"]))
    knpub.add_argument("--key", required=True, help="Stable note key (latest-by-key).")
    knpub.add_argument("-m", "--message", required=True, help="The insight (not a copy of the anchor).")
    knpub.add_argument("--verified-against", dest="verified_against",
                       help="Ref/SHA the insight was verified against (default: HEAD).")
    knpub.add_argument("--scope", choices=lesson_scope_choices,
                       help="Lesson scope (required for --type lesson).")
    knpub.add_argument("--trigger", help="Lesson trigger text (required for --type lesson).")
    knpub.add_argument("--evidence-ref", dest="evidence_ref",
                       help="Lesson evidence reference (required for --type lesson).")
    knpub.add_argument("--applies-to", dest="applies_to",
                       help="Comma-separated safe-slug lesson tags.")
    knpub.add_argument("--owner", help="Lesson owner agent (default: publisher).")
    knpub.add_argument("--review-after", dest="review_after",
                       help="Lesson review-after ISO date/datetime.")
    knpub.add_argument("--expires-at", dest="expires_at",
                       help="Lesson expiry ISO date/datetime.")
    knpub.add_argument("--supersedes", help="Comma-separated lesson keys superseded by this lesson.")
    _add_anchor_args(knpub)
    knpub.set_defaults(func=cmd_knowledge)

    kncur = knsub.add_parser("curate", help="Verify or retract a note (owner/curator/lead).")
    kncursub = kncur.add_subparsers(dest="curate_cmd")
    for sub_name, helptext in (("verify", "Bless a note as verified."),
                               ("retract", "Tombstone a note (needs --reason).")):
        c = kncursub.add_parser(sub_name, help=helptext)
        c.add_argument("--from", dest="actor", help="Curating agent.")
        c.add_argument("--domain", required=True)
        c.add_argument("--key", required=True)
        c.add_argument("--reason", help="Reason (required for retract).")
        c.set_defaults(func=cmd_knowledge)
    kncur.set_defaults(func=cmd_knowledge, curate_cmd=None)

    knpull = knsub.add_parser(
        "pull", help="List active notes and lessons (default: curated, non-stale).")
    knpull.add_argument("--domain", help="Limit to a domain.")
    knpull.add_argument("--type", choices=sorted(["seam", "gotcha", "decision", "pointer", "lesson"]))
    knpull.add_argument("--scope", choices=lesson_scope_choices,
                        help="Limit lesson pull to a scope.")
    knpull.add_argument("--tags", help="Comma-separated lesson applies_to tags.")
    knpull.add_argument("--limit", type=int, default=5, help="Limit lesson pull rows (default 5).")
    knpull.add_argument("--include-stale", dest="include_stale", action="store_true")
    knpull.add_argument("--include-uncurated", dest="include_uncurated", action="store_true")
    knpull.add_argument("--json", action="store_true")
    knpull.add_argument(
        "--output-schema", choices=["legacy"],
        help="With --json, emit the legacy pointer-only array.")
    knpull.set_defaults(func=cmd_knowledge)

    knsearch = knsub.add_parser(
        "search", help="Substring search over pointer and lesson fields.")
    knsearch.add_argument("query", help="Search string.")
    knsearch.add_argument("--domain")
    knsearch.add_argument("--type", choices=sorted(["seam", "gotcha", "decision", "pointer", "lesson"]))
    knsearch.add_argument("--scope", choices=lesson_scope_choices,
                          help="Limit lesson search to a scope.")
    knsearch.add_argument("--tags", help="Comma-separated lesson applies_to tags.")
    knsearch.add_argument("--limit", type=int, help="Limit lesson search rows.")
    knsearch.add_argument("--include-stale", dest="include_stale", action="store_true")
    knsearch.add_argument("--include-uncurated", dest="include_uncurated", action="store_true")
    knsearch.add_argument("--json", action="store_true")
    knsearch.add_argument(
        "--output-schema", choices=["legacy"],
        help="With --json, emit the legacy pointer-only array.")
    knsearch.set_defaults(func=cmd_knowledge)

    knonb = knsub.add_parser(
        "onboard", help="Bounded mixed digest (20 pointer notes plus 5 lessons by default).")
    knonb.add_argument("--domain")
    knonb.add_argument("--type",
                       choices=sorted(["seam", "gotcha", "decision", "pointer", "lesson"]))
    knonb.add_argument("--scope", choices=lesson_scope_choices,
                       help="Limit lessons to a scope (implies lesson-only).")
    knonb.add_argument("--tags", help="Comma-separated lesson applies_to tags.")
    knonb.add_argument("--for", dest="for_agent", help="Agent the digest is for (label only).")
    knonb.add_argument("--include-uncurated", dest="include_uncurated", action="store_true")
    knonb.add_argument("--include-stale", dest="include_stale", action="store_true")
    knonb.add_argument("--limit", type=int, default=20,
                       help="Max notes in the digest (default 20; deterministic order, "
                            "grouped by domain then type). Truncated beyond this.")
    knonb.add_argument("--include-lessons", dest="include_lessons", action="store_true",
                       help="Deprecated no-op; lessons are included by default.")
    knonb.add_argument("--exclude-lessons", dest="exclude_lessons", action="store_true",
                       help="Exclude lessons from the onboarding digest.")
    knonb.add_argument("--lesson-limit", dest="lesson_limit", type=int, default=5,
                       help="Max active lessons in the digest (default 5).")
    knonb.add_argument("--json", action="store_true")
    knonb.add_argument(
        "--output-schema", choices=["legacy"],
        help="With --json, emit the legacy pointer-only array.")
    knonb.set_defaults(func=cmd_knowledge)

    # ----- onboarding (project/codebase analysis ledger) -----
    ponb = sub.add_parser(
        "onboarding",
        help="Track new-project or existing-codebase analysis: segments, claims, drift, and unknowns.",
    )
    ponb.set_defaults(func=cmd_onboarding, onboarding_cmd=None)
    onbsub = ponb.add_subparsers(dest="onboarding_cmd")

    onb_create = onbsub.add_parser("create", help="Start an onboarding run.")
    onb_create.add_argument("--id", dest="run_id", help="Run id (default: generated ob-*).")
    onb_create.add_argument("--from", dest="actor", help="Lead/agent creating the run.")
    onb_create.add_argument("--title", required=True, help="Short run title.")
    onb_create.add_argument("--objective", help="What this onboarding pass must answer.")
    onb_create.add_argument("--base-ref", dest="base_ref", help="Repo ref/SHA this pass describes.")
    onb_create.add_argument("--state", choices=sorted(ob.RUN_STATES), default="scanning")
    onb_create.add_argument("--json", action="store_true")
    onb_create.set_defaults(func=cmd_onboarding)

    onb_list = onbsub.add_parser("list", help="List onboarding runs.")
    onb_list.add_argument("--limit", type=int, default=20)
    onb_list.add_argument("--json", action="store_true")
    onb_list.set_defaults(func=cmd_onboarding)

    onb_show = onbsub.add_parser("show", help="Show one onboarding run.")
    onb_show.add_argument("--id", dest="run_id", required=True)
    onb_show.add_argument("--json", action="store_true")
    onb_show.set_defaults(func=cmd_onboarding)

    onb_state = onbsub.add_parser("state", help="Update onboarding run lifecycle state.")
    onb_state.add_argument("--id", dest="run_id", required=True)
    onb_state.add_argument("--from", dest="actor", help="Agent recording the state.")
    onb_state.add_argument("--state", required=True, choices=sorted(ob.RUN_STATES))
    onb_state.add_argument("--summary", help="Short reason/evidence for the transition.")
    onb_state.add_argument("--json", action="store_true")
    onb_state.set_defaults(func=cmd_onboarding)

    onb_record = onbsub.add_parser(
        "record",
        help="Record or replace a segment/claim/drift/unknown row in an onboarding run.",
    )
    onb_record.add_argument("--id", dest="run_id", required=True)
    onb_record.add_argument("--from", dest="actor", help="Agent recording this row.")
    onb_record.add_argument("--kind", required=True, choices=sorted(ob.ITEM_KINDS))
    onb_record.add_argument("--key", required=True, help="Stable row key; latest event wins.")
    onb_record.add_argument("--status", required=True,
                            help="Status for the kind (validated by kind).")
    onb_record.add_argument("--summary", required=True,
                            help="Bounded finding summary; point to evidence with --ref/--path.")
    onb_record.add_argument("--segment", help="Segment key this row belongs to.")
    onb_record.add_argument("--owner", help="Agent owning the segment or finding.")
    onb_record.add_argument("--checker", action="append", help="Agent who checked it (repeatable).")
    onb_record.add_argument("--ref", action="append", help="Evidence pointer/id (repeatable).")
    onb_record.add_argument("--path", action="append", help="Repo-relative evidence path (repeatable).")
    onb_record.add_argument("--source", choices=sorted(ob.CLAIM_SOURCES),
                            help="Where a claim/finding came from.")
    onb_record.add_argument("--confidence", choices=sorted(ob.CONFIDENCE_LEVELS))
    onb_record.add_argument("--blocking", action="store_true",
                            help="Mark an open unknown as blocking further work.")
    onb_record.add_argument("--json", action="store_true")
    onb_record.set_defaults(func=cmd_onboarding)

    pbar = sub.add_parser(
        "barrier",
        help="Fire a global epoch barrier (#19): marks everything before it as "
             "a previous epoch. `barrier bump --from <agent> --scope global "
             "-m <reason>`. Any active member may bump (trusted-team only).",
    )
    barsub = pbar.add_subparsers(dest="barrier_cmd")
    pbar_bump = barsub.add_parser("bump", help="Bump the global epoch.")
    pbar_bump.add_argument("--from", dest="from_agent",
                           help="Bumping agent (active; default $AGENTTALK_SELF).")
    pbar_bump.add_argument("--scope", default="global",
                           help="Only 'global' in this release (reserved).")
    pbar_bump.add_argument("-m", "--message", help="Audit reason (body).")
    pbar_bump.add_argument("--json", action="store_true",
                           help='{"epoch", "scope"}.')
    pbar_bump.set_defaults(func=cmd_barrier)
    pbar.set_defaults(func=cmd_barrier, barrier_cmd=None)

    pprn = sub.add_parser(
        "prune",
        help="Quarantine invalid message files: move everything the "
             "INVALID report names into .agenttalk/quarantine/ "
             "(recoverable - restore by moving the file back; never "
             "overwritten, never deleted). --dry-run lists without "
             "moving. Valid files are untouched by construction.",
    )
    pprn.add_argument("--invalid", action="store_true",
                      help="Select validation-failing files (required - "
                           "the only selector today).")
    pprn.add_argument("--dry-run", dest="dry_run", action="store_true",
                      help="List what would move; move nothing.")
    pprn.add_argument("--json", action="store_true",
                      help='{"selected", "moved", "dry_run"} - stable contract.')
    pprn.add_argument("--quiet", action="store_true",
                      help="Summary line only.")
    pprn.set_defaults(func=cmd_prune)

    pesc = sub.add_parser(
        "escalate",
        description="Route an operator-input question to a human-facing agent. "
                    "Target resolution: --to override -> operator-facing liaison "
                    "-> the single role=lead agent (fallback) -> refuse (exit 2) "
                    "with a remediation naming `roster set-operator-facing` and "
                    "`roster set-role <agent> lead`.",
        help="Route an operator-input question to the operator-facing agent "
             "(the liaison). Resolution: --to override -> liaison -> the single "
             "role=lead agent (fallback) -> refuse. Mints an esc- request_id "
             "(printed as `request_id=<id>` for the follow-up `wait "
             "--to-request`). Refuses (exit 2) only when none of those resolve.",
    )
    pesc.add_argument("--from", dest="sender",
                      help="Sender agent name (default: $AGENTTALK_SELF)")
    pesc.add_argument("--to",
                      help="Explicit target override (default resolution: the "
                           "operator-facing liaison, else the single role=lead "
                           "agent as a fallback).")
    pesc.add_argument("--subject",
                      help="One-line summary (default: 'operator input needed')")
    pesc.add_argument("--meta", action="append", help="key=value (repeatable)")
    pesc.add_argument("--origin-request",
                      help="Original enforced request id (requires --origin-id).")
    pesc.add_argument("--origin-id",
                      help="Exact original inbound id (requires --origin-request).")
    pesc.add_argument("--operation-nonce", dest="operation_nonce",
                      help=argparse.SUPPRESS)
    pesc.add_argument("-m", "--message",
                      help="The operator question (else --file or stdin). Required.")
    pesc.add_argument("--file", help="Read body from this file path ('-' = stdin)")
    pesc.add_argument("--quiet", action="store_true",
                      help="Print only the request_id line.")
    # Typed attention fields (0.56.0) -> nested meta.attention, for the ranked attention
    # queue. All optional; a malformed field exits 2 before any write.
    pesc.add_argument("--decision", help="Single-line decision needed (typed attention).")
    pesc.add_argument("--why", help="Single-line why-it-matters / impact.")
    pesc.add_argument("--option", action="append", help="An option to choose (repeatable).")
    pesc.add_argument("--recommendation", help="Your recommended option/action.")
    pesc.add_argument("--risk-if-ignored", dest="risk_if_ignored",
                      help="What happens if no operator decision arrives.")
    pesc.add_argument("--risk-severity", choices=["low", "medium", "high"],
                      help="Risk severity if ignored.")
    pesc.add_argument("--confidence", choices=["low", "medium", "high"],
                      help="Your confidence in the recommendation.")
    pesc.add_argument("--priority", choices=["low", "normal", "high", "urgent"],
                      help="Operator priority.")
    pesc.add_argument("--needed-by", dest="needed_by",
                      help="ISO-8601 date or datetime (a naive datetime is treated as UTC).")
    pesc.add_argument("--affected", action="append",
                      help="An affected ref e.g. agent:beta / request:esc-.. (repeatable).")
    pesc.set_defaults(func=cmd_escalate)

    # attention: the ranked/deduped operator queue over existing signals (0.56.0).
    pattn = sub.add_parser(
        "attention",
        description="Operator attention queue: a derived, ranked, deduped read-only view "
                    "over pending escalations, config-blocked holds, dead letters, gate "
                    "HOLDs, process-tree HOLDs, and lead-loop-unarmed signals, plus "
                    "allowed operator dispositions (blocking sources are nondismissible). "
                    "Creates no work objects; mutates only the disposition log.",
        help="Ranked operator attention queue + source-appropriate dispositions.")
    pattn.add_argument("--for", dest="for_agent",
                       help="Whose queue (default: the operator-facing liaison, else the sole lead).")
    pattn.add_argument("--json", action="store_true", help="Machine-readable output.")
    pattn.add_argument("--include-deferred", action="store_true", dest="include_deferred")
    pattn.add_argument("--include-dismissed", action="store_true", dest="include_dismissed")
    pattn.add_argument("--include-resolved", action="store_true", dest="include_resolved")
    pattn.add_argument("--all", action="store_true",
                       help="Show active + deferred + dismissed + resolved.")
    pattn.add_argument("--source", help="Filter to one source (e.g. needs_operator).")
    pattn.add_argument("--limit", type=int, help="Show at most N items.")
    pattn.add_argument("--stats", action="store_true",
                       help="Show derived counts (surfaced/dispositioned/dwell) instead of the "
                            "item list. Content-blind, no new state.")
    pattn.set_defaults(func=cmd_attention, attn_cmd=None)
    attn_sub = pattn.add_subparsers(dest="attn_cmd")

    a_show = attn_sub.add_parser("show", help="Show one item by id (incl. dispositioned).")
    a_show.add_argument("--item", required=True, help="item_id")
    a_show.add_argument("--for", dest="for_agent")
    a_show.add_argument("--json", action="store_true")
    # mirror the main view so a DISPOSITIONED item is auditable by id (fable-max #1)
    a_show.add_argument("--include-deferred", action="store_true", dest="include_deferred")
    a_show.add_argument("--include-dismissed", action="store_true", dest="include_dismissed")
    a_show.add_argument("--include-resolved", action="store_true", dest="include_resolved")
    a_show.add_argument("--all", action="store_true",
                        help="Look across active + deferred + dismissed + resolved.")
    a_show.set_defaults(func=cmd_attention, attn_cmd="show")

    def _disp_parser(name: str, *, until: bool = False, evidence: bool = False):
        p = attn_sub.add_parser(name, help=f"{name} an attention item (operator disposition).")
        p.add_argument("--item", required=True, help="item_id")
        p.add_argument("--reason", required=True, help="Required non-empty reason (audited).")
        p.add_argument("--from", dest="sender",
                       help="Actor (default $AGENTTALK_SELF); must resolve to the liaison "
                            "or sole lead. There is no --by.")
        if until:
            p.add_argument("--until", required=True, help="ISO-8601 defer-until.")
        if evidence:
            p.add_argument("--evidence", help="Optional pointer to where the answer landed.")
        p.set_defaults(func=cmd_attention, attn_cmd=name)

    _disp_parser("defer", until=True)
    _disp_parser("dismiss")
    _disp_parser("answered-elsewhere", evidence=True)

    ppro = sub.add_parser(
        "propose",
        help="Propose a concrete solution for the peer to accept / reject / "
             "counter. Auto-mints a `pp-` correlation id so the response is "
             "trackable by `agenttalk threads`. Peer replies with `reply "
             "--kind proposal-response --meta status=accepted|rejected|"
             "countered`; a counter is a fresh `propose --in-reply-to <id>`.",
    )
    ppro.add_argument("--from", dest="sender",
                      help="Sender agent name (default: $AGENTTALK_SELF)")
    ppro.add_argument("--to", dest="recipient",
                      help="Recipient agent name (default: $AGENTTALK_PEER, "
                           "or the single other agent in the roster)")
    ppro.add_argument("--subject", help="One-line summary")
    ppro.add_argument("--meta", action="append", help="key=value (repeatable)")
    ppro.add_argument("-m", "--message", help="Body text (else --file or stdin)")
    ppro.add_argument("--file", help="Read body from this file path ('-' = stdin)")
    ppro.add_argument("--in-reply-to", dest="in_reply_to",
                      help="request_id of a prior proposal this one counters "
                           "(sets meta.in_reply_to; the prior proposal should "
                           "also get a proposal-response status=countered).")
    ppro.add_argument("--print-id", action="store_true",
                      help="Print the proposal's correlation id (request_id) "
                           "on its own line — the token a counter references.")
    ppro.add_argument("--quiet", action="store_true")
    ppro.set_defaults(func=cmd_propose)

    pbc = sub.add_parser(
        "broadcast",
        help="Send one message to a whole group (or --all) via fan-out — "
             "one point-to-point copy per member sharing a `b-` broadcast_id. "
             "`--kind question` is the 'everyone please weigh in' pattern "
             "(tracked per recipient in `agenttalk threads`); note/message "
             "are FYI. Reply-all is just another broadcast.",
    )
    pbc.add_argument("--from", dest="sender",
                     help="Sender agent name (default: $AGENTTALK_SELF)")
    bgrp = pbc.add_mutually_exclusive_group(required=True)
    bgrp.add_argument("--to-group", dest="to_group", help="Target group name.")
    bgrp.add_argument("--to-role", dest="to_role",
                      help="Target every roster member holding this ROLE "
                           "(resolved from the roles map at send time and "
                           "frozen into each copy's meta - later role "
                           "changes never alter historical obligations). "
                           "Unknown/empty role refuses loudly.")
    bgrp.add_argument("--all", action="store_true",
                      help="Target the whole roster (implicit group).")
    bgrp.add_argument("--resume", dest="resume",
                      help="Recover a PARTIAL fan-out: re-send the missing "
                           "frozen copies of this batch id (kind/subject/"
                           "body/meta come from the original copies). "
                           "Broadcaster-only; takes no body/kind overrides.")
    pbc.add_argument("--kind", default="message",
                     choices=["message", "note", "question"],
                     help="Broadcast kind (default: message). Use `question` "
                          "when you want everyone to respond.")
    pbc.add_argument("--subject", help="One-line summary.")
    pbc.add_argument(
        "--response-policy",
        choices=["each", "any", "quorum"],
        help="Question completion policy (default: each).",
    )
    pbc.add_argument(
        "--response-quorum",
        type=int,
        help="Required answer count for --response-policy quorum.",
    )
    pbc.add_argument("--meta", action="append", help="key=value (repeatable).")
    pbc.add_argument("-m", "--message", help="Body text (else --file or stdin).")
    pbc.add_argument("--file", help="Read body from this file path ('-' = stdin).")
    pbc.add_argument("--allow-empty", action="store_true")
    pbc.add_argument("--print-id", action="store_true",
                     help="Print the broadcast_id on its own line.")
    pbc.add_argument("--json", action="store_true",
                     help="On PARTIAL fan-out failure (exit 5), emit the "
                          "delivered/missed manifest as JSON.")
    pbc.add_argument("--quiet", action="store_true")
    pbc.set_defaults(func=cmd_broadcast)

    pr = sub.add_parser("recv", help="Print all queued messages for an agent.")
    pr.add_argument("--for", dest="agent", help="Agent name (default: $AGENTTALK_SELF)")
    pr.add_argument("--since", help="Only messages with id > this (default: agent cursor)")
    pr.add_argument("--ack", action="store_true", help="Advance cursor past the last shown msg")
    pr.add_argument("-n", "--limit", type=_positive_int_arg,
                    help="Show at most N surfaced messages; with --ack, consume only that page.")
    pr.add_argument("--quiet", action="store_true")
    pr.add_argument("--include-control", action="store_true",
                    help="Also surface control-plane kinds ('composing') that the "
                         "default view hides. Useful for debugging wait extensions.")
    pr.add_argument("--json", action="store_true",
                    help="Emit one structured JSON record per message (the same "
                         "schema the wrapper's in-process recv_api returns: id, ts, "
                         "from, to, kind, subject, body, meta, request_id, "
                         "broadcast_id, correlation_id, mode, cursor). A debug "
                         "mirror; machines use recv_api in-process, not this.")
    pr.set_defaults(func=cmd_recv)

    pdr = sub.add_parser(
        "drain",
        help="Consume the inbox: print all unread for an agent AND advance "
             "the cursor to the newest message. Equivalent to `recv --ack`, "
             "but unmissable — use this instead of hand-rolled polling.",
    )
    pdr.add_argument("--for", dest="agent", help="Agent name (default: $AGENTTALK_SELF)")
    pdr.add_argument("-n", "--limit", type=_positive_int_arg,
                     help="Consume at most N surfaced messages (required for pipe output).")
    pdr.add_argument("--quiet", action="store_true")
    pdr.add_argument("--include-control", action="store_true",
                     help="Also surface control-plane kinds ('composing') that the "
                          "default view hides. The cursor advances past them either way.")
    pdr.set_defaults(func=cmd_drain)

    pcompact = sub.add_parser(
        "compact",
        help="Archive a safe prefix of old messages to archived/compacted/ "
             "(cold storage), bounding live-store growth. Never archives "
             "unread, epoch-barrier, protected-thread, or invalid messages.",
    )
    pcompact.add_argument("--dry-run", action="store_true",
                          help="Show what would be archived (and which keep_floor "
                               "component caps it) without moving any file.")
    pcompact.add_argument("--keep-count", dest="keep_count", type=int, default=None,
                          help="Override config: always keep at least this many "
                               "newest messages live.")
    pcompact.add_argument("--keep-age-days", dest="keep_age_days", type=float,
                          default=None,
                          help="Override config: always keep messages younger "
                               "than this many days live.")
    pcompact.add_argument("--json", action="store_true",
                          help="Emit a JSON result (keep_floor, capped_by, "
                               "archived ids, components).")
    pcompact.set_defaults(func=cmd_compact)

    pw = sub.add_parser("wait", help="Block until a new message arrives for an agent, then print it.")
    pw.add_argument("--for", dest="agent", help="Agent name (default: $AGENTTALK_SELF)")
    pw.add_argument("--timeout", type=float, default=120.0, help="Seconds to wait (0 = forever, default 120)")
    pw.add_argument("--interval", type=float, default=0.3, help="Poll interval in seconds (default 0.3)")
    pw.add_argument("--max-poll-interval", dest="max_poll_interval", type=float, default=2.0,
                    help="Cap (seconds) for adaptive idle poll backoff: when the bus is "
                         "quiet the poll interval grows from --interval up to this cap, "
                         "resetting to --interval on any activity. Bounds the per-waiter "
                         "idle CPU cost. Set <= --interval to disable (fixed-interval "
                         "polling). (default 2.0)")
    pw.add_argument("--refuse-stacked-wait", dest="refuse_stacked_wait", action="store_true",
                    help="Exit 6 instead of warning when another LIVE process already "
                         "holds this agent's mailbox, so a terminal can't stack duplicate "
                         "poll loops. Default: warn only (re-arm loops rely on the warning).")
    pw.add_argument("--ack", action="store_true", default=True,
                    help="Advance cursor past the received msg (default true)")
    pw.add_argument("--no-ack", dest="ack", action="store_false")
    pw.add_argument("--quiet", action="store_true")
    pw.add_argument("--heartbeat-interval", type=float, default=10.0,
                    help="Seconds between heartbeat stamps in .agenttalk/state/<agent>.heartbeat (0 = off, default 10)")
    pw.add_argument("--grace", type=float, default=2.0,
                    help="Seconds of post-timeout grace: after the deadline fires, "
                         "sleep this long and do ONE more inbox scan before exiting 1. "
                         "Catches replies that landed just past the deadline. (default 2.0, 0 = off)")
    pw.add_argument("--composing-extend", type=float, default=120.0,
                    help="Seconds to extend the deadline for each fresh 'composing' "
                         "ping from the peer. Capped at 1800s total per wait. "
                         "(default 120, 0 = off)")
    pw.add_argument("--to-request", dest="to_request",
                    help="SCOPED wait: wake only on a message with this "
                         "request_id; ignore (and leave unread) all other "
                         "traffic. Non-consuming — advances only the per-thread "
                         "pointer, never the global cursor. Close the thread "
                         "with `ack --to-request`. Exit codes: 0 reply, "
                         "1 timeout, 3 the request was RESCINDED (wakes "
                         "immediately; do not act on it), 6 superseded by "
                         "a newer same-thread waiter.")
    pw.add_argument("--kind",
                    help="With --to-request: further restrict the scoped wait "
                         "to this message kind. Note: the per-thread pointer "
                         "still advances to the matched message, so earlier "
                         "OTHER-kind messages on that thread are skipped — run "
                         "an unfiltered `wait --to-request` or `drain` first if "
                         "you need them.")
    pw.set_defaults(func=cmd_wait)

    pa = sub.add_parser("ack", help="Advance an agent's cursor, OR close a thread (--to-request).")
    pa.add_argument("--for", dest="agent", help="Agent name (default: $AGENTTALK_SELF)")
    pa.add_argument("--id", help="Specific message id (default: latest message for this agent)")
    pa.add_argument("--to-request", dest="to_request",
                    help="Explicitly CLOSE this request thread for the agent "
                         "(manual closure / escape hatch). Does not touch the "
                         "global cursor. Permanent: a later message on the same "
                         "request_id won't re-open the thread in `threads` (it's "
                         "still delivered by drain/wait/sync) — a fresh exchange "
                         "needs a new request_id.")
    pa.set_defaults(func=cmd_ack)

    psync = sub.add_parser(
        "sync",
        help="Rejoin digest: identity + roster, actionable request threads "
             "(who owes whom, age, next-action hint), last decision per "
             "thread, and recent unread FYI traffic kept separate from owed "
             "work. Run it on restart/rejoin before acting.",
    )
    psync.add_argument("--for", dest="agent", help="Agent name (default: $AGENTTALK_SELF)")
    psync.add_argument("--lesson-tag", dest="lesson_tag", action="append",
                       help="Extra safe-slug tag for matching active lessons (repeatable).")
    psync.add_argument("--json", action="store_true",
                       help="Emit the structured digest for skills to parse.")
    psync.set_defaults(func=cmd_sync)

    pwho = sub.add_parser(
        "whoami",
        help="Diagnostic: effective --root, resolved self/peer, roster "
             "membership + role/groups, and unread/owed counts. Warns on a "
             "misplaced --root (self not in roster) or an unset identity.",
    )
    pwho.add_argument("--for", dest="agent", help="Agent name (default: $AGENTTALK_SELF)")
    pwho.add_argument("--json", action="store_true")
    pwho.set_defaults(func=cmd_whoami)

    pt = sub.add_parser("transcript", help="Export the full conversation.")
    pt.add_argument("--format", choices=["md", "jsonl"], default="md")
    pt.add_argument("--out", help="Output path (default: .agenttalk/sessions/transcript-<session>.<ext>)")
    pt.set_defaults(func=cmd_transcript)

    pe = sub.add_parser("end", help="Send an 'end' message to the other agent(s) and export the transcript.")
    pe.add_argument("--from", dest="sender", help="Sender agent name (default: $AGENTTALK_SELF)")
    pe.add_argument("--reason", help="Free-text reason")
    pe.set_defaults(func=cmd_end)

    prel = sub.add_parser(
        "release",
        help="Signal an agent (or team) to STAND DOWN and exit its listen loop. "
             "Distinct from `end`: no transcript export, restartable later. Requires "
             "an authority mode - --relay-human (relaying a human's decision) XOR "
             "--emergency (lead's narrow override) - and --reason; a bare release "
             "sends nothing. Only the operator-facing / sole-lead sender is "
             "authorized (fail-closed). Target exactly one of --to/--to-group/--all.",
    )
    prel.add_argument("--from", dest="sender", help="Sender agent name (default: $AGENTTALK_SELF)")
    prel.add_argument("--to", dest="recipient", help="Release ONE agent (point-to-point).")
    prel.add_argument("--to-group", dest="to_group", help="Release every member of this group.")
    prel.add_argument("--all", action="store_true", help="Release all other active agents.")
    rel_mode = prel.add_mutually_exclusive_group()
    rel_mode.add_argument("--relay-human", dest="relay_human", action="store_true",
                          help="You are RELAYING a human operator's stand-down decision.")
    rel_mode.add_argument("--emergency", dest="emergency", action="store_true",
                          help="Narrow lead override for a malfunctioning/rogue agent "
                               "(report to the operator immediately after).")
    prel.add_argument("-m", "--message", "--reason", dest="message",
                      help="REQUIRED stand-down reason (the human's decision, or why an "
                           "emergency could not wait).")
    prel.add_argument("--file", dest="file", help="Read the reason from this file ('-' = stdin).")
    prel.add_argument("--quiet", action="store_true")
    prel.set_defaults(func=cmd_release)

    phb = sub.add_parser(
        "heartbeat",
        help="Stamp this agent's activity heartbeat (the supervisor's "
             "stuck-detection signal). Throttled — a no-op if the heartbeat is "
             "younger than --min-interval. Wire as a Claude PostToolUse / Codex "
             "hook so it stays fresh while the model works.",
    )
    phb.add_argument("--for", dest="agent", help="Agent name (default: $AGENTTALK_SELF)")
    phb.add_argument("--fallback-for", dest="fallback_for",
                     help="Hook-only fallback identity when --for and AGENTTALK_SELF are absent.")
    phb.add_argument("--min-interval", dest="min_interval", type=float, default=5.0,
                     help="No-op if the heartbeat is younger than this many "
                          "seconds (default 5).")
    phb.add_argument("--hook", action="store_true",
                     help="Soft hook mode for a PostToolUse/Codex hook: NEVER block "
                          "a tool call - swallow every error (unresolved identity, "
                          "uninitialized store, write failure) and exit 0, silently. "
                          "Manual use stays strict (exit 2 on a bad identity).")
    phb.set_defaults(func=cmd_heartbeat)

    pcheckpoint = sub.add_parser(
        "checkpoint",
        help="Save, resume, or inspect deterministic external state around "
             "context compaction.",
    )
    checkpoint_sub = pcheckpoint.add_subparsers(dest="checkpoint_mode", required=True)

    checkpoint_save = checkpoint_sub.add_parser(
        "save",
        help="Capture context headroom, Git state, and actionable bus threads.",
    )
    checkpoint_save.add_argument(
        "--for",
        dest="agent",
        help="Agent name (default: $AGENTTALK_SELF).",
    )
    checkpoint_save.add_argument(
        "--fallback-for",
        dest="fallback_for",
        help="Hook-only fallback identity when --for and AGENTTALK_SELF are absent.",
    )
    checkpoint_save.add_argument(
        "--trigger",
        choices=["auto", "manual"],
        default="manual",
        help="Compaction trigger for non-hook saves (default: manual).",
    )
    checkpoint_save.add_argument(
        "--hook",
        action="store_true",
        help="PreCompact hook mode: read bounded JSON from stdin, stay silent, "
             "swallow every error, and always exit 0.",
    )
    checkpoint_save.set_defaults(func=cmd_checkpoint_save)

    checkpoint_resume = checkpoint_sub.add_parser(
        "resume",
        help="Render the latest checkpoint for a resumed compacted session.",
    )
    checkpoint_resume.add_argument(
        "--for",
        dest="agent",
        help="Agent name (default: $AGENTTALK_SELF).",
    )
    checkpoint_resume.add_argument(
        "--fallback-for",
        dest="fallback_for",
        help="Hook-only fallback identity when --for and AGENTTALK_SELF are absent.",
    )
    checkpoint_resume.add_argument(
        "--hook",
        action="store_true",
        help="SessionStart hook mode: emit only the additionalContext JSON "
             "envelope and always exit 0.",
    )
    checkpoint_resume.set_defaults(func=cmd_checkpoint_resume)

    checkpoint_show = checkpoint_sub.add_parser(
        "show",
        help="Inspect the latest saved checkpoint.",
    )
    checkpoint_show.add_argument(
        "--for",
        dest="agent",
        help="Agent name (default: $AGENTTALK_SELF).",
    )
    checkpoint_show.add_argument("--json", action="store_true")
    checkpoint_show.set_defaults(func=cmd_checkpoint_show)

    prr = sub.add_parser(
        "request-restart",
        help="Queue a MANUAL restart of an agent (the external supervisor "
             "relaunches it and clears the request). A protected "
             "(operator_facing/lead) agent requires --force-protected; a fresh "
             "protected heartbeat also requires --acknowledge-live-protected-kill.",
    )
    prr.add_argument("--for", dest="agent", required=True, help="Agent to restart.")
    prr.add_argument("--from", dest="sender",
                     help="Requester (default: $AGENTTALK_SELF; must be authorized).")
    prr.add_argument("--reason", help="Free-text reason.")
    prr.add_argument("--force-protected", dest="force_protected", action="store_true",
                     help="Allow restarting a protected (operator_facing/lead) agent.")
    prr.add_argument("--acknowledge-live-protected-kill",
                     dest="acknowledge_live_protected_kill", action="store_true",
                     help="Second operator-facing acknowledgement required before a "
                          "freshly heartbeating protected agent is killed.")
    prr.set_defaults(func=cmd_request_restart)

    pcg = sub.add_parser(
        "commit-gate",
        help="Inspect detection-grade commit enforcement or audit-reset its breaker.",
    )
    pcg.add_argument("--for", dest="agent", required=True, help="Wrapped agent name.")
    pcg.add_argument("--json", action="store_true")
    pcg_sub = pcg.add_subparsers(dest="gate_action")
    pcg_status = pcg_sub.add_parser("status", help="Show activation and breaker state.")
    pcg_status.set_defaults(func=cmd_commit_gate)
    pcg_reset = pcg_sub.add_parser("reset", help="Authenticated, audited breaker reset.")
    pcg_reset.add_argument("--from", dest="sender", required=True,
                           help="Operator-facing liaison or sole lead.")
    pcg_reset.add_argument("--reason", required=True, help="Audit reason for reset.")
    pcg_reset.set_defaults(func=cmd_commit_gate)
    pcg.set_defaults(func=cmd_commit_gate, gate_action="status")

    prl = sub.add_parser(
        "request-launch",
        help="Queue an evidence-only ephemeral adversarial review launch marker "
             "for the supervisor.",
    )
    prl.add_argument("--from", dest="sender", required=True,
                     help="Authorized requester (operator-facing agent or sole lead).")
    prl.add_argument("--profile", required=True, help="Supervisor-whitelisted profile.")
    prl.add_argument("--skill", required=True, help="Supervisor-whitelisted review skill/lens.")
    prl.add_argument("--revision", required=True,
                     help="Ref or SHA; frozen to a full SHA via git.")
    prl.add_argument("--base-revision", dest="base_revision",
                     help="Optional base revision for the review scope.")
    prl.add_argument("--path", action="append",
                     help="Changed/scoped path for the review (repeatable).")
    prl.add_argument("--summary", help="Short scope summary.")
    prl.add_argument("--request-id", dest="request_id",
                     help="Explicit launch request id (default: generated lr-*).")
    prl.add_argument("--lane-id",
                     help="Provisioned lane whose worktree should be used for the launch.")
    prl.add_argument("--timeout-seconds", dest="timeout_seconds", type=int,
                     help="Requested timeout; must not exceed supervisor default.")
    prl.add_argument("--role", help="Requested temporary role (must be allowed).")
    prl.add_argument("--group", action="append",
                     help="Requested temporary group (repeatable; must be allowed).")
    prl.add_argument("-m", "--message", help="Review prompt (else --file or stdin).")
    prl.add_argument("--file", help="Read review prompt from this file path ('-' = stdin).")
    prl.set_defaults(func=cmd_request_launch)

    pwrap = sub.add_parser(
        "wrap",
        help="Run an agent CLI under the progress-adapter wrapper: launch it in "
             "structured-stream mode, stamp heartbeat on real progress events "
             "(throttled), render readable output, and detect degraded output. "
             "Codex (`codex exec --json`) and Claude (`stream-json`) structured "
             "streams are supported.",
    )
    launch_admission.add_wrap_arguments(pwrap)
    pwrap.set_defaults(func=cmd_wrap)

    pcheckwrap = sub.add_parser(
        "_internal-check-wrap-dispatch",
        help=argparse.SUPPRESS,
    )
    pcheckwrap.add_argument("argv", nargs=argparse.REMAINDER)
    pcheckwrap.set_defaults(func=cmd_internal_check_wrap_dispatch)

    pdl = sub.add_parser(
        "dead-letter",
        help="Inspect + recover dead-lettered (poison) messages: valid messages the "
             "wrapped model failed deterministically, moved out of the inbox so it is "
             "never blocked. Separate from `prune --invalid` (forged/invalid files).",
    )
    pdl.set_defaults(func=cmd_dead_letter, dead_letter_cmd=None)
    dlsub = pdl.add_subparsers(dest="dead_letter_cmd")
    dll = dlsub.add_parser("list", help="List dead-lettered messages (unresolved by default).")
    dll.add_argument("--agent", help="Limit to one agent (default: all).")
    dll.add_argument("--json", action="store_true")
    dll.add_argument("--resolved", action="store_true", help="Show RESOLVED entries only.")
    dll.add_argument("--all", action="store_true", help="Show resolved + unresolved.")
    dll.set_defaults(func=cmd_dead_letter)
    dls = dlsub.add_parser("show", help="Show one dead-letter's metadata + original body.")
    dls.add_argument("--agent", required=True)
    dls.add_argument("--id", required=True, help="The dead-lettered message id.")
    dls.add_argument("--json", action="store_true")
    dls.set_defaults(func=cmd_dead_letter)
    dlr = dlsub.add_parser("requeue",
                           help="Re-inject as a FRESH message (new id, own fresh attempt "
                                "count); original evidence preserved. No cursor rewind.")
    dlr.add_argument("--agent", required=True)
    dlr.add_argument("--id", required=True, help="The dead-lettered message id.")
    dlr.add_argument("--force-resolved", dest="force_resolved", action="store_true",
                     help="Requeue an item that was RESOLVED (requires --reason).")
    dlr.add_argument("--reason", help="Reason (required with --force-resolved).")
    dlr.add_argument("--from", dest="sender", help="Actor for the audit event.")
    dlr.set_defaults(func=cmd_dead_letter)
    dlres = dlsub.add_parser("resolve",
                             help="Operator decision distinct from requeue: mark a "
                                  "dead-letter handled (payload preserved; audited). "
                                  "Removes it from the default list/doctor nagging.")
    dlres.add_argument("--agent", required=True)
    dlres.add_argument("--id", required=True, help="The dead-lettered message id.")
    dlres.add_argument("--reason", required=True, help="Required non-empty reason (audited).")
    dlres.add_argument("--evidence", help="Optional pointer to where it was handled.")
    dlres.add_argument("--from", dest="sender",
                       help="Actor (default $AGENTTALK_SELF); must resolve to the "
                            "liaison or sole lead. No --by.")
    dlres.set_defaults(func=cmd_dead_letter, dead_letter_cmd="resolve")
    dlp = dlsub.add_parser(
        "purge",
        help="Archive resolved dead-letter payloads out of the live sink.")
    dlp.add_argument("--resolved", action="store_true",
                     help="Required selector; only resolved entries can be purged.")
    dlp.add_argument("--agent", help="Limit to one agent (default: all).")
    dlp.add_argument("--from", dest="sender",
                     help="Actor (default $AGENTTALK_SELF); must resolve to the liaison "
                          "or sole lead.")
    dlp.add_argument("--dry-run", action="store_true")
    dlp.add_argument("--json", action="store_true")
    dlp.set_defaults(func=cmd_dead_letter, dead_letter_cmd="purge")

    pmll = sub.add_parser(
        "managed-lead-loop",
        help="Configure/inspect managed lead-loop identities: a team mailbox OWNED "
             "by a wrapped controller that cannot silently un-arm. Generic by agent "
             "name, never by cli.")
    pmll.set_defaults(func=cmd_managed_lead_loop, managed_cmd=None)
    mllsub = pmll.add_subparsers(dest="managed_cmd")
    mset = mllsub.add_parser("set", help="Mark an agent a managed lead-loop.")
    mset.add_argument("agent")
    mset.add_argument("--ttl", type=float, default=None,
                      help="Lease TTL seconds (must exceed cadence).")
    mset.add_argument("--cadence", type=float, default=None, help="Renew cadence seconds.")
    mset.set_defaults(func=cmd_managed_lead_loop)
    mclr = mllsub.add_parser("clear", help="Unmark a managed lead-loop agent.")
    mclr.add_argument("agent")
    mclr.set_defaults(func=cmd_managed_lead_loop)
    mlst = mllsub.add_parser("list", help="List managed lead-loop identities + state.")
    mlst.add_argument("--json", action="store_true")
    mlst.set_defaults(func=cmd_managed_lead_loop)

    psup = sub.add_parser(
        "supervise",
        help="External-supervisor support (thin): scaffold the config+scripts, "
             "emit the read-only liveness report, compute the safe action plan, "
             "or clear a restart marker. The generated script owns the loop.",
    )
    gsup = psup.add_mutually_exclusive_group(required=True)
    gsup.add_argument("--init", action="store_true",
                      help="Scaffold supervisor.json + PowerShell supervisor helpers.")
    gsup.add_argument("--refresh-scripts", dest="refresh_scripts", action="store_true",
                      help="Refresh generated PowerShell helpers and shim; preserve config/state.")
    gsup.add_argument("--select-pwsh", dest="select_pwsh", action="store_true",
                      help="Select and probe PowerShell Core 7+ for this project.")
    gsup.add_argument("--repair-instance-marker", dest="repair_instance_marker",
                      action="store_true",
                      help="Explicitly recover an invalid singleton marker.")
    gsup.add_argument("--report", action="store_true",
                      help="Emit the read-only per-agent liveness snapshot (JSON).")
    gsup.add_argument("--bootstrap-check", dest="bootstrap_check", action="store_true",
                      help="Emit a read-only team bootstrap readiness check (JSON): "
                           "roster, operator-facing lead, supervisor config, wrapped "
                           "Claude/Codex launch invariants, and fresh heartbeats.")
    gsup.add_argument("--plan", action="store_true",
                      help="Emit the action plan (the shared decision table) as JSON.")
    gsup.add_argument(
        "--reset-process-tree-ownership",
        dest="reset_process_tree_ownership",
        action="store_true",
        help="Operator-attended disposition of a configured agent or terminal "
             "ephemeral request's invalid/truncated owned-process-tree HOLD. "
             "Configured resets require exact identity verification; terminal "
             "ephemeral archives require explicit operator attestations. Never "
             "kills or launches.",
    )
    gsup.add_argument("--clear-restart", dest="clear_restart", action="store_true",
                      help="Clear a restart-request marker by --for + --request-id.")
    gsup.add_argument("--record-launch", dest="record_launch", action="store_true",
                      help="(script use) Apply launch-success state for --for: "
                           "Claude pins --session-id; Codex marks launched + no "
                           "pinned id. Needs --state-file.")
    gsup.add_argument("--prepare-launch-request", dest="prepare_launch_request",
                      action="store_true",
                      help="(script use) Claim an ephemeral launch request, roster "
                           "the temp identity, and print its launch spec.")
    psup.add_argument(
        "--launch-agenttalk-python",
        help="(script use) Running supervisor's exact AGENTTALK_PY value.",
    )
    psup.add_argument(
        "--launch-src-on-pythonpath",
        choices=("true", "false"),
        help="(script use) Whether the running supervisor prepends <root>/src.",
    )
    psup.add_argument(
        "--supervisor-config-sha256",
        help=argparse.SUPPRESS,
    )
    gsup.add_argument("--record-ephemeral-launch", dest="record_ephemeral_launch",
                      action="store_true",
                      help="(script use) Record ephemeral launch pid/deadline.")
    gsup.add_argument("--archive-launch-request", dest="archive_launch_request",
                      action="store_true",
                      help="(script use) Archive a terminal ephemeral launch request.")
    gsup.add_argument("--janitor-ephemeral", dest="janitor_ephemeral",
                      action="store_true",
                      help="(script use) Retire a stale adversary-* identity.")
    gsup.add_argument("--seed-codex-config", dest="seed_codex_config", action="store_true",
                      help="(script use) Overlay the unattended auto-mode keys "
                           "(approval_policy/sandbox_mode/[windows] sandbox/"
                           "writable_roots) onto config.toml in --home. Idempotent; "
                           "preserves other keys.")
    gsup.add_argument("--seed-claude-settings", dest="seed_claude_settings",
                      action="store_true",
                      help="(script use) Merge {\"defaultMode\": --mode} into "
                           "<--dir>/.claude/settings.json (the Claude unattended seed).")
    gsup.add_argument("--claim-instance", dest="claim_instance", action="store_true",
                      help="(script use) Claim the singleton supervisor instance lock.")
    gsup.add_argument("--validate-current-pwsh", dest="validate_current_pwsh",
                      action="store_true", help=argparse.SUPPRESS)
    gsup.add_argument("--prepare-task-install", dest="prepare_task_install",
                      action="store_true", help=argparse.SUPPRESS)
    gsup.add_argument("--commit-task-install", dest="commit_task_install",
                      action="store_true", help=argparse.SUPPRESS)
    gsup.add_argument("--clear-task-binding", dest="clear_task_binding",
                      action="store_true", help=argparse.SUPPRESS)
    gsup.add_argument("--validate-task-start", dest="validate_task_start",
                      action="store_true", help=argparse.SUPPRESS)
    gsup.add_argument("--release-instance", dest="release_instance", action="store_true",
                      help="(script use) Release the singleton supervisor instance lock.")
    gsup.add_argument("--drain-intents", dest="drain_intents", action="store_true",
                      help="(script use) Drain queued dashboard intents once.")
    gsup.add_argument("--launch-barrier", dest="launch_barrier", action="store_true",
                      help="(script use) Verify no same-agent wrapper survived before launch.")
    psup.add_argument("--home", help="(--seed-codex-config) the isolated CODEX_HOME dir.")
    psup.add_argument("--repo", help="(--seed-codex-config) repo abs path for "
                                     "writable_roots (default: the --root store dir).")
    psup.add_argument("--sandbox", help="(--seed-codex-config) [windows] sandbox value "
                                        "(default 'unelevated').")
    psup.add_argument("--dir", help="(--seed-claude-settings) the agent launch dir.")
    psup.add_argument("--mode", help="(--seed-claude-settings) defaultMode "
                                     "(default 'bypassPermissions').")
    psup.add_argument("--cli", help="(--record-launch) the agent CLI ('claude'|'codex').")
    psup.add_argument("--pid", type=int, default=None,
                       help="Process id for record-launch or live-supervisor "
                            "identity checks.")
    psup.add_argument("--pid-start", dest="pid_start", default=None,
                       help="Process start-time for record-launch or live-supervisor "
                            "anti-pid-reuse checks.")
    psup.add_argument("--pwsh",
                      help="Absolute pwsh.exe path (terminal explicit selection; no fallback).")
    psup.add_argument("--artifact-boundary", dest="artifact_boundary",
                      choices=sorted(sup.ARTIFACT_BOUNDARIES), default="full",
                      help=argparse.SUPPRESS)
    psup.add_argument("--task-name", dest="task_name", help=argparse.SUPPRESS)
    psup.add_argument("--selection-revision", dest="selection_revision", type=int,
                      default=0, help=argparse.SUPPRESS)
    psup.add_argument("--selection-fingerprint", dest="selection_fingerprint",
                      help=argparse.SUPPRESS)
    psup.add_argument("--task-execute", dest="task_execute", help=argparse.SUPPRESS)
    psup.add_argument("--task-arguments", dest="task_arguments", help=argparse.SUPPRESS)
    psup.add_argument("--task-working-directory", dest="task_working_directory",
                      help=argparse.SUPPRESS)
    psup.add_argument("--quarantine", action="store_true",
                      help="(--repair-instance-marker) move the invalid marker aside.")
    psup.add_argument("--acknowledge-no-live-supervisor",
                      dest="acknowledge_no_live_supervisor", action="store_true",
                      help="Acknowledge no supervisor is live for an attended "
                           "instance-marker repair or process-tree reset.")
    psup.add_argument(
        "--acknowledge-owned-processes-stopped",
        dest="acknowledge_owned_processes_stopped",
        action="store_true",
        help="(--reset-process-tree-ownership) acknowledge the attended teardown, "
             "including any identities omitted by a truncated record.",
    )
    psup.add_argument(
        "--hold-source-hash",
        dest="hold_source_hash",
        help="(--reset-process-tree-ownership) exact current process-tree Attention "
             "source hash.",
    )
    psup.add_argument(
        "--verified-launch-nonce",
        dest="verified_launch_nonce",
        help="(configured --reset-process-tree-ownership) launch nonce read from "
             "the live wrapper command line before attended teardown.",
    )
    psup.add_argument(
        "--from",
        dest="sender",
        help="(--reset-process-tree-ownership) operator-facing liaison or sole lead "
             "recording the attended reset.",
    )
    psup.add_argument("--instance-token", dest="instance_token",
                       help="(--release-instance/--drain-intents/"
                            "--archive-launch-request) supervisor instance token.")
    psup.add_argument("--max-per-tick", dest="max_per_tick", type=int, default=25,
                      help="(--drain-intents) maximum queued intents to claim in one tick.")
    psup.add_argument("--session-id", dest="session_id",
                      help="(--record-launch) the minted session id (Claude).")
    psup.add_argument("--timeout-seconds", dest="timeout_seconds", type=int,
                      help="(--record-ephemeral-launch) ephemeral timeout.")
    psup.add_argument("--terminal-state", dest="terminal_state",
                      choices=[eph.STATE_COMPLETED, eph.STATE_DENIED,
                               eph.STATE_FAILED, eph.STATE_TIMED_OUT],
                      help="(--archive-launch-request) terminal state.")
    psup.add_argument(
        "--reason",
        help="Terminal archive reason or attended process-tree reset audit reason.",
    )
    psup.add_argument("--completion-json", dest="completion_json",
                      help="(--archive-launch-request) JSON review-result "
                           "completion evidence from the supervisor plan.")
    psup.add_argument("--snapshot-file", dest="snapshot_file", default=None,
                      help="(--plan) the executor's process snapshot JSON (list of "
                           "{pid,parent_pid,name,command_line,start_time,"
                           "start_filetime}). Missing "
                           "or unreadable => UNAVAILABLE (brain-required CLI fails closed).")
    psup.add_argument("--pre-snapshot-file", dest="pre_snapshot_file", default=None,
                      help="(--record-launch/--record-ephemeral-launch) process snapshot "
                           "captured immediately before Start-Process.")
    psup.add_argument("--post-snapshot-file", dest="post_snapshot_file", default=None,
                      help="(--record-launch/--record-ephemeral-launch) process snapshot "
                           "captured immediately after Start-Process.")
    psup.add_argument("--launcher-nonce", dest="launcher_nonce",
                      help=argparse.SUPPRESS)
    psup.add_argument("--launcher-nonce-injected", dest="launcher_nonce_injected",
                      action="store_true", help=argparse.SUPPRESS)
    psup.add_argument("--launcher-nonce-source", dest="launcher_nonce_source",
                      help=argparse.SUPPRESS)
    psup.add_argument("--launcher-nonce-missing-reason",
                      dest="launcher_nonce_missing_reason", help=argparse.SUPPRESS)
    gsup.add_argument("--install-activity-hook", dest="install_activity_hook",
                      action="store_true",
                      help="MERGE the activity heartbeat hook and Claude checkpoint "
                           "hooks into project config (plus the heartbeat in "
                           ".codex/hooks.json with --codex). Never global, never "
                           "clobbers. Unlocks stuck-recovery once you set "
                           "activity_hook=true.")
    psup.add_argument("--codex", action="store_true",
                      help="(--install-activity-hook) ALSO install the Codex hook.")
    psup.add_argument("--codex-only", dest="codex_only", action="store_true",
                      help="(--install-activity-hook) install ONLY the Codex hook.")
    psup.add_argument("--interactive-for", dest="interactive_for",
                      help="(--install-activity-hook) install a Claude hook "
                           "bound to the operator-facing liaison identity.")
    psup.add_argument(
        "--force",
        action="store_true",
        help="(--init) refresh generated scripts/shim; preserve config and runtime state.",
    )
    psup.add_argument("--now", type=float, default=None,
                      help="Override 'now' (epoch seconds) for report/plan — test hook.")
    psup.add_argument("--report-file", dest="report_file",
                      help="(--plan) read the report from this JSON file instead of live.")
    psup.add_argument("--state-file", dest="state_file",
                      help="(--plan) the supervisor's local state JSON (pids/backoff).")
    psup.add_argument("--record-events", dest="record_events", action="store_true",
                      help="(--plan, script use) append bounded redacted decision events.")
    psup.add_argument(
        "--for",
        dest="agent",
        help="Agent name for --clear-restart or configured "
             "--reset-process-tree-ownership.",
    )
    psup.add_argument(
        "--request-id",
        dest="request_id",
        help="Request id for --clear-restart or terminal ephemeral "
             "--reset-process-tree-ownership.",
    )
    psup.set_defaults(func=cmd_supervise)

    pdm = sub.add_parser(
        "deadman",
        help="Content-blind mail-age SLO check for stale actionable inbox work.",
    )
    pdm.add_argument("--threshold-seconds", dest="threshold_seconds", type=float,
                     default=None,
                     help="Override deadman.mail_age_slo_seconds for this check.")
    pdm.add_argument("--json", action="store_true",
                     help="Emit the content-blind JSON report.")
    pdm.add_argument("--alarm-unread-response", dest="alarm_unread_response",
                     action="store_true", default=None,
                     help="Treat stale unread responses as alarming, not advisory.")
    pdm.add_argument("--now", type=float, default=None, help=argparse.SUPPRESS)
    pdm.set_defaults(func=cmd_deadman)

    prpl = sub.add_parser(
        "reply",
        help="Reply to the most recent received message. Auto-derives "
             "recipient (= sender of last message) and echoes request_id "
             "for correlation. Use --meta to override / extend defaults.",
    )
    prpl.add_argument("--from", dest="sender",
                      help="Sender agent name (default: $AGENTTALK_SELF)")
    # --to-id and --to-request are two ways to name ONE anchor; allowing
    # both would silently pick one and defeat the point of anchoring.
    anchor_grp = prpl.add_mutually_exclusive_group()
    anchor_grp.add_argument("--to-id", dest="to_id",
                            help="Anchor to a SPECIFIC received message id "
                                 "instead of the most recent (must be addressed "
                                 "to you). Use when several threads are open so "
                                 "you echo the right request_id.")
    anchor_grp.add_argument("--to-request", dest="to_request",
                            help="Anchor to the latest received message in this "
                                 "correlation thread (by request_id).")
    prpl.add_argument("--kind", default=None,
                      help="Message kind (default: message; see `agenttalk send --help`)")
    prpl.add_argument("--subject", help="One-line summary")
    prpl.add_argument("--meta", action="append",
                      help="key=value (repeatable); request_id is auto-echoed if not set")
    prpl.add_argument("-m", "--message", help="Body text (else --file or stdin)")
    prpl.add_argument("--file", help="Read body from this file path ('-' = stdin)")
    prpl.add_argument("--operation-nonce", dest="operation_nonce",
                      help=argparse.SUPPRESS)
    prpl.add_argument("--allow-empty", action="store_true")
    prpl.add_argument(
        "--await-reply",
        action="store_true",
        help="Record an explicit across-turn reply wait when this reply opens "
             "a counter-review or counter-proposal. Managed-wrapper turns only.",
    )
    prpl.add_argument("--na", action="store_true",
                      help="Not-applicable response (0.15.0): closes your "
                           "obligation on a question thread, displayed as "
                           "(n/a) so the asker never mistakes it for a "
                           "substantive answer. Body optional (defaults to "
                           "'n/a'). Refused on review-request/proposal "
                           "threads - those need typed responses. Mutually "
                           "exclusive with --kind.")
    prpl.add_argument("--dry-run", action="store_true",
                      help="Resolve and print the recipient + echoed request_id "
                           "+ kind WITHOUT sending. Use it to confirm a reply "
                           "routes to the intended thread when several are open.")
    prpl.add_argument("--quiet", action="store_true")
    prpl.set_defaults(func=cmd_reply)

    pt2 = sub.add_parser(
        "tail",
        help="Passive monitor: stream all messages as they arrive. "
             "Does NOT advance cursors or write heartbeats — safe to run "
             "in a third terminal alongside two active agents.",
    )
    pt2.add_argument("--from-start", action="store_true",
                     help="Also replay all existing messages in the store before tailing.")
    pt2.add_argument("--interval", type=float, default=0.5,
                     help="Poll interval in seconds (default: 0.5)")
    pt2.add_argument("--timeout", type=float, default=0,
                     help="Exit after N seconds (default: 0 = run until Ctrl-C)")
    pt2.set_defaults(func=cmd_tail)

    pst = sub.add_parser(
        "start",
        help="Step-0 bootstrap: optionally init an explicit root, then start the Team Console.",
    )
    loc = pst.add_mutually_exclusive_group()
    loc.add_argument("--here", action="store_true",
                     help="With --init-if-absent, initialize/use the current directory.")
    loc.add_argument("--path", help="With --init-if-absent, initialize/use this project root.")
    pst.add_argument("--init-if-absent", dest="init_if_absent", action="store_true",
                     help="Initialize the store only when it is absent; requires an explicit location and --agents.")
    pst.add_argument("--agents", help="Comma-separated roster for --init-if-absent.")
    pst.add_argument("--host", default="127.0.0.1",
                     help="Bind address. Only loopback values are accepted.")
    pst.add_argument("--port", type=int, default=8765,
                     help="TCP port (default: 8765; pass 0 for an OS-chosen ephemeral port).")
    pst.add_argument("--enable-actions", action="store_true",
                     help="Enable browser intent enqueueing. Off by default.")
    pst.add_argument("--no-browser", action="store_true",
                     help="Do not open the browser after the server starts.")
    pst.add_argument("--no-supervisor", action="store_true",
                     help="Do not start an existing supervisor.ps1 scaffold.")
    pst.add_argument("--pwsh",
                     help="Absolute pwsh.exe path to select before starting the supervisor.")
    pst.add_argument("--dry-run", action="store_true",
                     help="Validate the bootstrap decision and print JSON without starting processes.")
    pst.add_argument("--quiet", action="store_true", default=True,
                     help="Suppress per-request access logs (default: true)")
    pst.add_argument("--access-log", dest="quiet", action="store_false",
                     help="Print per-request access logs to stderr")
    pst.set_defaults(func=cmd_start)

    psv = sub.add_parser(
        "serve",
        help="Start a read-only local web dashboard on http://127.0.0.1:8765/ "
             "to browse the message log in a real browser. Loopback-only by "
             "design — there is no flag to expose it elsewhere. If you need "
             "to view it from another machine, SSH-tunnel localhost:<port>.",
    )
    psv.add_argument("--host", default="127.0.0.1",
                     help="Bind address. Only loopback values are accepted: "
                          "127.0.0.1 (default), ::1, or localhost.")
    psv.add_argument("--port", type=int, default=8765,
                     help="TCP port (default: 8765; pass 0 for an OS-chosen ephemeral port)")
    psv.add_argument("--quiet", action="store_true", default=True,
                     help="Suppress per-request access logs (default: true)")
    psv.add_argument("--access-log", dest="quiet", action="store_false",
                     help="Print per-request access logs to stderr")
    psv.add_argument("--enable-actions", action="store_true",
                     help="Enable browser intent enqueueing. Off by default.")
    psv.set_defaults(func=cmd_serve, landing="/")

    pdb = sub.add_parser(
        "dashboard",
        help="Multi-root obligation dashboard (read-only, loopback-only) at "
             "http://127.0.0.1:8765/dashboard — who owes what, whose turn it "
             "is, and the next action, across one or many projects. Same "
             "server as `serve` (it just lands on the hierarchy view). "
             "Repeat --store to watch several projects in one browser tab.",
    )
    pdb.add_argument("--port", type=int, default=8765,
                     help="TCP port (default: 8765; pass 0 for an OS-chosen "
                          "ephemeral port)")
    pdb.add_argument("--store", action="append", dest="stores", metavar="PATH",
                     help="Project root to watch (repeatable; default: the "
                          "resolved current project). The path itself is the "
                          "project root — no upward search is performed.")
    pdb.add_argument("--quiet", action="store_true", default=True,
                     help="Suppress per-request access logs (default: true)")
    pdb.add_argument("--access-log", dest="quiet", action="store_false",
                     help="Print per-request access logs to stderr")
    pdb.add_argument("--enable-actions", action="store_true",
                     help="Enable browser intent enqueueing. Off by default.")
    # Deliberately NO --host: the alias binds 127.0.0.1, period (NFR-002a).
    pdb.set_defaults(func=cmd_serve, landing="/dashboard")

    prst = sub.add_parser(
        "reset",
        help="Clear active bus state (messages + cursors + heartbeats); "
             "preserves historical transcripts in .agenttalk/sessions/. "
             "Preserves config (roster). Use --archive to move EVERYTHING "
             "(messages + state + sessions) under "
             ".agenttalk/archived/<session_id>/ instead.",
    )
    prst.add_argument("--archive", action="store_true",
                      help="Move old messages/state/sessions to "
                           ".agenttalk/archived/<session_id>/ instead of "
                           "deleting active state.")
    prst.set_defaults(func=cmd_reset)

    pd = sub.add_parser(
        "doctor",
        help="Run health checks (init state, skill install freshness, codex-config, heartbeats).",
    )
    pd.add_argument("--json", action="store_true",
                    help="Emit structured JSON instead of human-readable text.")
    pd.set_defaults(func=cmd_doctor)

    pgw = sub.add_parser(
        "gateway",
        help="Manage the loopback-only watched OVH/Qwen trial gateway.",
    )
    gwsub = pgw.add_subparsers(dest="gateway_action", required=True)
    gw_init = gwsub.add_parser("init", help="One-time ledger/config/token initialization.")
    gw_init.add_argument("--litellm-executable", required=True)
    gw_init.add_argument(
        "--opening-eur",
        dest="opening_micro_eur",
        type=_micro_eur_arg,
        required=True,
        help="Current OVH month-to-date spend in EUR (up to six decimal places).",
    )
    gw_init.add_argument(
        "--opening-evidence",
        required=True,
        help="Short source and observed-at description for the opening balance.",
    )
    gw_init.set_defaults(func=cmd_gateway)
    gw_task = gwsub.add_parser("task-install", help="Install or verify the project task.")
    gw_task.set_defaults(func=cmd_gateway)
    gw_start = gwsub.add_parser("start", help="Start the verified managed gateway task.")
    gw_start.set_defaults(func=cmd_gateway)
    gw_stop = gwsub.add_parser("stop", help="Gracefully stop the managed gateway task.")
    gw_stop.add_argument("--timeout", type=_finite_float_arg, default=30.0)
    gw_stop.set_defaults(func=cmd_gateway)
    gw_status = gwsub.add_parser("status", help="Show an allowlisted non-secret status.")
    gw_status.set_defaults(func=cmd_gateway)
    gw_reconfigure = gwsub.add_parser(
        "reconfigure",
        help=(
            "Re-render config to the pinned endpoint + rebind the manifest "
            "(ledger/token-preserving; the gateway must be stopped first)."
        ),
    )
    gw_reconfigure.set_defaults(func=cmd_gateway)
    gw_runtime_rebind = gwsub.add_parser(
        "runtime-rebind",
        help=(
            "Probe and rebind a trusted LiteLLM runtime. The unsandboxed candidate "
            "has your filesystem authority; exit 3 means unknown and exit 2 means refusal."
        ),
        description=(
            "Probe and rebind a trusted LiteLLM runtime while the gateway is stopped. "
            "The candidate runs unsandboxed with your filesystem authority. AgentTalk "
            "rewrites only the manifest runtime field. Exit 3 is a retryable unknown "
            "probe outcome; exit 2 is a determinate refusal."
        ),
    )
    gw_runtime_rebind.add_argument(
        "--litellm-executable",
        required=True,
        help="Path to a trusted LiteLLM launcher to execute for the capability probe.",
    )
    gw_runtime_rebind.set_defaults(func=cmd_gateway)
    gw_reconcile = gwsub.add_parser(
        "reconcile",
        help="Explicitly resolve one uncertain provider attempt.",
    )
    gw_reconcile.add_argument("attempt_id")
    gw_reconcile.add_argument(
        "--outcome",
        choices=("no-send", "charge-reserve"),
        required=True,
    )
    gw_reconcile.add_argument("--reason", required=True)
    gw_reconcile.set_defaults(func=cmd_gateway)
    gw_cap_install = gwsub.add_parser(
        "cap-install",
        help="Durably install or verify fail-closed per-child turn caps.",
    )
    gw_cap_install.set_defaults(func=cmd_gateway)
    gw_canary = gwsub.add_parser(
        "canary-verify",
        help="Compare one settled attempt with the operator-observed dashboard delta.",
    )
    gw_canary.add_argument("attempt_id")
    gw_canary.add_argument(
        "--dashboard-delta-eur",
        dest="dashboard_delta_micro_eur",
        type=_micro_eur_arg,
        required=True,
    )
    gw_canary.set_defaults(func=cmd_gateway)
    gw_hold = gwsub.add_parser("hold", help="Durably block new provider transport.")
    gw_hold.add_argument("--reason", required=True)
    gw_hold.set_defaults(func=cmd_gateway)
    gw_clear_hold = gwsub.add_parser(
        "clear-hold",
        help="Explicitly clear a service hold after all attempts are reconciled.",
    )
    gw_clear_hold.add_argument("--reason", required=True)
    gw_clear_hold.set_defaults(func=cmd_gateway)
    gw_run = gwsub.add_parser("run", help=argparse.SUPPRESS)
    gw_run.set_defaults(func=cmd_gateway)

    ph = sub.add_parser(
        "hmac-init",
        help="Generate the HMAC signing key for this project. "
             "Stored outside .agenttalk/ (in the per-user config dir) so "
             "another local user with project-dir access cannot read it. "
             "Existence of the key automatically activates enforcement; "
             "to disable, delete the key file.",
    )
    ph.add_argument("--force", action="store_true",
                    help="Overwrite an existing key. Every message signed with "
                         "the old key becomes unverifiable.")
    ph.set_defaults(func=cmd_hmac_init)

    pcap = sub.add_parser(
        "capacity",
        help="Advisory headroom: publish your own 5h/weekly rate-limit budget + "
             "context-window fill (refresh) or view the team's (show) so a lead can plan work.",
    )
    pcap.add_argument("mode", nargs="?", choices=["show", "refresh"], default="show",
                      help="show (default) the team's published budgets, or refresh (publish your own)")
    pcap.add_argument("--for", dest="agent", help="Agent name (default: $AGENTTALK_SELF)")
    pcap.add_argument("--source", choices=["auto", "claude", "codex"], default="auto",
                      help="On refresh, which local source to read (default: auto-detect)")
    pcap.add_argument("--threshold", type=float, default=80.0,
                      help="Flag a window at/above this %% used (default: 80)")
    pcap.add_argument("--context-threshold", dest="context_threshold", type=float, default=80.0,
                      help="Flag context-window fill at/above this %% — near (auto)compaction (default: 80)")
    pcap.add_argument("--reset-soon-min", dest="reset_soon_min", type=int, default=30,
                      help="Flag a window resetting within this many minutes (default: 30)")
    pcap.add_argument("--statusline-path", help="Override the Claude status-line dump path (refresh)")
    pcap.add_argument("--sessions-dir", help="Override the Codex sessions dir (refresh)")
    pcap.set_defaults(func=cmd_capacity)

    pis = sub.add_parser(
        "install-skills",
        help="Copy bundled skills out: agenttalk bus skills to ~/.claude/commands/ + "
             "~/.codex/skills/, and the dev-discipline pack to ~/.claude/skills/ + "
             "~/.codex/skills/ (skip with --no-devkit).",
    )
    pis.add_argument("--claude-only", action="store_true", help="Install only Claude-side bus skills")
    pis.add_argument("--codex-only", action="store_true", help="Install only Codex-side bus skills")
    pis.add_argument("--no-devkit", action="store_true",
                     help="Skip the dev-discipline pack; install only the agenttalk bus skills")
    pis.add_argument("--devkit-only", action="store_true",
                     help="Install ONLY the dev-discipline pack (to the Agent-Skills dirs), "
                          "not the agenttalk bus skills")
    pis.add_argument("--claude-dir", help="Override Claude bus-command dir (default: ~/.claude/commands)")
    pis.add_argument("--codex-dir", help="Override Codex bus-skills dir (default: ~/.codex/skills)")
    pis.add_argument("--claude-skills-dir",
                     help="Override Claude devkit Agent-Skills dir (default: ~/.claude/skills)")
    pis.add_argument("--codex-skills-dir",
                     help="Override Codex devkit skills dir (default: ~/.codex/skills)")
    pis.add_argument("--force", action="store_true",
                     help="Overwrite existing files even if they differ from bundled")
    pis.add_argument("--dry-run", action="store_true", help="Show what would happen without writing")
    pis.set_defaults(func=cmd_install_skills)

    pc = sub.add_parser(
        "codex-config",
        help="Manage the per-project block in ~/.codex/config.toml "
             "so Codex can call agenttalk from inside its sandbox.",
    )
    grp = pc.add_mutually_exclusive_group()
    grp.add_argument("--enable", action="store_true", default=True,
                     help="Add/update approval_policy and sandbox_mode (default)")
    grp.add_argument("--disable", action="store_true",
                     help="Remove approval_policy and sandbox_mode (keeps trust_level)")
    grp.add_argument("--status", action="store_true", help="Show current state of the project block")
    pc.add_argument("--project", help="Project dir to enable/disable (default: CWD)")
    pc.add_argument("--config-path", help="Codex config path (default: ~/.codex/config.toml)")
    pc.set_defaults(func=cmd_codex_config)

    return p


def main(argv: list[str] | None = None) -> int:
    # Round 24 connector finding: this try used to wrap only args.func(args),
    # so a KeyboardInterrupt during build_parser()/parse_args() - before this
    # try ever started - propagated straight past main() (exiting 130 at
    # Python's own top level, the conventional cancellation status). Once
    # console_main's broad `except BaseException` was added (round 20), that
    # same propagating KeyboardInterrupt fell into ITS crash-reporting branch
    # instead, misreporting a Ctrl-C as an unexpected crash (return 1). Fixed
    # by widening the try to cover the whole function body - the SAME
    # KeyboardInterrupt row already in the contract table now covers this
    # window too, rather than adding a second, special-cased catch in
    # console_main beside it.
    try:
        # On Windows the default console code page (cp1252) can't encode
        # many characters that turn up in agent messages (arrows,
        # em-dashes, etc.). Force UTF-8 on stdout/stderr so writes don't
        # raise UnicodeEncodeError.
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                try:
                    reconfigure(encoding="utf-8", errors="replace")
                except (ValueError, OSError):
                    pass
        parser = build_parser()
        args = parser.parse_args(argv)
        # Handle --here on init
        if getattr(args, "cmd", None) == "init" and getattr(args, "here", False) and not args.path:
            args.path = str(Path.cwd())
        return args.func(args)
    except KeyboardInterrupt:
        sys.stderr.write("\nagenttalk: interrupted\n")
        return 130
    except (ValueError, FileNotFoundError, OSError) as e:
        sys.stderr.write(f"agenttalk: {e}\n")
        return 2


def console_main(argv: list[str] | None = None) -> int:
    """Shared body for every REAL top-level script/console invocation of
    this CLI - agenttalk/__main__.py's `if __name__ == "__main__":` guard
    (python -m agenttalk), the installed `agenttalk` console script
    (pyproject.toml's [project.scripts] entry, autogenerated by
    setuptools/pip as a thin `sys.exit(console_main())` wrapper), and this
    module's own `if __name__ == "__main__":` guard below.

    An embedder that imports this module and calls main([...]) directly
    never calls this function at all - main()'s own contract (propagate an
    unexpected exception's ORIGINAL type, uncaught) is exactly what such a
    caller gets, unchanged. This function exists ONLY to add one thing a
    real top-level process needs that an embedder must never be given:
    when an unexpected exception is genuinely uncaught, print it through
    the same bounded mechanism a supervised wrapper's own crash traceback
    already uses, instead of letting Python's default printer write an
    unbounded second copy to whatever raw stream is current by the time
    nothing is left to catch it. It installs no hook, replaces no stream,
    and leaves no state behind - the one call it makes beyond main() itself
    is a single, self-contained, best-effort write.
    """
    try:
        return main(argv)
    except SystemExit:
        raise
    except BaseException:
        from .wrapper_logs import print_bounded_uncaught_exception

        print_bounded_uncaught_exception()
        return 1


if __name__ == "__main__":
    sys.exit(console_main())
