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
_BELOW_ROOT = re.compile(r"<ROOT>[^\"'\s]*")  # a path below the replaced root, up to a quote or space
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
    "success": {"messages": ["one"], "script": ["ok"], "loop": {"max_turns": 1, "max_polls": 20}},
    "failure_then_success": {"messages": ["one"], "script": ["fail", "ok"],
                             "loop": {"max_turns": 1, "max_polls": 20, "k_poison": 3}},
    "repeated_failure": {"messages": ["one"], "script": ["fail"], "loop": {"max_polls": 60, "k_poison": 1}},
    "success_then_dead_letter": {"messages": ["one", "two"], "script": ["ok", "fail"],
                                 "loop": {"max_polls": 60, "k_poison": 1}},
    "gateway_hold": {"messages": ["one"], "script": ["held", "ok"], "loop": {"max_turns": 1, "max_polls": 20}},
    "e5_exception": {"messages": ["one"], "script": [RuntimeError("boom-e5")], "loop": {"max_polls": 6}},
}


# A value that depends on the release, not on what the loop did: the agenttalk version.
_RELEASE_KEYS = frozenset({"agenttalk_version"})
# #313 fix round 1 (tk-1bc26603ee28): a volatile per-attempt id (Store.record_attempt_start's
# attempt_id, uuid.uuid4().hex[:12]) is masked by KEY, not by the blind _HEX12 regex below -
# _HEX12 requires at least one a-f letter in its 12 characters (to avoid masking an unrelated
# 12-digit NUMBER elsewhere in a file or in stdout/log text), so a genuine attempt id that is
# all digits (roughly 1 run in 280) slipped through unmasked and never matched the golden
# file's frozen value. Masking by the field name we KNOW holds this value sidesteps the
# ambiguity entirely, for every digit/letter mix, without loosening _HEX12 for anything else.
#
# #313 fix round 2 (tk-9d374ffa5332): round 1 masked EVERY string under this key, selecting
# the right FIELD but not checking the right FORMAT - a malformed value actually on disk
# (empty, short, or containing a character outside 0-9a-f) was masked away just the same,
# so a real corruption of this field stopped failing the comparison at all. Masking now
# requires the value to fully match the one shape uuid.uuid4().hex[:12] can ever produce -
# exactly twelve lowercase hex characters; anything else is left visible, exactly as it
# would be without any key-based masking.
#
# #313 recast (tk-67438c3775be, codex-agenttalk-reviewer-1's final delta read): round 2's
# format check stops SHORT of one case - a value that is not well-formed is left exactly as
# captured, below, and if that captured text happens to BE this module's own placeholder
# text ("<HEX12>") it then compares EQUAL to a genuinely valid id's masked output, so a
# corrupted field that spells out the placeholder itself still passed as unchanged. No
# generator here can ever emit that text, so this masking fallback is not where the fix
# belongs: Store.record_attempt_start now validates the raw attempt_id contract itself,
# at capture time, before anything reaches disk (store.py's `_ATTEMPT_ID_RE`) - a malformed
# attempt_id can no longer be written at all, through the real record/capture path, so it
# can never reach this normaliser to collide with the placeholder in the first place. The
# format check below is kept as defense in depth for any OTHER text a hand-edited or
# otherwise unvalidated file might carry under this key.
_VOLATILE_ID_KEYS = frozenset({"last_attempt_id"})
_WELL_FORMED_ATTEMPT_ID = re.compile(r"[0-9a-f]{12}")
# A dead letter's record stores its payload's size on disk, which differs between platforms
# only by the payload's line endings (Windows writes CRLF). That one field is masked, and
# only once it is checked against the payload's bytes as captured.
_DEAD_LETTER_RECORD = re.compile(r"\.agenttalk/dead-letter/[^/]+/[^/]+\.deadletter\.json")


def _posix_below_root(match: re.Match) -> str:
    # In a decoded string or in plain text a backslash is a real separator (Windows);
    # the golden file is compared on every platform, so the parts are joined by "/".
    return match.group(0).replace("\\", "/")


def _root_free(text: str, root: Path) -> str:
    for form in (str(root), str(root).replace("\\", "/")):
        text = text.replace(form, "<ROOT>")
    return _BELOW_ROOT.sub(_posix_below_root, text)


def _decoded(value, root: Path):
    """A decoded JSON value made comparable across platforms and releases.

    Paths are rewritten here, on DECODED strings, never on serialized JSON text: there
    a backslash can also start an escape, so a corrupted path (a control character
    where a separator was) could be rewritten into a valid one and hidden."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            name = _root_free(key, root) if isinstance(key, str) else key
            if key in _RELEASE_KEYS and isinstance(item, str):
                out[name] = "<VERSION>"
            elif (key in _VOLATILE_ID_KEYS and isinstance(item, str)
                  and _WELL_FORMED_ATTEMPT_ID.fullmatch(item)):
                # Same placeholder _HEX12 already produces for this field when it
                # happens to contain a letter - the frozen golden file was made with
                # that text baked in, and re-making it is not how a difference here
                # is allowed to go away. A value that is NOT exactly twelve lowercase
                # hex characters (empty, short, or carrying a stray character) is left
                # exactly as captured, below, so a real corruption of this field still
                # fails the comparison.
                out[name] = "<HEX12>"
            else:
                out[name] = _decoded(item, root)
        return out
    if isinstance(value, list):
        return [_decoded(item, root) for item in value]
    if isinstance(value, str):
        return _root_free(value, root)
    return value


def _checked_payload_size(record: dict, root: Path, sizes: dict[str, int]) -> dict:
    """The dead-letter record with ``size_bytes`` masked only if it is the byte length of
    its payload as captured; a size that does not match stays visible, so the comparison
    fails."""
    recorded, payload = record.get("size_bytes"), record.get("payload_path")
    if type(recorded) is not int or not isinstance(payload, str):
        return record
    below = _root_free(payload, root)
    actual = sizes.get(below[len("<ROOT>/"):]) if below.startswith("<ROOT>/") else None
    checked = dict(record)
    if recorded == actual:
        checked["size_bytes"] = 0
    else:
        checked["size_bytes"] = "<SIZE MISMATCH %d != %s>" % (recorded, "no payload" if actual is None else actual)
    return checked


def normalise_file(text: str, root: Path, ids: list[str], *, name: str = "",
                   sizes: dict[str, int] | None = None) -> str:
    """One file's content. A JSON document, or a file of JSON lines, is decoded, made
    comparable value by value and written back in one fixed form; anything else is
    plain text (see ``normalise``). ``name`` is the file's path below the store root and
    ``sizes`` the byte length of every captured file, by that same path."""
    try:
        document = json.loads(text)
    except ValueError:
        document = None
    if isinstance(document, dict) and _DEAD_LETTER_RECORD.fullmatch(name):
        document = _checked_payload_size(document, root, sizes or {})
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
    paths ``_decoded`` already handled: rewriting backslashes in JSON text would treat
    an escape as a separator."""
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
    # Every file is read once, as bytes, before any is normalised: a dead letter's record
    # sorts before its payload, and its size is checked against the payload's bytes.
    captured = {path.relative_to(store_root).as_posix(): path.read_bytes()
                for path in sorted(p for p in store_root.rglob("*") if p.is_file())}
    sizes = {relative: len(data) for relative, data in captured.items()}
    files = {}
    for relative, data in captured.items():
        # decoded as Path.read_text would: UTF-8, bad bytes replaced, CRLF read as LF
        content = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8", errors="replace").read()
        files[relative] = normalise_file(content, store_root, ids, name=relative, sizes=sizes)
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
