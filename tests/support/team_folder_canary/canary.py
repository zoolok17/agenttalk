"""Team-folder canary (#336, design stage): where do a seat's children really write?

No model, no network, no gateway. It starts three kinds of child the way agenttalk
starts them, once with today's environment ("baseline") and once with the proposed
team-folder variables ("team"), and records where each one puts temporary and cache
files:

* wrapper: a wrapped seat's turn through the wrapper's real spawn path
  (``run.make_drive`` + ``loop.run_loop``), with a stub model CLI that probes itself
  and then answers on a temporary bus;
* gateway: a child started with the gateway-backed environment that the wrapper
  builds for that backend (``run._child_env``); it only probes, so it never contacts
  a gateway;
* gate: a pytest run the way the dev gate runs one: its own export of the committed
  checkout, its own allowlisted environment and its own isolated launcher. In
  baseline mode only the gate's run-folder choice is computed: running it would
  write into the user's real temp folder;
* comprehension: a child with the comprehension worker's own allowlisted environment
  and interpreter flags; it only probes itself.

About the user's temp folder, the canary claims only what one listing at the start
and one at the end can show: the new top-level names still there at the end. It
cannot see a file made and removed during the run, a write inside a folder that
already existed, or an overwrite, and it says nothing about who made a new name. A
listing that fails makes the result unknown, never zero.

Usage (one new, empty folder per run; everything is written inside it):

    python tests/support/team_folder_canary/canary.py --team-root <new folder> --out <file.json>

The output names locations only by kind ("team-root/tmp", "user-temp",
"user-home/AppData/Local/pip"), never by their full local path.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess  # nosec B404 - runs this interpreter on this file and the gate's own launcher
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
STUB = REPO / "tests" / "support" / "stub_cli.py"
sys.path.insert(0, str(HERE))

import probe  # noqa: E402 - found through the line above

BOUNDARIES = ("wrapper", "gateway", "gate", "comprehension")
#: Never passed on to a child of the canary (see the repository's safety rules).
DROPPED = ("AGENTTALK_ROOT", "AGENTTALK_TEST_GATEWAY_PORTS", "AGENTTALK_AUTHORIZE_SYMLINK_DEVMODE")

GATE_TEST = '''import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import team_folder_probe as probe  # noqa: E402


def test_where_a_dev_gate_child_writes(tmp_path):
    config = json.loads((HERE / "team_folder_probe.json").read_text(encoding="utf-8"))
    out = probe.observe("gate-child", str(tmp_path), True)
    out["pytest_tmp_path"] = str(tmp_path)
    Path(config["out"]).write_text(json.dumps(out, indent=2), encoding="utf-8")
'''


def team_variables(team: Path, seat: str = "beta") -> dict[str, str]:
    """The stage-1 proposal: every temporary and cache location inside the team root."""
    tmp = str(team / "tmp")
    return {
        "TEMP": tmp,
        "TMP": tmp,
        "TMPDIR": tmp,
        "PIP_CACHE_DIR": str(team / "cache" / "pip"),
        "npm_config_cache": str(team / "cache" / "npm"),
        "PYTHONPYCACHEPREFIX": str(team / "cache" / "pycache"),
        "XDG_CACHE_HOME": str(team / "cache" / "xdg"),
        "AGENTTALK_SCRATCH": str(team / "scratch" / seat),
        "AGENTTALK_TURN_EVENTS_DIR": str(team / "state" / "turn-events"),
    }


# --- the inner runs: one child process per mode and boundary ----------------------------


def _canary_settings(team: Path, mode: str, label: str, *, stub: bool) -> Path:
    out = team / "work" / f"raw-{mode}-{label}.json"
    work = team / "work" / f"{mode}-{label}"
    work.mkdir(parents=True, exist_ok=True)
    os.environ["AGENTTALK_CANARY_OUT"] = str(out)
    os.environ["AGENTTALK_CANARY_WORK"] = str(work)
    os.environ["AGENTTALK_CANARY_LABEL"] = label
    os.environ["AGENTTALK_CANARY_WRITE"] = "1" if mode == "team" else "0"
    if stub:
        os.environ["AGENTTALK_CANARY_STUB"] = str(STUB)
        os.environ["AGENTTALK_STUB_SCENARIO"] = "reply_ok"
    else:
        os.environ.pop("AGENTTALK_CANARY_STUB", None)
    return out


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def inner_wrapper(team: Path, mode: str) -> dict[str, Any]:
    from agenttalk import janitor, ovh_gateway, recovery, scratch, signing, turn_events, wrapper_logs
    from agenttalk.store import Store
    from agenttalk.wrapper import loop, recv_api, run, session
    from agenttalk.wrapper.obligations import DETECTION_GRADE, DetectionCommitGate, PolicySnapshot

    bus = team / "projects" / mode
    store = Store(bus)
    store.init(["alpha", "beta", "lead"])
    store.set_operator_facing("lead")
    inbound = store.send(sender="alpha", recipient="beta", kind="question", body="What is 19 * 21?",
                         meta={"request_id": "q-canary"})
    store.write_waiting("beta", {"mode": "wrapper-loop", "wrapper_generation": "canary-1",
                                 "wait_token": "canary-1", "pid": os.getpid()})
    policy = PolicySnapshot.from_mapping({"schema_version": 1, "agents": {"beta": {"grade": DETECTION_GRADE}}},
                                         "beta")
    gate = DetectionCommitGate(store, "beta", policy, fence="canary-1")
    assert recv_api.next_record(store, "beta") is not None
    child_out = _canary_settings(team, mode, "wrapper-child", stub=True)
    drive = run.make_drive(store, "beta", "claude", session.load_session(store, "beta", "claude"),
                           [sys.executable, str(HERE / "child.py")], render=False)
    turns = loop.run_loop(store, "beta", drive, commit_gate=gate, clock=lambda: 0.0,
                          sleep=lambda _d: None, max_turns=1, max_polls=6)
    replied = any(m.sender == "beta" and (m.meta or {}).get("in_reply_to") == inbound.id
                  for m in store.valid_messages())
    process = probe.observe("wrapper-process", str(team / "work" / f"{mode}-wrapper-process"), mode == "team")
    process["agenttalk_folders"] = {
        "wrapper_logs": str(wrapper_logs.default_wrapper_log_root(bus)),
        "turn_events": str(turn_events.default_turn_events_root(bus)),
        "signing_keys": str(signing.default_keys_dir()),
        "recovery": str(recovery.default_recovery_dir()),
        "gateway_secrets": str(ovh_gateway.default_secret_dir()),
        "scratch_root": str(scratch.resolve_scratch_root(bus)),
        "janitor_tmp_root": str(janitor.JanitorConfig.load(bus).tmp_root),
        "claude_context_signal_dir": tempfile.gettempdir(),
    }
    return {"turns": turns, "reply_landed": replied, "process": process, "child": _read(child_out)}


def inner_gateway(team: Path, mode: str) -> dict[str, Any]:
    from agenttalk.wrapper import run

    bus = team / "projects" / mode
    child_out = _canary_settings(team, mode, "gateway-child", stub=False)
    # A placeholder front token: the child environment drops it, and no capability is
    # minted, so nothing here can reach a gateway.
    env = run._child_env(bus, backend_profile="ovh-qwen", profile_env={
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:4000",
        "ANTHROPIC_AUTH_TOKEN": "canary-placeholder",
    })
    done = subprocess.run(  # nosec B603 - this interpreter on the canary's own child script
        [sys.executable, str(HERE / "child.py")], env=env, cwd=str(bus), capture_output=True, text=True,
        timeout=300, creationflags=run._child_creationflags(env),
    )
    return {"exit": done.returncode, "env_names": sorted(env), "child": _read(child_out)}


def inner_gate(team: Path, mode: str) -> dict[str, Any]:
    from agenttalk import dev_gate

    external = dev_gate._default_external_base(REPO, None)
    result: dict[str, Any] = {"external_base": str(external)}
    if mode != "team":
        return result  # running it would write into the user's real temp folder
    child_out = team / "work" / f"raw-{mode}-gate-child.json"
    external.mkdir(parents=True, exist_ok=True)
    run_root = Path(tempfile.mkdtemp(prefix="agenttalk-dev-gate-", dir=str(external))).resolve()
    binding = dev_gate.capture_candidate_binding(REPO)
    source_root = dev_gate._export_phase(binding, run_root, "source")
    tests = source_root / "tests"
    shutil.copyfile(HERE / "probe.py", tests / "team_folder_probe.py")
    (tests / "team_folder_probe.json").write_text(json.dumps({"out": str(child_out)}), encoding="utf-8")
    (tests / "test_zz_team_folder_probe.py").write_text(GATE_TEST, encoding="utf-8")
    env = dev_gate.source_environment(dev_gate._base_env(run_root), source_root)
    argv = dev_gate.isolated_tool_argv(sys.executable, "pytest", "-q", "tests/test_zz_team_folder_probe.py",
                                       candidate_import_root=source_root / "src")
    done = subprocess.run(  # nosec B603 - the dev gate's own isolated pytest launcher
        argv, cwd=str(source_root), env=env, capture_output=True, text=True, timeout=400,
    )
    result.update({
        "run_root": str(run_root),
        "exit": done.returncode,
        "pytest_cache_in_export": (source_root / ".pytest_cache").exists(),
        "env_names": sorted(env),
        "child": _read(child_out),
    })
    return result


def inner_comprehension(team: Path, mode: str) -> dict[str, Any]:
    from agenttalk.comprehension import worker

    out = team / "work" / f"raw-{mode}-comprehension-child.json"
    work = team / "work" / f"{mode}-comprehension-child"
    work.mkdir(parents=True, exist_ok=True)
    # The worker's own environment builder and import root, and its interpreter flags;
    # its allowlist keeps no AGENTTALK_ variable, so the settings go on the command line.
    env = worker.sanitized_worker_env(dict(os.environ))
    env["PYTHONPATH"] = worker._derive_child_import_root()
    interpreter_and_flags = worker._worker_subprocess_argv()[:3]
    done = subprocess.run(  # nosec B603 - the worker's interpreter flags on the canary's child script
        [*interpreter_and_flags, str(HERE / "child.py"), "--canary", str(out), str(work), "comprehension-child",
         "1" if mode == "team" else "0"],
        env=env, cwd=str(work), capture_output=True, text=True, timeout=300,
    )
    return {"exit": done.returncode, "flags": interpreter_and_flags[1:], "env_names": sorted(env),
            "child": _read(out)}


# --- the outer run: environments, classification, report -------------------------------

_RANDOM = [
    (re.compile(r"agenttalk-dev-gate-[A-Za-z0-9_]+"), "agenttalk-dev-gate-<id>"),
    (re.compile(r"team-canary-[A-Za-z0-9_]+"), "team-canary-<id>"),
    (re.compile(r"pytest-of-[^/]+"), "pytest-of-<user>"),
    (re.compile(r"module-[0-9a-f]+"), "module-<id>"),
]


def _generic(parts: tuple[str, ...]) -> str:
    text = "/".join(parts)
    for pattern, word in _RANDOM:
        text = pattern.sub(word, text)
    return text


def classify(value: Any, anchors: list[tuple[str, Path, int]]) -> Any:
    """A location by kind, never its local path. None stays None (unset or unknown)."""
    if not isinstance(value, str) or not value:
        return value
    if value == "pip-cache-disabled":
        return value
    if not Path(value).is_absolute():
        # A flag such as "1" is shown as it is; anything else relative is only named.
        return value if len(value) <= 16 and "/" not in value and "\\" not in value else "relative path"
    original = Path(os.path.abspath(value))
    path = Path(os.path.normcase(str(original)))
    for name, root, depth in anchors:
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        tail = original.parts[len(original.parts) - len(rel.parts):]  # the original spelling
        shown = _generic(tail[:depth])
        more = "/..." if len(rel.parts) > depth else ""
        return f"{name}/{shown}{more}" if shown else name
    return "elsewhere"


def _classify_tree(obj: Any, anchors: list[tuple[str, Path, int]]) -> Any:
    if isinstance(obj, dict):
        return {key: (obj[key] if key in {"label", "python", "exit", "turns", "reply_landed", "flags",
                                          "dont_write_bytecode", "pytest_cache_in_export", "env_names",
                                          "pip_stderr_lines"}
                      else _classify_tree(obj[key], anchors)) for key in obj}
    if isinstance(obj, list):
        return [_classify_tree(item, anchors) for item in obj]
    return classify(obj, anchors)


def _names(folder: Path) -> tuple[set[str] | None, str | None]:
    """The top-level names in ``folder``, or None and the error's kind: never an empty guess."""
    try:
        return set(os.listdir(folder)), None
    except OSError as exc:
        return None, type(exc).__name__


