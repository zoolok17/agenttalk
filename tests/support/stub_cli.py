#!/usr/bin/env python
"""Deterministic NO-MODEL stub "claude" CLI for the enforcement canary (task #34).

This process stands in for a real ``claude -p --output-format stream-json`` child.
The wrapper's REAL spawn path (:class:`agenttalk.wrapper.run._ProcStream`) launches
it as ``[sys.executable, stub_cli.py, <per-turn claude flags...>]`` with the per-turn
prompt on STDIN, and reads our STDOUT as claude stream-json.

It is intentionally dependency-free (stdlib only) and emits event shapes the real
``agenttalk.wrapper.claude_adapter`` accepts:

  * ``{"type":"stream_event","event":{"type":"message_start"}}``  -> TURN_STARTED
  * ``{"type":"assistant","message":{"content":[{"type":"text",...}]}}`` -> MODEL_OUTPUT
  * ``{"type":"stream_event","event":{"type":"message_stop"}}``   -> TURN_FINISHED
  * ``{"type":"result","subtype":"success",...}``                 -> terminal summary (ok)
  * ``{"type":"result","subtype":"error_during_execution",...}``  -> terminal ADAPTER_ERROR

Behavior is selected by the ``AGENTTALK_STUB_SCENARIO`` env var. When it performs a
bus reply it invokes the REAL agenttalk CLI against the SAME store (root from
``AGENTTALK_ROOT``, python from ``AGENTTALK_PY``/``sys.executable``), shell=False,
cross-platform.
"""

from __future__ import annotations

import json
import os
import re
import subprocess  # nosec B404 - runs only the pinned agenttalk bus CLI, shell=False
import sys


