"""agenttalk CLI: init, send, wait, recv, ack, transcript, end, status."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agenttalk import __version__
from agenttalk.display import render
from agenttalk.store import (
    COMPOSING_INTENT_STALE_SECONDS,
    CONTROL_KINDS,
    OPENER_KINDS,
    Store,
    find_root,
    find_stores_upward,
    validate_agent_name,
    validate_rescind,
)
from agenttalk import transcript as tx
from agenttalk import codex_config as cxc
from agenttalk import doctor as dr
from agenttalk import install_skills as iskl
from agenttalk import signing as _signing
from agenttalk import threads as th

# Hard ceiling on cumulative deadline extension from `composing` pings,
# regardless of how many arrive. Prevents a misbehaving (or stuck) peer
# from holding a waiter forever. 30 min was picked to comfortably cover
# long substantive review cycles without being effectively infinite.
_COMPOSING_MAX_EXTEND_SECONDS = 1800.0


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
    if getattr(args, "message", None):
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


# Kinds that OPEN a trackable thread get a request_id auto-minted if the
# caller didn't pass one, so `agenttalk threads` can correlate the reply.
# Distinct prefixes make the id self-describing in logs/transcripts.
_AUTOGEN_REQUEST_ID_PREFIX = {
    "review-request": "rq-",
    "question": "q-",
    "proposal": "pp-",
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
        label = "proposal id" if kind == "proposal" else "auto request_id"
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
    root = Path(args.path).resolve() if args.path else Path.cwd().resolve()
    store = Store(root)
    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    if len(agents) < 2:
        sys.stderr.write("agenttalk init: need at least two agents (e.g. --agents claude,codex)\n")
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
    # right agent name. Concrete examples when the roster is exactly 2.
    print()
    if len(agents) == 2:
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

# An outbound request still unanswered after this long is surfaced as a
# status warning — the peer may have missed it, or you forgot to wait.
OPEN_OUTBOUND_STALE_SECONDS = 600.0


def _gather_status(store: Store) -> dict:
    """Build the structured status payload shared by both output modes."""
    cfg = store.load_config()
    roles = cfg.get("roles", {}) or {}
    liaison = store.operator_facing()
    msgs = store.all_messages()
    now = datetime.now(timezone.utc)
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
        row = {
            "name": a,
            "role": roles.get(a),
            "cursor": store.cursor(a) or None,
            "unread": len(store.unread_for(a)),
            "heartbeat": heartbeat_iso,
            "last_seen_seconds": (round(last_seen_s, 3)
                                  if last_seen_s is not None else None),
            "stale": stale,
            "waiting": waiting,
            "waiting_stale": waiting_stale,
        }
        if a == liaison:
            row["operator_facing"] = True  # additive: absent unless set
        agents.append(row)
    invalid = store.list_invalid_messages()
    quarantined = store.quarantined_count()
    signing_enforced = store.signing_enforced()
    # project_id is path-derived; surfaces here for diagnostics
    project_id = store.project_id()
    payload = {
        "root": str(store.root),
        "session_id": cfg.get("session_id"),
        "project_id": project_id,
        "signing_enforced": signing_enforced,
        "message_count": len(msgs),
        "invalid_messages": [{"id": mid, "reason": reason} for mid, reason in invalid],
        "agents": agents,
        "stale_threshold_seconds": STALE_THRESHOLD_SECONDS,
        "warnings": _status_warnings(agents) + _thread_warnings(store, cfg),
    }
    if quarantined:
        payload["quarantined"] = quarantined  # additive: absent when zero
    if signing_enforced:
        health = _signing.inspect_key(project_id, store.root)
        payload["hmac_key"] = health.to_dict()
    return payload


def _status_warnings(agents: list[dict]) -> list[str]:
    """Actionable diagnostics derived from the per-agent status rows.

    Two issue-#5 footguns made visible:
    1. An agent with unread but a never-set cursor — it has been reading
       with plain `recv` (or not at all) and its read state is a lie.
    2. A soft-deadlock: two or more agents blocked in `wait` at the same
       time. In normal flow exactly one waits while the other works, so
       simultaneous live waiters means nobody is going to send next.
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
    live_waiters = sorted(
        (a["name"] for a in agents if a["waiting"] and not a["waiting_stale"])
    )
    if len(live_waiters) >= 2:
        with_unread = sorted(
            a["name"] for a in agents
            if a["waiting"] and not a["waiting_stale"] and a["unread"] > 0
        )
        msg = (
            f"soft-deadlock: {', '.join(live_waiters)} are all blocked in "
            f"`wait` simultaneously — nobody is positioned to send next."
        )
        if with_unread:
            verb = "has" if len(with_unread) == 1 else "have"
            msg += (
                f" {', '.join(with_unread)} already {verb} unread waiting: "
                f"run `agenttalk drain --for <agent>`, then reply."
            )
        else:
            msg += " Whoever owes the next reply should send it."
        warnings.append(msg)
    return warnings


