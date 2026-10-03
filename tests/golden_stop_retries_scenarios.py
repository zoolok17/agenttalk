"""Loop scenarios and a normalised record of what they leave behind, for comparing
the wrapper loop with the "stop retrying at a usage limit" switch OFF against what
the code did BEFORE that switch existed.

This module imports nothing from the usage-limit park on purpose: the very same
file is run against agenttalk master fc791ece to make the golden file, and
against the current code (switch at 0) to check it.

Making the golden file (once, from master fc791ece's code):

    git archive fc791ece src | tar -x -C <scratch>/master
    PYTHONPATH=<scratch>/master/src python tests/make_stop_retries_golden.py

and committing ``tests/golden/stop_retries_off_fc791ece.json``. Re-making it from the
current code is NOT allowed to be the way a difference is made to go away: the
file records the code before the switch.

What is compared, per scenario: the loop's result and sleeps, any exception
(type and text), the wrapper's lifecycle log lines, and every file the run left
in the project store (names and contents). Volatile values are replaced by
placeholders: the project folder, timestamps, message ids (numbered in order),
uuids, hashes, session ids and process ids.

The two real Claude usage-limit cases are captured stream-json lines (sanitised);
the other streams are synthetic.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
from datetime import datetime, timedelta, timezone
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
_BELOW_ROOT = re.compile(r"<ROOT>[^\"'\s]*")  # a path below the replaced root, up to a quote or space
# Values that depend on the platform or the release, never on what the loop did: the stored
# byte count (Windows writes "\r\n" line ends) and the release number a health record carries.
_RELEASE_KEYS = frozenset({"agenttalk_version"})
_PLATFORM_SIZE_KEYS = frozenset({"size_bytes"})


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


# The two real captured cases (sanitised stream-json lines, kept as printed).
REAL_CASE_FIVE_HOUR = [
    {"type": "system", "subtype": "status", "status": "requesting"},
    {"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "resetsAt": 1788948000, "rateLimitType": "five_hour",
        "overageStatus": "rejected", "overageDisabledReason": "out_of_credits", "isUsingOverage": False,
        "unifiedWindows": {"five_hour": {"utilization": 1.03, "resetsAt": 1788948000},
                           "seven_day": {"utilization": 0.86, "resetsAt": 1789160400}}}},
    {"type": "assistant", "error": "rate_limit", "is_api_error_message": True,
     "message": {"model": "<synthetic>", "content": [
         {"type": "text", "text": "You've hit your session limit · resets 12pm (<timezone>)"}]}},
    {"type": "result", "subtype": "success", "is_error": True, "terminal_reason": "api_error",
     "api_error_status": 429, "stop_reason": "stop_sequence",
     "result": "You've hit your session limit · resets 12pm (<timezone>)"},
]
REAL_CASE_SEVEN_DAY = [
    {"type": "system", "subtype": "status", "status": "requesting"},
    {"type": "system", "subtype": "status", "status": "compacting"},
    {"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "resetsAt": 1790370000, "rateLimitType": "seven_day",
        "overageStatus": "rejected", "overageDisabledReason": "out_of_credits", "isUsingOverage": False,
        "unifiedWindows": {"five_hour": {"utilization": 0, "resetsAt": 1790066400},
                           "seven_day": {"utilization": 1, "resetsAt": 1790370000}}}},
    {"type": "system", "subtype": "status", "status": None, "compact_result": "failed",
     "compact_error": "You've hit your weekly limit · resets Sep 25, 11pm (<timezone>)"},
    {"type": "assistant", "error": "invalid_request", "is_api_error_message": True,
     "message": {"model": "<synthetic>", "content": [
         {"type": "text", "text": "Prompt is too long · automatic compaction failed: You've hit your weekly "
                                  "limit · resets Sep 25, 11pm (<timezone>)"}]}},
    {"type": "result", "subtype": "success", "is_error": True, "terminal_reason": "blocking_limit",
     "api_error_status": None, "stop_reason": "stop_sequence",
     "result": "Prompt is too long · automatic compaction failed: You've hit your weekly limit · "
               "resets Sep 25, 11pm (<timezone>)"},
]


def _lines(events) -> list[str]:
    return [json.dumps(event) for event in events]


def rejected_then_success() -> list[str]:
    """A rejected event followed by a turn that then succeeds (synthetic)."""
    return _lines([REAL_CASE_FIVE_HOUR[1]]) + claude_turn()


def _spawns(script):
    """spawn(argv, stdin) following `script`: "ok", "fail", "held", "limit1", "limit2",
    "reject_ok" or an exception."""
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
        if step == "limit1":
            return _lines(REAL_CASE_FIVE_HOUR)
        if step == "limit2":
            return _lines(REAL_CASE_SEVEN_DAY)
        if step == "reject_ok":
            return rejected_then_success()
        raise step

    return spawn


def _hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


SCENARIOS = {
    "success": {"messages": ["one"], "script": ["ok"], "loop": {"max_turns": 1, "max_polls": 20}},
    "failure_then_success": {
        "messages": ["one"], "script": ["fail", "ok"],
        "loop": {"max_turns": 1, "max_polls": 20, "k_poison": 3}},
    "repeated_failure": {"messages": ["one"], "script": ["fail"], "loop": {"max_polls": 60, "k_poison": 1}},
    "success_then_dead_letter": {
        "messages": ["one", "two"], "script": ["ok", "fail"], "loop": {"max_polls": 60, "k_poison": 1}},
    "gateway_hold": {"messages": ["one"], "script": ["held", "ok"], "loop": {"max_turns": 1, "max_polls": 20}},
    "e5_exception": {"messages": ["one"], "script": [RuntimeError("boom-e5")], "loop": {"max_polls": 6}},
    # the two real cases: today the wrapper retries them every poll
    "real_five_hour_retries": {"messages": ["one"], "script": ["limit1"], "loop": {"max_polls": 30}},
    "real_seven_day_retries": {"messages": ["one"], "script": ["limit2"], "loop": {"max_polls": 30}},
    "limit_then_success": {
        "messages": ["one"], "script": ["limit1", "limit1", "ok"], "loop": {"max_turns": 1, "max_polls": 30}},
    "rejected_event_then_success": {
        "messages": ["one"], "script": ["reject_ok"], "loop": {"max_turns": 1, "max_polls": 20}},
    # an old history: 99 infra attempts over 6 hours, then one more limit result
    "old_infra_history_exhausts": {
        "messages": ["one"], "script": ["limit1"], "loop": {"max_polls": 10},
        "ledger": {"attempts_started": 99, "infra_failures": 99, "last_failure_class": "known_global_infra",
                   "first_started_hours_ago": 6.0}},
    # an old history one attempt below the high-attempt backstop
    "old_ambiguous_history_backstop": {
        "messages": ["one"], "script": ["fail"], "loop": {"max_polls": 10, "k_poison": 0},
        "ledger": {"attempts_started": 19, "ambiguous_failures": 19, "last_failure_class": "ambiguous_or_unknown",
                   "first_started_hours_ago": 0.5}},
}

def _posix_below_root(match: re.Match) -> str:
    # In a decoded string or in plain text a backslash is a real separator (Windows); the
    # golden file is compared on every platform, so the parts are joined by "/".
    return match.group(0).replace("\\", "/")


def _root_free(text: str, root: Path) -> str:
    for form in (str(root), str(root).replace("\\", "/")):
        text = text.replace(form, "<ROOT>")
    return _BELOW_ROOT.sub(_posix_below_root, text)


def _decoded(value, root: Path):
    """A decoded JSON value made comparable across platforms and releases.

    Paths are rewritten here, on DECODED strings, never on serialized JSON text: there a
    backslash can also start an escape, so a corrupted path (a control character where a
    separator was) could be rewritten into a valid one and hidden."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            name = _root_free(key, root) if isinstance(key, str) else key
            if key in _RELEASE_KEYS and isinstance(item, str):
                out[name] = "<VERSION>"
            elif key in _PLATFORM_SIZE_KEYS and type(item) is int:
                out[name] = 0
            else:
                out[name] = _decoded(item, root)
        return out
    if isinstance(value, list):
        return [_decoded(item, root) for item in value]
    if isinstance(value, str):
        return _root_free(value, root)
    return value


