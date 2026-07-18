"""Deterministic subprocess used by the detection-grade enforcement canary.

The wrapper launches this file as if it were the Codex CLI.  It emits the small
JSONL event subset consumed by the real Codex adapter and either executes the
exact owed-action transport from the prompt or merely prints prose.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess  # nosec B404 - the canary executes the gate-pinned argv only
import sys


CONTROL_ENV = "AGENTTALK_ENFORCEMENT_CANARY_CONTROL"
OWED_MARKER = "== OWED ACTION TRANSPORT =="
BUS_TIMEOUT_SECONDS = 15.0


def _emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False), flush=True)


def _owed_action(prompt: str) -> dict | None:
    if OWED_MARKER not in prompt:
        return None
    section = prompt.rsplit(OWED_MARKER, 1)[1]
    fenced = section.split("```json", 1)
    if len(fenced) != 2:
        raise ValueError("owed-action JSON fence is absent")
    payload = fenced[1].split("```", 1)[0]
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("owed-action transport is not an object")
    return value


def _append_trace(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _cursor(control: dict) -> str:
    path = (
        Path(str(control["root"]))
        / ".agenttalk"
        / "state"
        / f"{control['agent']}.cursor"
    )
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def _validated_reply_argv(control: dict, owed: dict) -> list[str]:
    raw = owed.get("argv")
    if not isinstance(raw, list):
        raise RuntimeError("owed-action argv is not a list")
    argv = [str(part) for part in raw]
    expected = [
        sys.executable,
        "-m",
        "agenttalk",
        "reply",
        "--from",
        str(control["agent"]),
        "--to-id",
        str(owed["exact_inbound_id"]),
        "--operation-nonce",
        str(owed["dispatch_nonce"]),
        "--file",
        str(owed["draft_path"]),
    ]
    same_executable = bool(argv) and os.path.normcase(
        str(Path(argv[0]).resolve())
    ) == os.path.normcase(str(Path(expected[0]).resolve()))
    if not same_executable or argv[1:] != expected[1:]:
        raise RuntimeError("owed-action transport is outside the canary allowlist")
    draft = Path(str(owed["draft_path"])).resolve()
    draft_root = (
        Path(str(control["root"]))
        / ".agenttalk"
        / "state"
        / "owed-action"
        / "drafts"
        / str(control["agent"])
    ).resolve()
    if draft_root not in draft.parents:
        raise RuntimeError("owed-action draft is outside the canary store")
    return argv


def main() -> int:
    configured = os.environ.get(CONTROL_ENV)
    if not configured:
        raise RuntimeError(f"{CONTROL_ENV} is required")
    control = json.loads(Path(configured).read_text(encoding="utf-8"))
    if not isinstance(control, dict):
        raise ValueError("canary control must be an object")
    mode = str(control.get("mode") or "")
    if mode not in {"compliant", "print_not_run"}:
        raise ValueError(f"unsupported canary mode: {mode!r}")

    prompt = sys.stdin.read()
    owed = _owed_action(prompt)
    trace_path = Path(str(control["trace_path"]))
    existing = (
        trace_path.read_text(encoding="utf-8").splitlines()
        if trace_path.exists()
        else []
    )
    invocation = len(existing) + 1
    command_attempted = False
    command_timed_out = False
    command_exit_code: int | None = None
    command_output = ""
    transport_allowlisted = False
    cursor_before = _cursor(control)

    _emit({"type": "thread.started", "thread_id": "enforcement-canary-thread"})
    _emit({"type": "turn.started"})

    if mode == "compliant":
        if owed is None:
            raise RuntimeError("compliant canary turn has no owed-action transport")
        argv = _validated_reply_argv(control, owed)
        transport_allowlisted = True
        draft_path = Path(str(owed["draft_path"]))
        draft_path.write_text(str(control["reply_body"]), encoding="utf-8")
        command_attempted = True
        try:
            command_result = subprocess.run(  # noqa: S603  # nosec B603
                argv,
                cwd=os.environ.get("AGENTTALK_ROOT"),
                env=os.environ.copy(),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=BUS_TIMEOUT_SECONDS,
            )
            command_exit_code = command_result.returncode
            command_output = (command_result.stdout or "") + (
                command_result.stderr or ""
            )
        except subprocess.TimeoutExpired:
            command_timed_out = True
            command_exit_code = 124
            command_output = (
                f"agenttalk reply timed out after {BUS_TIMEOUT_SECONDS:.0f} seconds"
            )
        _emit({
            "type": "item.completed",
            "item": {
                "id": f"command-{invocation}",
                "type": "command_execution",
                "command": argv,
                "aggregated_output": command_output,
                "exit_code": command_exit_code,
                "status": "failed" if command_exit_code else "completed",
            },
        })
        model_text = "Executed the reserved AgentTalk reply transport."
    else:
        model_text = "I would reply to the requester now."

    _emit({
        "type": "item.completed",
        "item": {
            "id": f"message-{invocation}",
            "type": "agent_message",
            "text": model_text,
        },
    })
    _emit({"type": "turn.completed", "usage": {"input_tokens": 0, "output_tokens": 0}})

    _append_trace(
        trace_path,
        {
            "argv": sys.argv[1:],
            "bus_exit_code": command_exit_code,
            "command_executed": command_attempted,
            "command_timed_out": command_timed_out,
            "cursor_after_child": _cursor(control),
            "cursor_before_child": cursor_before,
            "dispatch_nonce": owed.get("dispatch_nonce") if owed else None,
            "exact_inbound_id": owed.get("exact_inbound_id") if owed else None,
            "inbound_request_id": os.environ.get("AGENTTALK_INBOUND_REQUEST_ID"),
            "invocation": invocation,
            "mode": mode,
            "obligation_key_digest": (
                owed.get("obligation_key_digest") if owed else None
            ),
            "owed_transport_present": owed is not None,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "purpose": owed.get("purpose") if owed else None,
            "python_executable": sys.executable,
            "transport_allowlisted": transport_allowlisted,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