def _closed_rids(store: Store, agent: str) -> set[str]:
    """request_ids ``agent`` has explicitly closed via `ack --to-request`."""
    return {
        rid for rid, e in store.read_threadstate(agent).items()
        if isinstance(e, dict) and e.get("closed") is True
    }


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
        role = f" role={a['role']}" if a.get("role") else ""
        of = " [operator-facing]" if a.get("operator_facing") else ""
        print(f"  {a['name']:<10}{role}{of} cursor={cursor:<32} unread={a['unread']:<3} {seen}")
    for w in payload.get("warnings", []):
        print(f"WARN:       {w}")
    return 0


def _format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds / 60)}min ago"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h ago"
    return f"{int(seconds / 86400)}d ago"


def cmd_send(args: argparse.Namespace) -> int:
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
    _warn_missing_request_id(args.kind, meta)
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
    if args.print_id:
        print(msg.id)
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
    msg = store.send(
        sender=sender,
        recipient=recipient,
        body=body,
        kind="composing",
        subject=args.subject or "composing",
        meta=meta,
    )
    if rid:
        store.write_composing_intent(sender, rid, recipient)  # best-effort
    if not args.quiet:
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
    if args.json:
        out = {"request_id": rid, "state": "current", "rescind": None}
        if epoch_obj is not None:
            out["epoch"] = epoch_obj
        print(json.dumps(out, indent=2))
    else:
        if exit_code == 0:
            print(f"current     {rid}")
            if epoch_obj is not None:
                print(f"  epoch: current ({epoch_obj['current_epoch'] or 'no barrier'})")
        elif epoch_obj["state"] == "previous-epoch":
            print(f"previous-epoch  {rid}")
            print(f"  this request predates the current global epoch "
                  f"({epoch_obj['current_epoch']}) — do NOT act on it; re-ask "
                  f"under the current barrier for irreversible actions.")
        else:  # unknown-pre-epoch
            print(f"pre-epoch   {rid}")
            print(f"  this opener predates epochs (no epoch_at_send) and a "
                  f"barrier exists ({epoch_obj['current_epoch']}) — do NOT act; "
                  f"re-ask under the current barrier for irreversible actions.")
    return exit_code


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
        target = store.operator_facing()
        if target is None:
            raw = store.operator_facing_raw()
            if raw:
                sys.stderr.write(
                    f"agenttalk escalate: configured liaison {raw!r} is not in "
                    f"the roster {sorted(roster)} — fix it with `agenttalk "
                    f"roster set-operator-facing <agent>` (or --clear), or "
                    f"pass --to <agent> explicitly.\n"
                )
            else:
                sys.stderr.write(
                    "agenttalk escalate: no operator-facing agent is "
                    "configured — run `agenttalk roster set-operator-facing "
                    "<agent>`, or pass --to <agent> explicitly.\n"
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
    if "request_id" not in meta:
        meta["request_id"] = "esc-" + uuid.uuid4().hex[:12]
    msg = store.send(
        sender=sender,
        recipient=target,
        body=body,
        kind="question",
        subject=args.subject or "operator input needed",
        meta=meta,
    )
    if not args.quiet:
        print(render(msg, header=f"AGENTTALK :: ESCALATE  {sender} -> {target}"))
    # Always print the machine-parseable correlation line: the caller's
    # next move is `agenttalk wait --to-request <this>`.
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
                or args.kind != "message" or args.meta):
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