def user_temp_summary(before: set[str] | None, after: set[str] | None, errors: list[str]) -> dict[str, Any]:
    """New top-level names still present at the end, or unknown when either listing failed."""
    if before is None or after is None:
        return {"status": "unknown", "errors": errors, "surviving_new_top_level_names": None,
                "surviving_new_names_like_the_canary_s": None}
    new = sorted(after - before)
    return {
        "status": "listed at start and end",
        "errors": [],
        "surviving_new_top_level_names": len(new),
        "surviving_new_names_like_the_canary_s": len(
            [name for name in new if name.startswith(("team-canary-", "agenttalk-dev-gate-", "pytest-of-"))]
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--team-root", required=True)
    parser.add_argument("--out")
    parser.add_argument("--inner", choices=BOUNDARIES)
    parser.add_argument("--mode", choices=("baseline", "team"))
    parser.add_argument("--raw-out")
    args = parser.parse_args()
    team = Path(args.team_root).resolve()
    if args.inner:
        runner = {"wrapper": inner_wrapper, "gateway": inner_gateway, "gate": inner_gate,
                  "comprehension": inner_comprehension}[args.inner]
        Path(args.raw_out).write_text(json.dumps(runner(team, args.mode), indent=2), encoding="utf-8")
        return 0
    if team.exists() and any(team.iterdir()):
        parser.error("--team-root must be a new or empty folder")
    for part in ("projects", "work", "scratch/beta", "tmp", "cache", "state"):
        (team / part).mkdir(parents=True, exist_ok=True)
    proposal = team_variables(team)
    # Baseline keeps the user's own temp folder and drops every other proposed variable
    # (Windows variable names are not case-sensitive, so compare them uppercased).
    added = {name.upper() for name in proposal} - {"TEMP", "TMP", "TMPDIR"}
    base = {key: value for key, value in os.environ.items()
            if key.upper() not in added and key.upper() not in DROPPED}
    base["PYTHONPATH"] = str(REPO / "src")
    user_temp = Path(os.path.normcase(os.path.abspath(tempfile.gettempdir())))
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or str(Path.home())
    anchors = [
        ("team-root", Path(os.path.normcase(str(team))), 2),
        ("user-temp", user_temp, 0),
        ("python-install", Path(os.path.normcase(sys.base_prefix)), 2),
        ("checkout", Path(os.path.normcase(str(REPO))), 2),
        ("user-home", Path(os.path.normcase(os.path.abspath(home))), 3),
    ]
    (before, before_error), started = _names(user_temp), time.time()
    raw: dict[str, Any] = {}
    for mode in ("baseline", "team"):
        env = dict(base)
        if mode == "team":
            env.update(proposal)
        for boundary in BOUNDARIES:
            raw_out = team / "work" / f"raw-{mode}-{boundary}.json"
            subprocess.run(  # nosec B603 - this interpreter on this file
                [sys.executable, str(Path(__file__).resolve()), "--team-root", str(team), "--inner", boundary,
                 "--mode", mode, "--raw-out", str(raw_out)],
                env=env, check=True, timeout=500,
            )
            raw[f"{mode}/{boundary}"] = json.loads(raw_out.read_text(encoding="utf-8"))
    after, after_error = _names(user_temp)
    report = {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "seconds": round(time.time() - started, 1),
        "user_temp": user_temp_summary(before, after, [e for e in (before_error, after_error) if e]),
        "results": _classify_tree(raw, anchors),
    }
    text = json.dumps(report, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
