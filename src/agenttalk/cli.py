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
    CONTROL_KINDS,
    Store,
    find_root,
    validate_agent_name,
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
    if getattr(args, "file", None):
        return Path(args.file).read_text(encoding="utf-8")
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


# ------------------------------------------------------------------- handlers

def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve() if args.path else Path.cwd().resolve()
    store = Store(root)
    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    if len(agents) < 2:
        sys.stderr.write("agenttalk init: need at least two agents (e.g. --agents claude,codex)\n")
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
        agents.append({
            "name": a,
            "cursor": store.cursor(a) or None,
            "unread": len(store.unread_for(a)),
            "heartbeat": heartbeat_iso,
            "last_seen_seconds": (round(last_seen_s, 3)
                                  if last_seen_s is not None else None),
            "stale": stale,
            "waiting": waiting,
            "waiting_stale": waiting_stale,
        })
    invalid = store.list_invalid_messages()
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
    out: list[str] = []
    for a in roster:
        rows = th.derive_threads(msgs, agent=a, cursor=store.cursor(a), now=now)
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
        ]
        if stale:
            t0 = stale[0]
            out.append(
                f"{a}: outbound {t0.opener_kind} {t0.request_id} still "
                f"awaiting {t0.peer} after {_format_age(t0.age_seconds)} — "
                f"peer may have missed it; check `agenttalk threads --for {a}`."
            )
    return out


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
    )
    cnts = th.counts(all_rows)
    shown = all_rows if args.all else [t for t in all_rows if t.state in th.ACTIONABLE_STATES]

    if args.json:
        print(json.dumps({
            "agent": agent,
            "threads": [t.to_dict() for t in shown],
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
    }
    for t in shown:
        age = _format_age(t.age_seconds) if t.age_seconds is not None else "?"
        flag = " (unread)" if t.unread else ""
        subj = f'  "{t.subject}"' if t.subject else ""
        print(
            f"  [{label.get(t.state, t.state)}] {t.request_id:<16} "
            f"{t.opener_kind:<15} peer={t.peer:<10} age={age:<8}{subj}{flag}"
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
              f"(see `agenttalk status --json` for details under invalid_messages[])")
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
        print(f"  {a['name']:<10} cursor={cursor:<32} unread={a['unread']:<3} {seen}")
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
    sender = _resolve_self(args.sender, roster=roster)
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
    recipient = _resolve_peer(args.recipient, cfg, sender)
    body = args.message or "still drafting — please hold the line"
    meta = _parse_meta(args.meta)
    msg = store.send(
        sender=sender,
        recipient=recipient,
        body=body,
        kind="composing",
        subject=args.subject or "composing",
        meta=meta,
    )
    if not args.quiet:
        print(f"(composing ping sent: {sender} -> {recipient}; id={msg.id})")
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


def cmd_wait(args: argparse.Namespace) -> int:
    store = _get_store(args)
    agent = _resolve_self(args.agent, roster=store.load_config().get("agents") or [])
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
    if args.id:
        store.advance_cursor(agent, args.id)
    else:
        msgs = store.messages_for(agent)
        if msgs:
            store.advance_cursor(agent, msgs[-1].id)
    print(f"cursor[{agent}] = {store.cursor(agent) or '(none)'}")
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
    print(f"agenttalk doctor — {report.project_root}")
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
    body = _read_body(args)
    if not body and not args.allow_empty:
        sys.stderr.write("agenttalk reply: empty body (use -m TEXT, --file PATH, pipe stdin, or --allow-empty)\n")
        return 2
    meta = _parse_meta(args.meta)
    # Auto-echo request_id for correlation. Explicit --meta wins.
    # EXCEPTION: a reply that is ITSELF a thread-opening kind
    # (review-request = counter-review; proposal = counter-proposal)
    # opens a NEW correlation thread, so it must NOT inherit the anchor's
    # request_id — doing so would alias two distinct request/response
    # pairs and make later responses ambiguous. For those we skip the
    # echo and let _maybe_autogen_request_id mint a fresh id below
    # (unless the user passed an explicit --meta one).
    if (
        args.kind not in ("review-request", "proposal")
        and "request_id" not in meta
        and "request_id" in (anchor.meta or {})
    ):
        meta["request_id"] = anchor.meta["request_id"]
    _maybe_autogen_request_id(args.kind, meta, quiet=args.quiet)
    _warn_missing_request_id(args.kind, meta)
    msg = store.send(
        sender=sender,
        recipient=anchor.sender,
        body=body,
        kind=args.kind,
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
    roster = cfg.get("agents") or []
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
    """Start the read-only local web dashboard."""
    from agenttalk import web as _web
    store = _get_store(args)
    try:
        srv = _web.make_server(store, args.host, args.port, quiet=args.quiet)
    except ValueError as e:
        sys.stderr.write(f"agenttalk serve: {e}\n")
        return 2
    actual_port = srv.server_address[1]
    url = _web._format_url(args.host, actual_port)
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
    p.add_argument("--root", help="Project root (default: walk up from CWD looking for .agenttalk/)")
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
    pse.add_argument("--file", help="Read body from this file path")
    pse.add_argument("--allow-empty", action="store_true")
    pse.add_argument("--print-id", action="store_true", help="Print the new message id on its own line")
    pse.add_argument("--quiet", action="store_true")
    pse.set_defaults(func=cmd_send)

    pcomp = sub.add_parser(
        "composing",
        help="Send a 'composing' ping to the peer to extend their `wait` "
             "deadline. Use periodically while drafting a long reply so a "
             "240s default timeout doesn't fire mid-thought.",
    )
    pcomp.add_argument("--from", dest="sender",
                       help="Sender agent name (default: $AGENTTALK_SELF)")
    pcomp.add_argument("--to", dest="recipient",
                       help="Recipient agent name (default: $AGENTTALK_PEER, "
                            "or the single other agent in the roster)")
    pcomp.add_argument("--subject",
                       help="One-line summary (default: 'composing')")
    pcomp.add_argument("--meta", action="append",
                       help="key=value (repeatable); e.g. request_id=<id> to "
                            "correlate with the pending request")
    pcomp.add_argument("-m", "--message",
                       help="Body text (default: 'still drafting — please hold "
                            "the line')")
    pcomp.add_argument("--quiet", action="store_true")
    pcomp.set_defaults(func=cmd_composing)

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
    ppro.add_argument("--file", help="Read body from this file path")
    ppro.add_argument("--in-reply-to", dest="in_reply_to",
                      help="request_id of a prior proposal this one counters "
                           "(sets meta.in_reply_to; the prior proposal should "
                           "also get a proposal-response status=countered).")
    ppro.add_argument("--print-id", action="store_true",
                      help="Print the proposal's correlation id (request_id) "
                           "on its own line — the token a counter references.")
    ppro.add_argument("--quiet", action="store_true")
    ppro.set_defaults(func=cmd_propose)

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
    pw.set_defaults(func=cmd_wait)

    pa = sub.add_parser("ack", help="Advance an agent's cursor (defaults to latest message).")
    pa.add_argument("--for", dest="agent", help="Agent name (default: $AGENTTALK_SELF)")
    pa.add_argument("--id", help="Specific message id (default: latest message for this agent)")
    pa.set_defaults(func=cmd_ack)

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
    prpl.add_argument("--kind", default="message",
                      help="Message kind (default: message; see `agenttalk send --help`)")
    prpl.add_argument("--subject", help="One-line summary")
    prpl.add_argument("--meta", action="append",
                      help="key=value (repeatable); request_id is auto-echoed if not set")
    prpl.add_argument("-m", "--message", help="Body text (else --file or stdin)")
    prpl.add_argument("--file", help="Read body from this file path")
    prpl.add_argument("--allow-empty", action="store_true")
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
    psv.set_defaults(func=cmd_serve)

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