def cmd_roster(args: argparse.Namespace) -> int:
    """View or manage the roster, roles, and groups.

    `roster` (no subcommand) shows the team; `add` / `remove` /
    `set-role` / `set-group` are deliberate local admin ops (not a
    security boundary, not process supervision).
    """
    store = _get_store(args)
    action = getattr(args, "roster_cmd", None)
    if action == "add":
        store.add_agent(args.name, role=getattr(args, "role", None),
                        groups=getattr(args, "group", None))
        print(f"roster: added {args.name}")
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
        store.set_role(args.name, args.role)
        print(f"roster: {args.name} role={args.role}")
        return 0
    if action == "set-group":
        members = [m.strip() for m in args.members.split(",") if m.strip()]
        store.set_group(args.group, members)
        print(f"roster: group '{args.group}' = {', '.join(members) or '(empty)'}")
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

    # show
    cfg = store.load_config()
    roster = cfg.get("agents", []) or []
    roles = cfg.get("roles", {}) or {}
    groups = cfg.get("groups", {}) or {}
    liaison = store.operator_facing()
    self_name = os.environ.get("AGENTTALK_SELF")
    if self_name not in roster:
        self_name = None
    if getattr(args, "json", False):
        print(json.dumps({
            "agents": roster,
            "roles": roles,
            "groups": groups,
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
        print(f"  {a}{you}  role={role}  groups=[{gl}]{of}")
    if groups:
        print("groups:")
        for g, ms in groups.items():
            print(f"  @{g}: {', '.join(ms) or '(empty)'}")
    print(f"  @all: {', '.join(roster)}  (implicit)")
    return 0


def _do_recv(
    store: Store,
    agent: str,
    *,
    since: str | None,
    ack: bool,
    include_control: bool,
    quiet: bool,
    emit_hint: bool,
) -> int:
    """Shared inbox-print path behind both `recv` and `drain`.

    `drain` is exactly `recv --ack` with the hint suppressed, so the
    two can never diverge (issue #5 constraint). `--ack` advances the
    cursor past the newest message INCLUDING hidden control-plane kinds
    (composing) even when nothing visible was printed — that's what
    lets `drain` clear a stale-control/cursor backlog.
    """
    cursor = since if since is not None else store.cursor(agent)
    msgs = store.messages_for(agent, since_id=cursor or None)
    # Hide control-plane kinds (composing) from the default view — they
    # are wait-loop signals, not agent content. --include-control opts
    # back in for debugging.
    visible = msgs if include_control else [m for m in msgs if m.kind not in CONTROL_KINDS]
    if not visible:
        if not quiet:
            print(f"(no new messages for {agent})")
        if ack and msgs:
            store.advance_cursor(agent, msgs[-1].id)
        return 0
    for m in visible:
        print(render(m, header=f"AGENTTALK :: INBOX  {m.sender} -> {m.recipient}"))
    if ack:
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


def cmd_recv(args: argparse.Namespace) -> int:
    store = _get_store(args)
    agent = _resolve_self(args.agent, roster=store.load_config().get("agents") or [])
    return _do_recv(
        store,
        agent,
        since=args.since,
        ack=args.ack,
        include_control=args.include_control,
        quiet=args.quiet,
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
    return _do_recv(
        store,
        agent,
        since=None,  # drain always consumes from the cursor forward
        ack=True,
        include_control=args.include_control,
        quiet=args.quiet,
        emit_hint=False,  # drain IS the remedy; never hint
    )


def _write_waiting_marker(
    store: Store, agent: str, *, cursor_at_start: str, timeout: float,
    deadline: float | None,
) -> None:
    """Best-effort write of the observational `.waiting` marker.

    Records who is blocked, since when, on what cursor, and (for a
    bounded wait) the current epoch deadline so `status` can tell a
    live wait from an orphaned file left by a crashed shell. Any write
    failure is swallowed — this is diagnostics, never correctness.
    """
    try:
        store.write_waiting(agent, {
            "agent": agent,
            "pid": os.getpid(),
            "since": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "cursor_at_start": cursor_at_start or "",
            "timeout_seconds": timeout,
            # epoch seconds; None when --timeout 0 (waits forever). Updated
            # in place when composing pings push the deadline out.
            "deadline_epoch": deadline,
        })
    except OSError:
        pass


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


def _scoped_wait(store: Store, agent: str, args: argparse.Namespace) -> int:
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
    if heartbeat_interval > 0:
        store.write_heartbeat(agent)
        last_heartbeat = time.time()
    _write_waiting_marker(
        store, agent, cursor_at_start=store.thread_seen(agent, rid),
        timeout=args.timeout, deadline=deadline,
    )
    try:
        while True:
            # Floor delivery at BOTH the per-thread seen pointer AND the
            # global cursor: a message already consumed globally (a `drain`
            # or plain `wait` advanced the cursor past it) must NOT be
            # re-surfaced by a scoped wait — otherwise, e.g., after draining
            # and answering a needs-info, `wait --to-request` would re-show
            # that old needs-info instead of awaiting the next reply. On a
            # match we still advance ONLY the thread pointer (never the
            # global cursor), so scoped wait stays non-consuming.
            floor = max(store.thread_seen(agent, rid), store.cursor(agent))
            match = None
            for m in store.messages_for(agent):
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
            time.sleep(interval)
    finally:
        store.clear_waiting(agent)


def cmd_wait(args: argparse.Namespace) -> int:
    store = _get_store(args)
    agent = _resolve_self(args.agent, roster=store.load_config().get("agents") or [])
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
    # still visible).
    _foreign = store.foreign_wait_pid(agent, os.getpid(), now=time.time(),
                                      stale_after=STALE_THRESHOLD_SECONDS)
    if _foreign is not None:
        sys.stderr.write(
            f"warning: another live process (PID {_foreign}) is already "
            f"waiting as {agent!r} in this store. One window per agent is "
            f"assumed; concurrent same-agent use can lose cursor/threadstate "
            f"updates.\n")
    if getattr(args, "to_request", None):
        return _scoped_wait(store, agent, args)
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
    # Stamp once up front so peers see the listener immediately.
    if heartbeat_interval > 0:
        store.write_heartbeat(agent)
        last_heartbeat = time.time()
    # Observational waiting marker: lets `status` flag two agents that
    # are blocked on each other (issue #5 soft-deadlock). Cleared in the
    # finally below so a normal exit (message/timeout) never leaves it,
    # and a crashed shell's orphan is detectable via heartbeat/deadline.
    _write_waiting_marker(
        store, agent, cursor_at_start=cursor_at_start,
        timeout=args.timeout, deadline=deadline,
    )
    try:
        while True:
            msgs = store.messages_for(agent, since_id=cursor_at_start or None)
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
            time.sleep(interval)
    finally:
        # Always retract the marker — message received, timeout, or the
        # KeyboardInterrupt that main() catches one frame up. Best-effort.
        store.clear_waiting(agent)


def cmd_ack(args: argparse.Namespace) -> int:
    store = _get_store(args)
    agent = _resolve_self(args.agent, roster=store.load_config().get("agents") or [])
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


def _render_doctor_human(report) -> None:
    # Root is the FIRST line (0.14.0, #13) — same contract as whoami: the
    # wrong-root footgun must be diagnosable from line one.
    print(f"root: {report.project_root}")
    print("agenttalk doctor")
    print(f"  agenttalk version  {report.agenttalk_version}")
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


def cmd_install_skills(args: argparse.Namespace) -> int:
    claude = not args.codex_only
    codex = not args.claude_only
    if not claude and not codex:
        sys.stderr.write("agenttalk install-skills: nothing to do (both sides excluded)\n")
        return 2

    claude_dir = Path(args.claude_dir) if args.claude_dir else None
    codex_dir = Path(args.codex_dir) if args.codex_dir else None

    res = iskl.install(
        claude=claude,
        codex=codex,
        claude_dir=claude_dir,
        codex_dir=codex_dir,
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
    # Auto-echo request_id for correlation. Explicit --meta wins.
    # EXCEPTION: a reply that is ITSELF a thread-opening kind
    # (review-request = counter-review; proposal = counter-proposal)
    # opens a NEW correlation thread, so it must NOT inherit the anchor's
    # request_id — doing so would alias two distinct request/response
    # pairs and make later responses ambiguous. For those we skip the
    # echo and let _maybe_autogen_request_id mint a fresh id below
    # (unless the user passed an explicit --meta one).
    if (
        kind not in ("review-request", "proposal")
        and "request_id" not in meta
        and "request_id" in (anchor.meta or {})
    ):
        meta["request_id"] = anchor.meta["request_id"]
    _maybe_autogen_request_id(kind, meta, quiet=args.quiet)
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
    msg = store.send(
        sender=sender,
        recipient=anchor.sender,
        body=body,
        kind=kind,
        subject=args.subject or "",
        meta=meta,
    )
    if not args.quiet:
        print(render(msg, header=f"AGENTTALK :: REPLY  {msg.sender} -> {msg.recipient}"))
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
                               extra=extra)
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


# --------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agenttalk",
        description="File-backed message bus for two agent CLIs.",
    )
    p.add_argument("--version", action="version", version=f"agenttalk {__version__}")
    p.add_argument("--root",
                   help="Project root. Resolution precedence: this flag > "
                        "$AGENTTALK_ROOT > walk up from CWD looking for "
                        ".agenttalk/. A pinned root (flag or env) that has no "
                        "store fails loudly — it never falls back to the walk.")
    sub = p.add_subparsers(dest="cmd", required=True)

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
    proster.set_defaults(func=cmd_roster, roster_cmd=None)
    rsub = proster.add_subparsers(dest="roster_cmd")
    r_add = rsub.add_parser("add", help="Add an agent (idempotent).")
    r_add.add_argument("name")
    r_add.add_argument("--role", help="Role label (e.g. implementer, reviewer, lead).")
    r_add.add_argument("--group", action="append",
                       help="Add the agent to this group (repeatable).")
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
    pse.add_argument("--print-id", action="store_true", help="Print the new message id on its own line")
    pse.add_argument("--quiet", action="store_true")
    pse.set_defaults(func=cmd_send)

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
    pchk.add_argument("--json", action="store_true",
                      help='{"request_id", "state", "rescind"} — stable contract.')
    pchk.set_defaults(func=cmd_check)

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
        help="Route an operator-input question to the operator-facing agent "
             "(the liaison). Mints an esc- request_id (printed as "
             "`request_id=<id>` for the follow-up `wait --to-request`). "
             "Refuses (exit 2) when no liaison is configured — pass --to "
             "to override explicitly.",
    )
    pesc.add_argument("--from", dest="sender",
                      help="Sender agent name (default: $AGENTTALK_SELF)")
    pesc.add_argument("--to",
                      help="Explicit target override (default: the roster's "
                           "operator-facing agent).")
    pesc.add_argument("--subject",
                      help="One-line summary (default: 'operator input needed')")
    pesc.add_argument("--meta", action="append", help="key=value (repeatable)")
    pesc.add_argument("-m", "--message",
                      help="The operator question (else --file or stdin). Required.")
    pesc.add_argument("--file", help="Read body from this file path ('-' = stdin)")
    pesc.add_argument("--quiet", action="store_true",
                      help="Print only the request_id line.")
    pesc.set_defaults(func=cmd_escalate)

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
    pr.add_argument("--quiet", action="store_true")
    pr.add_argument("--include-control", action="store_true",
                    help="Also surface control-plane kinds ('composing') that the "
                         "default view hides. Useful for debugging wait extensions.")
    pr.set_defaults(func=cmd_recv)

    pdr = sub.add_parser(
        "drain",
        help="Consume the inbox: print all unread for an agent AND advance "
             "the cursor to the newest message. Equivalent to `recv --ack`, "
             "but unmissable — use this instead of hand-rolled polling.",
    )
    pdr.add_argument("--for", dest="agent", help="Agent name (default: $AGENTTALK_SELF)")
    pdr.add_argument("--quiet", action="store_true")
    pdr.add_argument("--include-control", action="store_true",
                     help="Also surface control-plane kinds ('composing') that the "
                          "default view hides. The cursor advances past them either way.")
    pdr.set_defaults(func=cmd_drain)

    pw = sub.add_parser("wait", help="Block until a new message arrives for an agent, then print it.")
    pw.add_argument("--for", dest="agent", help="Agent name (default: $AGENTTALK_SELF)")
    pw.add_argument("--timeout", type=float, default=120.0, help="Seconds to wait (0 = forever, default 120)")
    pw.add_argument("--interval", type=float, default=0.3, help="Poll interval in seconds (default 0.3)")
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
                         "immediately; do not act on it).")
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
    prpl.add_argument("--allow-empty", action="store_true")
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

    pis = sub.add_parser(
        "install-skills",
        help="Copy bundled agenttalk skill files to ~/.claude/commands/ and ~/.codex/skills/.",
    )
    pis.add_argument("--claude-only", action="store_true", help="Install only Claude-side skills")
    pis.add_argument("--codex-only", action="store_true", help="Install only Codex-side skills")
    pis.add_argument("--claude-dir", help="Override Claude commands dir (default: ~/.claude/commands)")
    pis.add_argument("--codex-dir", help="Override Codex skills dir (default: ~/.codex/skills)")
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
    # On Windows the default console code page (cp1252) can't encode many
    # characters that turn up in agent messages (arrows, em-dashes, etc.).
    # Force UTF-8 on stdout/stderr so writes don't raise UnicodeEncodeError.
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
    try:
        return args.func(args)
    except KeyboardInterrupt:
        sys.stderr.write("\nagenttalk: interrupted\n")
        return 130
    except (ValueError, FileNotFoundError, OSError) as e:
        sys.stderr.write(f"agenttalk: {e}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