def _reconfigure_utf8() -> None:
    """Survive Windows cp1252: force UTF-8 on stdout/stderr (mirrors agentchat)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _emit_start() -> None:
    _emit({"type": "stream_event", "event": {"type": "message_start"}})


def _emit_assistant_text(text: str) -> None:
    _emit({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})


def _emit_stop() -> None:
    _emit({"type": "stream_event", "event": {"type": "message_stop"}})


def _emit_success(session_id: str | None) -> None:
    _emit({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 0,
        "num_turns": 1,
        "session_id": session_id or "",
        "total_cost_usd": 0,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    })


def _flag_value(argv: list[str], flag: str) -> str | None:
    """Return the value following ``flag`` in argv, or None."""
    for idx, token in enumerate(argv):
        if token == flag and idx + 1 < len(argv):
            return argv[idx + 1]
        if token.startswith(flag + "="):
            return token.split("=", 1)[1]
    return None


def _read_prompt() -> str:
    try:
        data = sys.stdin.buffer.read()
    except (AttributeError, ValueError):
        return sys.stdin.read()
    return data.decode("utf-8", errors="replace")


def _fenced_json_blocks(prompt: str) -> list[dict]:
    """Parse every ```json ... ``` fenced block in the prompt into dicts."""
    blocks: list[dict] = []
    for match in re.finditer(r"```json\s*\n(.*?)\n```", prompt, re.DOTALL):
        try:
            blocks.append(json.loads(match.group(1)))
        except (ValueError, TypeError):
            continue
    return blocks


def _owed_action(prompt: str) -> dict | None:
    """Find the wrapper-supplied owed_action transport (argv + draft_path)."""
    for block in _fenced_json_blocks(prompt):
        if isinstance(block, dict):
            if isinstance(block.get("argv"), list) and block.get("draft_path"):
                return block
            owed = block.get("owed_action")
            if isinstance(owed, dict) and isinstance(owed.get("argv"), list):
                return owed
    return None


def _request_id(prompt: str) -> str | None:
    m = re.search(r'"request_id":\s*"([^"]+)"', prompt)
    if m:
        return m.group(1)
    m = re.search(r"request_id=([^\s)\"']+)", prompt)
    return m.group(1) if m else None


def _self_agent(prompt: str) -> str | None:
    """The replying agent is the 'to:' field of the wrapper prompt header line
    (``from: X  to: Y  kind: ...``)."""
    m = re.search(r"to:\s*([A-Za-z0-9._-]+)\s+kind:", prompt)
    return m.group(1) if m else None


def _agenttalk_py() -> str:
    return os.environ.get("AGENTTALK_PY") or sys.executable


def _run_bus(argv: list[str]) -> int:
    """Run the real agenttalk bus CLI, shell=False, inheriting AGENTTALK_ROOT."""
    proc = subprocess.run(  # noqa: S603 - fixed argv, shell=False, pinned python
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        sys.stderr.write(f"[stub] bus command failed rc={proc.returncode}: {proc.stdout}\n")
        sys.stderr.flush()
    return proc.returncode


def _perform_reply(prompt: str, body: str) -> None:
    """Reply on the bus. Prefer the wrapper-supplied owed_action transport (which
    carries the operation-nonce that the commit gate requires); otherwise fall back
    to a plain ``reply --to-request`` for a non-commit-gated deployment."""
    owed = _owed_action(prompt)
    if owed is not None:
        draft_path = owed["draft_path"]
        with open(draft_path, "w", encoding="utf-8") as handle:
            handle.write(body)
        _run_bus([str(part) for part in owed["argv"]])
        return
    rid = _request_id(prompt)
    if rid is None:
        sys.stderr.write("[stub] no request_id found in prompt; cannot reply\n")
        return
    argv = [_agenttalk_py(), "-m", "agenttalk", "reply", "--to-request", rid, "-m", body]
    agent = _self_agent(prompt)
    if agent is not None:
        argv[4:4] = ["--from", agent]
    _run_bus(argv)


def _reply_draft_path(prompt: str) -> str | None:
    """The wrapper-declared freeform draft path (#201): the indented path line
    inside the PREFERRED DRAFT CHANNEL section."""
    m = re.search(
        r"PREFERRED DRAFT CHANNEL.*?\n {2}(\S.*?)\n",
        prompt,
        re.DOTALL,
    )
    return m.group(1).strip() if m else None


def _scenario_draft_only(prompt: str, session_id: str | None) -> int:
    """#201: a sandbox-blocked child — writes the wrapper-declared reply draft
    with its 'structured write' (a plain file write here) and NEVER runs a bus
    command. The wrapper must deliver the draft itself."""
    _emit_start()
    draft = _reply_draft_path(prompt)
    if draft is None:
        _emit_assistant_text("No draft channel declared; cannot reply from this sandbox.")
        _emit_stop()
        _emit_success(session_id)
        return 0
    with open(draft, "w", encoding="utf-8") as handle:
        handle.write("result=399 via wrapper-owned draft delivery\n")
    _emit_assistant_text("Wrote the answer to the declared reply draft; ending turn.")
    _emit_stop()
    _emit_success(session_id)
    return 0


def _scenario_reply_ok(prompt: str, session_id: str | None) -> int:
    _emit_start()
    _emit_assistant_text("Computed the answer (19 * 21 = 399); replying on the bus.")
    _perform_reply(prompt, "result=399, stub online")
    _emit_stop()
    _emit_success(session_id)
    return 0


def _scenario_compute_no_reply(session_id: str | None) -> int:
    _emit_start()
    _emit_assistant_text(
        "Plan: I would compute 19 * 21 = 399 and reply, but I am NOT emitting the "
        "owed reply this turn."
    )
    _emit_stop()
    _emit_success(session_id)
    return 0


def _scenario_resume_missing(argv: list[str], prompt: str) -> int:
    resume_id = _flag_value(argv, "--resume")
    if resume_id is not None:
        # Reproduce the observed real claude failure shape: a plain-text diagnostic
        # line (which the stream-json adapter discards) then a terminal error result.
        sys.stdout.write(f"No conversation found with session ID: {resume_id}\n")
        sys.stdout.flush()
        _emit({
            "type": "result",
            "subtype": "error_during_execution",
            "duration_ms": 0,
            "is_error": True,
            "num_turns": 0,
            "stop_reason": None,
            "session_id": resume_id,
            "total_cost_usd": 0,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        })
        return 1
    # Fresh --session-id turn: behave like reply_ok.
    return _scenario_reply_ok(prompt, _flag_value(argv, "--session-id"))


def main(argv: list[str]) -> int:
    _reconfigure_utf8()
    prompt = _read_prompt()
    scenario = os.environ.get("AGENTTALK_STUB_SCENARIO", "reply_ok")
    session_id = _flag_value(argv, "--session-id") or _flag_value(argv, "--resume")
    if scenario == "reply_ok":
        return _scenario_reply_ok(prompt, session_id)
    if scenario == "draft_only":
        return _scenario_draft_only(prompt, session_id)
    if scenario == "compute_no_reply":
        return _scenario_compute_no_reply(session_id)
    if scenario == "resume_missing":
        return _scenario_resume_missing(argv, prompt)
    sys.stderr.write(f"[stub] unknown scenario {scenario!r}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