def normalise_file(text: str, root: Path, ids: list[str]) -> str:
    """One file's content. A JSON document, or a file of JSON lines, is decoded, made
    comparable value by value and written back in one fixed form; anything else is plain
    text (see ``normalise``)."""
    try:
        document = json.loads(text)
    except ValueError:
        document = None
    if isinstance(document, (dict, list)):
        return normalise(json.dumps(_decoded(document, root), indent=2), root, ids, paths=False)
    try:
        lines = [json.loads(line) for line in text.splitlines() if line.strip()]
    except ValueError:
        lines = []
    if lines and all(isinstance(line, (dict, list)) for line in lines):
        body = "\n".join(json.dumps(_decoded(line, root)) for line in lines)
        return normalise(body, root, ids, paths=False)
    return normalise(text, root, ids)


def normalise(text: str, root: Path, ids: list[str], *, paths: bool = True) -> str:
    """Plain text (output, log lines, non-JSON files). ``paths=False`` for JSON text whose
    paths ``_decoded`` already handled: rewriting backslashes in JSON text would treat an
    escape as a separator."""
    if paths:
        text = _root_free(text, root)
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


def _seed_ledger(store: Store, agent: str, spec: dict) -> None:
    """Pre-write an attempt record for the head message: an old history."""
    from agenttalk.wrapper import recv_api

    head = recv_api.next_record(store, agent)
    data = store.dead_letter_attempts(agent)
    record = {
        "id": head["id"], "cursor_before": store.cursor(agent), "request_id": head.get("request_id"),
        "broadcast_id": head.get("broadcast_id"), "from": head.get("from"), "to": head.get("to"),
        "kind": head.get("kind"), "subject": head.get("subject"),
        "first_started_at": _hours_ago(spec["first_started_hours_ago"]),
        "attempts_started": spec.get("attempts_started", 0),
        "poison_eligible_failures": spec.get("poison_eligible_failures", 0),
        "infra_failures": spec.get("infra_failures", 0),
        "ambiguous_failures": spec.get("ambiguous_failures", 0),
        "last_failure_class": spec.get("last_failure_class"),
        "last_failure_summary": "seeded history", "escalated": False, "in_progress": False,
        "last_started_at": _hours_ago(0.01), "last_attempt_id": "seeded",
    }
    data["messages"][head["id"]] = record
    store._write_attempts(agent, data)


def run_scenario(name: str, root: Path) -> dict:
    spec = SCENARIOS[name]
    store = Store(root)
    store.init(["alpha", "beta"])
    for body in spec["messages"]:
        store.send(sender="alpha", recipient="beta", body=body)
    if "ledger" in spec:
        _seed_ledger(store, "beta", spec["ledger"])
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
    # Lock files and their generation guards are how a platform locks, not what the loop did.
    for path in sorted(p for p in store_root.rglob("*")
                       if p.is_file() and not p.name.endswith((".lock", ".generation"))):
        relative = path.relative_to(store_root).as_posix()
        files[relative] = normalise_file(path.read_text(encoding="utf-8", errors="replace"), store_root, ids)
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
