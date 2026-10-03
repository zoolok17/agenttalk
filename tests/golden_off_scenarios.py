"""Loop scenarios run with the turn journal OFF, and a normalised record of what
they leave behind, for comparing against what the code did BEFORE the journal
existed.

This module imports nothing from the journal on purpose: the very same file is
run against agenttalk master 2112cec to make the golden file, and against the
current code to check it.

Making the golden file (once, from master 2112cec's code):

    git archive 2112cec src | tar -x -C <scratch>/master
    PYTHONPATH=<scratch>/master/src python tests/make_off_golden.py

and committing ``tests/golden/off_loop_2112cec.json``. Re-making it from the
current code is NOT allowed to be the way a difference is made to go away: the
file records the code before the journal.

What is compared, per scenario: the loop's result and sleeps, any exception
(type and text), the wrapper's lifecycle log lines, and every file the run left
in the project store (names and contents). Volatile values are replaced by
placeholders: the project folder, timestamps, message ids (numbered in order),
uuids, hashes, session ids and process ids.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
from pathlib import Path

from agenttalk.store import Store
from agenttalk.wrapper import loop, run, session
from agenttalk.wrapper_logs import WrapperLifecycleLog

_TS = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z?")
_MSG = re.compile(r"\d{8}-\d{6}-\d{6}-[A-Za-z0-9]{4}")
_SESSION = re.compile(r"\d{8}T\d{6}-[A-Za-z0-9]+")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX12 = re.compile(r"(?<![0-9a-f])(?=[0-9]*[a-f])[0-9a-f]{12}(?![0-9a-f])")
_PID = re.compile(r'("(?:[a-z_]*pid)":\s*)\d+')
_EPOCH = re.compile(r'("(?:[a-z_]*(?:epoch|monotonic|nonce|token|start))":\s*)(?:"[^"]*"|[\d.]+)')


def claude_turn(*, error: bool = False) -> list[str]:
    result = {"type": "result", "is_error": error, "result": "boom" if error else "done", "num_turns": 1}
    result["usage"] = {"input_tokens": 3, "output_tokens": 2, "cache_read_input_tokens": 0}
    lines = [
        {"type": "stream_event", "event": {"type": "message_start", "message": {"role": "assistant"}}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
        {"type": "stream_event", "event": {"type": "message_stop"}},
        result,
    ]
    return [json.dumps(line) for line in lines]




def _spawns(script):
    """spawn(argv, stdin) following `script`: "ok", "fail", "held" or an exception."""
    state = {"n": 0}

    def spawn(argv, stdin):
        step = script[min(state["n"], len(script) - 1)]
        state["n"] += 1
        if step == "ok":
            return claude_turn()
        if step == "fail":
            return claude_turn(error=True)
        if step == "held":
            raise run._GatewayChildCapUnavailable("held", transient=True)
        raise step

    return spawn


SCENARIOS = {
    "success": dict(messages=["one"], script=["ok"], loop=dict(max_turns=1, max_polls=20)),
    "failure_then_success": dict(messages=["one"], script=["fail", "ok"], loop=dict(max_turns=1, max_polls=20, k_poison=3)),
    "repeated_failure": dict(messages=["one"], script=["fail"], loop=dict(max_polls=60, k_poison=1)),
    "success_then_dead_letter": dict(messages=["one", "two"], script=["ok", "fail"], loop=dict(max_polls=60, k_poison=1)),
    "gateway_hold": dict(messages=["one"], script=["held", "ok"], loop=dict(max_turns=1, max_polls=20)),
    "e5_exception": dict(messages=["one"], script=[RuntimeError("boom-e5")], loop=dict(max_polls=6)),
}


def normalise(text: str, root: Path, ids: list[str]) -> str:
    for form in (str(root), str(root).replace("\\", "/"), str(root).replace("\\", "\\\\")):
        text = text.replace(form, "<ROOT>")
    for found in _MSG.findall(text):
        if found not in ids:
            ids.append(found)
    text = _SESSION.sub("<SESSION>", text)
    text = _TS.sub("<TS>", text)
    text = _UUID.sub("<UUID>", text)
    text = _HEX64.sub("<HASH>", text)
    text = _HEX32.sub("<HEX32>", text)
    text = _HEX12.sub("<HEX12>", text)
    text = _PID.sub(r"\g<1>0", text)
    text = _EPOCH.sub(r'\g<1>"<V>"', text)
    return text


def run_scenario(name: str, root: Path) -> dict:
    spec = SCENARIOS[name]
    store = Store(root)
    store.init(["alpha", "beta"])
    for body in spec["messages"]:
        store.send(sender="alpha", recipient="beta", body=body)
    log = io.StringIO()
    lifecycle = WrapperLifecycleLog("beta", stream=log, enabled=True)
    sleeps: list[float] = []
    stamps: list[int] = []
    drive = run.make_drive(
        store,
        "beta",
        "claude",
        session.SessionState(cli="claude", claude_session_id="sid-1"),
        ["claude"],
        spawn=_spawns(spec["script"]),
        clock=lambda: 0.0,
        render=False,
        heartbeat=lambda: stamps.append(1),
        lifecycle_log=lifecycle,
    )
    out, err = io.StringIO(), io.StringIO()
    raised = None
    turns = None
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            turns = loop.run_loop(
                store,
                "beta",
                drive,
                clock=lambda: 0.0,
                sleep=sleeps.append,
                heartbeat=lambda: stamps.append(1),
                **spec["loop"],
            )
        except Exception as exc:  # noqa: BLE001 - the exception IS the observable
            raised = "%s: %s" % (type(exc).__name__, exc)
    return {"turns": turns, "sleeps": sleeps, "stamps": len(stamps), "raised": raised,
            "stdout": out.getvalue(), "stderr": err.getvalue(), "log": log.getvalue(), "store": store}


def capture(name: str, root: Path) -> dict:
    """The normalised record of one scenario."""
    got = run_scenario(name, root)
    store_root = Path(got.pop("store").root)
    ids: list[str] = []
    files = {}
    for path in sorted(p for p in store_root.rglob("*") if p.is_file()):
        relative = path.relative_to(store_root).as_posix()
        files[relative] = normalise(path.read_text(encoding="utf-8", errors="replace"), store_root, ids)
    text = {key: normalise(got[key], store_root, ids) if isinstance(got[key], str) else got[key]
            for key in ("stdout", "stderr", "log", "raised")}
    record = {"turns": got["turns"], "sleeps": got["sleeps"], "stamps": got["stamps"],
              "raised": text["raised"], "stdout": text["stdout"], "stderr": text["stderr"],
              "log": text["log"].splitlines(), "files": files}
    # number the ids in time order, in names and contents alike
    numbered = {found: "<MSG%d>" % (index + 1) for index, found in enumerate(sorted(ids))}

    def renumber(value):
        if isinstance(value, str):
            return _MSG.sub(lambda m: numbered.get(m.group(0), "<MSG?>"), value)
        if isinstance(value, list):
            return [renumber(v) for v in value]
        if isinstance(value, dict):
            return {renumber(k): renumber(v) for k, v in value.items()}
        return value

    return renumber(record)


def capture_all(base: Path) -> dict:
    return {name: capture(name, base / name) for name in SCENARIOS}
