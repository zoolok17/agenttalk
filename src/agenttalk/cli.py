"""agenttalk CLI: init, send, wait, recv, ack, transcript, end, status."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from agenttalk.display import render
from agenttalk.store import Message, Store, find_root
from agenttalk import transcript as tx
from agenttalk import codex_config as cxc


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
            raise SystemExit(f"--meta expects key=value, got {item!r}")
        k, v = item.split("=", 1)
        out[k.strip()] = v.strip()
    return out


# ------------------------------------------------------------------- handlers

def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve() if args.path else Path.cwd().resolve()
    store = Store(root)
    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    if len(agents) < 2:
        sys.stderr.write("agenttalk init: need at least two agents (e.g. --agents claude,codex)\n")
        return 2
    cfg = store.init(agents, force=args.force)
    print(f"agenttalk initialized at {store.dir}")
    print(f"  agents:     {', '.join(cfg['agents'])}")
    print(f"  session_id: {cfg['session_id']}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = _get_store(args)
    cfg = store.load_config()
    msgs = store.all_messages()
    print(f"root:       {store.root}")
    print(f"session_id: {cfg.get('session_id')}")
    print(f"agents:     {', '.join(cfg.get('agents', []))}")
    print(f"messages:   {len(msgs)}")
    for a in cfg.get("agents", []):
        unread = len(store.unread_for(a))
        print(f"  {a:<10} cursor={store.cursor(a) or '(none)':<32} unread={unread}")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    store = _get_store(args)
    body = _read_body(args)
    if not body and not args.allow_empty:
        sys.stderr.write("agenttalk send: empty body (use -m TEXT, --file PATH, pipe stdin, or --allow-empty)\n")
        return 2
    meta = _parse_meta(args.meta)
    msg = store.send(
        sender=args.sender,
        recipient=args.recipient,
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


def cmd_recv(args: argparse.Namespace) -> int:
    store = _get_store(args)
    cursor = args.since if args.since is not None else store.cursor(args.agent)
    msgs = store.messages_for(args.agent, since_id=cursor or None)
    if not msgs:
        if not args.quiet:
            print(f"(no new messages for {args.agent})")
        return 0
    for m in msgs:
        print(render(m, header=f"AGENTTALK :: INBOX  {m.sender} -> {m.recipient}"))
    if args.ack:
        store.advance_cursor(args.agent, msgs[-1].id)
    return 0


def cmd_wait(args: argparse.Namespace) -> int:
    store = _get_store(args)
    deadline = time.time() + args.timeout if args.timeout > 0 else None
    interval = max(0.1, args.interval)
    cursor_at_start = store.cursor(args.agent)
    while True:
        msgs = store.messages_for(args.agent, since_id=cursor_at_start or None)
        if msgs:
            m = msgs[0]
            print(render(m, header=f"AGENTTALK :: RECEIVED  {m.sender} -> {m.recipient}"))
            if args.ack:
                store.advance_cursor(args.agent, m.id)
            return 0
        if deadline is not None and time.time() >= deadline:
            if not args.quiet:
                print(f"(timeout: no new messages for {args.agent} in {args.timeout}s)")
            return 1
        time.sleep(interval)


def cmd_ack(args: argparse.Namespace) -> int:
    store = _get_store(args)
    if args.id:
        store.advance_cursor(args.agent, args.id)
    else:
        msgs = store.messages_for(args.agent)
        if msgs:
            store.advance_cursor(args.agent, msgs[-1].id)
    print(f"cursor[{args.agent}] = {store.cursor(args.agent) or '(none)'}")
    return 0


def cmd_transcript(args: argparse.Namespace) -> int:
    store = _get_store(args)
    out = Path(args.out).resolve() if args.out else None
    path = tx.export(store, fmt=args.format, out=out)
    print(str(path))
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


def cmd_end(args: argparse.Namespace) -> int:
    store = _get_store(args)
    cfg = store.load_config()
    others = [a for a in cfg.get("agents", []) if a != args.sender]
    if not others:
        sys.stderr.write("agenttalk end: no other agents registered\n")
        return 2
    body = args.reason or "session ended"
    for other in others:
        store.send(
            sender=args.sender,
            recipient=other,
            body=body,
            kind="end",
        )
    out = tx.export(store, fmt="md")
    print(f"agenttalk: ended session, transcript at {out}")
    return 0


# --------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agenttalk", description="File-backed message bus for two agent CLIs.")
    p.add_argument("--root", help="Project root (default: walk up from CWD looking for .agenttalk/)")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="Initialize a fresh .agenttalk/ store in the current dir.")
    pi.add_argument("--agents", default="claude,codex", help="Comma-separated agent names (default: claude,codex)")
    pi.add_argument("--path", help="Directory to init (default: CWD)")
    pi.add_argument("--here", action="store_true", help="(alias for --path .)")
    pi.add_argument("--force", action="store_true", help="Overwrite existing config")
    pi.set_defaults(func=cmd_init)

    ps = sub.add_parser("status", help="Show roster, message count, per-agent cursor + unread.")
    ps.set_defaults(func=cmd_status)

    pse = sub.add_parser("send", help="Send a message from one agent to another.")
    pse.add_argument("--from", dest="sender", required=True, help="Sender agent name")
    pse.add_argument("--to", dest="recipient", required=True, help="Recipient agent name")
    pse.add_argument("--kind", default="message", help="message | review-request | review-result | ack | note | end")
    pse.add_argument("--subject", help="One-line summary")
    pse.add_argument("--meta", action="append", help="key=value (repeatable)")
    pse.add_argument("-m", "--message", help="Body text (else --file or stdin)")
    pse.add_argument("--file", help="Read body from this file path")
    pse.add_argument("--allow-empty", action="store_true")
    pse.add_argument("--print-id", action="store_true", help="Print the new message id on its own line")
    pse.add_argument("--quiet", action="store_true")
    pse.set_defaults(func=cmd_send)

    pr = sub.add_parser("recv", help="Print all queued messages for an agent.")
    pr.add_argument("--for", dest="agent", required=True)
    pr.add_argument("--since", help="Only messages with id > this (default: agent cursor)")
    pr.add_argument("--ack", action="store_true", help="Advance cursor past the last shown msg")
    pr.add_argument("--quiet", action="store_true")
    pr.set_defaults(func=cmd_recv)

    pw = sub.add_parser("wait", help="Block until a new message arrives for an agent, then print it.")
    pw.add_argument("--for", dest="agent", required=True)
    pw.add_argument("--timeout", type=float, default=120.0, help="Seconds to wait (0 = forever, default 120)")
    pw.add_argument("--interval", type=float, default=0.3, help="Poll interval in seconds (default 0.3)")
    pw.add_argument("--ack", action="store_true", default=True, help="Advance cursor past the received msg (default true)")
    pw.add_argument("--no-ack", dest="ack", action="store_false")
    pw.add_argument("--quiet", action="store_true")
    pw.set_defaults(func=cmd_wait)

    pa = sub.add_parser("ack", help="Advance an agent's cursor (defaults to latest message).")
    pa.add_argument("--for", dest="agent", required=True)
    pa.add_argument("--id", help="Specific message id (default: latest message for this agent)")
    pa.set_defaults(func=cmd_ack)

    pt = sub.add_parser("transcript", help="Export the full conversation.")
    pt.add_argument("--format", choices=["md", "jsonl"], default="md")
    pt.add_argument("--out", help="Output path (default: .agenttalk/sessions/transcript-<session>.<ext>)")
    pt.set_defaults(func=cmd_transcript)

    pe = sub.add_parser("end", help="Send an 'end' message to the other agent(s) and export the transcript.")
    pe.add_argument("--from", dest="sender", required=True)
    pe.add_argument("--reason", help="Free-text reason")
    pe.set_defaults(func=cmd_end)

    pc = sub.add_parser(
        "codex-config",
        help="Manage the per-project block in ~/.codex/config.toml so Codex can call agenttalk from inside its sandbox.",
    )
    grp = pc.add_mutually_exclusive_group()
    grp.add_argument("--enable", action="store_true", default=True, help="Add/update approval_policy and sandbox_mode (default)")
    grp.add_argument("--disable", action="store_true", help="Remove approval_policy and sandbox_mode (keeps trust_level)")
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
