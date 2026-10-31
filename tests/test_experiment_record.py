"""Tests for the experiment recorder (tests/experiment_record.py, audit step P3).

The record schema these tests read is documented in the module docstring of `experiment_record`; the field names
used here are the frozen ones.  Most tests start a real pytest (with two real xdist workers where it matters) in a
throw-away project made by `pytester`.  Plugin autoload is switched off and `-p xdist.plugin` is written out,
exactly as the development gate does.  The record folder is always a sibling of the inner run's basetemp, never
inside it.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest_plugins = ["pytester"]

TESTS_DIR = Path(__file__).resolve().parent
SRC_DIR = TESTS_DIR.parent / "src"
HANDOFF_NAME = "AGENTTALK_EXPERIMENT_RECORD_HANDOFF"


def gate_repair_source():
    """The gate's grouped-ID repair hook, copied exactly as `tests/conftest.py` has it (decorator included), or ""
    if the gate no longer has it. pytest 9.2 reads `_id` where xdist 3.8 writes `_nodeid`, so an inner run on 9.2
    has no `@group` suffix in its raw ids without the repair; on 9.1 the repair changes nothing."""
    source = (TESTS_DIR / "conftest.py").read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == "pytest_collection_finish":
            first = min([decorator.lineno for decorator in node.decorator_list] + [node.lineno])
            return "\n".join(source.splitlines()[first - 1 : node.end_lineno])
    return ""


CONFTEST = (
    """
import experiment_record
import pytest


def pytest_addoption(parser):
    experiment_record.add_option(parser)


def pytest_configure(config):
    experiment_record.configure(config)


"""
    + gate_repair_source()
    + "\n"
)

GROUPED = """
import pytest

pytestmark = pytest.mark.xdist_group("g1")


@pytest.mark.parametrize("value", ["one", "two", "three"])
def test_x(value):
    assert value
"""


@pytest.fixture
def er():
    import experiment_record

    return experiment_record


@pytest.fixture
def project(pytester, monkeypatch, er):
    """A throw-away project whose conftest wires the recorder the way tests/conftest.py does."""
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTHONPATH", str(TESTS_DIR))
    monkeypatch.delenv(HANDOFF_NAME, raising=False)
    pytester.makeconftest(CONFTEST)
    return pytester


def run_parallel(project, rec, *args):
    return project.runpytest_subprocess(
        "-p", "xdist.plugin", "-n", "2", "--dist", "loadgroup", f"--experiment-record={rec}", *args, timeout=180
    )


def run_serial(project, rec, *args):
    return project.runpytest_subprocess(f"--experiment-record={rec}", *args, timeout=180)


def collected_ids(project, *args):
    result = project.runpytest_subprocess("--collect-only", "-q", *args)
    return [line for line in result.outlines if "::" in line]


def read_journals(rec):
    """{file name: [record, ...]}.  Lines are split on "\\n" only: U+2028 is legal inside a record."""
    found = {}
    for path in sorted(Path(rec).glob("journal-*.jsonl")):
        text = path.read_bytes().decode("utf-8")
        found[path.name] = [json.loads(line) for line in text.split("\n") if line]
    return found


def records(rec, kind):
    return [(name, r) for name, recs in read_journals(rec).items() for r in recs if r["kind"] == kind]


def identity_fields(raw, group):
    """The identity a recorder writes for an item: the canonical id (the raw id minus its `@group` suffix), no
    parameter id (these hand-made ids have none) and the requested and effective group."""
    suffixed = group is not None and raw.endswith("@" + group)
    return {
        "canonical_id": raw[: -len(group) - 1] if suffixed else raw,
        "param_id": None,
        "requested_group": group,
        "effective_group": group if suffixed else None,
    }


def item_start_fields(raw, group):
    fields = identity_fields(raw, group)
    return {
        "nodeid": raw,
        "requested_group": group,
        "effective_group": fields["effective_group"],
        "suffix_missing": group is not None and fields["effective_group"] is None,
        "canonical_id": fields["canonical_id"],
        "param_id": None,
    }


def phase_fields(raw, group, when, outcome):
    return {
        "nodeid": raw,
        "when": when,
        "outcome": outcome,
        "duration": 0.01,
        "xfail": False,
        "reason": None,
        "reason_redactions": 0,
        **identity_fields(raw, group),
    }


def manifest_fields(rows, source="session", source_worker=None):
    """A collection manifest for `rows` [(raw id, group)]; the controller's (`source="node"`) has raw ids only."""
    fields = {"source": source, "source_worker": source_worker, "count": len(rows), "raw_ids": [r for r, _ in rows]}
    if source == "session":
        fields["identities"] = [identity_fields(raw, group) for raw, group in rows]
    return fields


def write_journal(
    rec,
    role,
    pid,
    *,
    worker=None,
    starts=(),
    trailer=True,
    extra=(),
    plugins=("xdist.plugin",),
    nested_token=None,
    loadgroup=True,
    phases=None,
    collected=None,
    unfinished=(),
    manifest=True,
    parent_pid=None,
):
    """A hand-made, well-formed journal, for the reducer tests.

    Every started item gets the three passed phases unless `phases` says otherwise ({raw id: [(when, outcome), ...]},
    or False for none at all); a serial, worker or nested journal gets a collection manifest of `collected` (default:
    the started items); `unfinished` is [(raw id, group, [(when, outcome), ...])]: started, some phases, no end."""
    start = {"started_ms": 1000, "loadgroup": loadgroup, "nested_token": nested_token}
    if parent_pid is None and nested_token is not None:
        parent_pid = 11  # the hand-made launcher is the serial journal of pid 11, unless a test says otherwise
    if parent_pid is not None:
        start["parent_pid"] = parent_pid
    if plugins is not None:
        start["plugins"] = list(plugins)
    events = [("process-start", start)]
    if manifest and role in ("serial", "worker", "nested"):
        rows = list(starts) if collected is None else list(collected)
        events.append(("collection-manifest", manifest_fields(rows)))
    for raw, group in starts:
        events.append(("item-start", item_start_fields(raw, group)))
        sequence = [("setup", "passed"), ("call", "passed"), ("teardown", "passed")]
        if phases is False:
            sequence = []
        elif phases is not None and raw in phases:
            sequence = phases[raw]
        for when, outcome in sequence:
            events.append(("phase", phase_fields(raw, group, when, outcome)))
        events.append(("item-end", {"nodeid": raw}))
    for raw, group, seen in unfinished:
        events.append(("item-start", item_start_fields(raw, group)))
        for when, outcome in seen:
            events.append(("phase", phase_fields(raw, group, when, outcome)))
    events.extend(extra)
    if trailer:
        events.append(("trailer", {"complete": True, "records": len(events), "exitstatus": 0}))
    lines = []
    for seq, (kind, fields) in enumerate(events, 1):
        lines.append(
            json.dumps(
                {
                    "v": 2,
                    "seq": seq,
                    "kind": kind,
                    "role": role,
                    "worker": worker,
                    "pid": pid,
                    "t_wall": 1000.0 + pid + seq / 100,
                    "t_mono": float(seq),
                    **fields,
                }
            )
        )
    path = Path(rec) / f"journal-{role}-{pid}-1000.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def kinds(problems):
    return {p["kind"] for p in problems}


# --- 1-2: the canonical id -------------------------------------------------------------------------------------


def test_a_parameter_filtered_group_member_joins_to_the_serial_collection(project, er, tmp_path):
    project.makepyfile(test_mod=GROUPED)
    selected = "test_mod.py::test_x[two]"
    expected = collected_ids(project, selected)
    assert expected == [selected]
    rec = tmp_path / "rec"

    run_parallel(project, rec, selected).assert_outcomes(passed=1)

    (start,) = [r for _, r in records(rec, "item-start")]
    assert start["nodeid"] == selected + "@g1"
    assert (start["requested_group"], start["effective_group"], start["suffix_missing"]) == ("g1", "g1", False)
    summary = er.reduce_journals(rec, expected)
    assert summary["accepted"], summary["problems"]
    assert list(summary["items"]) == [selected]


def test_a_parameter_containing_at_and_a_bracket_keeps_its_own_text(project, er, tmp_path):
    project.makepyfile(test_mod=GROUPED.replace('["one", "two", "three"]', '["a@b]", "c@g1", "d"]'))
    expected = collected_ids(project)
    assert len(expected) == 3
    rec = tmp_path / "rec"

    run_parallel(project, rec).assert_outcomes(passed=3)

    raw = sorted(r["nodeid"] for _, r in records(rec, "item-start"))
    assert raw == sorted(f"{i}@g1" for i in expected)
    summary = er.reduce_journals(rec, expected)
    assert summary["accepted"], summary["problems"]
    assert sorted(summary["items"]) == sorted(expected)
    assert "test_mod.py::test_x[c@g1]" in summary["items"]


def test_canonical_id_removes_a_suffix_only_when_it_equals_the_requested_group(er):
    assert er.canonical_id("t.py::t[a@b]]@g", "g") == "t.py::t[a@b]]"
    assert er.canonical_id("t.py::t[x@g]@g", "g") == "t.py::t[x@g]"  # only one suffix goes
    assert er.canonical_id("t.py::t[v@pwsh]", None) == "t.py::t[v@pwsh]"  # no group requested: nothing goes
    assert er.canonical_id("t.py::t[v]@other", "g") == "t.py::t[v]@other"  # a different suffix stays


# --- 3: a missing suffix is a flag, not acceptance ---------------------------------------------------------------


def test_a_missing_group_suffix_is_flagged_and_not_accepted(project, er, tmp_path):
    # A marker added by a plain-priority hook comes too late for xdist, which looks for markers first: the item
    # is in a group on paper, but its node id has no "@group" and the scheduler ignores it.
    project.makeconftest(
        CONFTEST
        + """
import pytest


def pytest_collection_modifyitems(items):
    for item in items:
        item.add_marker(pytest.mark.xdist_group(name="g1"))
"""
    )
    project.makepyfile(test_mod="def test_a():\n    pass\n\n\ndef test_b():\n    pass\n")
    expected = collected_ids(project)
    rec = tmp_path / "rec"

    run_parallel(project, rec).assert_outcomes(passed=2)

    starts = [r for _, r in records(rec, "item-start")]
    assert starts and all(r["requested_group"] == "g1" and r["suffix_missing"] for r in starts)
    assert all(r["effective_group"] is None for r in starts)
    summary = er.reduce_journals(rec, expected)
    assert "suffix-missing" in kinds(summary["problems"])
    assert summary["accepted"] is False


LATE_MARKER = """
import pytest


def pytest_collection_modifyitems(items):
    for item in items:
        item.add_marker(pytest.mark.xdist_group(name="g1"))
"""


@pytest.mark.parametrize("shape", ["with-suffix", "without-suffix"])
def test_the_identity_fields_say_what_the_raw_id_shows_in_either_shape(project, er, tmp_path, shape):
    # CI (pytest 9.2 without the gate's repair): an inner run's raw ids carry no `@g1`; with the repair, or on 9.1,
    # they do. The recorder computes the canonical id and the effective group from what the item reports, so in both
    # shapes every record agrees with its own raw id and the join to the serial collection holds.
    suffixed = shape == "with-suffix"
    if suffixed:
        project.makepyfile(test_mod=GROUPED)
    else:
        project.makeconftest(CONFTEST + LATE_MARKER)  # a marker added too late for xdist: no suffix on the raw id
        project.makepyfile(test_mod="def test_x():\n    pass\n\n\ndef test_y():\n    pass\n")
    expected = collected_ids(project)
    rec = tmp_path / "rec"

    run_parallel(project, rec)

    starts = [r for _, r in records(rec, "item-start")]
    phases = [r for n, r in records(rec, "phase") if n.startswith("journal-worker-")]
    assert starts and phases
    for record in [*starts, *phases]:
        assert record["requested_group"] == "g1"
        assert (record["effective_group"] == "g1") is suffixed
        assert record["nodeid"] == record["canonical_id"] + ("@g1" if suffixed else "")
    ((_, manifest),) = [(n, r) for n, r in records(rec, "collection-manifest") if n.startswith("journal-worker-")][:1]
    assert [i["canonical_id"] for i in manifest["identities"]] == expected
    assert len(manifest["raw_ids"]) == len(expected)
    assert all(
        raw == canonical + ("@g1" if suffixed else "")
        for raw, canonical in zip(manifest["raw_ids"], expected, strict=True)
    )
    summary = er.reduce_journals(rec, expected)
    assert sorted(summary["items"]) == sorted(expected)  # the join to the serial collection holds in both shapes
    assert ("suffix-missing" in kinds(summary["problems"])) is not suffixed
    assert summary["accepted"] is suffixed


# --- 4: the one-to-one join --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case, problem",
    [
        ("duplicate", "duplicate-id"),
        ("missing", "missing-id"),
        ("unexpected", "unexpected-id"),
    ],
)
def test_a_duplicate_missing_or_unexpected_id_fails_the_join(er, tmp_path, case, problem):
    expected = ["t.py::a", "t.py::b"]
    starts0 = [("t.py::a@g", "g")]
    starts1 = [("t.py::b@g", "g")]
    if case == "duplicate":
        starts1.append(("t.py::a@g", "g"))
    if case == "missing":
        starts1 = []
    if case == "unexpected":
        starts1.append(("t.py::zzz@g", "g"))
    write_journal(tmp_path, "worker", 11, worker="gw0", starts=starts0)
    write_journal(tmp_path, "worker", 12, worker="gw1", starts=starts1)

    summary = er.reduce_journals(tmp_path, expected)

    assert problem in kinds(summary["problems"])
    assert summary["accepted"] is False


def test_an_exact_one_to_one_join_is_accepted(er, tmp_path):
    rows0, rows1 = [("t.py::a@g1", "g1")], [("t.py::b@g2", "g2")]
    both = rows0 + rows1  # every worker collects the one common list and starts only its own items
    nodes = [
        ("collection-manifest", manifest_fields(both, "node", "gw0")),
        ("collection-manifest", manifest_fields(both, "node", "gw1")),
    ]
    write_journal(tmp_path, "controller", 10, extra=nodes)  # an xdist run has its controller's journal
    write_journal(tmp_path, "worker", 11, worker="gw0", starts=rows0, collected=both)
    write_journal(tmp_path, "worker", 12, worker="gw1", starts=rows1, collected=both)
    summary = er.reduce_journals(tmp_path, ["t.py::a", "t.py::b"])
    assert summary["accepted"], summary["problems"]
    assert summary["status"] == "complete"


# --- 5: cross-file pwsh members share one worker -----------------------------------------------------------------


def test_pwsh_members_of_two_files_run_on_one_worker_and_are_recorded_so(er, tmp_path):
    members = [
        "tests/test_supervisor.py::test_generated_ps1_is_bom_ascii_and_parses",
        "tests/test_coverage_producer.py::test_artifact_writer_refuses_reparse_output_parent",
    ]
    rec = tmp_path / "rec"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_ADDOPTS", "PYTHONPATH"))}
    env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONPATH=str(SRC_DIR))
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "xdist.plugin",
            "-n",
            "2",
            "--dist",
            "loadgroup",
            f"--experiment-record={rec}",
            "--basetemp",
            str(tmp_path / "inner"),
            *members,
        ],
        cwd=TESTS_DIR.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode in (0, 1), done.stdout + done.stderr  # 2, 3 and 4 would be a broken run, not a test outcome

    summary = er.reduce_journals(rec, members)
    assert summary["accepted"], summary["problems"]
    assert {i["group"] for i in summary["items"].values()} == {"pwsh"}
    assert len(summary["groups"]["pwsh"]) == 1, summary["groups"]
    assert "group-split" not in kinds(summary["problems"])


def test_members_of_one_group_on_two_workers_are_a_group_split(er, tmp_path):
    write_journal(tmp_path, "worker", 11, worker="gw0", starts=[("t.py::a@pwsh", "pwsh")])
    write_journal(tmp_path, "worker", 12, worker="gw1", starts=[("u.py::b@pwsh", "pwsh")])
    summary = er.reduce_journals(tmp_path, ["t.py::a", "u.py::b"])
    assert "group-split" in kinds(summary["problems"])
    assert summary["accepted"] is False


# --- 6: a killed worker -------------------------------------------------------------------------------------------


def test_a_worker_killed_mid_test_leaves_its_started_item_and_no_trailer(project, er, tmp_path):
    project.makepyfile(
        test_mod="""
import os


def test_before():
    pass


def test_dies():
    os._exit(7)


def test_after():
    pass
"""
    )
    rec = tmp_path / "rec"

    run_parallel(project, rec, "--max-worker-restart=0")

    journals = read_journals(rec)
    dying = [
        name
        for name, recs in journals.items()
        if any(r["kind"] == "item-start" and r["nodeid"].endswith("::test_dies") for r in recs)
    ]
    assert len(dying) == 1, journals.keys()
    recs = journals[dying[0]]
    victim = [r for r in recs if r.get("nodeid", "").endswith("::test_dies")]
    assert [r["kind"] for r in victim][:2] == ["item-start", "phase"]
    assert victim[1]["when"] == "setup" and victim[1]["outcome"] == "passed"
    assert not any(r["kind"] == "phase" and r["when"] == "call" for r in victim)
    assert all(r["kind"] != "trailer" for r in recs)

    assert any(r["has_error"] and r["error_text"] for _, r in records(rec, "worker-down"))
    summary = er.reduce_journals(rec)
    assert summary["status"] == "incomplete"
    (process,) = [p for p in summary["processes"] if p["file"] == dying[0]]
    assert process["complete"] is False and process["records"] == len(recs)
    assert {"no-trailer", "item-unfinished"} <= kinds(summary["problems"])
    assert summary["accepted"] is False
    assert summary["first_error"] is not None


def test_a_missing_trailer_keeps_the_partial_records_and_is_incomplete(er, tmp_path):
    write_journal(tmp_path, "worker", 11, worker="gw0", starts=[("t.py::a@g", "g")], trailer=False)
    summary = er.reduce_journals(tmp_path, ["t.py::a"])
    assert summary["status"] == "incomplete"
    assert summary["items"]["t.py::a"]["started"] == 1
    assert "no-trailer" in kinds(summary["problems"])
    assert summary["accepted"] is False


# --- 7: the flag off ----------------------------------------------------------------------------------------------


def test_with_the_flag_off_nothing_is_registered_and_no_file_is_written(project, tmp_path):
    project.makepyfile(
        test_mod="""
import os

import experiment_record


def test_nothing_is_registered(pytestconfig):
    manager = pytestconfig.pluginmanager
    assert manager.get_plugin(experiment_record.PLUGIN_NAME) is None
    assert not [p for p in manager.get_plugins() if isinstance(p, experiment_record.Recorder)]
    assert experiment_record.HANDOFF_ENV not in os.environ
"""
    )
    result = project.runpytest_subprocess("-p", "xdist.plugin", "-n", "2", "--dist", "loadgroup", timeout=180)
    result.assert_outcomes(passed=1)
    assert not list(project.path.rglob("journal-*"))


def test_the_real_conftest_registers_the_recorder_only_when_asked(er, tmp_path):
    test = "tests/test_dev_gate.py::test_committed_pytest_timeout_has_wheel_leg_headroom"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_ADDOPTS", "PYTHONPATH"))}
    env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONPATH=str(SRC_DIR))

    def run(*extra, name):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "--trace-config",
                "--basetemp",
                str(tmp_path / name),
                test,
                *extra,
            ],
            cwd=TESTS_DIR.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )

    off = run(name="off")
    assert off.returncode == 0, off.stdout + off.stderr
    assert "experiment_record.Recorder" not in off.stdout

    rec = tmp_path / "rec"
    on = run(f"--experiment-record={rec}", name="on")
    assert on.returncode == 0, on.stdout + on.stderr
    assert "experiment_record.Recorder" in on.stdout
    summary = er.reduce_journals(rec, [test])
    assert summary["accepted"], summary["problems"]
    assert [p["role"] for p in summary["processes"]] == ["serial"]


# --- 8-9: errors are records --------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["serial", "parallel"])
def test_a_module_that_raises_at_import_gives_a_collection_error_and_is_the_first_error(project, er, tmp_path, mode):
    project.makepyfile(test_ok="def test_ok():\n    pass\n", test_bad="raise RuntimeError('boom at import')\n")
    rec = tmp_path / "rec"

    (run_serial if mode == "serial" else run_parallel)(project, rec)

    found = records(rec, "collection-error")
    assert found and all(r["nodeid"] == "test_bad.py" for _, r in found)
    assert all("boom at import" in r["error_text"] for _, r in found)
    summary = er.reduce_journals(rec, ["test_ok.py::test_ok"])
    assert summary["first_error"]["kind"] == "collection-error"
    assert summary["first_error"]["nodeid"] == "test_bad.py"
    assert summary["accepted"] is False


def test_a_hook_that_raises_gives_an_internal_error_and_a_clean_trailer_does_not_hide_it(project, er, tmp_path):
    # The second plugin is registered after the recorder and answers the hook itself, as the terminal reporter
    # does: the recorder must still have recorded the error. (pytest 9.1 calls every implementation, so this check
    # cannot tell tryfirst apart there; on a pytest where the hook stops at the first answer it can.)
    project.makeconftest(
        """
import experiment_record


def pytest_addoption(parser):
    experiment_record.add_option(parser)


class Eater:
    def pytest_internalerror(self, excrepr):
        return True


def pytest_configure(config):
    experiment_record.configure(config)
    config.pluginmanager.register(Eater())


def pytest_runtest_logstart(nodeid, location):
    raise RuntimeError("hook exploded")
"""
    )
    project.makepyfile(test_mod="def test_a():\n    pass\n")
    rec = tmp_path / "rec"

    run_serial(project, rec)

    (journal,) = read_journals(rec).values()
    order = [r["kind"] for r in journal]
    assert "internal-error" in order
    assert order.index("internal-error") < order.index("exit-status") < order.index("trailer") == len(order) - 1
    assert journal[-1]["complete"] is True
    summary = er.reduce_journals(rec)
    assert summary["status"] == "complete"
    assert summary["first_error"]["kind"] == "internal-error"
    assert "hook exploded" in summary["first_error"]["error_text"]
    assert summary["accepted"] is False


def test_the_first_error_is_never_replaced_by_a_later_one(er, tmp_path):
    first = ("collection-error", {"nodeid": "a.py", "error_text": "first"})
    later = ("internal-error", {"error_text": "later"})
    write_journal(tmp_path, "serial", 11, extra=[first, later, ("exit-status", {"exitstatus": 3})])
    summary = er.reduce_journals(tmp_path)
    assert summary["first_error"]["error_text"] == "first"


# --- 10: every event is fsynced -----------------------------------------------------------------------------------


def test_every_journal_event_calls_fsync_once_on_the_journal_file(project, monkeypatch, tmp_path):
    project.makepyfile(test_mod="def test_a():\n    pass\n\n\ndef test_b():\n    pass\n")
    rec = tmp_path / "rec"
    real_fsync = os.fsync
    seen = []

    def counting_fsync(fd):
        info = os.fstat(fd)
        seen.append((info.st_dev, info.st_ino))
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", counting_fsync)
    project.runpytest_inprocess(f"--experiment-record={rec}").assert_outcomes(passed=2)
    monkeypatch.undo()

    (journal,) = Path(rec).glob("journal-*.jsonl")
    info = os.stat(journal)
    events = len([line for line in journal.read_bytes().split(b"\n") if line])
    assert events >= 8
    assert seen.count((info.st_dev, info.st_ino)) == events


# --- D06: the plugin inventory -------------------------------------------------------------------------------------


def test_every_process_records_the_plugins_it_loaded_and_a_missing_field_is_flagged(project, er, tmp_path):
    project.makepyfile(test_mod="def test_a():\n    pass\n\n\ndef test_b():\n    pass\n")
    rec = tmp_path / "rec"
    run_parallel(project, rec).assert_outcomes(passed=2)

    starts = records(rec, "process-start")
    assert {r["role"] for _, r in starts} >= {"controller", "worker"}
    for _, r in starts:
        assert isinstance(r["plugins"], list) and "xdist.plugin" in r["plugins"], r["role"]
        assert r["plugins"] == sorted(r["plugins"])
    clean = er.reduce_journals(rec)
    assert "plugin-inventory-missing" not in kinds(clean["problems"])
    assert all(p["plugins"] and "xdist.plugin" in p["plugins"] for p in clean["processes"])

    # the control: the same record without the field is flagged
    name, _ = next((n, r) for n, r in starts if r["role"] == "worker")
    path = Path(rec) / name
    lines = path.read_bytes().decode("utf-8").split("\n")
    first = json.loads(lines[0])
    del first["plugins"]
    lines[0] = json.dumps(first, ensure_ascii=False)
    path.write_bytes("\n".join(lines).encode("utf-8"))
    flagged = er.reduce_journals(rec)
    assert "plugin-inventory-missing" in kinds(flagged["problems"])
    assert flagged["accepted"] is False


# --- D08: nested pytest runs ---------------------------------------------------------------------------------------

NESTED_CONFTEST = json.dumps(CONFTEST)[1:-1]  # the text of a double-quoted Python string literal

NESTED_PARENT = """
import os
import subprocess
import sys

import experiment_record

CONFTEST = "{conftest}"


def launch(tmp_path, env):
    (tmp_path / "conftest.py").write_text(CONFTEST, encoding="utf-8")
    (tmp_path / "test_child.py").write_text(
        "import os\\n\\n\\ndef test_child():\\n"
        f"    assert {experiment_record.HANDOFF_ENV!r} not in os.environ\\n",
        encoding="utf-8",
    )
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--basetemp", str(tmp_path / "bt"),
         str(tmp_path / "test_child.py")],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120,
    )


def test_with_the_helper(tmp_path):
    done = launch(tmp_path, experiment_record.nested_environment())
    assert done.returncode == 0, done.stdout + done.stderr


def test_without_the_helper(tmp_path):
    assert experiment_record.HANDOFF_ENV not in os.environ
    done = launch(tmp_path, dict(os.environ))
    assert done.returncode == 0, done.stdout + done.stderr


def test_helper_plus_addopts(tmp_path):
    env = experiment_record.nested_environment()
    # a parent that turned recording on through PYTEST_ADDOPTS: the child inherits the option AND the handoff
    env["PYTEST_ADDOPTS"] = "--experiment-record=" + experiment_record._ACTIVE.directory.as_posix()  # shlex-split
    done = launch(tmp_path, env)
    assert done.returncode == 0, done.stdout + done.stderr
""".replace("{conftest}", NESTED_CONFTEST)


def test_a_nested_run_started_with_the_helper_has_its_own_journal_tied_to_the_launch(project, er, tmp_path):
    project.makepyfile(test_parent=NESTED_PARENT)
    rec = tmp_path / "rec"

    run_serial(project, rec, "-k", "test_with_the_helper").assert_outcomes(passed=1)

    journals = read_journals(rec)
    assert len(journals) == 2, journals.keys()
    ((_, launch),) = records(rec, "nested-launch")
    ((_, parent_start),) = [(n, r) for n, r in records(rec, "process-start") if r["role"] == "serial"]
    ((_, nested_start),) = [(n, r) for n, r in records(rec, "process-start") if r["role"] == "nested"]
    assert nested_start["nested_token"] == launch["token"]
    assert nested_start["parent_pid"] == parent_start["pid"]
    assert nested_start["pid"] != parent_start["pid"]
    summary = er.reduce_journals(rec, ["test_parent.py::test_with_the_helper"])
    assert summary["accepted"], summary["problems"]
    assert summary["nested"] == {"launched": 1, "journaled": 1}
    assert sorted(p["role"] for p in summary["processes"]) == ["nested", "serial"]


def test_an_ordinary_nested_run_never_inherits_the_handoff(project, er, tmp_path):
    project.makepyfile(test_parent=NESTED_PARENT)
    rec = tmp_path / "rec"

    run_serial(project, rec, "-k", "test_without_the_helper").assert_outcomes(passed=1)

    assert list(read_journals(rec)) and len(read_journals(rec)) == 1
    assert [r["role"] for _, r in records(rec, "process-start")] == ["serial"]
    assert records(rec, "nested-launch") == []


def test_a_launch_without_a_nested_journal_is_a_failure_never_compliance(er, tmp_path):
    write_journal(
        tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=[("nested-launch", {"token": "0000000000000001"})]
    )
    summary = er.reduce_journals(tmp_path, ["t.py::a"])
    assert "nested-journal-missing" in kinds(summary["problems"])
    assert summary["accepted"] is False


def test_nested_sessions_seen_elsewhere_but_not_journaled_are_a_failure(er, tmp_path):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    assert er.reduce_journals(tmp_path, ["t.py::a"], nested_seen=0)["accepted"]
    summary = er.reduce_journals(tmp_path, ["t.py::a"], nested_seen=2)
    assert "nested-journal-missing" in kinds(summary["problems"])
    assert summary["accepted"] is False


def test_a_nested_journal_nobody_launched_is_flagged(er, tmp_path):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    write_journal(tmp_path, "nested", 12, nested_token="0000000000000002")
    summary = er.reduce_journals(tmp_path, ["t.py::a"])
    assert "nested-journal-unlaunched" in kinds(summary["problems"])
    assert summary["accepted"] is False


def test_the_helper_without_a_recorder_hands_off_nothing_and_drops_a_stale_entry(er, monkeypatch):
    # "without a recorder" holds in this process only if none is active: a run of this module with --experiment-record
    # (tests/conftest.py) has one, and the helper would then write a nested-launch to the real journal
    monkeypatch.setattr(er, "_ACTIVE", None)
    env = er.nested_environment({"Agenttalk_Experiment_Record_Handoff": "stale", "A": "1"})
    assert env == {"A": "1"}


@pytest.mark.parametrize(
    "value, message",
    [
        ("not json", "is not valid JSON"),
        (json.dumps({"v": 1, "dir": "rel", "token": "0000000000000001", "parent_pid": 1}), "needs an absolute folder"),
        (
            json.dumps({"v": 1, "dir": "ABSOLUTE-MISSING", "token": "0000000000000001", "parent_pid": 1}),
            "not an existing",
        ),
        (json.dumps({"v": 1, "dir": "ABSOLUTE-OK", "token": "../../escape", "parent_pid": 1}), "has a bad token"),
        (json.dumps({"v": 2, "dir": "ABSOLUTE-OK", "token": "0000000000000001", "parent_pid": 1}), "not a version 1"),
        ("x" * 5000, "is too long"),
    ],
    ids=["not-json", "relative-dir", "missing-dir", "bad-token", "bad-version", "too-long"],
)
def test_a_bad_handoff_is_refused_and_the_message_names_it(project, monkeypatch, tmp_path, value, message):
    (tmp_path / "exists").mkdir()
    (project.path / "rel").mkdir()  # a relative folder that does exist from the run's own folder
    value = value.replace("ABSOLUTE-MISSING", json.dumps(str(tmp_path / "nope"))[1:-1])
    value = value.replace("ABSOLUTE-OK", json.dumps(str(tmp_path / "exists"))[1:-1])
    monkeypatch.setenv(HANDOFF_NAME, value)
    project.makepyfile(test_mod="def test_a():\n    pass\n")

    result = project.runpytest_subprocess()

    assert result.ret == 4, result.stdout.str() + result.stderr.str()
    assert HANDOFF_NAME in result.stderr.str() and message in result.stderr.str(), result.stderr.str()
    assert not list(tmp_path.rglob("journal-*"))
    assert not list(project.path.rglob("journal-*"))


# --- the folder, the files, the text -----------------------------------------------------------------------------


def test_a_record_folder_inside_a_basetemp_is_refused(project, tmp_path):
    project.makepyfile(test_mod="def test_a():\n    pass\n")
    basetemp = project.path / "bt"

    result = project.runpytest_subprocess("--basetemp", str(basetemp), f"--experiment-record={basetemp / 'rec'}")

    assert result.ret == 4, result.stdout.str() + result.stderr.str()
    assert "inside the basetemp" in result.stderr.str()
    assert not (basetemp / "rec").exists()


def test_the_record_folder_reaches_workers_through_workerinput_not_the_environment(project, tmp_path):
    rec = tmp_path / "rec"
    project.makepyfile(
        test_mod=f"""
import os


def test_a():
    assert not [k for k, v in os.environ.items() if {str(rec)!r} in v], "the record folder is in the environment"


def test_b():
    test_a()
"""
    )
    run_parallel(project, rec).assert_outcomes(passed=2)
    roles = {r["role"] for _, r in records(rec, "process-start")}
    assert roles == {"controller", "worker"}
    assert len(records(rec, "trailer")) == 3
    assert not [r for n, r in records(rec, "phase") if n.startswith("journal-controller-")]
    worker_phases = sorted(r["when"] for n, r in records(rec, "phase") if n.startswith("journal-worker-"))
    assert worker_phases == ["call", "call", "setup", "setup", "teardown", "teardown"]


def test_a_serial_run_is_a_one_worker_run_for_the_reducer(project, er, tmp_path):
    project.makepyfile(test_mod="def test_a():\n    pass\n\n\ndef test_b():\n    pass\n")
    expected = collected_ids(project)
    rec = tmp_path / "rec"
    run_serial(project, rec).assert_outcomes(passed=2)
    summary = er.reduce_journals(rec, expected)
    assert summary["accepted"], summary["problems"]
    assert [p["role"] for p in summary["processes"]] == ["serial"]
    assert sorted(summary["items"]) == sorted(expected)
    ((_, collection),) = records(rec, "collection-finish")
    assert collection["count"] == 2 and collection["digest_algorithm"] == "sha256"
    assert collection["raw_ids_sha256"] == hashlib.sha256("\n".join(expected).encode("utf-8")).hexdigest()


def test_a_journal_name_carries_the_role_pid_and_start_time(project, tmp_path):
    project.makepyfile(test_mod="def test_a():\n    pass\n")
    rec = tmp_path / "rec"
    run_serial(project, rec).assert_outcomes(passed=1)
    ((name, start),) = records(rec, "process-start")
    pid, started = re.fullmatch(r"journal-serial-(\d+)-(\d+)\.jsonl", name).groups()
    assert (start["pid"], start["started_ms"]) == (int(pid), int(started))
    assert [r["seq"] for r in read_journals(rec)[name]] == list(range(1, len(read_journals(rec)[name]) + 1))


def test_non_ascii_text_is_written_as_utf_8_not_escaped(project, tmp_path):
    project.makeini("[pytest]\ndisable_test_id_escaping_and_forfeit_all_rights_to_community_support = true\n")
    project.makepyfile(
        test_mod="""
import pytest


@pytest.mark.parametrize("value", ["caf\\u00e9-\\u00fc", "\\u65e5\\u672c"])
def test_x(value):
    assert value
"""
    )
    rec = tmp_path / "rec"
    run_serial(project, rec).assert_outcomes(passed=2)
    (path,) = Path(rec).glob("journal-*.jsonl")
    raw = path.read_bytes()
    assert "café-ü".encode() in raw and "日本".encode() in raw
    assert b"\\u00e9" not in raw and b"\\u65e5" not in raw


def test_a_long_error_text_is_bounded_with_a_flag_and_the_digest_of_the_full_text(project, er, tmp_path):
    project.makepyfile(
        test_mod="""
def test_big():
    assert False, "x" * 200000 + "\\u2028tail \\u00e9"


def test_small():
    assert False, "short\\u2028line \\u00e9"
"""
    )
    rec = tmp_path / "rec"
    reprec = project.inline_run(f"--experiment-record={rec}")
    reports = {r.nodeid.split("::")[1]: r for r in reprec.getreports("pytest_runtest_logreport") if r.when == "call"}

    phases = {r["nodeid"].split("::")[1]: r for _, r in records(rec, "phase") if r["when"] == "call"}
    for name, bounded in (("test_big", True), ("test_small", False)):
        full = str(reports[name].longrepr)
        record = phases[name]
        assert record["error_digest_algorithm"] == "sha256"
        assert record["error_digest"] == hashlib.sha256(full.encode("utf-8")).hexdigest()
        assert record["error_truncated"] is bounded
        assert record["error_length"] == len(full.encode("utf-8"))
        assert len(record["error_text"].encode("utf-8")) <= er.MAX_ERROR_TEXT
        if not bounded:
            assert record["error_text"] == full
    (path,) = Path(rec).glob("journal-*.jsonl")
    assert path.read_bytes().count(b"\n") == len(read_journals(rec)[path.name])


def test_skip_and_xfail_reasons_are_recorded_with_the_phase(project, tmp_path):
    project.makepyfile(
        test_mod="""
import pytest


@pytest.mark.skip(reason="not today")
def test_skipped():
    pass


@pytest.mark.xfail(reason="known gap")
def test_xfailed():
    assert False


def test_body_skip():
    pytest.skip("body says no")
"""
    )
    rec = tmp_path / "rec"
    run_serial(project, rec).assert_outcomes(skipped=2, xfailed=1)
    reasons = {
        r["nodeid"].split("::")[1]: (r["outcome"], r["reason"]) for _, r in records(rec, "phase") if r.get("reason")
    }
    assert "not today" in reasons["test_skipped"][1]
    assert "body says no" in reasons["test_body_skip"][1]
    assert reasons["test_xfailed"] == ("skipped", "known gap")


def test_the_item_end_is_recorded_when_a_no_body_hook_answers_true(project, er, tmp_path):
    project.makeconftest(
        CONFTEST
        + """
import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    return True
"""
    )
    project.makepyfile(test_mod="def test_a():\n    pass\n")
    rec = tmp_path / "rec"
    run_serial(project, rec)
    (journal,) = read_journals(rec).values()
    order = [r["kind"] for r in journal if r["kind"].startswith("item")]
    assert order == ["item-start", "item-end"]


def test_an_existing_journal_file_is_never_reused(er, tmp_path):
    existing = tmp_path / "journal-serial-5-6.jsonl"
    existing.write_bytes(b"keep\n")
    with pytest.raises(FileExistsError):
        er.open_journal(tmp_path, "serial", 5, 6)
    assert existing.read_bytes() == b"keep\n"


def test_the_reducer_refuses_a_journal_with_two_names(er, tmp_path):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    os.link(path, tmp_path / "journal-serial-99-1000.jsonl")
    summary = er.reduce_journals(tmp_path, ["t.py::a"])
    assert "journal-not-plain" in kinds(summary["problems"])
    assert summary["accepted"] is False


def test_the_reducer_refuses_a_journal_over_the_size_limit(er, tmp_path, monkeypatch):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    monkeypatch.setattr(er, "MAX_JOURNAL_BYTES", 100)
    summary = er.reduce_journals(tmp_path, ["t.py::a"])
    assert "journal-too-large" in kinds(summary["problems"])
    assert summary["accepted"] is False


def test_a_torn_last_line_and_a_bad_line_are_reported(er, tmp_path):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], trailer=False)
    with open(path, "ab") as handle:
        handle.write(b'{"v": 1, "seq": 99, "kind": "phase", "no')
    summary = er.reduce_journals(tmp_path, ["t.py::a"])
    assert {"torn-last-line", "no-trailer"} <= kinds(summary["problems"])
    other = write_journal(tmp_path, "serial", 12, starts=[], trailer=False)
    other.write_bytes(other.read_bytes() + b"not json\n")
    assert "bad-line" in kinds(er.reduce_journals(tmp_path)["problems"])


def test_the_reducer_flags_a_gap_a_missing_start_records_after_the_trailer_and_bad_bytes(er, tmp_path):
    def lines(path):
        return path.read_bytes().split(b"\n")[:-1]

    gap = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    kept = lines(gap)
    gap.write_bytes(b"\n".join(kept[:1] + kept[2:]) + b"\n")
    assert "sequence-gap" in kinds(er.reduce_journals(tmp_path)["problems"])

    nostart = write_journal(tmp_path, "serial", 12, starts=[("t.py::b", None)])
    nostart.write_bytes(b"\n".join(lines(nostart)[1:]) + b"\n")
    assert "no-process-start" in kinds(er.reduce_journals(tmp_path)["problems"])

    after = write_journal(tmp_path, "serial", 13, starts=[])
    extra = json.loads(lines(after)[-1])
    extra.update(seq=extra["seq"] + 1, kind="exit-status", exitstatus=0)
    after.write_bytes(after.read_bytes() + json.dumps(extra).encode() + b"\n")
    summary = er.reduce_journals(tmp_path)
    assert "records-after-trailer" in kinds(summary["problems"])
    assert summary["status"] == "incomplete"

    garbage = tmp_path / "journal-serial-14-1000.jsonl"
    garbage.write_bytes(b"\xff\xfe\n")
    assert "bad-encoding" in kinds(er.reduce_journals(tmp_path)["problems"])


class _Factory:
    def __init__(self, basetemp):
        self._basetemp = basetemp

    def getbasetemp(self):
        return self._basetemp


def _fake_config(tmp_path, basetemp, given=None):
    from types import SimpleNamespace

    basetemp.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        option=SimpleNamespace(basetemp=given),
        _tmp_path_factory=_Factory(basetemp),
        invocation_params=SimpleNamespace(dir=tmp_path),
    )


def test_record_folder_containment_survives_dot_segments_case_and_a_name_prefix_sibling(er, tmp_path):
    root = tmp_path / "pytest-of-someone"
    basetemp = root / "pytest-3"
    config = _fake_config(tmp_path, basetemp)

    def check(path):
        return er._checked_directory(config, str(path), create=False, source="test")

    detour = tmp_path / "elsewhere" / ".." / "pytest-of-someone" / "pytest-3" / "rec"  # dot segments lead back in
    for inside in (basetemp / "rec", detour, root / "pytest-1" / "rec"):
        with pytest.raises(pytest.UsageError, match="inside the basetemp"):
            check(inside)
    if os.name == "nt":
        with pytest.raises(pytest.UsageError, match="inside the basetemp"):
            check(str(basetemp / "rec").upper())
    other = tmp_path / "pytest-of-someone-else"  # shares the root's name prefix, is not inside it
    other.mkdir()
    assert check(other) == other.resolve()

    # with an explicit --basetemp the numbered siblings are not at risk; a name-prefix sibling of it is outside
    given = tmp_path / "chosen"
    explicit = _fake_config(tmp_path, basetemp, given=str(given))
    for outside in (root / "pytest-1", root / "pytest-3-extra", tmp_path / "chosen-extra"):
        outside.mkdir()
        assert er._checked_directory(explicit, str(outside), create=False, source="test") == outside.resolve()
    given.mkdir()
    with pytest.raises(pytest.UsageError, match="inside the basetemp"):
        er._checked_directory(explicit, str(given / "rec"), create=False, source="test")


# --- fix round 1: CX1 redaction, CX2 torn tail, CX3 damaged fields, CX4 trailer count, CX5 spacing -----------------


def journal_bytes(rec):
    return b"\n".join(path.read_bytes() for path in sorted(Path(rec).glob("journal-*.jsonl")))


def fake_home(tmp_path, monkeypatch):
    home = str(tmp_path / "fake-home-folder-abcdef")
    monkeypatch.setenv("USERPROFILE", home)
    monkeypatch.setenv("HOME", home)
    return home


LEAKS = """
import os

import pytest


def test_assertion():
    assert os.environ["P3_S_ASSERT"] == "something else entirely"


def test_raise():
    raise RuntimeError("boom " + os.environ["P3_S_RAISE"])


def test_skip():
    pytest.skip("skip " + os.environ["P3_S_SKIP"])


@pytest.mark.xfail(reason="xfail marker " + os.environ["P3_S_XFAIL"])
def test_xfail_marker():
    assert False


def test_xfail_call():
    pytest.xfail("xfail call " + os.environ["P3_S_XFAIL"])


def test_home():
    assert os.path.expanduser("~") == "nowhere"
"""

SENTINELS = {
    "P3_S_ASSERT": "sentinel-assert-7Hq2",
    "P3_S_RAISE": "sentinel-raise-9Kd4",
    "P3_S_SKIP": "sentinel-skip-3Fw8",
    "P3_S_XFAIL": "sentinel-xfail-6Zr1",
    "P3_S_COLLECT": "sentinel-collect-5Tb0",
    "P3_S_INTERNAL": "sentinel-internal-2Nx7",
}


def plant(monkeypatch):
    for name, value in SENTINELS.items():
        monkeypatch.setenv(name, value)


def phases_of(rec):
    return {(r["nodeid"].split("::")[-1], r["when"]): r for _, r in records(rec, "phase")}


def test_environment_values_and_the_home_folder_never_reach_a_journal_from_failures_skips_and_xfails(
    project, er, tmp_path, monkeypatch
):
    # CX1, ruling 2-4: values from the recording process's environment (8 characters or longer) and the home folder are
    # replaced in every stored free text, whatever put them there: an assertion, a raised message, a skip reason, an
    # xfail marker's reason, an xfail called in the body.
    plant(monkeypatch)
    home = fake_home(tmp_path, monkeypatch)
    project.makepyfile(test_leaks=LEAKS.replace('"nowhere"', repr(home + "-is-not-it")))
    rec = tmp_path / "rec"

    run_serial(project, rec)

    # Search the DECODED text too: in the file JSON doubles every backslash, which hides a Windows path from a
    # byte search.
    raw = journal_bytes(rec)
    decoded = "\n".join(
        value for recs in read_journals(rec).values() for r in recs for value in r.values() if isinstance(value, str)
    )
    for name, value in SENTINELS.items():
        assert value.encode() not in raw and value not in decoded, f"{name} reached the journal"
    for form in {home, home.replace("\\", "/"), home.replace("\\", "\\\\")}:
        assert form.encode() not in raw and form not in decoded, f"the home folder reached the journal: {form}"
    phases = phases_of(rec)
    assert "<env:P3_S_ASSERT>" in phases[("test_assertion", "call")]["error_text"]
    assert phases[("test_assertion", "call")]["error_redactions"] >= 1
    assert "<env:P3_S_RAISE>" in phases[("test_raise", "call")]["error_text"]
    assert "<home>" in phases[("test_home", "call")]["error_text"]
    assert "<env:P3_S_SKIP>" in phases[("test_skip", "call")]["reason"]
    assert phases[("test_skip", "call")]["reason_redactions"] >= 1
    assert "<env:P3_S_XFAIL>" in phases[("test_xfail_marker", "call")]["reason"]
    assert "<env:P3_S_XFAIL>" in phases[("test_xfail_call", "call")]["reason"]


def test_a_collection_error_text_is_cleaned(project, er, tmp_path, monkeypatch):
    # CX1, ruling 3: collection-error text.
    plant(monkeypatch)
    project.makepyfile(test_bad="import os\nraise RuntimeError('import ' + os.environ['P3_S_COLLECT'])\n")
    rec = tmp_path / "rec"
    run_serial(project, rec)
    ((_, record),) = records(rec, "collection-error")
    assert "<env:P3_S_COLLECT>" in record["error_text"] and record["error_redactions"] >= 1
    assert SENTINELS["P3_S_COLLECT"].encode() not in journal_bytes(rec)


def test_an_internal_error_text_is_cleaned(project, er, tmp_path, monkeypatch):
    # CX1, ruling 3: internal-error text.
    plant(monkeypatch)
    project.makeconftest(
        CONFTEST
        + """
import os


def pytest_runtest_logstart(nodeid, location):
    raise RuntimeError("hook " + os.environ["P3_S_INTERNAL"])
"""
    )
    project.makepyfile(test_mod="def test_a():\n    pass\n")
    rec = tmp_path / "rec"
    run_serial(project, rec)
    ((_, record),) = records(rec, "internal-error")
    assert "<env:P3_S_INTERNAL>" in record["error_text"] and record["error_redactions"] >= 1
    assert SENTINELS["P3_S_INTERNAL"].encode() not in journal_bytes(rec)


def _recorder(er, tmp_path):
    config = _fake_config(tmp_path, tmp_path / "bt")
    directory = tmp_path / "record-folder"
    directory.mkdir()
    return er.Recorder(config, directory, "serial", None, None, None), directory


def test_a_worker_down_text_is_cleaned(er, tmp_path, monkeypatch):
    # CX1, ruling 3: worker-down text (the controller's record of a crashed worker).
    monkeypatch.setenv("P3_S_WORKER", "sentinel-worker-4Vc9")
    recorder, directory = _recorder(er, tmp_path)
    try:
        node = type("Node", (), {"gateway": type("Gateway", (), {"id": "gw7"})()})()
        recorder.pytest_testnodedown(node, "worker crashed near sentinel-worker-4Vc9")
    finally:
        recorder.close()
    ((_, record),) = records(directory, "worker-down")
    assert "<env:P3_S_WORKER>" in record["error_text"] and record["error_redactions"] == 1
    assert b"sentinel-worker-4Vc9" not in journal_bytes(directory)


def test_the_digest_is_over_the_original_text_and_the_stored_copy_is_cleaned(er, tmp_path, monkeypatch):
    # CX1, ruling 1: error_digest, error_length and the algorithm come from the ORIGINAL full text; the stored copy is
    # cleaned afterwards and error_redactions counts the replacements.
    monkeypatch.setenv("P3_S_DIGEST", "sentinel-digest-8Lp3")
    recorder, directory = _recorder(er, tmp_path)
    original = "left sentinel-digest-8Lp3 and again sentinel-digest-8Lp3 é"
    try:
        fields = recorder.error_fields(original)
    finally:
        recorder.close()
    assert fields["error_digest"] == hashlib.sha256(original.encode("utf-8")).hexdigest()
    assert fields["error_length"] == len(original.encode("utf-8"))
    assert fields["error_digest_algorithm"] == "sha256"
    assert fields["error_text"] == "left <env:P3_S_DIGEST> and again <env:P3_S_DIGEST> é"
    assert fields["error_redactions"] == 2


def test_a_value_is_cleaned_from_eight_characters_and_not_below(er, tmp_path, monkeypatch):
    # CX1, ruling 2 and 5: 'values shorter than 8 characters cannot be recognised'; the limit is stated, and exact.
    monkeypatch.setenv("P3_SEVEN", "abcdefg")
    monkeypatch.setenv("P3_EIGHT", "hijklmno")
    recorder, _ = _recorder(er, tmp_path)
    try:
        fields = recorder.error_fields("seven abcdefg eight hijklmno")
    finally:
        recorder.close()
    assert fields["error_text"] == "seven abcdefg eight <env:P3_EIGHT>"


def test_every_part_of_a_path_list_value_is_cleaned_and_a_blank_value_is_not(er, tmp_path, monkeypatch):
    # CX1, ruling 2: 'every os.pathsep-separated part of such a value'; a value of blanks would erase every run
    # of spaces.
    monkeypatch.setenv("P3_LIST", f"first-part-folder{os.pathsep}second-part-folder")
    monkeypatch.setenv("P3_BLANK", " " * 12)
    recorder, _ = _recorder(er, tmp_path)
    try:
        fields = recorder.error_fields("in second-part-folder only, with gaps            kept")
    finally:
        recorder.close()
    assert fields["error_text"] == "in <env:P3_LIST> only, with gaps            kept"


def test_the_longest_value_is_replaced_first(er, tmp_path, monkeypatch):
    # CX1, ruling 2: 'longest first': a long value that contains a shorter one is replaced whole.
    monkeypatch.setenv("P3_LONG", "ABCDEFGHIJKLMNOP")
    monkeypatch.setenv("P3_SHORT_ONE", "ABCDEFGH")
    recorder, _ = _recorder(er, tmp_path)
    try:
        fields = recorder.error_fields("x ABCDEFGHIJKLMNOP y ABCDEFGH z")
    finally:
        recorder.close()
    assert fields["error_text"] == "x <env:P3_LONG> y <env:P3_SHORT_ONE> z"


def test_the_known_folders_are_named_not_shown(er, tmp_path, monkeypatch):
    # CX1, ruling 2: home -> <home>, temp -> <temp>, the record folder -> <record>, the basetemp -> <basetemp>,
    # in either slash direction (and without regard to case on Windows).
    import tempfile

    home = fake_home(tmp_path, monkeypatch)
    temp = str(tmp_path / "fake-temp-folder-ghijkl")
    monkeypatch.setattr(tempfile, "tempdir", temp)
    recorder, directory = _recorder(er, tmp_path)
    basetemp = str(tmp_path / "bt")
    try:
        text = " | ".join(
            [
                home + "\\a.py",
                temp + "/b.py",
                str(directory) + "\\c.json",
                basetemp + "/d.txt",
                str(directory).replace("\\", "/") + "/e",
                repr(home)[1:-1] + "\\g.py",
            ]
        )
        fields = recorder.error_fields(text)
        shouted = recorder.error_fields(str(directory).upper() + "\\f")
    finally:
        recorder.close()
    assert fields["error_text"] == (
        "<home>\\a.py | <temp>/b.py | <record>\\c.json | <basetemp>/d.txt | <record>/e | <home>\\g.py"
    )
    assert fields["error_redactions"] == 6
    if os.name == "nt":
        assert shouted["error_text"] == "<record>\\f"


def test_a_value_cut_by_the_size_limit_cannot_leak_a_fragment(er, tmp_path, monkeypatch):
    # CX1, ruling 2: 'cleaned, before it is bounded': a secret that straddles the 64 KiB cut is replaced first, so no
    # half of it is stored.
    monkeypatch.setenv("P3_S_EDGE", "sentinel-edge-1Qa5-extra-long-tail")
    recorder, _ = _recorder(er, tmp_path)
    text = "x" * (er.MAX_ERROR_TEXT - 6) + "sentinel-edge-1Qa5-extra-long-tail" + "y" * 100
    try:
        fields = recorder.error_fields(text)
    finally:
        recorder.close()
    assert fields["error_truncated"] is True
    assert "sentinel" not in fields["error_text"] and "Qa5" not in fields["error_text"]
    assert fields["error_digest"] == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_a_child_killed_inside_a_multibyte_character_keeps_the_complete_lines_before_it(project, er, tmp_path):
    # CX2: the writer dies after the first byte of a two-byte character. The earlier complete lines are durable; the
    # reducer keeps them and reports the damaged tail, instead of dropping the whole journal.
    project.makepyfile(
        test_mod="""
import os

import experiment_record


def test_dies(pytestconfig):
    recorder = pytestconfig.pluginmanager.get_plugin(experiment_record.PLUGIN_NAME)
    os.write(recorder._fd, bytes([0xC3]))
    os._exit(77)
"""
    )
    rec = tmp_path / "rec"
    result = run_serial(project, rec)
    assert result.ret == 77
    (path,) = Path(rec).glob("journal-*.jsonl")
    assert path.read_bytes().endswith(bytes([0xC3]))

    summary = er.reduce_journals(rec)
    (process,) = summary["processes"]
    assert process["records"] >= 4, summary["problems"]
    assert {"torn-last-line", "no-trailer"} <= kinds(summary["problems"])
    assert "bad-encoding" not in kinds(summary["problems"])
    assert summary["items"]["test_mod.py::test_dies"]["started"] == 1
    assert summary["status"] == "incomplete" and summary["accepted"] is False


def test_a_complete_line_that_is_not_utf_8_costs_only_that_line(er, tmp_path):
    # CX2: bad bytes inside a COMPLETE line are reported for that line alone; the lines around it are kept.
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None), ("t.py::b", None)])
    lines = path.read_bytes().split(b"\n")
    lines.insert(3, b'{"seq": 3, "kind": "x", "bad": "\xff"}')
    path.write_bytes(b"\n".join(lines))
    summary = er.reduce_journals(tmp_path, ["t.py::a", "t.py::b"])
    assert "bad-encoding" in kinds(summary["problems"])
    assert summary["items"]["t.py::a"]["started"] == 1 and summary["items"]["t.py::b"]["started"] == 1


def damaged(path, match, **changes):
    """Rewrite the first record whose kind is `match`: a value of None deletes the key."""
    lines = path.read_bytes().decode("utf-8").split("\n")
    for index, line in enumerate(lines):
        if line and json.loads(line)["kind"] == match:
            record = json.loads(line)
            for key, value in changes.items():
                if value is None:
                    record.pop(key, None)
                else:
                    record[key] = value
            lines[index] = json.dumps(record)
            break
    else:
        raise AssertionError(f"no {match} record")
    path.write_bytes("\n".join(lines).encode("utf-8"))


@pytest.mark.parametrize(
    "kind, changes",
    [
        ("item-end", {"t_wall": {}}),
        ("item-start", {"t_wall": "yesterday"}),
        ("item-start", {"nodeid": None}),
        ("item-start", {"nodeid": 7}),
        ("item-start", {"requested_group": ["g"]}),
        ("phase", {"outcome": ["failed"]}),
        ("item-end", {"pid": "x"}),
        ("process-start", {"role": 5}),
        ("process-start", {"plugins": [1, 2]}),
        ("process-start", {"nested_token": 12}),
        ("trailer", {"records": "many"}),
        ("trailer", {"records": True}),
        ("item-end", {"pid": True}),
        ("trailer", {"complete": "yes"}),
        ("exit-status", {"exitstatus": "ok"}),
        ("worker-down", {"has_error": ["boom"]}),
        ("collection-error", {"error_text": 3}),
        ("nested-launch", {"token": 9}),
    ],
    ids=lambda v: v if isinstance(v, str) else next(iter(v)),
)
def test_a_record_with_a_damaged_field_is_reported_and_the_rest_is_kept(er, tmp_path, kind, changes):
    # CX3: a valid JSON line with a malformed field is a problem to report, never an exception from the reducer;
    # the usable evidence is kept (here: the second item still joins).
    extra = [
        ("phase", {"nodeid": "t.py::a", "when": "call", "outcome": "passed", "duration": 0.1}),
        ("worker-down", {"down_worker": "gw0", "has_error": True, "error_text": "x"}),
        ("collection-error", {"nodeid": "c.py", "error_text": "boom"}),
        ("exit-status", {"exitstatus": 1}),
        ("nested-launch", {"token": "0000000000000001"}),
    ]
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None), ("t.py::b", None)], extra=extra)
    damaged(path, kind, **changes)

    summary = er.reduce_journals(tmp_path, ["t.py::a", "t.py::b"])

    assert "bad-field" in kinds(summary["problems"]), summary["problems"]
    assert "sequence-gap" not in kinds(summary["problems"]), "a set-aside record must keep its place in the sequence"
    assert summary["accepted"] is False
    assert "t.py::b" in summary["items"] and summary["items"]["t.py::b"]["started"] == 1


def test_a_trailer_counts_every_record_before_it_in_a_real_journal(project, er, tmp_path):
    # CX4: `records` is how many records precede the trailer; asserted against a real written journal.
    project.makepyfile(test_mod="def test_a():\n    pass\n\n\ndef test_b():\n    pass\n")
    rec = tmp_path / "rec"
    run_serial(project, rec).assert_outcomes(passed=2)
    (recs,) = read_journals(rec).values()
    trailer = recs[-1]
    assert trailer["kind"] == "trailer"
    assert trailer["records"] == len(recs) - 1
    assert er.reduce_journals(rec)["status"] == "complete"


def test_a_trailer_that_miscounts_is_reported(er, tmp_path):
    # CX4: the reducer checks the count too.
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    damaged(path, "trailer", records=99)
    summary = er.reduce_journals(tmp_path, ["t.py::a"])
    assert "trailer-count-mismatch" in kinds(summary["problems"])
    assert summary["accepted"] is False


def test_the_conftest_wiring_line_is_formatted_as_ruff_wants():
    # CX5: two spaces before an inline comment, on the one line P3 adds to tests/conftest.py (the file was already
    # unformatted elsewhere; P3 must not add to that).
    text = (TESTS_DIR / "conftest.py").read_text(encoding="utf-8")
    (line,) = [row for row in text.splitlines() if "experiment_record.configure(config)" in row]
    code = line.split("#", 1)[0].rstrip()
    assert code.endswith(")"), repr(line)
    assert line.index("#") - len(code) == 2, repr(line)


# --- fix round 2: N1 shortened long values, CX3 rest (numbers outside the float range) -------------------------------

LONG_SENTINELS = {
    "P3_S_LONG32": "Q7xK2mP9vL4nR8tW3yB6cD1fG5hJ0zA9",
    "P3_S_LONG64": "mZ8cV3bN6qW1eR4tY7uI0oP2aS5dF9gH3jK6lX1zC4vB7nM0qW8eR2tY5uI9oP3a",
}

LONG_TESTS = """
import os


def test_long_32():
    assert os.environ["P3_S_LONG32"] == "short"


def test_long_64():
    assert os.environ["P3_S_LONG64"] == "short"
"""


def windows_of(value, size=8):
    return {value[i : i + size] for i in range(len(value) - size + 1)}


def test_a_long_value_that_pytest_shortens_does_not_reach_the_journal(project, er, tmp_path, monkeypatch):
    # N1 (P1): pytest shows a long compared value as 'head...tail' (about 12 characters each side); neither piece is the
    # whole value, so the first pass cannot find it. The second pass joins head and tail back to one recognised value.
    for name, value in LONG_SENTINELS.items():
        monkeypatch.setenv(name, value)
    project.makepyfile(test_long=LONG_TESTS)
    rec = tmp_path / "rec"

    run_serial(project, rec)

    decoded = "\n".join(
        value for recs in read_journals(rec).values() for r in recs for value in r.values() if isinstance(value, str)
    )
    raw = journal_bytes(rec).decode("utf-8")
    for name, value in LONG_SENTINELS.items():
        leaked = [w for w in windows_of(value) if w in decoded or w in raw]
        assert not leaked, f"{name}: pieces of the value reached the journal: {sorted(leaked)[:3]}"
    phases = phases_of(rec)
    assert "<env:P3_S_LONG32>" in phases[("test_long_32", "call")]["error_text"]
    assert "<env:P3_S_LONG64>" in phases[("test_long_64", "call")]["error_text"]


def test_a_shortened_form_is_joined_back_to_its_value_and_counted(er, tmp_path, monkeypatch):
    value = LONG_SENTINELS["P3_S_LONG32"]
    monkeypatch.setenv("P3_S_LONG32", value)
    recorder, _ = _recorder(er, tmp_path)
    try:
        fields = recorder.error_fields(f"assert '{value[:11]}...{value[-11:]}' == 'x'")
    finally:
        recorder.close()
    assert fields["error_text"] == "assert '<env:P3_S_LONG32>' == 'x'"
    assert fields["error_redactions"] == 1


@pytest.mark.parametrize(
    ("text", "unchanged"),
    [
        ("{h3}...{t3}", True),  # pieces under 4 characters are not recognised
        ("{h4}...{t4}", False),  # 4 and 4 are
        ("{h4}...{o4}", True),  # a head of this value with the tail of another value
        ("Loading...", True),  # an ellipsis in ordinary text
        ("{h4} ... {t4}", True),  # the ellipsis must sit between the pieces
    ],
)
def test_the_shortened_form_needs_both_pieces_of_one_value(er, tmp_path, monkeypatch, text, unchanged):
    value = LONG_SENTINELS["P3_S_LONG32"]
    other = LONG_SENTINELS["P3_S_LONG64"]
    monkeypatch.setenv("P3_S_LONG32", value)
    monkeypatch.setenv("P3_S_LONG64", other)
    given = text.format(h3=value[:3], t3=value[-3:], h4=value[:4], t4=value[-4:], o4=other[-4:])
    recorder, _ = _recorder(er, tmp_path)
    try:
        fields = recorder.error_fields(given)
    finally:
        recorder.close()
    assert (fields["error_text"] == given) is unchanged, fields["error_text"]
    assert fields["error_redactions"] == (0 if unchanged else 1)


_RUNNER_PATH = (
    "/opt/hostedtoolcache/Python/3.12.15/x64/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
)


def _needles_with_a_long_path(value):
    """The needles of a Linux runner: the sentinel, the whole PATH and its parts, labelled as the recorder does."""
    found = {value: "<env:P3_S_LONG32>", _RUNNER_PATH: "<env:PATH>"}
    for part in _RUNNER_PATH.split(":"):
        if len(part) >= 8:
            found.setdefault(part, "<env:PATH>")
    return tuple(sorted(found, key=lambda needle: (-len(needle), needle))), found


@pytest.mark.parametrize("ignorecase", [False, True], ids=["case-kept-posix", "case-folded-windows"])
def test_the_text_folder_is_the_same_function_on_every_os(er, ignorecase):
    # CI round 19, part A: a second module-level `_identity` (for stat results) replaced the text folder's, so with
    # ignorecase False (Linux and macOS) every comparison saw the same constant and every value matched everything.
    fold = er._folder(ignorecase)
    assert fold("Ab9zA9") == ("ab9za9" if ignorecase else "Ab9zA9")
    assert fold("Ab9zA9") != fold("other")


@pytest.mark.parametrize("ignorecase", [False, True], ids=["case-kept-posix", "case-folded-windows"])
@pytest.mark.parametrize(
    ("template", "masked"),
    [
        ("{h3}...{t3}", False),  # pieces under 4 characters are not recognised
        ("{h4}...{t4}", True),
        ("{h4}...{o4}", False),
        ("Loading...", False),
        ("{h4} ... {t4}", False),
        ("[{h12}...", True),  # a lone head of 12 characters
        ("...{t12}]", True),  # a lone tail of 12 characters
    ],
)
def test_a_shortened_form_is_matched_the_same_way_with_a_long_path_in_the_environment(er, ignorecase, template, masked):
    value = LONG_SENTINELS["P3_S_LONG32"]
    ordered, found = _needles_with_a_long_path(value)
    text = template.format(
        h3=value[:3], t3=value[-3:], h4=value[:4], t4=value[-4:], o4="zzzz", h12=value[:12], t12=value[-12:]
    )
    spans = er._shortened_spans(text, ordered, found, ignorecase)
    assert bool(spans) is masked, spans
    assert {label for _, _, label in spans} <= ({"<env:P3_S_LONG32>"} if masked else set()), spans


def test_a_huge_integer_as_the_time_is_a_bad_field_not_a_crash(er, tmp_path):
    # CX3 rest: math.isfinite(10**400) raises OverflowError; the whole summary was lost.
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None), ("t.py::b", None)])
    damaged(path, "item-end", t_wall=10**400)
    summary = er.reduce_journals(tmp_path, ["t.py::a", "t.py::b"])
    assert "bad-field" in kinds(summary["problems"]) and "sequence-gap" not in kinds(summary["problems"])
    assert summary["items"]["t.py::b"]["started"] == 1


def test_an_integer_too_long_for_json_is_a_bad_line_not_a_crash(er, tmp_path):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    lines = path.read_bytes().split(b"\n")
    lines.insert(2, b'{"v": 1, "seq": 3, "kind": "x", "role": "serial", "pid": 11, "t_wall": ' + b"9" * 5000 + b"}")
    path.write_bytes(b"\n".join(lines))
    summary = er.reduce_journals(tmp_path, ["t.py::a"])
    assert "bad-line" in kinds(summary["problems"])
    assert summary["items"]["t.py::a"]["started"] == 1


def test_the_docstring_states_the_limits_of_the_cleaning(er):
    text = er.__doc__.lower()
    assert "node ids are kept exactly as pytest makes them" in text
    assert "recognised only by its first and last characters" in text
    flat = " ".join(text.split())
    assert "both looked for in the original text" in flat
    assert "a 64 kib text full of `...` is cleaned" in flat
    assert "the masked stretch is the union of their spans" in flat
    assert "crafted on purpose to overlap or compete" in flat
    assert "a lone piece is masked as well" in flat
    assert "a lone head or tail under 8 characters" in flat
    assert "the probe jobs run without secrets" in flat


# --- fix round 3: N1 values that start alike, P2 a slow second pass ----------------------------------------------

_LONG32 = LONG_SENTINELS["P3_S_LONG32"]
_LONG64 = LONG_SENTINELS["P3_S_LONG64"]
OVERLAP_SENTINELS = {
    32: _LONG32,
    64: _LONG64,
    200: (_LONG32[::-1] + _LONG64 + _LONG32.swapcase() + _LONG64[::-1] + _LONG32)[:200],
}

OVERLAP_TESTS = "import os\n\nimport pytest\n" + "".join(
    f"""

def test_assert_{size}():
    assert os.environ["P3_S_OVL{size}"] == "short"


def test_raise_{size}():
    value = os.environ["P3_S_OVL{size}"]
    raise ValueError(f"{{value[:12]}}...{{value[-12:]}}")


def test_skip_{size}():
    value = os.environ["P3_S_OVL{size}"]
    pytest.skip(f"{{value[:12]}}...{{value[-12:]}}")
"""
    for size in OVERLAP_SENTINELS
)


def set_overlapping_values(monkeypatch):
    """Each sentinel, and a second variable that holds only its first 12 characters."""
    for size, value in OVERLAP_SENTINELS.items():
        monkeypatch.setenv(f"P3_S_OVL{size}", value)
        monkeypatch.setenv(f"P3_S_OVL{size}_HEAD", value[:12])


def test_a_value_that_is_the_start_of_another_does_not_hide_its_shortened_form(project, er, tmp_path, monkeypatch):
    # N1 (P1, round 4): the whole-value pass used to replace the shared first 12 characters first, so the shortened
    # 'head...tail' could no longer be matched to the long value and its tail was stored.
    set_overlapping_values(monkeypatch)
    project.makepyfile(test_overlap=OVERLAP_TESTS)
    rec = tmp_path / "rec"

    run_serial(project, rec)

    decoded = "\n".join(
        value for recs in read_journals(rec).values() for r in recs for value in r.values() if isinstance(value, str)
    )
    raw = journal_bytes(rec).decode("utf-8")
    for size, value in OVERLAP_SENTINELS.items():
        leaked = [w for w in windows_of(value) if w in decoded or w in raw]
        assert not leaked, f"{size} characters: pieces of the value reached the journal: {sorted(leaked)[:3]}"
    phases = phases_of(rec)
    for size in OVERLAP_SENTINELS:
        assert f"<env:P3_S_OVL{size}>" in phases[(f"test_raise_{size}", "call")]["error_text"]
        assert phases[(f"test_skip_{size}", "call")]["reason"] == f"Skipped: <env:P3_S_OVL{size}>"
        assert phases[(f"test_assert_{size}", "call")]["error_redactions"] >= 1


@pytest.mark.parametrize("size", sorted(OVERLAP_SENTINELS))
def test_overlapping_values_keep_the_digest_of_the_original_text(er, tmp_path, monkeypatch, size):
    set_overlapping_values(monkeypatch)
    value = OVERLAP_SENTINELS[size]
    shortened = f"{value[:12]}...{value[-12:]}"
    text = f"ValueError: '{shortened}' and in full {value}"
    recorder, _ = _recorder(er, tmp_path)
    try:
        fields = recorder.error_fields(text)
        reason = recorder.reason_fields(shortened)
    finally:
        recorder.close()
    label = f"<env:P3_S_OVL{size}>"
    assert fields["error_text"] == f"ValueError: '{label}' and in full {label}"
    assert fields["error_redactions"] == 2
    assert fields["error_digest"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert fields["error_length"] == len(text.encode("utf-8"))
    assert (reason["reason"], reason["reason_redactions"]) == (label, 1)


def test_a_value_that_ends_inside_a_head_is_one_replacement_with_the_longer_label(er, tmp_path, monkeypatch):
    # The two spans start in different places: the value `pre-<first 6 characters>` begins 4 characters before the head.
    value = LONG_SENTINELS["P3_S_LONG32"]
    monkeypatch.setenv("P3_S_LONG32", value)
    monkeypatch.setenv("P3_S_ENDING", "pre-" + value[:6])
    recorder, _ = _recorder(er, tmp_path)
    try:
        fields = recorder.error_fields(f"got 'pre-{value[:11]}...{value[-11:]}' here")
    finally:
        recorder.close()
    assert fields["error_text"] == "got '<env:P3_S_LONG32>' here"
    assert fields["error_redactions"] == 1


def test_two_neighbouring_values_are_still_two_replacements(er, tmp_path, monkeypatch):
    # Spans that only touch are not merged: each value keeps its own label and its own count.
    monkeypatch.setenv("P3_S_FIRST", "first-value-9Qw2")
    monkeypatch.setenv("P3_S_SECOND", "second-value-4Zr7")
    recorder, _ = _recorder(er, tmp_path)
    try:
        fields = recorder.error_fields("first-value-9Qw2second-value-4Zr7")
    finally:
        recorder.close()
    assert fields["error_text"] == "<env:P3_S_FIRST><env:P3_S_SECOND>"
    assert fields["error_redactions"] == 2


CLEAN_TIME_LIMIT = 5.0  # seconds a child process may take to clean each text below; the real figure is far smaller

TIMING_SCRIPT = """
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, sys.argv[1])
import experiment_record

directory = Path(sys.argv[2])
config = SimpleNamespace(option=SimpleNamespace(basetemp=None), _tmp_path_factory=None)
redactor = experiment_record.Redactor(config, directory)
size = 64 * 1024
form = "{head}...{tail} ".format(head=os.environ["P3_S_TIME00"][:11], tail=os.environ["P3_S_TIME00"][-11:])
texts = {
    "dots": "." * size,
    "ellipses": "..." * (size // 3),
    "shortened forms": form * (size // len(form)),
    "heads without tails": (os.environ["P3_S_TIME00"][:11] + "... ") * (size // 16),
    "tails without heads": ("..." + os.environ["P3_S_TIME00"][-11:] + " ") * (size // 15),
}
seconds = {}
for name, text in texts.items():
    started = time.perf_counter()
    cleaned, count = redactor.clean(text)
    seconds[name] = round(time.perf_counter() - started, 3)
print(json.dumps(seconds))
"""


def test_a_long_text_full_of_ellipses_is_cleaned_within_a_small_time_limit(tmp_path):
    # P2 (round 4): the shortened-form pass tried every value at up to 256 lengths for every '...', so 64 KiB of dots
    # took over a minute. It runs in a child process so that a slow pass is stopped by the timeout, not waited for.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "AGENTTALK_", "PYTHONPATH"))}
    for number in range(60):
        env[f"P3_S_TIME{number:02d}"] = f"time-value-{number:02d}-" + "wQ7zK2mP9vL4nR8tW3yB6cD1fG5hJ0"
    done = subprocess.run(
        [sys.executable, "-I", "-c", TIMING_SCRIPT, str(TESTS_DIR), str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    seconds = json.loads(done.stdout.strip().splitlines()[-1])
    assert set(seconds) == {"dots", "ellipses", "shortened forms", "heads without tails", "tails without heads"}
    slow = {name: taken for name, taken in seconds.items() if taken > CLEAN_TIME_LIMIT}
    assert not slow, f"cleaning took longer than {CLEAN_TIME_LIMIT} seconds: {slow}"


# --- fix round 4: N1 the union of every valid shortened-form match -------------------------------------------------

COMPETING_HEAD = "Hq3vN8zK2mXw"
COMPETING_TAIL = "Ry5tB7cJ9dLp"
COMPETING_B64 = COMPETING_HEAD + "Z" * 40 + COMPETING_TAIL
COMPETING_A200 = COMPETING_HEAD + "A" * 184 + COMPETING_TAIL[:4]

COMPETING_TESTS = """
import os

import pytest


def pieces():
    big = os.environ["P3_S_CB64"]
    return big[:12], big[-12:]


def test_raise():
    head, tail = pieces()
    raise ValueError(f"{head}...{tail}")


def test_skip():
    head, tail = pieces()
    pytest.skip(f"{head}...{tail}")
"""


def set_competing_values(monkeypatch):
    """Two values that both fit `H...T` (the longer one shows only T[:4] of the tail) and a third that equals H."""
    monkeypatch.setenv("P3_S_CB64", COMPETING_B64)
    monkeypatch.setenv("P3_S_CA200", COMPETING_A200)
    monkeypatch.setenv("P3_S_CHEAD", COMPETING_HEAD)


def test_two_values_that_fit_one_shortened_form_are_masked_by_their_union(er, tmp_path, monkeypatch):
    # N1 (P1, round 5): the first value that fitted (the longer A200) masked only T[:4] and left T[4:] of B64 stored.
    set_competing_values(monkeypatch)
    text = f"{COMPETING_HEAD}...{COMPETING_TAIL}"
    recorder, _ = _recorder(er, tmp_path)
    try:
        fields = recorder.error_fields(text)
        reason = recorder.reason_fields(text)
    finally:
        recorder.close()
    assert fields["error_text"] == "<env:P3_S_CB64>"
    assert fields["error_redactions"] == 1
    assert (reason["reason"], reason["reason_redactions"]) == ("<env:P3_S_CB64>", 1)
    assert fields["error_digest"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert fields["error_length"] == len(text.encode("utf-8"))


def test_the_union_of_matches_reaches_the_journal_of_a_real_run(project, er, tmp_path, monkeypatch):
    # The same case through a real pytest child: a raised message and a skip reason, nothing of B64's tail stored.
    set_competing_values(monkeypatch)
    project.makepyfile(test_competing=COMPETING_TESTS)
    rec = tmp_path / "rec"

    run_serial(project, rec)

    decoded = "\n".join(
        value for recs in read_journals(rec).values() for r in recs for value in r.values() if isinstance(value, str)
    )
    raw = journal_bytes(rec).decode("utf-8")
    leaked = [w for w in windows_of(COMPETING_TAIL) if w in decoded or w in raw]
    assert not leaked, f"pieces of the longer value's tail reached the journal: {sorted(leaked)[:3]}"
    phases = phases_of(rec)
    assert "<env:P3_S_CB64>" in phases[("test_raise", "call")]["error_text"]
    assert phases[("test_skip", "call")]["reason"] == "Skipped: <env:P3_S_CB64>"


# --- fix round 5: N1 a lone head or a lone tail of one value ------------------------------------------------------

LONE_VALUE = LONG_SENTINELS["P3_S_LONG64"]
LONE_NEIGHBOUR = "a-long-neighbouring-item-0123456789"

LONE_TESTS = """
import os

VALUE = os.environ["P3_S_ONE"]
NEIGHBOUR = "a-long-neighbouring-item-0123456789"


def test_list_value_first():
    assert [VALUE, NEIGHBOUR] == ["x"]


def test_list_value_last():
    assert [NEIGHBOUR, VALUE] == ["x"]


def test_dict_value_as_the_first_key():
    assert {VALUE: 1, "z": NEIGHBOUR} == {"x": 1}


def test_dict_value_last():
    assert {"z": NEIGHBOUR, "k": VALUE} == {"x": 1}
"""


def test_a_lone_head_or_tail_of_one_value_in_a_real_run_is_masked(project, er, tmp_path, monkeypatch):
    # N1 (P1, round 6): pytest shortens the container's repr, so a value first in a list shows only its head before the
    # '...', and a value last shows only its tail after it. The cleaner used to need both pieces.
    monkeypatch.setenv("P3_S_ONE", LONE_VALUE)
    project.makepyfile(test_lone=LONE_TESTS)
    rec = tmp_path / "rec"

    run_serial(project, rec)

    decoded = "\n".join(
        value for recs in read_journals(rec).values() for r in recs for value in r.values() if isinstance(value, str)
    )
    raw = journal_bytes(rec).decode("utf-8")
    leaked = [w for w in windows_of(LONE_VALUE) if w in decoded or w in raw]
    assert not leaked, f"pieces of the value reached the journal: {sorted(leaked)[:3]}"
    phases = phases_of(rec)
    for name in ("list_value_first", "list_value_last", "dict_value_as_the_first_key", "dict_value_last"):
        assert "<env:P3_S_ONE>" in phases[(f"test_{name}", "call")]["error_text"], name


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("['{head}...m-0123456789']", "['<env:P3_S_ONE>...m-0123456789']"),  # the value first: only its head shows
        ("['a-long-lea...{tail}']", "['a-long-lea...<env:P3_S_ONE>']"),  # the value last: only its tail shows
        ("{{'{head}...m-0123456789': 1}}", "{{'<env:P3_S_ONE>...m-0123456789': 1}}"),
        ("{{'z': 'a-long-lea...{tail}'}}", "{{'z': 'a-long-lea...<env:P3_S_ONE>'}}"),
    ],
)
def test_a_lone_head_or_tail_of_one_value_is_masked_by_the_cleaner(er, tmp_path, monkeypatch, template, expected):
    monkeypatch.setenv("P3_S_ONE", LONE_VALUE)
    text = template.format(head=LONE_VALUE[:11], tail=LONE_VALUE[-11:])
    recorder, _ = _recorder(er, tmp_path)
    try:
        fields = recorder.error_fields(text)
        reason = recorder.reason_fields(text)
    finally:
        recorder.close()
    want = expected.format()
    assert fields["error_text"] == want
    assert fields["error_redactions"] == 1
    assert (reason["reason"], reason["reason_redactions"]) == (want, 1)
    assert fields["error_digest"] == hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    ("template", "unchanged"),
    [
        ("{h7}...rest", True),  # a piece under 8 characters stays as it is (the stated limit)
        ("{h8}...rest", False),
        ("rest...{t7}", True),
        ("rest...{t8}", False),
        ("{m11}...rest", True),  # a stretch from the middle of the value is not its start
        ("rest...{m11}", True),  # nor its end
        ("{h11} ...rest", True),  # the ellipsis must touch the piece
        ("rest... {t11}", True),
    ],
)
def test_a_lone_piece_needs_eight_characters_and_must_be_a_start_or_an_end(
    er, tmp_path, monkeypatch, template, unchanged
):
    monkeypatch.setenv("P3_S_ONE", LONE_VALUE)
    v = LONE_VALUE
    given = template.format(h7=v[:7], h8=v[:8], t7=v[-7:], t8=v[-8:], m11=v[20:31], h11=v[:11], t11=v[-11:])
    recorder, _ = _recorder(er, tmp_path)
    try:
        fields = recorder.error_fields(given)
    finally:
        recorder.close()
    assert (fields["error_text"] == given) is unchanged, fields["error_text"]
    assert fields["error_redactions"] == (0 if unchanged else 1)


# --- fix round 6: R1 collection manifest, R2 identity on every phase, R3 missing outcomes -----------------------

MIXED = """
import pytest


@pytest.mark.xdist_group("g1")
@pytest.mark.parametrize("value", ["a@b]", "plain"])
def test_param(value):
    assert value


def test_ordinary():
    pass


@pytest.mark.skip(reason="not today")
def test_skipped():
    pass


@pytest.mark.xfail(reason="known")
def test_xfailed():
    assert False
"""

IDENTITY = {
    # canonical id: (parameter id, requested group, effective group in the parallel run)
    "test_mixed.py::test_param[a@b]]": ("a@b]", "g1", "g1"),
    "test_mixed.py::test_param[plain]": ("plain", "g1", "g1"),
    "test_mixed.py::test_ordinary": (None, None, None),
    "test_mixed.py::test_skipped": (None, None, None),
    "test_mixed.py::test_xfailed": (None, None, None),
}


def manifest_records(rec, role):
    return [(n, r) for n, r in records(rec, "collection-manifest") if n.startswith(f"journal-{role}-")]


def test_a_serial_run_writes_the_ordered_collection_manifest(project, er, tmp_path):
    # R1: the ordered collected identities are in the journal, not only a count and a hash.
    project.makepyfile(test_mixed=MIXED)
    expected = collected_ids(project)
    rec = tmp_path / "rec"

    run_serial(project, rec).assert_outcomes(passed=3, skipped=1, xfailed=1)

    ((_, manifest),) = records(rec, "collection-manifest")
    assert (manifest["source"], manifest["source_worker"], manifest["count"]) == ("session", None, 5)
    assert manifest["raw_ids"] == expected  # the same ids in the same order as the serial collection
    by_raw = dict(zip(manifest["raw_ids"], manifest["identities"], strict=True))
    for canonical, (param, group, _effective) in IDENTITY.items():
        assert by_raw[canonical] == {
            "canonical_id": canonical,
            "param_id": param,
            "requested_group": group,
            "effective_group": None,  # a serial run adds no `@group` suffix
        }
    summary = er.reduce_journals(rec, expected)
    assert summary["accepted"], summary["problems"]
    assert summary["collection"] == {"collected": 5, "never_started": [], "nodes": {}}


def test_the_controller_writes_a_manifest_for_each_worker_that_equals_the_workers_own(project, er, tmp_path):
    # R1: "a collection manifest written by the controller".
    project.makepyfile(test_mixed=MIXED)
    expected = collected_ids(project)
    rec = tmp_path / "rec"

    run_parallel(project, rec).assert_outcomes(passed=3, skipped=1, xfailed=1)

    nodes = {r["source_worker"]: r for _, r in manifest_records(rec, "controller")}
    own = {r["worker"]: r for _, r in manifest_records(rec, "worker")}
    assert sorted(nodes) == ["gw0", "gw1"] == sorted(own)
    for worker, record in nodes.items():
        assert record["source"] == "node" and "identities" not in record
        assert record["raw_ids"] == own[worker]["raw_ids"], "the controller and the worker saw different ids"
        assert record["count"] == 5
    raw_ids = nodes["gw0"]["raw_ids"]
    assert [i["canonical_id"] for i in own["gw0"]["identities"]] == expected
    assert [raw for raw in raw_ids if raw.endswith("@g1")] == [
        "test_mixed.py::test_param[a@b]]@g1",
        "test_mixed.py::test_param[plain]@g1",
    ]
    summary = er.reduce_journals(rec, expected)
    assert summary["accepted"], summary["problems"]
    assert summary["collection"] == {"collected": 5, "never_started": [], "nodes": {"gw0": 5, "gw1": 5}}


def test_a_run_stopped_by_the_first_failure_shows_the_items_that_never_started(project, er, tmp_path):
    project.makepyfile(
        test_mod="def test_a():\n    assert False\n\n\ndef test_b():\n    pass\n\n\ndef test_c():\n    pass\n"
    )
    expected = collected_ids(project)
    rec = tmp_path / "rec"

    run_serial(project, rec, "-x").assert_outcomes(failed=1)

    for reference in (expected, None):  # the manifest alone is enough; the serial reference only adds to it
        summary = er.reduce_journals(rec, reference)
        assert summary["collection"]["never_started"] == expected[1:]
        assert "never-started" in kinds(summary["problems"])
        assert summary["accepted"] is False
        assert list(summary["items"]) == expected[:1]


KILLED_GROUP = """
import os

import pytest

pytestmark = pytest.mark.xdist_group("g1")


def test_before():
    pass


def test_dies():
    os._exit(7)


def test_after():
    pass
"""


def test_a_killed_workers_unstarted_items_and_crashed_item_are_visible(project, er, tmp_path):
    # R1 and R2: the group runs on one worker; it dies in the second test, so the third never starts, and the
    # controller's crash report (no item object there) is joined to the manifest by its raw id.
    project.makepyfile(test_mod=KILLED_GROUP)
    expected = collected_ids(project)
    rec = tmp_path / "rec"

    run_parallel(project, rec, "--max-worker-restart=0")

    summary = er.reduce_journals(rec, expected)
    assert summary["collection"]["never_started"] == [expected[2]]
    assert {"never-started", "item-unfinished"} <= kinds(summary["problems"])
    unfinished = next(p for p in summary["problems"] if p["kind"] == "item-unfinished")
    assert unfinished["crash_recorded"] is True and unfinished["phases_seen"] == ["setup"]
    assert "crash-unrecorded" not in kinds(summary["problems"])
    ((_, crash),) = [(n, r) for n, r in records(rec, "phase") if r["when"] == "???"]
    assert crash["nodeid"] == expected[1] + "@g1"
    assert (crash["canonical_id"], crash["param_id"], crash["requested_group"], crash["effective_group"]) == (
        None,
        None,
        None,
        None,
    )  # the controller has no item: the identity is the manifest row of the same raw id
    assert summary["first_failure"]["resolved"] == {
        "canonical_id": expected[1],
        "param_id": None,
        "requested_group": "g1",
        "effective_group": "g1",
    }
    (start,) = [r for _, r in records(rec, "item-start") if r["canonical_id"] == expected[1]]
    assert (start["nodeid"], start["param_id"], start["requested_group"], start["effective_group"]) == (
        expected[1] + "@g1",
        None,
        "g1",
        "g1",
    )


def test_every_phase_record_carries_the_identity_of_its_item(project, er, tmp_path):
    # R2: ordinary, parameterized, skipped and xfail items; every phase, not only the item-start.
    project.makepyfile(test_mixed=MIXED)
    rec = tmp_path / "rec"

    run_parallel(project, rec).assert_outcomes(passed=3, skipped=1, xfailed=1)

    phases = [r for n, r in records(rec, "phase") if n.startswith("journal-worker-")]
    by_item: dict[str, list] = {}
    for record in phases:
        by_item.setdefault(record["canonical_id"], []).append(record)
    assert sorted(by_item) == sorted(IDENTITY)
    for canonical, (param, group, effective) in IDENTITY.items():
        for record in by_item[canonical]:
            assert (record["param_id"], record["requested_group"], record["effective_group"]) == (
                param,
                group,
                effective,
            )
            assert record["nodeid"] == canonical + (f"@{effective}" if effective else "")
    whens = {canonical: [r["when"] for r in rows] for canonical, rows in by_item.items()}
    assert whens["test_mixed.py::test_skipped"] == ["setup", "teardown"]  # a setup skip has no call phase
    assert all(w == ["setup", "call", "teardown"] for c, w in whens.items() if c != "test_mixed.py::test_skipped")
    skipped = by_item["test_mixed.py::test_skipped"][0]
    assert (skipped["outcome"], skipped["reason"]) == ("skipped", "Skipped: not today")
    (xfailed,) = [r for r in by_item["test_mixed.py::test_xfailed"] if r["when"] == "call"]
    assert (xfailed["outcome"], xfailed["xfail"], xfailed["reason"]) == ("skipped", True, "known")
    starts = {r["canonical_id"]: r for n, r in records(rec, "item-start")}
    assert starts["test_mixed.py::test_param[a@b]]"]["param_id"] == "a@b]"
    assert er.reduce_journals(rec, collected_ids(project))["accepted"]


def test_the_manifest_tells_a_controller_and_a_worker_that_disagree(er, tmp_path):
    rows = [("t.py::a@g", "g"), ("t.py::b@g", "g")]
    write_journal(tmp_path, "controller", 10, extra=[("collection-manifest", manifest_fields(rows[:1], "node", "gw0"))])
    write_journal(tmp_path, "worker", 11, worker="gw0", starts=rows, collected=rows)

    summary = er.reduce_journals(tmp_path)

    assert "manifest-disagreement" in kinds(summary["problems"])
    assert summary["accepted"] is False


def test_a_process_that_wrote_no_collection_manifest_is_not_accepted(er, tmp_path):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], manifest=False)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert kinds(summary["problems"]) == {"manifest-missing"}
    assert summary["accepted"] is False
    assert summary["collection"]["collected"] is None


def test_a_collection_that_differs_from_the_serial_reference_is_refused(er, tmp_path):
    rows = [("t.py::a", None), ("t.py::b", None)]
    write_journal(tmp_path, "serial", 11, starts=rows)

    summary = er.reduce_journals(tmp_path, ["t.py::a", "t.py::c"])

    assert "collection-mismatch" in kinds(summary["problems"])
    assert summary["accepted"] is False


def test_collected_items_that_were_not_started_are_listed_from_the_manifest_alone(er, tmp_path):
    collected = [("t.py::a", None), ("t.py::b", None), ("t.py::c", None)]
    write_journal(tmp_path, "serial", 11, starts=collected[:1], collected=collected)

    summary = er.reduce_journals(tmp_path)

    assert summary["collection"]["never_started"] == ["t.py::b", "t.py::c"]
    assert [p["nodeid"] for p in summary["problems"] if p["kind"] == "never-started"] == ["t.py::b", "t.py::c"]
    assert summary["accepted"] is False


# R3: every started item must have the outcomes pytest gives it.

PASSED = [("setup", "passed"), ("call", "passed"), ("teardown", "passed")]


@pytest.mark.parametrize(
    ("phases", "problem"),
    [
        (PASSED, None),
        ([("setup", "skipped"), ("teardown", "passed")], None),  # a setup skip: no call
        ([("setup", "failed"), ("teardown", "passed")], None),  # a setup failure: no call
        ([("setup", "passed"), ("call", "failed"), ("teardown", "passed")], None),
        ([("setup", "passed"), ("call", "skipped"), ("teardown", "failed")], None),
        ([], "phase-missing"),  # nothing recorded at all
        ([("setup", "passed"), ("teardown", "passed")], "phase-missing"),  # the call after a good setup is lost
        ([("setup", "passed"), ("call", "passed")], "phase-missing"),  # an interrupted teardown
        ([("call", "passed"), ("teardown", "passed")], "phase-missing"),  # no setup
        ([("setup", "skipped"), ("call", "passed"), ("teardown", "passed")], "phase-order"),  # a call after a skip
        ([("setup", "failed"), ("call", "passed"), ("teardown", "passed")], "phase-order"),  # and after a failure
        ([("setup", "passed"), ("call", "passed"), ("call", "passed"), ("teardown", "passed")], "phase-order"),
        ([("teardown", "passed"), ("setup", "passed"), ("call", "passed")], "phase-order"),
    ],
    ids=[
        "ordinary",
        "setup-skip",
        "setup-failure",
        "call-failure",
        "skip-then-teardown-failure",
        "no-phase-at-all",
        "call-lost",
        "teardown-lost",
        "setup-lost",
        "call-after-skip",
        "call-after-failure",
        "call-twice",
        "wrong-order",
    ],
)
def test_the_phases_of_an_ended_item_are_checked_against_what_pytest_does(er, tmp_path, phases, problem):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], phases={"t.py::a": phases})

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    found = kinds(summary["problems"])
    assert found == ({problem} if problem else set()), summary["problems"]
    assert summary["accepted"] is (problem is None)


def test_a_record_of_another_schema_version_is_refused_and_the_docstring_says_so(er, tmp_path):
    assert er.SCHEMA_VERSION == 2 and er.HANDOFF_VERSION == 1
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None), ("t.py::b", None)])
    damaged(path, "item-start", v=1)

    summary = er.reduce_journals(tmp_path, ["t.py::a", "t.py::b"])

    assert "bad-field" in kinds(summary["problems"]) and summary["accepted"] is False
    flat = " ".join(er.__doc__.split())
    for text in (
        "schema version, 2",
        "collection-manifest the ordered collected identities",
        "EVERY phase carries them",
        "joins them and shows the row as `resolved`",
        "phase-missing (an item that ended lacks its setup",
        "never-started (collected but never started",
        "crash-unrecorded (an unfinished item of a worker",
        "passes `require_phases=False`",
    ):
        assert text in flat, text


def test_a_phase_that_belongs_to_no_started_item_is_reported(er, tmp_path):
    extra = [("phase", phase_fields("t.py::ghost", None, "call", "passed"))]
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=extra)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert "phase-outside-item" in kinds(summary["problems"])
    assert summary["accepted"] is False


def test_a_probe_run_without_test_bodies_can_ask_not_to_need_phases(er, tmp_path):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None), ("t.py::b", None)], phases=False)

    assert er.reduce_journals(tmp_path, ["t.py::a", "t.py::b"])["accepted"] is False
    summary = er.reduce_journals(tmp_path, ["t.py::a", "t.py::b"], require_phases=False)
    assert summary["accepted"], summary["problems"]


def test_an_unfinished_item_with_a_crash_record_says_so_and_one_without_is_a_new_problem(er, tmp_path):
    unfinished = [("t.py::a@g", "g", [("setup", "passed")])]
    write_journal(tmp_path, "worker", 11, worker="gw0", trailer=False, unfinished=unfinished)

    lonely = er.reduce_journals(tmp_path)
    assert {"item-unfinished", "crash-unrecorded"} <= kinds(lonely["problems"])

    down = [("worker-down", {"down_worker": "gw0", "has_error": True, "error_text": "crashed"})]
    write_journal(tmp_path, "controller", 10, extra=down)
    recorded = er.reduce_journals(tmp_path)
    assert "item-unfinished" in kinds(recorded["problems"])
    assert "crash-unrecorded" not in kinds(recorded["problems"])
    assert next(p for p in recorded["problems"] if p["kind"] == "item-unfinished")["crash_recorded"] is True
    assert recorded["accepted"] is False


NEGATIVE_CONFTEST = """
import experiment_record

experiment_record.Recorder.pytest_runtest_logreport = lambda self, report: None  # the phase writer is off


def pytest_addoption(parser):
    experiment_record.add_option(parser)


def pytest_configure(config):
    experiment_record.configure(config)
"""


def test_a_failing_test_whose_phase_lines_were_never_written_is_not_accepted(project, er, tmp_path):
    # R3: with only the phase writer disabled, the reducer used to say complete, accepted, no problem, no failure.
    project.makeconftest(NEGATIVE_CONFTEST)
    project.makepyfile(test_mod="def test_fails():\n    assert False\n\n\ndef test_ok():\n    pass\n")
    expected = collected_ids(project)
    rec = tmp_path / "rec"

    run_serial(project, rec).assert_outcomes(failed=1, passed=1)

    assert not list(records(rec, "phase"))
    summary = er.reduce_journals(rec, expected)
    assert summary["status"] == "complete" and summary["first_failure"] is None  # the symptom the maintainers saw
    assert summary["problem_counts"]["phase-missing"] == 2
    assert summary["accepted"] is False


def without_phases(rec):
    """Remove every phase line of every journal in `rec`, renumber the rest and fix the trailer's count."""
    for path in Path(rec).glob("journal-*.jsonl"):
        kept = [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line]
        kept = [r for r in kept if r["kind"] != "phase"]
        for number, record in enumerate(kept, 1):
            record["seq"] = number
            if record["kind"] == "trailer":
                record["records"] = number - 1
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in kept) + "\n", encoding="utf-8")


@pytest.mark.parametrize("parallel", [False, True], ids=["serial", "parallel"])
def test_real_journals_with_every_phase_line_removed_are_refused(project, er, tmp_path, parallel):
    project.makepyfile(test_mixed=MIXED)
    expected = collected_ids(project)
    rec = tmp_path / "rec"
    (run_parallel if parallel else run_serial)(project, rec).assert_outcomes(passed=3, skipped=1, xfailed=1)
    assert er.reduce_journals(rec, expected)["accepted"], "the real journals are accepted before the lines go"

    without_phases(rec)

    summary = er.reduce_journals(rec, expected)
    assert summary["status"] == "complete" and "sequence-gap" not in kinds(summary["problems"])
    assert summary["problem_counts"]["phase-missing"] == 5  # one per item
    assert summary["accepted"] is False


MANY_OUTCOMES = """
import pytest


@pytest.fixture
def broken_setup():
    raise RuntimeError("setup")


@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError("teardown")


def test_fail():
    assert False


def test_setup_error(broken_setup):
    pass


def test_teardown_error(broken_teardown):
    pass


@pytest.mark.skipif(True, reason="never")
def test_skip():
    pass


@pytest.mark.xfail(reason="x")
def test_xfail():
    assert False


@pytest.mark.xfail(reason="x")
def test_xpass():
    pass


def test_ok():
    pass
"""


def test_every_ordinary_pytest_outcome_is_reconciled_without_a_problem(project, er, tmp_path):
    # Guard against a check that is too strict: failures, errors in setup and teardown, skips and xfail are valid.
    project.makepyfile(test_many=MANY_OUTCOMES)
    expected = collected_ids(project)
    rec = tmp_path / "rec"

    run_serial(project, rec)

    summary = er.reduce_journals(rec, expected)
    assert summary["problems"] == [], summary["problems"]
    assert summary["accepted"] is True and summary["first_failure"] is not None


# --- the PR 588 CI addendum: pytest's own run variables are not masked -------------------------------------------

RUN_VARIABLES = (
    "PYTEST_CURRENT_TEST",
    "PYTEST_VERSION",
    "PYTEST_XDIST_WORKER",
    "PYTEST_XDIST_TESTRUNUID",
)


def test_pytests_own_run_variables_are_not_masked_but_an_ordinary_value_next_to_them_is(er, tmp_path, monkeypatch):
    # CI on Linux: PYTEST_CURRENT_TEST holds the current test's id, and `:` is the path separator there, so its
    # part `test_mod.py` matched ordinary error text: `test_mod.py:6: AssertionError` became `<env:...>:6: ...`.
    current = os.pathsep.join(["test_mod.py", "test_x (call)"])
    monkeypatch.setenv("PYTEST_CURRENT_TEST", current)
    others = {name: f"{name.lower()}-value-1234" for name in RUN_VARIABLES if name != "PYTEST_CURRENT_TEST"}
    for name, value in others.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("P3_S_NEXT_TO_IT", "ordinary-value-9Kq2")
    text = " | ".join(["test_mod.py:6: AssertionError", current, *others.values(), "ordinary-value-9Kq2"])
    recorder, _ = _recorder(er, tmp_path)
    try:
        fields = recorder.error_fields(text)
        reason = recorder.reason_fields(text)
    finally:
        recorder.close()
    kept = text.replace("ordinary-value-9Kq2", "<env:P3_S_NEXT_TO_IT>")
    assert fields["error_text"] == kept
    assert fields["error_redactions"] == 1
    assert (reason["reason"], reason["reason_redactions"]) == (kept, 1)
    assert fields["error_digest"] == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_an_other_pytest_variable_such_as_the_addopts_is_masked_like_any_value(er, tmp_path, monkeypatch):
    # round 10, claude-rev N2: only the run-time names are exempt, not everything that starts with PYTEST_
    monkeypatch.setenv("PYTEST_ADDOPTS", "--secret-token=Zx81-qq77")
    recorder, _ = _recorder(er, tmp_path)
    try:
        fields = recorder.error_fields("failed with --secret-token=Zx81-qq77 given")
    finally:
        recorder.close()
    assert "Zx81-qq77" not in fields["error_text"] and "<env:PYTEST_ADDOPTS>" in fields["error_text"]


def test_the_docstring_states_that_pytests_run_variables_are_not_masked(er):
    flat = " ".join(er.__doc__.split())
    assert "`PYTEST_CURRENT_TEST`, `PYTEST_VERSION` or starting with `PYTEST_XDIST_`" in flat
    assert "Other `PYTEST_*` variables (such as `PYTEST_ADDOPTS`" in flat


# --- fix round 8: nested runs reconciled, a missing controller journal, require_phases and order -----------------

NESTED_NEGATIVE_PARENT = (
    NESTED_PARENT.replace(NESTED_CONFTEST, json.dumps(NEGATIVE_CONFTEST)[1:-1])  # the child's phase writer is off
    .replace('f"    assert {experiment_record.HANDOFF_ENV!r} not in os.environ\\n"', '"    assert False\\n"')
    .replace("assert done.returncode == 0", "assert done.returncode == 1")  # the child's test fails, as it should
)


def test_a_nested_run_whose_phase_lines_were_never_written_is_not_accepted(project, er, tmp_path):
    # R3 for a nested run: with only the phase writer off in a nested child that has a failing test, the reducer said
    # accepted, no problem, no failure, because nested journals skipped the reconciliation.
    project.makepyfile(test_parent=NESTED_NEGATIVE_PARENT)
    rec = tmp_path / "rec"

    run_serial(project, rec, "-k", "test_with_the_helper").assert_outcomes(passed=1)

    ((_, launch),) = records(rec, "nested-launch")
    summary = er.reduce_journals(rec, ["test_parent.py::test_with_the_helper"])
    assert summary["nested"] == {"launched": 1, "journaled": 1} and summary["first_failure"] is None
    missing = [p for p in summary["problems"] if p["kind"] == "phase-missing"]
    assert [p["nested_run"] for p in missing] == [launch["token"]], summary["problems"]
    assert list(summary["items"]) == ["test_parent.py::test_with_the_helper"]  # no nested id in the root collection
    assert summary["accepted"] is False


NESTED_TOKEN = "0000000000000001"


def test_nested_journals_are_reconciled_per_run_and_their_ids_stay_out_of_the_root_collection(er, tmp_path):
    launch = [("nested-launch", {"token": NESTED_TOKEN})]
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=launch)
    backwards = [("teardown", "passed"), ("setup", "passed"), ("call", "passed")]
    write_journal(
        tmp_path, "nested", 12, nested_token=NESTED_TOKEN, starts=[("t.py::a", None)], phases={"t.py::a": backwards}
    )

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert [(p["kind"], p["nested_run"]) for p in summary["problems"]] == [("phase-order", NESTED_TOKEN)]
    assert summary["items"]["t.py::a"]["started"] == 1  # the nested item of the same id is not the root item
    assert summary["accepted"] is False


def test_a_nested_workers_unfinished_item_needs_a_crash_record_of_its_own_run(er, tmp_path):
    launch = [("nested-launch", {"token": NESTED_TOKEN})]
    root_down = [("worker-down", {"down_worker": "gw0", "has_error": True, "error_text": "root crash"})]
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=launch)
    write_journal(tmp_path, "controller", 10, extra=root_down)  # a crash of ANOTHER run's gw0
    unfinished = [("n.py::x@g", "g", [("setup", "passed")])]
    write_journal(tmp_path, "worker", 13, worker="gw0", nested_token=NESTED_TOKEN, trailer=False, unfinished=unfinished)

    lonely = er.reduce_journals(tmp_path)
    assert {"item-unfinished", "crash-unrecorded"} <= kinds(lonely["problems"])

    nested_down = [("worker-down", {"down_worker": "gw0", "has_error": True, "error_text": "nested crash"})]
    write_journal(tmp_path, "nested", 12, nested_token=NESTED_TOKEN, extra=nested_down)
    recorded = er.reduce_journals(tmp_path)
    assert "crash-unrecorded" not in kinds(recorded["problems"])
    assert next(p for p in recorded["problems"] if p["kind"] == "item-unfinished")["crash_recorded"] is True


def test_a_two_worker_run_without_its_controller_journal_is_refused(project, er, tmp_path):
    # The manifest checks used to depend on the controller journal being there; removing it left accepted=true.
    project.makepyfile(test_mixed=MIXED)
    expected = collected_ids(project)
    rec = tmp_path / "rec"
    run_parallel(project, rec).assert_outcomes(passed=3, skipped=1, xfailed=1)
    assert er.reduce_journals(rec, expected)["accepted"], "the real journals are accepted before the file goes"

    (controller,) = rec.glob("journal-controller-*.jsonl")
    controller.unlink()

    summary = er.reduce_journals(rec, expected)
    assert "controller-missing" in kinds(summary["problems"])
    assert summary["accepted"] is False


def test_worker_journals_with_no_controller_journal_are_refused_and_a_serial_run_is_not(er, tmp_path):
    write_journal(tmp_path, "worker", 11, worker="gw0", starts=[("t.py::a@g", "g")])
    assert "controller-missing" in kinds(er.reduce_journals(tmp_path, ["t.py::a"])["problems"])
    other = tmp_path / "serial"
    other.mkdir()
    write_journal(other, "serial", 12, starts=[("t.py::a", None)])
    assert er.reduce_journals(other, ["t.py::a"])["accepted"]


@pytest.mark.parametrize(
    "phases",
    [
        PASSED,
        [("setup", "passed"), ("teardown", "passed")],
        [("setup", "passed")],
        [("teardown", "passed"), ("setup", "passed"), ("call", "passed")],
        [("setup", "skipped"), ("call", "passed"), ("teardown", "passed")],
        [("setup", "passed"), ("call", "passed"), ("call", "passed"), ("teardown", "passed")],
    ],
    ids=["ordinary", "call-lost", "setup-only", "wrong-order", "call-after-skip", "call-twice"],
)
def test_require_phases_false_refuses_any_phase_of_a_started_item(er, tmp_path, phases):
    # The lead's ruling: a no-body probe has NO phase at all, so any phase (a probe that ran a body by mistake, a phase
    # out of order, a lone setup) is a problem of its own kind, and the run is not accepted.
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], phases={"t.py::a": phases})

    summary = er.reduce_journals(tmp_path, ["t.py::a"], require_phases=False)

    assert kinds(summary["problems"]) == {"phase-in-no-body-probe"}, summary["problems"]
    assert summary["problems"][0]["nodeid"] == "t.py::a"
    assert summary["accepted"] is False


def test_require_phases_false_accepts_a_probe_with_no_phase_and_the_default_still_refuses_it(er, tmp_path):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], phases={"t.py::a": []})

    accepted = er.reduce_journals(tmp_path, ["t.py::a"], require_phases=False)
    refused = er.reduce_journals(tmp_path, ["t.py::a"])

    assert accepted["problems"] == [] and accepted["accepted"] is True
    assert kinds(refused["problems"]) == {"phase-missing"} and refused["accepted"] is False


def test_require_phases_false_also_refuses_phases_of_an_unfinished_item_and_of_a_nested_run(er, tmp_path):
    unfinished = [("t.py::a", None, [("setup", "passed")])]
    write_journal(tmp_path, "serial", 11, trailer=False, unfinished=unfinished)
    summary = er.reduce_journals(tmp_path, require_phases=False)
    assert "phase-in-no-body-probe" in kinds(summary["problems"])

    other = tmp_path / "nested"
    other.mkdir()
    launch = [("nested-launch", {"token": NESTED_TOKEN})]
    write_journal(other, "serial", 11, starts=[("t.py::a", None)], phases={"t.py::a": []}, extra=launch)
    write_journal(other, "nested", 12, nested_token=NESTED_TOKEN, starts=[("n.py::x", None)])  # ran its body
    nested = er.reduce_journals(other, ["t.py::a"], require_phases=False)
    assert [(p["kind"], p["nested_run"]) for p in nested["problems"]] == [("phase-in-no-body-probe", NESTED_TOKEN)]


def test_the_docstring_states_the_round_8_rules(er):
    flat = " ".join(er.__doc__.split())
    for text in (
        "controller-missing (worker journals of a run and no controller journal",
        "Nested journals are reconciled the same way, run by run",
        "requires that a started item has NO phase at all",
        "phase-in-no-body-probe (under `require_phases=False`",
        "about 3 KB per item",
        "P8's dry run measures the real serial journal size",
    ):
        assert text in flat, text


# --- fix round 9: the automated reviewer's P1 (reparse points) and three P2 ----------------------------------------


def _a_journal_outside(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    return write_journal(outside, "serial", 11, starts=[("t.py::a", None)])


def _folder_with_a_link_to(target, tmp_path):
    folder = tmp_path / "record"
    folder.mkdir()
    link = folder / target.name
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("this system cannot create a symbolic link here")
    return folder


def test_a_symbolic_link_named_like_a_journal_is_refused(er, tmp_path):
    # P1: Windows has no O_NOFOLLOW, so the open followed the link and fstat saw only a plain target.
    folder = _folder_with_a_link_to(_a_journal_outside(tmp_path), tmp_path)

    summary = er.reduce_journals(folder, ["t.py::a"])

    assert kinds(summary["problems"]) >= {"journal-not-plain"}, summary["problems"]
    assert summary["status"] == "incomplete" and summary["items"] == {}
    assert summary["accepted"] is False


def _lstat_that(monkeypatch, name, change):
    """os.lstat of the path whose last part is `name` answers through `change(result, call_number)`."""
    real = os.lstat
    calls = []

    def fake(path, *args, **kwargs):
        result = real(path, *args, **kwargs)
        if Path(path).name != name:
            return result
        calls.append(path)
        fields = {k: getattr(result, k) for k in ("st_mode", "st_dev", "st_ino", "st_nlink", "st_size")}
        fields["st_file_attributes"] = getattr(result, "st_file_attributes", 0)
        return SimpleNamespace(**change(fields, len(calls)))

    monkeypatch.setattr(os, "lstat", fake)


@pytest.mark.parametrize("looks", ["every-look", "first-look-only"])
def test_a_journal_that_lstat_shows_as_a_reparse_point_is_refused_on_every_system(er, tmp_path, monkeypatch, looks):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    assert er.reduce_journals(tmp_path, ["t.py::a"])["accepted"], "the journal is fine before the attribute is set"
    first_only = looks == "first-look-only"
    listing = 0 if er._HAS_DIR_FD else 1  # where names come from the path, the listing's own look comes first

    _lstat_that(
        monkeypatch,
        path.name,
        lambda fields, call: {**fields, "st_file_attributes": 1024 * (call == 1 + listing or not first_only)},
    )

    summary = er.reduce_journals(tmp_path, ["t.py::a"])
    assert kinds(summary["problems"]) >= {"journal-not-plain"}, summary["problems"]
    assert summary["accepted"] is False


def test_a_journal_that_becomes_a_reparse_point_while_it_is_opened_is_refused(er, tmp_path, monkeypatch):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    listing = 0 if er._HAS_DIR_FD else 1  # where names come from the path, the listing's own look comes first
    _lstat_that(
        monkeypatch, path.name, lambda fields, call: {**fields, "st_file_attributes": 1024 * (call > 1 + listing)}
    )

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert "journal-not-plain" in kinds(summary["problems"]), summary["problems"]
    assert summary["accepted"] is False


def test_a_journal_whose_identity_changes_while_it_is_opened_is_refused(er, tmp_path, monkeypatch):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    _lstat_that(monkeypatch, path.name, lambda fields, call: {**fields, "st_ino": fields["st_ino"] + (call > 1)})

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert kinds(summary["problems"]) >= {"journal-not-plain"}, summary["problems"]
    assert summary["accepted"] is False


def test_a_record_folder_that_is_a_link_is_refused(er, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    write_journal(real, "serial", 11, starts=[("t.py::a", None)])
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this system cannot create a symbolic link here")

    summary = er.reduce_journals(link, ["t.py::a"])

    assert "folder-not-plain" in kinds(summary["problems"]), summary["problems"]
    assert summary["processes"] == [] and summary["accepted"] is False


@pytest.mark.skipif(sys.platform != "win32", reason="a junction is a Windows reparse point")
def test_a_record_folder_that_is_a_real_junction_is_refused(er, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    write_journal(real, "serial", 11, starts=[("t.py::a", None)])
    junction = tmp_path / "junction"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(real)], capture_output=True, text=True)
    if made.returncode != 0:
        pytest.skip(f"cannot create a junction here: {made.stdout} {made.stderr}")

    summary = er.reduce_journals(junction, ["t.py::a"])

    assert "folder-not-plain" in kinds(summary["problems"]), summary["problems"]
    assert summary["processes"] == [] and summary["accepted"] is False
    assert er.reduce_journals(real, ["t.py::a"])["accepted"], "the target itself is a plain folder"


def test_a_record_folder_that_lstat_shows_as_a_reparse_point_is_refused_on_every_system(er, tmp_path, monkeypatch):
    folder = tmp_path / "record"
    folder.mkdir()
    write_journal(folder, "serial", 11, starts=[("t.py::a", None)])
    _lstat_that(monkeypatch, "record", lambda fields, call: {**fields, "st_file_attributes": 1024})

    summary = er.reduce_journals(folder, ["t.py::a"])

    assert "folder-not-plain" in kinds(summary["problems"]), summary["problems"]
    assert summary["accepted"] is False


def test_a_missing_or_plain_folder_is_not_a_folder_problem(er, tmp_path):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    assert er.reduce_journals(tmp_path, ["t.py::a"])["accepted"]
    assert er.reduce_journals(tmp_path / "nowhere")["problems"] == []  # nothing to read: incomplete, as before


def test_a_nested_run_whose_parent_also_turned_recording_on_through_pytest_addopts_is_still_nested(
    project, er, tmp_path
):
    # P2: the inherited option won over the handoff, so the child was labelled `serial` and the join failed.
    project.makepyfile(test_parent=NESTED_PARENT)
    rec = tmp_path / "rec"

    run_serial(project, rec, "-k", "test_helper_plus_addopts").assert_outcomes(passed=1)

    ((_, launch),) = records(rec, "nested-launch")
    roles = sorted(r["role"] for _, r in records(rec, "process-start"))
    assert roles == ["nested", "serial"], roles
    ((_, nested_start),) = [(n, r) for n, r in records(rec, "process-start") if r["role"] == "nested"]
    assert nested_start["nested_token"] == launch["token"]
    summary = er.reduce_journals(rec, ["test_parent.py::test_helper_plus_addopts"])
    assert summary["accepted"], summary["problems"]
    assert summary["nested"] == {"launched": 1, "journaled": 1}


@pytest.mark.parametrize(
    "version",
    [1, 3, 0, "2", 2.0, True, [2], None],
    ids=["one", "three", "zero", "text", "float", "bool", "list", "missing"],
)
def test_a_record_with_any_other_schema_version_is_refused(er, tmp_path, version):
    # P2: the common `v` field is checked for every record; a missing, damaged or other version is a bad-field.
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    assert er.reduce_journals(tmp_path, ["t.py::a"])["accepted"] and er.reduce_journals(tmp_path)["schema"] == 2
    damaged(path, "process-start", v=version)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert "bad-field" in kinds(summary["problems"]), summary["problems"]
    assert summary["accepted"] is False


def test_too_many_journal_bytes_are_refused_before_any_is_parsed(er, tmp_path, monkeypatch):
    # P2: each journal may be 64 MiB and all parsed journals were kept until the end.
    for pid in (11, 12, 13):
        write_journal(tmp_path, "serial", pid, starts=[("t.py::a", None)])
    total = sum(p.stat().st_size for p in tmp_path.glob("journal-*.jsonl"))
    monkeypatch.setattr(er, "_read_journal", lambda *a, **k: pytest.fail("a journal was parsed"))
    monkeypatch.setattr(er, "MAX_TOTAL_JOURNAL_BYTES", total - 1)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert "input-too-large" in kinds(summary["problems"]), summary["problems"]
    assert summary["processes"] == [] and summary["status"] == "incomplete" and summary["accepted"] is False


def test_too_many_journals_are_refused_before_any_is_parsed(er, tmp_path, monkeypatch):
    for pid in (11, 12, 13):
        write_journal(tmp_path, "serial", pid, starts=[("t.py::a", None)])
    monkeypatch.setattr(er, "_read_journal", lambda *a, **k: pytest.fail("a journal was parsed"))
    monkeypatch.setattr(er, "MAX_JOURNALS", 2)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert "input-too-large" in kinds(summary["problems"]), summary["problems"]
    assert summary["accepted"] is False


def test_input_exactly_at_the_limits_is_read(er, tmp_path, monkeypatch):
    write_journal(tmp_path, "worker", 11, worker="gw0", starts=[("t.py::a@g", "g")])
    write_journal(tmp_path, "worker", 12, worker="gw1", starts=[("t.py::b@g", "g")])
    total = sum(p.stat().st_size for p in tmp_path.glob("journal-*.jsonl"))
    monkeypatch.setattr(er, "MAX_TOTAL_JOURNAL_BYTES", total)
    monkeypatch.setattr(er, "MAX_JOURNALS", 2)

    summary = er.reduce_journals(tmp_path)

    assert "input-too-large" not in kinds(summary["problems"]) and len(summary["processes"]) == 2


def test_the_docstring_states_the_round_9_limits(er):
    flat = " ".join(er.__doc__.split())
    assert er.MAX_TOTAL_JOURNAL_BYTES == 256 * 1024 * 1024 and er.MAX_JOURNALS == 512
    for text in (
        "input-too-large",
        "folder-not-plain",
        "a link or a reparse point",
        "A valid nested handoff decides the role",
        "at most 512 journals and 256 MiB",
    ):
        assert text in flat, text


# --- fix round 10: the record folder's identity holds through every journal open ---------------------


def _swap_in_a_link(record, outside, saved):
    """Replace the folder `record` by a link to `outside`, keeping the original as `saved` (a junction on Windows, a
    symbolic link elsewhere); skip, with the folder put back, where the system cannot make one.  Returns False when
    the system refuses to move the folder (Windows does, while the reducer holds it): the swap did not happen."""
    try:
        record.rename(saved)
    except OSError:
        return False
    if sys.platform == "win32":
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(record), str(outside)], capture_output=True, text=True)
        refused = made.returncode != 0
    else:
        try:
            record.symlink_to(outside, target_is_directory=True)
            refused = False
        except (OSError, NotImplementedError):
            refused = True
    if refused:
        saved.rename(record)
        pytest.skip("this system cannot create a folder link here")
    return True


def _race_folders(tmp_path):
    work = tmp_path / "race"
    work.mkdir()
    record, saved, outside = work / "record", work / "record-before", work / "outside"
    record.mkdir()
    outside.mkdir()
    write_journal(record, "serial", 11, starts=[("t.py::safe", None)])
    write_journal(outside, "serial", 11, starts=[("t.py::outside", None)])
    assert reduce_with_the_module(record)["accepted"], "the record folder is fine before the race"
    return record, saved, outside


def reduce_with_the_module(folder):
    import experiment_record

    return experiment_record.reduce_journals(folder, ["t.py::safe"])


def test_a_folder_swapped_for_a_link_after_enumeration_is_refused_and_nothing_is_read_through_it(
    er, tmp_path, monkeypatch
):
    # codex-rev's race: the folder was checked once, then replaced by a junction before the first journal was opened,
    # and the unchanged file checks read the outside target: accepted=true, item t.py::outside.
    record, saved, outside = _race_folders(tmp_path)
    original = er._read_journal
    swapped = []

    def after_enumeration(path, problems):
        if not swapped:
            swapped.append(_swap_in_a_link(record, outside, saved))
        return original(path, problems)

    monkeypatch.setattr(er, "_read_journal", after_enumeration)

    summary = er.reduce_journals(record, ["t.py::safe"])

    assert len(swapped) == 1
    assert "t.py::outside" not in summary["items"], "something was read through the swapped folder"
    if swapped[0]:  # the swap happened (POSIX): it is seen
        assert "folder-changed" in kinds(summary["problems"]), summary["problems"]
        assert summary["items"] == {} and summary["accepted"] is False
    else:  # Windows: the held folder cannot be moved at all, and the read goes on in the real folder
        assert list(summary["items"]) == ["t.py::safe"] and summary["accepted"] is True, summary


def test_a_folder_whose_identity_changes_while_a_journal_is_opened_is_refused_on_every_system(
    er, tmp_path, monkeypatch
):
    folder = tmp_path / "record"
    folder.mkdir()
    write_journal(folder, "serial", 11, starts=[("t.py::a", None)])
    assert er.reduce_journals(folder, ["t.py::a"])["accepted"]
    _lstat_that(monkeypatch, "record", lambda fields, call: {**fields, "st_ino": fields["st_ino"] + (call > 1)})

    summary = er.reduce_journals(folder, ["t.py::a"])

    assert "folder-changed" in kinds(summary["problems"]), summary["problems"]
    assert summary["items"] == {} and summary["accepted"] is False


def test_a_folder_that_becomes_a_reparse_point_while_a_journal_is_opened_is_refused(er, tmp_path, monkeypatch):
    folder = tmp_path / "record"
    folder.mkdir()
    write_journal(folder, "serial", 11, starts=[("t.py::a", None)])
    _lstat_that(monkeypatch, "record", lambda fields, call: {**fields, "st_file_attributes": 1024 * (call > 1)})

    summary = er.reduce_journals(folder, ["t.py::a"])

    assert "folder-changed" in kinds(summary["problems"]), summary["problems"]
    assert summary["accepted"] is False


@pytest.mark.skipif(sys.platform != "win32", reason="the final path of an open handle is a Windows check")
def test_the_final_path_of_an_open_journal_is_its_real_path(er, tmp_path):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    with open(path, "rb") as handle:
        final = er._final_path(handle.fileno())
    assert os.path.normcase(final) == os.path.normcase(os.path.realpath(path))
    assert not final.startswith("\\\\?\\")


@pytest.mark.skipif(sys.platform != "win32", reason="the final path of an open handle is a Windows check")
def test_a_journal_whose_open_handle_lies_outside_the_folder_is_refused(er, tmp_path, monkeypatch):
    # a swap-and-restore around the open leaves the folder looking unchanged; the handle's own path still shows
    folder = tmp_path / "record"
    folder.mkdir()
    path = write_journal(folder, "serial", 11, starts=[("t.py::a", None)])
    monkeypatch.setattr(er, "_final_path", lambda fd: str(tmp_path / "elsewhere" / path.name))

    summary = er.reduce_journals(folder, ["t.py::a"])

    assert "folder-changed" in kinds(summary["problems"]), summary["problems"]
    assert summary["items"] == {} and summary["accepted"] is False


@pytest.mark.skipif(sys.platform == "win32", reason="a descriptor-relative open is the POSIX way")
def test_on_posix_the_journals_are_opened_through_the_folder_descriptor_held_from_the_start(er, tmp_path, monkeypatch):
    # with the after-open recheck turned off, a swapped folder still cannot lead the open outside: the journal is
    # opened relative to the descriptor of the folder that was verified, which no rename of the path changes
    record, saved, outside = _race_folders(tmp_path)
    original = er._read_journal
    swapped = []

    def after_enumeration(path, problems):
        if not swapped:
            swapped.append(_swap_in_a_link(record, outside, saved))
        return original(path, problems)

    monkeypatch.setattr(er, "_read_journal", after_enumeration)
    monkeypatch.setattr(er._Folder, "unchanged", lambda self: True)

    summary = er.reduce_journals(record, ["t.py::safe"])

    assert swapped == [True] and list(summary["items"]) == ["t.py::safe"], summary


def test_a_journal_can_still_be_read_on_its_own(er, tmp_path):
    # tests/experiment/w0.py (P5) reads single journals through this function, with no reduce_journals around it
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    problems = er._Problems()
    records = er._read_journal(path, problems)
    assert records and records[0]["kind"] == "process-start" and problems.listed == []


def test_the_docstring_states_what_the_folder_protection_covers_and_what_it_does_not(er):
    flat = " ".join(er.__doc__.split())
    for text in (
        "folder-changed",
        "its identity is kept for every journal open",
        "relative to the folder's own descriptor",
        "final path",
        "cannot be closed",
    ):
        assert text in flat, text


# --- fix round 11: one held folder handle is the baseline ------------------------------------------------------


def _ready_junction(work, outside):
    """A link to `outside` that can be renamed into the record folder's place, or a skip where none can be made."""
    ready = work / "ready-link"
    if sys.platform == "win32":
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(ready), str(outside)], capture_output=True, text=True)
        refused = made.returncode != 0
    else:
        try:
            ready.symlink_to(outside, target_is_directory=True)
            refused = False
        except (OSError, NotImplementedError):
            refused = True
    if refused:
        pytest.skip("this system cannot create a folder link here")
    return ready


def test_codex_revs_anchor_race_a_swap_and_restore_never_reads_the_outside_journal(er, tmp_path, monkeypatch):
    # codex-rev's round-10 P1, with real junctions: (1) a link is swapped in while the real path is looked up and the
    # original is put back; (2) after the listing the outside journal is opened through the link; (3) the original is
    # put back for the folder's recheck. Both path checks then passed: accepted=true, item t.py::outside.
    record, saved, outside = _race_folders(tmp_path)
    ready = _ready_junction(record.parent, outside)
    real_realpath, real_read, real_lstat = os.path.realpath, er._read_journal, os.lstat
    state = {"active": False, "blocked": 0}

    def to_outside():
        try:
            record.rename(saved)
        except OSError:
            state["blocked"] += 1
            return False
        ready.rename(record)
        return True

    def to_original():
        record.rename(ready)
        saved.rename(record)

    def at_realpath(path, *args, **kwargs):
        if Path(path) == record and not state.get("looked"):
            state["looked"] = True
            swapped = to_outside()
            try:
                return real_realpath(path, *args, **kwargs)
            finally:
                if swapped:
                    to_original()
        return real_realpath(path, *args, **kwargs)

    def after_listing(path, problems):
        state["active"] = to_outside()
        return real_read(path, problems)

    def at_lstat(path, *args, **kwargs):
        if state["active"] and Path(path) == record:
            to_original()
            state["active"] = False
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", at_realpath)
    monkeypatch.setattr(er, "_read_journal", after_listing)
    monkeypatch.setattr(os, "lstat", at_lstat)
    try:
        summary = er.reduce_journals(record, ["t.py::safe"])
    finally:
        monkeypatch.undo()
        if state["active"]:
            to_original()

    assert "t.py::outside" not in summary["items"], summary["items"]
    if state["blocked"]:  # Windows: the held folder cannot be renamed, so there is nothing to restore
        assert list(summary["items"]) == ["t.py::safe"] and summary["accepted"] is True, summary
    else:  # the swap happened: it is seen, or the read stayed in the held folder
        assert summary["accepted"] is False or list(summary["items"]) == ["t.py::safe"], summary


def test_a_link_swapped_in_between_the_check_and_the_open_is_refused_at_the_open(er, tmp_path, monkeypatch):
    # the baseline is the object that is opened: if the path names a link at that moment, nothing is read
    record, saved, outside = _race_folders(tmp_path)
    ready = _ready_junction(record.parent, outside)
    real_hold = er._hold_directory
    seen = []

    def swap_then_open(path):
        record.rename(saved)
        ready.rename(record)
        seen.append(True)
        return real_hold(path)

    monkeypatch.setattr(er, "_hold_directory", swap_then_open)

    summary = er.reduce_journals(record, ["t.py::safe"])
    record.rename(ready)  # put the original back (the reducer has let go of the folder by now)
    saved.rename(record)

    assert seen
    assert kinds(summary["problems"]) & {"folder-not-plain", "folder-not-held", "folder-changed"}, summary["problems"]
    assert summary["items"] == {} and summary["accepted"] is False


def test_the_baseline_comes_from_the_held_folder_not_from_a_second_lookup_of_the_path(er, tmp_path, monkeypatch):
    # codex-rev's step 1 as a patch that runs everywhere: a second lookup of the path (the real path) is made to
    # name another folder. The baseline comes from the held object, so the answer must not depend on it.
    folder = tmp_path / "record"
    folder.mkdir()
    write_journal(folder, "serial", 11, starts=[("t.py::a", None)])
    real_realpath = os.path.realpath

    def lying(path, *args, **kwargs):
        if Path(path) == folder:
            return str(tmp_path / "somewhere-else")
        return real_realpath(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", lying)

    summary = er.reduce_journals(folder, ["t.py::a"])

    assert summary["problems"] == [], summary["problems"]
    assert summary["accepted"] is True and list(summary["items"]) == ["t.py::a"]


@pytest.mark.skipif(sys.platform != "win32", reason="the sharing mode of the held handle is a Windows check")
def test_on_windows_the_held_folder_cannot_be_renamed_while_it_is_held_and_can_be_afterwards(er, tmp_path, monkeypatch):
    folder = tmp_path / "record"
    folder.mkdir()
    write_journal(folder, "serial", 11, starts=[("t.py::a", None)])
    real_read = er._read_journal
    outcome = []

    def try_to_move(path, problems):
        try:
            folder.rename(tmp_path / "moved")
            outcome.append("moved")
            (tmp_path / "moved").rename(folder)
        except OSError:
            outcome.append("refused")
        return real_read(path, problems)

    monkeypatch.setattr(er, "_read_journal", try_to_move)

    summary = er.reduce_journals(folder, ["t.py::a"])

    assert outcome == ["refused"], "the folder was moved while the reducer held it"
    assert summary["accepted"] is True
    folder.rename(tmp_path / "moved-after")  # the handle is closed again when the reduce ends
    assert (tmp_path / "moved-after").is_dir()


def test_a_folder_that_cannot_be_opened_and_held_is_refused_with_a_name_and_nothing_is_read(er, tmp_path, monkeypatch):
    # e.g. a share that refuses the open: never a silent fall back to the path checks
    folder = tmp_path / "record"
    folder.mkdir()
    write_journal(folder, "serial", 11, starts=[("t.py::a", None)])

    def refuse(path):
        raise OSError(5, "Access is denied")

    monkeypatch.setattr(er, "_hold_directory", refuse)

    summary = er.reduce_journals(folder, ["t.py::a"])

    assert "folder-not-held" in kinds(summary["problems"]), summary["problems"]
    assert summary["items"] == {} and summary["accepted"] is False
    assert er._read_journal(folder / "journal-serial-11-1000.jsonl", er._Problems()) is None


def test_the_held_folder_is_released_after_every_outcome(er, tmp_path, monkeypatch):
    folder = tmp_path / "record"
    folder.mkdir()
    write_journal(folder, "serial", 11, starts=[("t.py::a", None)])
    held = []
    real_hold = er._hold_directory

    def remember(path):
        fd = real_hold(path)
        held.append(fd)
        return fd

    monkeypatch.setattr(er, "_hold_directory", remember)
    er.reduce_journals(folder, ["t.py::a"])  # a good run
    monkeypatch.setattr(er, "MAX_JOURNALS", 0)
    er.reduce_journals(folder, ["t.py::a"])  # refused as too large
    monkeypatch.undo()
    monkeypatch.setattr(er, "_hold_directory", remember)
    monkeypatch.setattr(er._Folder, "unchanged", lambda self: False)
    er.reduce_journals(folder, ["t.py::a"])  # every journal refused
    er._read_journal(folder / "journal-serial-11-1000.jsonl", er._Problems())  # a single journal read

    assert len(held) == 4
    for fd in held:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.skipif(sys.platform == "win32", reason="listing through the held descriptor is the POSIX way")
def test_on_posix_the_listing_comes_from_the_held_directory_not_from_the_path(er, tmp_path, monkeypatch):
    folder = tmp_path / "record"
    folder.mkdir()
    write_journal(folder, "serial", 11, starts=[("t.py::a", None)])

    def no_path_listing(self, pattern):
        raise AssertionError("the journals were listed through the path")

    monkeypatch.setattr(Path, "glob", no_path_listing)

    summary = er.reduce_journals(folder, ["t.py::a"])

    assert summary["accepted"] is True and list(summary["items"]) == ["t.py::a"]


def test_the_docstring_states_the_single_baseline_and_the_refusal_to_fall_back(er):
    flat = " ".join(er.__doc__.split())
    for text in (
        "folder-not-held",
        "is opened ONCE and that held object is the baseline",
        "without delete sharing",
        "never a second lookup of the path",
        "never a silent fallback to path checks",
    ):
        assert text in flat, text


def test_the_held_folder_is_released_when_the_listing_fails(er, tmp_path, monkeypatch):
    # codex-rev's P2: the handle was taken before the listing, but only the caller's later try/finally closed it, so an
    # exception during the listing left the descriptor open
    folder = tmp_path / "record"
    folder.mkdir()
    write_journal(folder, "serial", 11, starts=[("t.py::a", None)])
    held = []
    real_hold = er._hold_directory

    def remember(path):
        fd = real_hold(path)
        held.append(fd)
        return fd

    def listing_fails(*args, **kwargs):
        raise RuntimeError("the listing failed")

    monkeypatch.setattr(er, "_hold_directory", remember)
    monkeypatch.setattr(Path, "glob", listing_fails)  # Windows lists through the path
    monkeypatch.setattr(os, "listdir", listing_fails)  # POSIX lists through the held descriptor

    with pytest.raises(RuntimeError, match="the listing failed"):
        er.reduce_journals(folder, ["t.py::a"])

    assert len(held) == 1
    with pytest.raises(OSError):
        os.fstat(held[0])


# --- fix round 13: the connector's second review (five threads on dddf037b) -------------------------------------

TOKEN2 = "0000000000000002"


def _launch(*tokens):
    return [("nested-launch", {"token": token}) for token in tokens]


# thread 5: a phase outcome is one of three words


@pytest.mark.parametrize("outcome", ["faield", "", "PASSED", "xfailed", "error"])
def test_a_damaged_phase_outcome_is_a_bad_field_and_the_run_is_not_accepted(er, tmp_path, outcome):
    phases = {"t.py::a": [("setup", "passed"), ("call", outcome), ("teardown", "passed")]}
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], phases=phases)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert "bad-field" in kinds(summary["problems"]), summary["problems"]
    assert summary["accepted"] is False


@pytest.mark.parametrize("when", ["Call", "tear down", "", "???x"])
def test_a_damaged_phase_name_is_a_bad_field_too(er, tmp_path, when):
    phases = {"t.py::a": [("setup", "passed"), (when, "passed"), ("teardown", "passed")]}
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], phases=phases)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert "bad-field" in kinds(summary["problems"]), summary["problems"]
    assert summary["accepted"] is False


@pytest.mark.parametrize("outcome", ["passed", "failed", "skipped"])
def test_the_three_real_outcomes_are_still_accepted(er, tmp_path, outcome):
    phases = {"t.py::a": [("setup", "passed"), ("call", outcome), ("teardown", "passed")]}
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], phases=phases)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert "bad-field" not in kinds(summary["problems"])


# thread 2: one root run per record folder


def test_two_complete_root_serial_journals_are_not_merged_into_one_run(er, tmp_path):
    # a reused record folder: one journal holds only `a`, another only `b`; no pytest run collected both
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    write_journal(tmp_path, "serial", 12, starts=[("t.py::b", None)])

    summary = er.reduce_journals(tmp_path, ["t.py::a", "t.py::b"])

    assert "multiple-root-runs" in kinds(summary["problems"]), summary["problems"]
    assert summary["accepted"] is False


def test_a_serial_journal_beside_worker_journals_is_a_mixed_topology(er, tmp_path):
    rows0, rows1 = [("t.py::a", None)], [("t.py::b", None)]
    both = rows0 + rows1
    nodes = [
        ("collection-manifest", manifest_fields(both, "node", "gw0")),
        ("collection-manifest", manifest_fields(both, "node", "gw1")),
    ]
    write_journal(tmp_path, "controller", 10, extra=nodes)
    write_journal(tmp_path, "worker", 11, worker="gw0", starts=rows0, collected=both)
    write_journal(tmp_path, "worker", 12, worker="gw1", starts=rows1, collected=both)
    assert er.reduce_journals(tmp_path, ["t.py::a", "t.py::b"])["accepted"], "the control: one xdist run is fine"
    write_journal(tmp_path, "serial", 13, starts=[("t.py::a", None)])  # stale evidence from an earlier serial run

    summary = er.reduce_journals(tmp_path, ["t.py::a", "t.py::b"])

    assert "mixed-root-topology" in kinds(summary["problems"]), summary["problems"]
    assert summary["accepted"] is False


def test_two_root_controllers_or_one_worker_id_twice_are_not_one_run(er, tmp_path):
    rows = [("t.py::a", None)]
    node = [("collection-manifest", manifest_fields(rows, "node", "gw0"))]
    write_journal(tmp_path, "controller", 10, extra=node)
    write_journal(tmp_path, "worker", 11, worker="gw0", starts=rows)
    assert er.reduce_journals(tmp_path, ["t.py::a"])["accepted"], "the control"

    write_journal(tmp_path, "worker", 12, worker="gw0", starts=rows)  # the same worker id again
    twice = er.reduce_journals(tmp_path, ["t.py::a"])
    (tmp_path / "journal-worker-12-1000.jsonl").unlink()
    write_journal(tmp_path, "controller", 13, extra=node)  # a second root controller
    controllers = er.reduce_journals(tmp_path, ["t.py::a"])

    assert "multiple-root-runs" in kinds(twice["problems"]) and twice["accepted"] is False
    assert "multiple-root-runs" in kinds(controllers["problems"]) and controllers["accepted"] is False


def test_nested_runs_do_not_count_as_root_runs(er, tmp_path):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=_launch(NESTED_TOKEN, TOKEN2))
    write_journal(tmp_path, "nested", 12, nested_token=NESTED_TOKEN, starts=[("n.py::x", None)])
    write_journal(tmp_path, "nested", 13, nested_token=TOKEN2, starts=[("n.py::y", None)])

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert summary["problems"] == [], summary["problems"]
    assert summary["accepted"] is True


# thread 3: nested manifests are reconciled per token


def test_a_nested_item_that_was_collected_and_never_started_is_a_problem_of_its_run(er, tmp_path):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=_launch(NESTED_TOKEN))
    write_journal(tmp_path, "nested", 12, nested_token=NESTED_TOKEN, starts=[], collected=[("n.py::x", None)])

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    (problem,) = [p for p in summary["problems"] if p["kind"] == "never-started"]
    assert problem["nodeid"] == "n.py::x" and problem["nested_run"] == NESTED_TOKEN
    assert summary["accepted"] is False
    assert "n.py::x" not in summary["items"], "the nested id stays out of the root summary"
    assert summary["collection"]["never_started"] == []


def test_each_nested_run_is_counted_on_its_own_when_two_runs_share_an_id(er, tmp_path):
    # the same id collected by both nested runs, started by only one of them
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=_launch(NESTED_TOKEN, TOKEN2))
    write_journal(tmp_path, "nested", 12, nested_token=NESTED_TOKEN, starts=[("n.py::x", None)])
    write_journal(tmp_path, "nested", 13, nested_token=TOKEN2, starts=[], collected=[("n.py::x", None)])

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert [(p["kind"], p["nested_run"]) for p in summary["problems"]] == [("never-started", TOKEN2)]


def test_a_nested_worker_that_never_started_a_collected_item_is_caught_too(er, tmp_path):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=_launch(NESTED_TOKEN))
    rows = [("n.py::x", None), ("n.py::y", None)]
    nodes = [("collection-manifest", manifest_fields(rows, "node", "gw0"))]
    write_journal(tmp_path, "nested", 12, nested_token=NESTED_TOKEN, extra=nodes)
    write_journal(tmp_path, "worker", 13, worker="gw0", nested_token=NESTED_TOKEN, starts=rows[:1], collected=rows)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert [(p["kind"], p["nodeid"]) for p in summary["problems"] if p["kind"] == "never-started"] == [
        ("never-started", "n.py::y")
    ]


# thread 4: a nested xdist run has its own controller, checked by token


def _nested_xdist(tmp_path, *, controller=True, node_rows=None, worker_rows=None):
    rows0, rows1 = [("n.py::x", None)], [("n.py::y", None)]
    both = rows0 + rows1  # xdist workers all collect the one common list; each starts only the items it is given
    node_rows = node_rows or {"gw0": both, "gw1": both}
    worker_rows = worker_rows or {"gw0": rows0, "gw1": rows1}
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=_launch(NESTED_TOKEN))
    if controller:
        nodes = [("collection-manifest", manifest_fields(rows, "node", w)) for w, rows in node_rows.items()]
        write_journal(tmp_path, "nested", 12, nested_token=NESTED_TOKEN, extra=nodes)
    for number, (worker, rows) in enumerate(worker_rows.items(), start=13):
        write_journal(tmp_path, "worker", number, worker=worker, nested_token=NESTED_TOKEN, starts=rows, collected=both)


def test_a_complete_nested_xdist_run_is_accepted(er, tmp_path):
    _nested_xdist(tmp_path)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert summary["problems"] == [], summary["problems"]
    assert summary["accepted"] is True


def test_a_nested_xdist_run_without_its_controller_journal_is_refused_by_token(er, tmp_path):
    _nested_xdist(tmp_path, controller=False)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    (problem,) = [p for p in summary["problems"] if p["kind"] == "controller-missing"]
    assert problem["nested_run"] == NESTED_TOKEN
    assert summary["accepted"] is False


def test_a_nested_controller_that_disagrees_with_a_nested_worker_is_refused(er, tmp_path):
    both = [("n.py::x", None), ("n.py::y", None)]
    _nested_xdist(tmp_path, node_rows={"gw0": [*both, ("n.py::z", None)], "gw1": both})

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    (problem,) = [p for p in summary["problems"] if p["kind"] == "manifest-disagreement"]
    assert problem["worker"] == "gw0" and problem["nested_run"] == NESTED_TOKEN


def test_a_nested_controller_that_recorded_a_worker_with_no_manifest_is_refused(er, tmp_path):
    both = [("n.py::x", None), ("n.py::y", None)]
    _nested_xdist(tmp_path, node_rows={"gw0": both, "gw1": both, "gw2": []})

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert [(p["kind"], p["worker"]) for p in summary["problems"]] == [("manifest-disagreement", "gw2")]


def test_a_root_xdist_run_and_a_nested_xdist_run_are_checked_apart(er, tmp_path):
    # the same worker id (gw0) in the root run and in the nested run must not be compared with each other
    rows = [("t.py::a", None)]
    node = [("collection-manifest", manifest_fields(rows, "node", "gw0"))]
    write_journal(tmp_path, "controller", 10, extra=node + _launch(NESTED_TOKEN))
    write_journal(tmp_path, "worker", 11, worker="gw0", starts=rows)
    nested_nodes = [("collection-manifest", manifest_fields([("n.py::x", None)], "node", "gw0"))]
    write_journal(tmp_path, "nested", 12, nested_token=NESTED_TOKEN, extra=nested_nodes, parent_pid=10)
    write_journal(
        tmp_path, "worker", 13, worker="gw0", nested_token=NESTED_TOKEN, starts=[("n.py::x", None)], parent_pid=10
    )

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert summary["problems"] == [], summary["problems"]
    assert summary["accepted"] is True


# thread 1: the inactive-helper test must not depend on whether the run is recorded

RECORDED_TEST = (
    "tests/test_experiment_record.py::test_the_helper_without_a_recorder_hands_off_nothing_and_drops_a_stale_entry"
)


def test_the_inactive_helper_test_passes_in_a_recorded_run_and_the_record_reduces_cleanly(er, tmp_path):
    rec = tmp_path / "rec"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_ADDOPTS", "PYTHONPATH", "PYTEST_CURRENT"))}
    env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONPATH=str(SRC_DIR))
    argv = [sys.executable, "-m", "pytest", RECORDED_TEST, "-q", "-p", "no:cacheprovider"]
    argv += ["--basetemp", str(tmp_path / "bt"), f"--experiment-record={rec}"]

    done = subprocess.run(argv, cwd=TESTS_DIR.parent, env=env, capture_output=True, text=True, timeout=300, check=False)

    assert done.returncode == 0, done.stdout + done.stderr
    summary = er.reduce_journals(rec, [RECORDED_TEST])
    assert summary["problems"] == [], summary["problems"]
    assert summary["accepted"] is True


# --- fix round 14: nested manifests must be present, and a nested token is one run -----------------------------------


def test_a_nested_serial_run_that_lost_its_manifest_is_refused_naming_its_run(er, tmp_path):
    # codex-rev's P1: with the manifest gone, a collected item that never ran could not be seen at all
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=_launch(NESTED_TOKEN))
    write_journal(tmp_path, "nested", 12, nested_token=NESTED_TOKEN, starts=[], manifest=False)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    (problem,) = [p for p in summary["problems"] if p["kind"] == "manifest-missing"]
    assert problem["nested_run"] == NESTED_TOKEN and problem["file"].startswith("journal-nested-12-")
    assert summary["accepted"] is False


def test_the_same_nested_serial_run_with_its_manifest_is_accepted(er, tmp_path):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=_launch(NESTED_TOKEN))
    write_journal(tmp_path, "nested", 12, nested_token=NESTED_TOKEN, starts=[("n.py::x", None)])

    assert er.reduce_journals(tmp_path, ["t.py::a"])["accepted"] is True


def _nested_xdist_without(tmp_path, *, worker_manifests, node_manifests):
    rows = {"gw0": [("n.py::x", None)], "gw1": [("n.py::y", None)]}
    both = [row for r in rows.values() for row in r]  # every worker collects the one common list
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=_launch(NESTED_TOKEN))
    nodes = [("collection-manifest", manifest_fields(both, "node", w)) for w in rows] if node_manifests else []
    write_journal(tmp_path, "nested", 12, nested_token=NESTED_TOKEN, extra=nodes)
    for number, (worker, r) in enumerate(rows.items(), start=13):
        write_journal(
            tmp_path,
            "worker",
            number,
            worker=worker,
            nested_token=NESTED_TOKEN,
            starts=r,
            collected=both,
            manifest=worker_manifests,
        )


def test_a_nested_xdist_run_whose_worker_and_controller_manifests_are_all_gone_is_refused(er, tmp_path):
    _nested_xdist_without(tmp_path, worker_manifests=False, node_manifests=False)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    missing = [p for p in summary["problems"] if p["kind"] == "manifest-missing"]
    assert sorted(p["file"] for p in missing) == ["journal-worker-13-1000.jsonl", "journal-worker-14-1000.jsonl"]
    assert {p["nested_run"] for p in missing} == {NESTED_TOKEN}
    assert summary["accepted"] is False


def test_a_nested_xdist_run_with_only_its_worker_manifests_gone_is_refused(er, tmp_path):
    _nested_xdist_without(tmp_path, worker_manifests=False, node_manifests=True)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert {"manifest-missing", "manifest-disagreement"} <= kinds(summary["problems"])
    assert summary["accepted"] is False


def test_a_nested_xdist_run_with_only_its_controller_manifests_gone_is_refused(er, tmp_path):
    _nested_xdist_without(tmp_path, worker_manifests=True, node_manifests=False)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert "manifest-disagreement" in kinds(summary["problems"]) and summary["accepted"] is False


def test_a_nested_controller_needs_no_manifest_of_its_own_like_the_root_controller(er, tmp_path):
    # the control for the presence rule: a run's controller does not collect, so only workers and serial runs need one
    _nested_xdist_without(tmp_path, worker_manifests=True, node_manifests=True)

    assert er.reduce_journals(tmp_path, ["t.py::a"])["accepted"] is True


def test_two_nested_children_with_one_token_are_not_merged_into_one_run(er, tmp_path):
    # claude-rev's P2: child A collected x and never started it, child B ran x; merged, the run looked complete
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=_launch(NESTED_TOKEN))
    write_journal(tmp_path, "nested", 12, nested_token=NESTED_TOKEN, starts=[], collected=[("n.py::x", None)])
    write_journal(tmp_path, "nested", 13, nested_token=NESTED_TOKEN, starts=[("n.py::x", None)])

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    (problem,) = [p for p in summary["problems"] if p["kind"] == "multiple-nested-runs"]
    assert problem["nested_run"] == NESTED_TOKEN
    assert summary["accepted"] is False


def test_one_worker_id_twice_under_one_nested_token_is_not_one_run(er, tmp_path):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=_launch(NESTED_TOKEN))
    rows = [("n.py::x", None)]
    nodes = [("collection-manifest", manifest_fields(rows, "node", "gw0"))]
    write_journal(tmp_path, "nested", 12, nested_token=NESTED_TOKEN, extra=nodes)
    write_journal(tmp_path, "worker", 13, worker="gw0", nested_token=NESTED_TOKEN, starts=rows)
    assert er.reduce_journals(tmp_path, ["t.py::a"])["accepted"] is True, "the control: one gw0 is fine"
    write_journal(tmp_path, "worker", 14, worker="gw0", nested_token=NESTED_TOKEN, starts=rows)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    (problem,) = [p for p in summary["problems"] if p["kind"] == "multiple-nested-runs"]
    assert problem["nested_run"] == NESTED_TOKEN and summary["accepted"] is False


def test_the_same_worker_id_under_two_different_tokens_is_two_runs_and_fine(er, tmp_path):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=_launch(NESTED_TOKEN, TOKEN2))
    for pid, token in ((12, NESTED_TOKEN), (14, TOKEN2)):
        rows = [("n.py::x", None)]
        nodes = [("collection-manifest", manifest_fields(rows, "node", "gw0"))]
        write_journal(tmp_path, "nested", pid, nested_token=token, extra=nodes)
        write_journal(tmp_path, "worker", pid + 1, worker="gw0", nested_token=token, starts=rows)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert summary["problems"] == [] and summary["accepted"] is True


# real nested two-worker children: no valid run may be refused

NESTED_XDIST_PARENT = """
import subprocess
import sys

import experiment_record

CONFTEST = "{conftest}"


def launch(tmp_path, env):
    (tmp_path / "conftest.py").write_text(CONFTEST, encoding="utf-8")
    (tmp_path / "test_child.py").write_text(
        "\\n\\n".join(f"def test_c{n}():\\n    pass\\n" for n in range(1, 5)), encoding="utf-8"
    )
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "xdist.plugin", "-n", "2", "-p", "no:cacheprovider",
         "--basetemp", str(tmp_path / "bt"), str(tmp_path / "test_child.py")],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=180,
    )


def test_with_the_helper(tmp_path):
    done = launch(tmp_path, experiment_record.nested_environment())
    assert done.returncode == 0, done.stdout + done.stderr
""".replace("{conftest}", NESTED_CONFTEST)


def test_a_real_nested_two_worker_child_inside_a_recorded_serial_run_is_accepted(project, er, tmp_path):
    project.makepyfile(test_parent=NESTED_XDIST_PARENT)
    rec = tmp_path / "rec"

    run_serial(project, rec, "-k", "test_with_the_helper").assert_outcomes(passed=1)

    summary = er.reduce_journals(rec, ["test_parent.py::test_with_the_helper"])
    assert summary["problems"] == [], summary["problems"]
    assert summary["accepted"] is True and summary["nested"] == {"launched": 1, "journaled": 1}
    roles = sorted((p["role"], p["worker"]) for p in summary["processes"])
    assert roles == [("nested", None), ("serial", None), ("worker", "gw0"), ("worker", "gw1")]


def test_a_real_nested_two_worker_child_inside_a_recorded_two_worker_run_is_accepted(project, er, tmp_path):
    project.makepyfile(test_parent=NESTED_XDIST_PARENT)
    rec = tmp_path / "rec"

    run_parallel(project, rec, "-k", "test_with_the_helper").assert_outcomes(passed=1)

    summary = er.reduce_journals(rec)
    assert summary["problems"] == [], summary["problems"]
    assert summary["accepted"] is True and summary["nested"] == {"launched": 1, "journaled": 1}
    roles = sorted((p["role"], p["worker"], bool(p["nested_token"])) for p in summary["processes"])
    assert roles.count(("worker", "gw0", True)) == 1 and roles.count(("worker", "gw0", False)) == 1
    assert ("controller", None, False) in roles and ("nested", None, True) in roles


# --- round 15: the connector's third review (seven threads on 75e82ca6) -----------------------------------------


TOKEN = "0000000000000001"


def tamper(path, kind, nth=0, **changes):
    """Rewrite the `nth` record of `kind` in a hand-made journal with `changes` (a value of `...` removes the key)."""
    lines = Path(path).read_text(encoding="utf-8").split("\n")
    seen = -1
    for number, line in enumerate(lines):
        if not line:
            continue
        record = json.loads(line)
        if record["kind"] != kind:
            continue
        seen += 1
        if seen == nth:
            for key, value in changes.items():
                if value is ...:
                    record.pop(key, None)
                else:
                    record[key] = value
            lines[number] = json.dumps(record)
            break
    Path(path).write_text("\n".join(lines), encoding="utf-8")


# thread 4429622268 (P1): a stale journal from an earlier run must not be accepted as this run's


def test_a_complete_journal_of_another_run_is_not_accepted_as_this_runs(er, tmp_path):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    tamper(path, "process-start", run_id="old-run")

    stale = er.reduce_journals(tmp_path, ["t.py::a"], run_id="new-run")
    same = er.reduce_journals(tmp_path, ["t.py::a"], run_id="old-run")

    assert stale["accepted"] is False and "run-mismatch" in kinds(stale["problems"])
    assert same["accepted"] is True, same["problems"]


def test_a_journal_with_no_run_id_does_not_match_a_run_that_asked_for_one(er, tmp_path):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])

    summary = er.reduce_journals(tmp_path, ["t.py::a"], run_id="new-run")

    (problem,) = [p for p in summary["problems"] if p["kind"] == "run-mismatch"]
    assert summary["accepted"] is False and problem["file"] == "journal-serial-11-1000.jsonl"


def test_without_a_run_id_the_reducer_behaves_as_before(er, tmp_path):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    tamper(path, "process-start", run_id="whatever")

    assert er.reduce_journals(tmp_path, ["t.py::a"])["accepted"] is True


@pytest.mark.parametrize("bad", ["", "has space", "a" * 65, 5, "../x"])
def test_a_malformed_run_id_is_refused_by_the_reducer(er, tmp_path, bad):
    with pytest.raises(ValueError, match="run_id"):
        er.reduce_journals(tmp_path, run_id=bad)


def test_an_early_failure_of_the_new_invocation_leaves_the_old_journal_unaccepted(project, er, tmp_path):
    project.makepyfile(test_mod=GROUPED)
    rec = tmp_path / "rec"
    run_serial(project, rec, "--experiment-record-run=first").assert_outcomes(passed=3)
    expected = collected_ids(project)
    assert er.reduce_journals(rec, expected, run_id="first")["accepted"] is True

    # the second invocation never reaches pytest_configure (it dies on an unknown option), so it writes nothing
    broken = project.runpytest_subprocess(f"--experiment-record={rec}", "--experiment-record-run=second", "--nonsense")
    summary = er.reduce_journals(rec, expected, run_id="second")

    assert broken.ret != 0
    assert summary["accepted"] is False and "run-mismatch" in kinds(summary["problems"])


def test_the_run_id_reaches_the_workers_and_a_nested_run(project, er, tmp_path):
    project.makepyfile(test_mod=GROUPED)
    rec = tmp_path / "rec"

    run_parallel(project, rec, "--experiment-record-run=r-2").assert_outcomes(passed=3)

    starts = [r for _, r in records(rec, "process-start")]
    assert {r["role"] for r in starts} == {"controller", "worker"} and {r["run_id"] for r in starts} == {"r-2"}
    assert er.reduce_journals(rec, collected_ids(project), run_id="r-2")["accepted"] is True


def test_the_run_id_reaches_a_nested_run_through_the_handoff(project, er, tmp_path):
    project.makepyfile(test_parent=NESTED_PARENT)
    rec = tmp_path / "rec"

    run_serial(project, rec, "-k", "test_with_the_helper", "--experiment-record-run=r-3").assert_outcomes(passed=1)

    assert {r["run_id"] for _, r in records(rec, "process-start")} == {"r-3"}
    summary = er.reduce_journals(rec, ["test_parent.py::test_with_the_helper"], run_id="r-3")
    assert summary["accepted"], summary["problems"]


def test_a_nested_journal_of_another_run_is_not_accepted(er, tmp_path):
    root = write_journal(
        tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=[("nested-launch", {"token": TOKEN})]
    )
    nested = write_journal(tmp_path, "nested", 12, nested_token=TOKEN, starts=[("n.py::b", None)])
    tamper(root, "process-start", run_id="r-4")
    tamper(nested, "process-start", run_id="elsewhere")

    summary = er.reduce_journals(tmp_path, ["t.py::a"], run_id="r-4")

    assert summary["accepted"] is False
    assert [p["file"] for p in summary["problems"] if p["kind"] == "run-mismatch"] == [nested.name]


@pytest.mark.parametrize("bad", ["", "has space", "a" * 65])
def test_a_malformed_run_id_option_is_a_usage_error(project, tmp_path, bad):
    project.makepyfile(test_mod=GROUPED)

    result = run_serial(project, tmp_path / "rec", f"--experiment-record-run={bad}")

    assert result.ret != 0 and "run id" in "\n".join(result.outlines + result.errlines)


def test_a_run_id_with_no_record_folder_is_a_usage_error(project):
    project.makepyfile(test_mod=GROUPED)

    result = project.runpytest_subprocess("--experiment-record-run=r-5")

    assert result.ret != 0 and "--experiment-record" in "\n".join(result.outlines + result.errlines)


# thread 4429622271 (P2): recording asked for through PYTEST_ADDOPTS must not be inherited by ordinary children


def test_a_run_recorded_through_addopts_does_not_make_ordinary_children_record(project, er, tmp_path, monkeypatch):
    project.makepyfile(test_parent=NESTED_PARENT)
    rec = tmp_path / "rec"
    monkeypatch.setenv("PYTEST_ADDOPTS", f"--experiment-record={rec.as_posix()}")

    project.runpytest_subprocess("-k", "test_without_the_helper").assert_outcomes(passed=1)

    assert [r["role"] for _, r in records(rec, "process-start")] == ["serial"]
    summary = er.reduce_journals(rec, ["test_parent.py::test_without_the_helper"])
    assert summary["accepted"], summary["problems"]


def test_an_explicit_handoff_still_records_when_recording_was_asked_for_through_addopts(
    project, er, tmp_path, monkeypatch
):
    project.makepyfile(test_parent=NESTED_PARENT)
    rec = tmp_path / "rec"
    monkeypatch.setenv("PYTEST_ADDOPTS", f"--experiment-record={rec.as_posix()}")

    project.runpytest_subprocess("-k", "test_with_the_helper").assert_outcomes(passed=1)

    assert sorted(r["role"] for _, r in records(rec, "process-start")) == ["nested", "serial"]
    assert er.reduce_journals(rec, ["test_parent.py::test_with_the_helper"])["accepted"] is True


ADDOPTS_PARENT = """
import os


def test_the_other_options_are_kept():
    assert os.environ["PYTEST_ADDOPTS"] == "-p no:cacheprovider -q"
"""


def test_only_the_recording_options_are_removed_from_addopts(project, tmp_path, monkeypatch):
    project.makepyfile(test_parent=ADDOPTS_PARENT)
    rec = tmp_path / "rec"
    monkeypatch.setenv(
        "PYTEST_ADDOPTS", f"-p no:cacheprovider --experiment-record={rec.as_posix()} -q --experiment-record-run=x"
    )

    project.runpytest_subprocess().assert_outcomes(passed=1)


def test_addopts_that_held_only_recording_options_is_removed(project, tmp_path, monkeypatch):
    project.makepyfile(test_parent="import os\n\n\ndef test_gone():\n    assert 'PYTEST_ADDOPTS' not in os.environ\n")
    monkeypatch.setenv("PYTEST_ADDOPTS", f"--experiment-record {(tmp_path / 'rec').as_posix()}")

    project.runpytest_subprocess().assert_outcomes(passed=1)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("--experiment-record=/r", ""),
        ("--experiment-record /r", ""),
        ("-q --experiment-record /r -x", "-q -x"),
        ("-q --experiment-record=/r --experiment-record-run=a -x", "-q -x"),
        ("--experiment-record-run a -q", "-q"),
        ("--experiment-record '/a b/r' -q", "-q"),
        ("-k 'not --experiment-record' -q", "-k 'not --experiment-record' -q"),
        ("--experiment-recorder=x -q", "--experiment-recorder=x -q"),
        ("-q", "-q"),
    ],
)
def test_the_recording_options_are_taken_out_of_addopts_and_nothing_else(er, text, expected):
    assert er._without_record_options(text) == expected


def test_addopts_with_unbalanced_quotes_is_left_exactly_as_it_was(er):
    assert er._without_record_options("-k 'oops --experiment-record=/r") == "-k 'oops --experiment-record=/r"


# thread 4429622273 (P2): an unknown event kind is a damaged record


def test_a_mistyped_event_kind_is_a_bad_field_and_cannot_hide_a_nested_launch(er, tmp_path):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=[("nested-launc", {"token": TOKEN})])
    write_journal(tmp_path, "nested", 12, nested_token=TOKEN, starts=[("n.py::b", None)])

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert summary["accepted"] is False
    assert any(p["kind"] == "bad-field" and "kind" in p["detail"] for p in summary["problems"])


def test_collection_finish_is_a_known_kind_with_checked_fields(er, tmp_path):
    finish = ("collection-finish", {"count": 1, "raw_ids_sha256": "0" * 64, "digest_algorithm": "sha256"})
    (tmp_path / "ok").mkdir()
    (tmp_path / "bad").mkdir()
    write_journal(tmp_path / "ok", "serial", 11, starts=[("t.py::a", None)], extra=[finish])
    write_journal(
        tmp_path / "bad", "serial", 11, starts=[("t.py::a", None)], extra=[("collection-finish", {"count": "1"})]
    )

    assert er.reduce_journals(tmp_path / "ok", ["t.py::a"])["accepted"] is True
    summary = er.reduce_journals(tmp_path / "bad", ["t.py::a"])
    assert summary["accepted"] is False and "bad-field" in kinds(summary["problems"])


def test_every_kind_the_recorder_writes_is_in_the_known_set(er):
    written = set(re.findall(r'self\.append\(\s*"([a-z-]+)"', Path(er.__file__).read_text(encoding="utf-8")))
    assert written <= set(er.EVENT_KINDS) and set(er.EVENT_KINDS) <= written | {"nested-launch"}


# thread 4429622276 (P2): role-appropriate identity fields


@pytest.mark.parametrize(
    ("role", "changes", "field"),
    [
        pytest.param("nested", {"nested_token": ...}, "nested_token", id="nested-without-token"),
        pytest.param("nested", {"nested_token": None}, "nested_token", id="nested-null-token"),
        pytest.param("nested", {"nested_token": "xyz"}, "nested_token", id="nested-bad-token"),
        pytest.param("nested", {"worker": "gw0"}, "worker", id="nested-with-worker"),
        pytest.param("worker", {"worker": None}, "worker", id="worker-without-id"),
        pytest.param("worker", {"worker": ""}, "worker", id="worker-empty-id"),
        pytest.param("worker", {"nested_token": "xyz"}, "nested_token", id="worker-bad-token"),
        pytest.param("serial", {"worker": "gw0"}, "worker", id="serial-with-worker"),
        pytest.param("serial", {"nested_token": TOKEN}, "nested_token", id="serial-with-token"),
        pytest.param("controller", {"worker": "gw0"}, "worker", id="controller-with-worker"),
        pytest.param("controller", {"nested_token": TOKEN}, "nested_token", id="controller-with-token"),
    ],
)
def test_a_process_whose_identity_does_not_fit_its_role_is_a_bad_field(er, tmp_path, role, changes, field):
    kwargs = {"worker": "gw0"} if role == "worker" else {}
    if role == "nested":
        kwargs["nested_token"] = TOKEN
    path = write_journal(tmp_path, role, 11, starts=[("t.py::a", None)] if role != "controller" else (), **kwargs)
    tamper(path, "process-start", **changes)

    summary = er.reduce_journals(tmp_path, ["t.py::a"] if role != "controller" else None)

    assert summary["accepted"] is False
    assert any(p["kind"] == "bad-field" and field in p["detail"] for p in summary["problems"])


def test_a_nested_journal_without_a_token_cannot_slip_through_with_an_empty_session(er, tmp_path):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    path = write_journal(tmp_path, "nested", 12, nested_token=TOKEN)
    tamper(path, "process-start", nested_token=...)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert summary["accepted"] is False


def test_the_roles_that_fit_their_fields_are_accepted(er, tmp_path):
    write_journal(tmp_path, "controller", 10)
    write_journal(tmp_path, "worker", 11, worker="gw0", starts=[("t.py::a", None)])
    write_journal(tmp_path, "worker", 12, worker="gw1", starts=[("t.py::b", None)])
    summary = er.reduce_journals(tmp_path, ["t.py::a", "t.py::b"])
    assert "bad-field" not in kinds(summary["problems"])


# thread 4429622280 (P2): a phase and its item agree on who the item is


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"canonical_id": "t.py::other"}, "canonical_id"),
        ({"param_id": "p1"}, "param_id"),
        ({"requested_group": "g9"}, "requested_group"),
        ({"effective_group": "g9"}, "effective_group"),
    ],
)
def test_a_phase_with_another_identity_than_its_item_is_not_accepted(er, tmp_path, changes, field):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], phases={"t.py::a": [("setup", "failed")]})
    tamper(path, "phase", nth=0, **changes)

    summary = er.reduce_journals(tmp_path, ["t.py::a"], require_phases=False)

    (problem,) = [p for p in summary["problems"] if p["kind"] == "phase-identity-mismatch"]
    assert field in problem["detail"] and summary["accepted"] is False


def test_a_failed_phase_with_a_false_identity_is_not_reported_as_accepted(er, tmp_path):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], phases={"t.py::a": [("setup", "failed")]})
    tamper(path, "phase", nth=0, canonical_id="t.py::liar")

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert summary["accepted"] is False and "phase-identity-mismatch" in kinds(summary["problems"])


def test_a_crash_report_with_no_item_identity_is_not_a_mismatch(er, tmp_path):
    write_journal(
        tmp_path,
        "controller",
        10,
        extra=[
            (
                "phase",
                {
                    **phase_fields("t.py::a", None, "???", "failed"),
                    "canonical_id": None,
                    "param_id": None,
                    "requested_group": None,
                    "effective_group": None,
                },
            )
        ],
    )

    summary = er.reduce_journals(tmp_path)

    assert "phase-identity-mismatch" not in kinds(summary["problems"])


@pytest.mark.parametrize(
    "changes",
    [{"canonical_id": "t.py::liar"}, {"param_id": "p1"}, {"effective_group": "g9"}],
)
def test_an_item_start_that_disagrees_with_its_manifest_row_is_not_accepted(er, tmp_path, changes):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    tamper(path, "item-start", **changes)
    tamper(path, "phase", nth=0, **changes)
    tamper(path, "phase", nth=1, **changes)
    tamper(path, "phase", nth=2, **changes)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert summary["accepted"] is False and "item-identity-mismatch" in kinds(summary["problems"])


def test_an_item_start_whose_canonical_id_does_not_follow_from_its_node_id_is_not_accepted(er, tmp_path):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", "g1")])
    tamper(path, "item-start", requested_group="g2", effective_group="g2")

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert "item-identity-mismatch" in kinds(summary["problems"])


# thread 4429622285 (P2): the Windows inventory looks at the entry itself


def test_the_inventory_without_a_directory_descriptor_never_follows_an_entry(er, tmp_path, monkeypatch):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    write_journal(tmp_path, "serial", 12, starts=[("t.py::b", None)])
    followed = []
    real = Path.stat

    def spy(self, *args, **kwargs):
        followed.append(self.name)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", spy)

    paths, total = er._listed_by_path(tmp_path)

    assert [p.name for p in paths] == ["journal-serial-11-1000.jsonl", "journal-serial-12-1000.jsonl"]
    assert total > 0 and not any(name.startswith("journal-") for name in followed)


def test_an_entry_that_is_a_link_does_not_count_the_size_of_its_target(er, tmp_path, monkeypatch):
    target = tmp_path / "elsewhere.bin"
    target.write_bytes(b"x" * 10_000)
    rec = tmp_path / "rec"
    rec.mkdir()
    try:
        os.symlink(target, rec / "journal-serial-1-1.jsonl")
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symbolic links")

    paths, total = er._listed_by_path(rec)

    assert [p.name for p in paths] == ["journal-serial-1-1.jsonl"] and total < 4096


# thread 4429622288 (P2): a record folder that cannot be examined is named, not silent


def test_a_record_folder_whose_lstat_fails_for_another_reason_is_a_named_problem(er, tmp_path, monkeypatch):
    rec = tmp_path / "rec"
    rec.mkdir()
    real = os.lstat

    def refuse(path, *args, **kwargs):
        if os.fspath(path) == os.fspath(rec):
            raise PermissionError(13, "Permission denied")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", refuse)

    summary = er.reduce_journals(rec)

    (problem,) = [p for p in summary["problems"] if p["kind"] == "folder-unreadable"]
    assert "Permission denied" in problem["detail"] and summary["accepted"] is False


@pytest.mark.parametrize("make", ["missing", "under-a-file"])
def test_a_record_folder_that_is_simply_not_there_is_still_just_incomplete(er, tmp_path, make):
    if make == "missing":
        rec = tmp_path / "nothing"
    else:
        (tmp_path / "file").write_text("x", encoding="utf-8")
        rec = tmp_path / "file" / "rec"

    summary = er.reduce_journals(rec)

    assert summary["problems"] == [] and summary["status"] == "incomplete" and summary["accepted"] is False


@pytest.mark.skipif(
    hasattr(os, "O_DIRECTORY") and os.open in os.supports_dir_fd, reason="the inventory by path is the Windows branch"
)
def test_reduce_journals_on_windows_never_follows_a_journal_entry_to_size_it(er, tmp_path, monkeypatch):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    followed = []
    real = Path.stat

    def spy(self, *args, **kwargs):
        followed.append(self.name)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", spy)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert summary["accepted"], summary["problems"]
    assert not any(name.startswith("journal-") for name in followed)


def test_the_inventory_by_path_uses_neither_stat_nor_a_link_size(er, tmp_path, monkeypatch):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    plain = path.stat().st_size
    seen = []
    real = os.stat

    def spy(target, *args, **kwargs):
        seen.append(Path(target).name)
        return real(target, *args, **kwargs)

    monkeypatch.setattr(os, "stat", spy)
    _lstat_that(monkeypatch, path.name, lambda fields, call: {**fields, "st_file_attributes": 1024, "st_size": 10**9})

    paths, total = er._listed_by_path(tmp_path)

    assert [p.name for p in paths] == [path.name] and total == 0 and plain > 0
    assert path.name not in seen


def test_a_plain_entry_is_counted_by_its_own_size(er, tmp_path):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])

    paths, total = er._listed_by_path(tmp_path)

    assert paths == [path] and total == path.stat().st_size


def test_a_handoff_with_a_bad_run_id_is_a_usage_error_and_a_good_one_is_read(er):
    handoff = {"v": 1, "dir": str(Path(__file__).resolve().parent), "token": "0000000000000001", "parent_pid": 1}

    assert er._read_handoff(json.dumps({**handoff, "run_id": "r-1"}))["run_id"] == "r-1"
    assert er._read_handoff(json.dumps(handoff))["run_id"] is None
    for bad in ("", "has space", "a" * 65, 5, ["x"]):
        with pytest.raises(pytest.UsageError, match="run id"):
            er._read_handoff(json.dumps({**handoff, "run_id": bad}))


@pytest.mark.parametrize(
    "changes",
    [{"count": "1"}, {"count": ...}, {"raw_ids_sha256": 5}, {"raw_ids_sha256": ...}, {"digest_algorithm": None}],
    ids=["count-text", "count-missing", "digest-number", "digest-missing", "algorithm-null"],
)
def test_each_field_of_collection_finish_is_checked_on_its_own(er, tmp_path, changes):
    finish = {"count": 1, "raw_ids_sha256": "0" * 64, "digest_algorithm": "sha256"}
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=[("collection-finish", finish)])
    tamper(path, "collection-finish", **changes)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert summary["accepted"] is False and "bad-field" in kinds(summary["problems"])


def test_an_item_start_not_in_the_manifest_still_has_its_canonical_id_checked_against_its_node_id(er, tmp_path):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], collected=[("t.py::other", None)])
    tamper(path, "item-start", canonical_id="t.py::liar")
    tamper(path, "phase", nth=0, canonical_id="t.py::liar")
    tamper(path, "phase", nth=1, canonical_id="t.py::liar")
    tamper(path, "phase", nth=2, canonical_id="t.py::liar")

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert "item-identity-mismatch" in kinds(summary["problems"]) and summary["accepted"] is False


# --- round 16: the connector's fourth review (five threads on fb060644) -------------------------------------------


def journal_record_count(path):
    return len([line for line in Path(path).read_text(encoding="utf-8").split("\n") if line])


# thread 4430325801 (P1): the number of records is capped before the journal is split


def test_a_journal_with_more_lines_than_the_cap_is_refused_before_it_is_split(er, tmp_path, monkeypatch):
    monkeypatch.setattr(er, "MAX_JOURNAL_RECORDS", 20)
    (tmp_path / "journal-serial-11-1000.jsonl").write_bytes(b"\n" * 21)

    summary = er.reduce_journals(tmp_path)

    (problem,) = [p for p in summary["problems"] if p["kind"] == "journal-too-many-records"]
    assert problem["file"] == "journal-serial-11-1000.jsonl" and "20" in problem["detail"]
    assert kinds(summary["problems"]) == {"journal-too-many-records"}  # no line was looked at, so no bad-line either
    assert summary["accepted"] is False and summary["status"] == "incomplete"


def test_a_journal_of_exactly_the_cap_is_read_and_one_record_more_is_refused(er, tmp_path, monkeypatch):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    count = journal_record_count(path)

    monkeypatch.setattr(er, "MAX_JOURNAL_RECORDS", count)
    assert er.reduce_journals(tmp_path, ["t.py::a"])["accepted"] is True
    monkeypatch.setattr(er, "MAX_JOURNAL_RECORDS", count - 1)
    refused = er.reduce_journals(tmp_path, ["t.py::a"])

    assert "journal-too-many-records" in kinds(refused["problems"]) and refused["accepted"] is False


def test_the_journals_together_may_not_hold_more_records_than_the_total_cap(er, tmp_path, monkeypatch):
    first = write_journal(tmp_path, "controller", 10)
    second = write_journal(tmp_path, "worker", 11, worker="gw0", starts=[("t.py::a", None)])
    total = journal_record_count(first) + journal_record_count(second)

    monkeypatch.setattr(er, "MAX_TOTAL_RECORDS", total)
    assert "journal-too-many-records" not in kinds(er.reduce_journals(tmp_path)["problems"])
    monkeypatch.setattr(er, "MAX_TOTAL_RECORDS", total - 1)
    refused = er.reduce_journals(tmp_path)

    (problem,) = [p for p in refused["problems"] if p["kind"] == "journal-too-many-records"]
    assert problem["file"] == second.name and refused["accepted"] is False


def test_a_journal_of_newlines_over_the_default_cap_is_refused_without_a_record_per_line(er, tmp_path):
    import tracemalloc

    (tmp_path / "journal-serial-11-1000.jsonl").write_bytes(b"\n" * (er.MAX_JOURNAL_RECORDS + 100_000))
    tracemalloc.start()
    try:
        summary = er.reduce_journals(tmp_path)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert "journal-too-many-records" in kinds(summary["problems"]) and summary["accepted"] is False
    # the read buffer (64 MiB, allocated up front) is the whole of it; a record per line would add ~250 bytes each
    assert peak < 128 * 1024 * 1024, peak


def test_the_record_caps_leave_room_for_a_real_run(er):
    # about 5 records per item (start, three phases, end) plus the manifests: a 12,000-item run is far below the cap
    assert er.MAX_JOURNAL_RECORDS >= 12_000 * 5 * 5 and er.MAX_TOTAL_RECORDS >= er.MAX_JOURNAL_RECORDS


# thread 4430325805 (P2): a manifest identity is bound to its own raw id, and a start must be in its own manifest


def test_a_manifest_whose_raw_id_disagrees_with_its_identity_is_not_accepted(er, tmp_path):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::b", None)])
    tamper(path, "collection-manifest", raw_ids=["t.py::a"])  # the identity still says t.py::b

    summary = er.reduce_journals(tmp_path, ["t.py::b"])

    (problem,) = [p for p in summary["problems"] if p["kind"] == "manifest-identity-mismatch"]
    assert problem["nodeid"] == "t.py::a" and summary["accepted"] is False


@pytest.mark.parametrize(
    "identity",
    [
        {"canonical_id": "t.py::a", "param_id": None, "requested_group": "g1", "effective_group": None},
        {"canonical_id": "t.py::a", "param_id": None, "requested_group": None, "effective_group": "g1"},
        {"canonical_id": "t.py::a@g1", "param_id": None, "requested_group": "g1", "effective_group": "g1"},
    ],
    ids=["group-suffix-not-seen", "effective-without-request", "canonical-keeps-suffix"],
)
def test_a_manifest_identity_whose_groups_do_not_follow_from_its_raw_id_is_not_accepted(er, tmp_path, identity):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a@g1", "g1")])
    tamper(path, "collection-manifest", identities=[identity])

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert "manifest-identity-mismatch" in kinds(summary["problems"]) and summary["accepted"] is False


def test_an_item_start_for_an_id_its_own_manifest_never_listed_is_not_accepted(er, tmp_path):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], collected=[("t.py::other", None)])

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    (problem,) = [p for p in summary["problems"] if p["kind"] == "item-not-in-manifest"]
    assert problem["nodeid"] == "t.py::a" and summary["accepted"] is False


def test_a_grouped_member_whose_identity_follows_from_its_raw_id_is_accepted(er, tmp_path):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a@g1", "g1"), ("t.py::b", None)])

    assert er.reduce_journals(tmp_path, ["t.py::a", "t.py::b"])["accepted"] is True


# thread 4430325808 (P2): a nonzero exit status in the completion trailer is an error on its own


@pytest.mark.parametrize("status", [2, 3, 4, 5])
def test_a_trailer_with_an_exit_status_other_than_ok_or_tests_failed_is_an_error(er, tmp_path, status):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    tamper(path, "trailer", exitstatus=status)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert summary["accepted"] is False and summary["first_error"]["kind"] == "trailer"
    assert summary["first_error"]["exitstatus"] == status


@pytest.mark.parametrize("status", [0, 1])
def test_a_trailer_with_status_zero_or_one_is_not_an_error(er, tmp_path, status):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    tamper(path, "trailer", exitstatus=status)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert summary["accepted"] is True and summary["first_error"] is None


def test_a_trailer_without_an_exit_status_is_a_bad_field(er, tmp_path):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    tamper(path, "trailer", exitstatus=...)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert summary["accepted"] is False and "bad-field" in kinds(summary["problems"])


def test_an_exit_status_event_and_a_trailer_that_disagree_are_not_accepted(er, tmp_path):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=[("exit-status", {"exitstatus": 0})])
    tamper(path, "trailer", exitstatus=1)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert "exit-status-mismatch" in kinds(summary["problems"]) and summary["accepted"] is False


def test_an_exit_status_event_and_a_trailer_that_agree_on_an_error_give_the_earlier_one_as_first(er, tmp_path):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=[("exit-status", {"exitstatus": 2})])
    tamper(path, "trailer", exitstatus=2)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert summary["first_error"]["kind"] == "exit-status" and "exit-status-mismatch" not in kinds(summary["problems"])


# thread 4430325811 (P2): an item end needs an active item


def test_a_second_item_end_for_one_start_is_not_accepted_and_is_not_counted(er, tmp_path):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=[("item-end", {"nodeid": "t.py::a"})])

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    (problem,) = [p for p in summary["problems"] if p["kind"] == "item-end-outside-item"]
    assert problem["nodeid"] == "t.py::a" and summary["items"]["t.py::a"]["ended"] == 1
    assert summary["accepted"] is False


def test_an_item_end_for_an_item_that_never_started_is_not_accepted(er, tmp_path):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=[("item-end", {"nodeid": "t.py::ghost"})])

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert "item-end-outside-item" in kinds(summary["problems"]) and summary["accepted"] is False


# thread 4430325814 (P2): an id is listed once in a manifest


def test_the_same_raw_id_twice_in_one_session_manifest_is_not_accepted(er, tmp_path):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], collected=[("t.py::a", None), ("t.py::a", None)])

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    (problem,) = [p for p in summary["problems"] if p["kind"] == "manifest-duplicate-id"]
    assert problem["nodeid"] == "t.py::a" and summary["accepted"] is False


def test_two_raw_ids_with_one_canonical_id_in_one_manifest_are_not_accepted(er, tmp_path):
    rows = [("t.py::a@g1", "g1"), ("t.py::a", None)]
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], collected=rows)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert "manifest-duplicate-id" in kinds(summary["problems"]) and summary["accepted"] is False


def test_the_same_raw_id_twice_in_a_controllers_node_manifest_is_not_accepted(er, tmp_path):
    node = manifest_fields([("t.py::a", None), ("t.py::a", None)], source="node", source_worker="gw0")
    write_journal(tmp_path, "controller", 10, extra=[("collection-manifest", node)])
    write_journal(tmp_path, "worker", 11, worker="gw0", starts=[("t.py::a", None)])

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert "manifest-duplicate-id" in kinds(summary["problems"]) and summary["accepted"] is False


def test_the_docstring_names_every_problem_kind_round_sixteen_added(er):
    for kind in (
        "journal-too-many-records",
        "manifest-identity-mismatch",
        "item-not-in-manifest",
        "manifest-duplicate-id",
        "item-end-outside-item",
        "exit-status-mismatch",
    ):
        assert kind in er.__doc__, kind


# --- round 17: the manifest check does not depend on where the records stand (thread 4430325805, claude-rev F1) ---


def edit_records(path, change):
    """Rewrite a hand-made journal: `change` gets the list of records (dicts, trailer last) and returns the new list;
    the sequence numbers and the trailer's count are then made right again."""
    records = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").split("\n") if line]
    records = change(records)
    for number, record in enumerate(records, 1):
        record["seq"] = number
    records[-1]["records"] = len(records) - 1
    Path(path).write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def item_block(raw, group=None, **identity_changes):
    """The five records of one finished item (start, three phases, end), as a hand-made journal writes them."""
    base = {"v": 2, "role": "serial", "worker": None, "pid": 11, "t_wall": 1500.0, "t_mono": 1.0}
    block = [("item-start", {**item_start_fields(raw, group), **identity_changes})]
    block += [("phase", {**phase_fields(raw, group, w, "passed"), **identity_changes}) for w in PHASE_ORDER]
    block.append(("item-end", {"nodeid": raw}))
    return [{**base, "kind": kind, **fields} for kind, fields in block]


PHASE_ORDER = ("setup", "call", "teardown")


def after_process_start_and_before_manifest(records, block):
    return records[:1] + block + records[1:]


def after_manifest(records, block):
    manifest = next(n for n, r in enumerate(records) if r["kind"] == "collection-manifest")
    return records[: manifest + 1] + block + records[manifest + 1 :]


def test_a_ghost_item_started_before_the_manifest_is_refused_without_expected_ids(er, tmp_path):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    edit_records(path, lambda r: after_process_start_and_before_manifest(r, item_block("t.py::ghost")))

    summary = er.reduce_journals(tmp_path)

    (problem,) = [p for p in summary["problems"] if p["kind"] == "item-not-in-manifest"]
    assert problem["nodeid"] == "t.py::ghost" and summary["accepted"] is False


def test_a_ghost_item_started_after_the_manifest_is_refused_too(er, tmp_path):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    edit_records(path, lambda r: after_manifest(r, item_block("t.py::ghost")))

    summary = er.reduce_journals(tmp_path)

    assert kinds(summary["problems"]) == {"item-not-in-manifest"} and summary["accepted"] is False


def test_an_item_the_manifest_lists_is_accepted_wherever_it_stands_among_the_records(er, tmp_path):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], collected=[("t.py::a", None)])

    def move_the_item_before_the_manifest(records):
        manifest = next(n for n, r in enumerate(records) if r["kind"] == "collection-manifest")
        item = [r for r in records if r["kind"] in ("item-start", "phase", "item-end")]
        rest = [r for r in records if r["kind"] not in ("item-start", "phase", "item-end")]
        return rest[:manifest] + item + rest[manifest:]

    edit_records(path, move_the_item_before_the_manifest)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert summary["accepted"] is True, summary["problems"]


@pytest.mark.parametrize("field", ["canonical_id", "param_id", "effective_group"])
def test_an_item_started_before_the_manifest_with_another_identity_than_its_row_is_refused(er, tmp_path, field):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)])
    value = {"canonical_id": "t.py::liar", "param_id": "p1", "effective_group": "g9"}[field]

    def replace_the_item(records):
        rest = [r for r in records if r["kind"] not in ("item-start", "phase", "item-end")]
        return after_process_start_and_before_manifest(rest, item_block("t.py::a", **{field: value}))

    edit_records(path, replace_the_item)

    summary = er.reduce_journals(tmp_path)

    assert "item-identity-mismatch" in kinds(summary["problems"]) and summary["accepted"] is False


# --- round 18: an exemption from the manifest is proven, not assumed (thread 4430325805, codex-rev N1) -------------

NESTED_TOKEN = "0000000000000001"


def nested_xdist_run(tmp_path, controller_items=()):
    """A recorded serial run that launched one nested xdist run: a real worker with its session manifest, and the
    nested controller (role "nested") with the node manifest and NO session manifest, running `controller_items`."""
    write_journal(
        tmp_path, "serial", 11, starts=[("root.py::a", None)], extra=[("nested-launch", {"token": NESTED_TOKEN})]
    )
    write_journal(tmp_path, "worker", 12, worker="gw0", nested_token=NESTED_TOKEN, starts=[("child.py::a", None)])
    node = manifest_fields([("child.py::a", None)], source="node", source_worker="gw0")
    return write_journal(
        tmp_path,
        "nested",
        13,
        nested_token=NESTED_TOKEN,
        manifest=False,
        starts=[(raw, None) for raw in controller_items],
        extra=[("collection-manifest", node)],
    )


def test_a_nested_xdist_controller_that_runs_no_item_needs_no_session_manifest(er, tmp_path):
    nested_xdist_run(tmp_path)

    summary = er.reduce_journals(tmp_path)

    assert summary["accepted"] is True, summary["problems"]


def test_a_nested_controller_with_no_session_manifest_that_runs_an_uncollected_item_is_refused(er, tmp_path):
    path = nested_xdist_run(tmp_path, controller_items=["child.py::ghost_never_collected"])

    summary = er.reduce_journals(tmp_path)

    (problem,) = [p for p in summary["problems"] if p["kind"] == "manifest-missing"]
    assert (
        problem["file"] == path.name and problem["nested_run"] == NESTED_TOKEN and "started items" in problem["detail"]
    )
    assert summary["accepted"] is False


def test_a_nested_controller_with_no_session_manifest_that_runs_a_listed_item_is_refused_too(er, tmp_path):
    # even an item that the workers did list: the controller itself said nothing about what it collected
    path = nested_xdist_run(tmp_path, controller_items=["child.py::a"])

    summary = er.reduce_journals(tmp_path, ["root.py::a"])

    assert any(p["kind"] == "manifest-missing" and p["file"] == path.name for p in summary["problems"])
    assert summary["accepted"] is False


def test_a_nested_controller_that_wrote_a_session_manifest_listing_what_it_ran_is_judged_like_any_nested_run(
    er, tmp_path
):
    write_journal(
        tmp_path, "serial", 11, starts=[("root.py::a", None)], extra=[("nested-launch", {"token": NESTED_TOKEN})]
    )
    write_journal(tmp_path, "nested", 13, nested_token=NESTED_TOKEN, starts=[("child.py::a", None)])

    assert er.reduce_journals(tmp_path, ["root.py::a"])["accepted"] is True


def test_a_root_xdist_controller_that_starts_an_item_is_refused(er, tmp_path):
    write_journal(tmp_path, "controller", 10, starts=[("t.py::a", None)], manifest=False)
    write_journal(tmp_path, "worker", 11, worker="gw0", starts=[("t.py::a", None)])

    summary = er.reduce_journals(tmp_path)

    (problem,) = [p for p in summary["problems"] if p["kind"] == "controller-ran-items"]
    assert problem["file"] == "journal-controller-10-1000.jsonl" and summary["accepted"] is False


def test_a_root_xdist_controller_that_starts_no_item_is_not_refused(er, tmp_path):
    write_journal(tmp_path, "controller", 10)
    write_journal(tmp_path, "worker", 11, worker="gw0", starts=[("t.py::a", None)])

    assert "controller-ran-items" not in kinds(er.reduce_journals(tmp_path)["problems"])


def test_the_docstring_names_the_two_refusals_round_eighteen_added(er):
    assert "controller-ran-items" in er.__doc__ and "started items" in er.__doc__


# --- round 19, part C: the connector's fifth review on c55e5194 (threads 4431208497 to 4431208517) -----------------


def _crash_report(raw):
    return (
        "phase",
        {
            **phase_fields(raw, None, "???", "failed"),
            "canonical_id": None,
            "param_id": None,
            "requested_group": None,
            "effective_group": None,
        },
    )


def _xdist_root(tmp_path, per_worker_collected, per_worker_starts=None, node_rows=None):
    """A controller and one worker per entry; each worker collected its own list and started its own items."""
    node_rows = node_rows or per_worker_collected
    nodes = [("collection-manifest", manifest_fields(rows, "node", w)) for w, rows in node_rows.items()]
    write_journal(tmp_path, "controller", 10, extra=nodes)
    for number, (worker, rows) in enumerate(per_worker_collected.items(), start=11):
        starts = (per_worker_starts or per_worker_collected)[worker]
        write_journal(tmp_path, "worker", number, worker=worker, starts=starts, collected=rows)


A_ROW, B_ROW = ("t.py::a", None), ("t.py::b", None)


def test_workers_that_collected_different_lists_are_refused_though_each_matches_its_controller_entry(er, tmp_path):
    # 4431208497 (P1): gw0 collected only a, gw1 only b; the controller's entries agree with each, and the union is the
    # expected list. xdist workers collect one common ordered list before any scheduling.
    _xdist_root(tmp_path, {"gw0": [A_ROW], "gw1": [B_ROW]})

    summary = er.reduce_journals(tmp_path, ["t.py::a", "t.py::b"])

    assert summary["accepted"] is False
    assert "workers-collected-differently" in kinds(summary["problems"])


def test_workers_that_collected_the_same_list_and_split_the_items_are_accepted(er, tmp_path):
    both = [A_ROW, B_ROW]
    _xdist_root(tmp_path, {"gw0": both, "gw1": both}, {"gw0": [A_ROW], "gw1": [B_ROW]})

    summary = er.reduce_journals(tmp_path, ["t.py::a", "t.py::b"])

    assert summary["problems"] == [] and summary["accepted"] is True


def test_workers_that_collected_the_same_ids_in_another_order_are_refused(er, tmp_path):
    _xdist_root(tmp_path, {"gw0": [A_ROW, B_ROW], "gw1": [B_ROW, A_ROW]}, {"gw0": [A_ROW], "gw1": [B_ROW]})

    summary = er.reduce_journals(tmp_path, ["t.py::a", "t.py::b"])

    assert "workers-collected-differently" in kinds(summary["problems"])


def test_workers_of_a_nested_run_must_agree_on_one_collection_too(er, tmp_path):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=_launch(NESTED_TOKEN))
    nodes = [("collection-manifest", manifest_fields([("n.py::x", None)], "node", "gw0"))]
    nodes += [("collection-manifest", manifest_fields([("n.py::y", None)], "node", "gw1"))]
    write_journal(tmp_path, "nested", 12, nested_token=NESTED_TOKEN, extra=nodes, parent_pid=11)
    for number, (worker, row) in enumerate({"gw0": ("n.py::x", None), "gw1": ("n.py::y", None)}.items(), start=13):
        write_journal(tmp_path, "worker", number, worker=worker, nested_token=NESTED_TOKEN, starts=[row], parent_pid=11)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    problem = next(p for p in summary["problems"] if p["kind"] == "workers-collected-differently")
    assert problem["nested_run"] == NESTED_TOKEN


def test_a_crash_report_in_a_serial_or_worker_journal_is_refused(er, tmp_path):
    # 4431208503 (P2): a complete serial journal with an empty manifest and one failed "???" phase for a ghost id.
    write_journal(tmp_path, "serial", 11, starts=[], extra=[_crash_report("ghost.py::g")])

    summary = er.reduce_journals(tmp_path, [])

    assert summary["accepted"] is False
    assert "crash-report-outside-controller" in kinds(summary["problems"])


def test_a_crash_report_for_an_id_the_controller_never_listed_is_refused(er, tmp_path):
    nodes = [("collection-manifest", manifest_fields([A_ROW], "node", "gw0"))]
    write_journal(tmp_path, "controller", 10, extra=[*nodes, _crash_report("ghost.py::g")])
    write_journal(tmp_path, "worker", 11, worker="gw0", starts=[A_ROW])

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert [p["nodeid"] for p in summary["problems"] if p["kind"] == "crash-report-unknown-item"] == ["ghost.py::g"]
    assert summary["accepted"] is False


def test_a_crash_report_for_a_listed_id_is_still_a_crash_record(er, tmp_path):
    nodes = [("collection-manifest", manifest_fields([A_ROW], "node", "gw0"))]
    write_journal(tmp_path, "controller", 10, extra=[*nodes, _crash_report("t.py::a")])
    write_journal(
        tmp_path, "worker", 11, worker="gw0", trailer=False, collected=[A_ROW], unfinished=[("t.py::a", None, [])]
    )

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert "crash-report-unknown-item" not in kinds(summary["problems"])
    assert "crash-report-outside-controller" not in kinds(summary["problems"])
    assert next(p for p in summary["problems"] if p["kind"] == "item-unfinished")["crash_recorded"] is True


def test_a_worker_crash_report_does_not_excuse_an_unfinished_item(er, tmp_path):
    nodes = [("collection-manifest", manifest_fields([A_ROW], "node", "gw0"))]
    write_journal(tmp_path, "controller", 10, extra=nodes)
    write_journal(
        tmp_path,
        "worker",
        11,
        worker="gw0",
        trailer=False,
        collected=[A_ROW],
        unfinished=[("t.py::a", None, [])],
        extra=[_crash_report("t.py::a")],
    )

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert "crash-report-outside-controller" in kinds(summary["problems"])
    assert next(p for p in summary["problems"] if p["kind"] == "item-unfinished")["crash_recorded"] is False


def test_a_nested_item_started_twice_is_refused_though_the_manifest_lists_it_once(er, tmp_path):
    # 4431208506 (P2): two complete start/phase/end blocks of one item in a nested run collapse into one set entry.
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=_launch(NESTED_TOKEN))
    write_journal(
        tmp_path,
        "nested",
        12,
        nested_token=NESTED_TOKEN,
        starts=[("n.py::x", None), ("n.py::x", None)],
        collected=[("n.py::x", None)],
        parent_pid=11,
    )

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    problem = next(p for p in summary["problems"] if p["kind"] == "duplicate-id")
    assert (problem["nodeid"], problem["nested_run"]) == ("n.py::x", NESTED_TOKEN)
    assert summary["accepted"] is False


def test_a_nested_item_started_once_is_accepted(er, tmp_path):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=_launch(NESTED_TOKEN))
    write_journal(tmp_path, "nested", 12, nested_token=NESTED_TOKEN, starts=[("n.py::x", None)], parent_pid=11)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert summary["problems"] == [] and summary["accepted"] is True


@pytest.mark.parametrize("persisted", [False, None, True], ids=["flag-false", "flag-omitted", "flag-true"])
def test_the_missing_group_suffix_is_derived_from_the_node_id_not_trusted_from_the_flag(er, tmp_path, persisted):
    # 4431208507 (P2): requested group g, no @g in the raw id; a damaged or omitted suffix_missing hid the problem.
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a", "g")])
    damaged(path, "item-start", suffix_missing=persisted)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert "suffix-missing" in kinds(summary["problems"])
    assert summary["accepted"] is False


def test_a_persisted_suffix_flag_that_contradicts_the_node_id_is_refused(er, tmp_path):
    path = write_journal(tmp_path, "serial", 11, starts=[("t.py::a@g", "g")])
    damaged(path, "item-start", suffix_missing=True)

    summary = er.reduce_journals(tmp_path, ["t.py"])

    assert "item-identity-mismatch" in kinds(summary["problems"])


def test_a_controller_with_no_worker_and_no_node_manifest_is_refused(er, tmp_path):
    # 4431208512 (P2): the controller role proves an xdist run; with no worker evidence at all the run was accepted.
    write_journal(tmp_path, "controller", 10)

    summary = er.reduce_journals(tmp_path, [])

    assert summary["accepted"] is False
    assert "worker-evidence-missing" in kinds(summary["problems"])


def test_a_controller_with_its_workers_has_no_worker_evidence_problem(er, tmp_path):
    _xdist_root(tmp_path, {"gw0": [A_ROW]})

    assert "worker-evidence-missing" not in kinds(er.reduce_journals(tmp_path, ["t.py::a"])["problems"])


def test_a_nested_journal_that_only_launched_itself_is_refused(er, tmp_path):
    # 4431208514 (P2): only a nested journal, with a nested-launch for its own token: no root process launched it.
    write_journal(
        tmp_path, "nested", 12, nested_token=NESTED_TOKEN, starts=[], extra=_launch(NESTED_TOKEN), parent_pid=12
    )

    summary = er.reduce_journals(tmp_path, [])

    assert summary["accepted"] is False
    assert "nested-run-unreachable" in kinds(summary["problems"])


def test_a_nested_journal_whose_parent_is_not_its_launcher_is_refused(er, tmp_path):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=_launch(NESTED_TOKEN))
    write_journal(tmp_path, "nested", 12, nested_token=NESTED_TOKEN, starts=[("n.py::x", None)], parent_pid=99)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert "nested-run-unreachable" in kinds(summary["problems"])
    assert summary["accepted"] is False


def test_a_chain_of_nested_launches_from_a_root_process_is_accepted(er, tmp_path):
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=_launch(NESTED_TOKEN))
    write_journal(
        tmp_path,
        "nested",
        12,
        nested_token=NESTED_TOKEN,
        starts=[("n.py::x", None)],
        extra=_launch(TOKEN2),
        parent_pid=11,
    )
    write_journal(tmp_path, "nested", 13, nested_token=TOKEN2, starts=[("m.py::z", None)], parent_pid=12)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert summary["problems"] == [] and summary["accepted"] is True


def test_two_nested_runs_that_only_launch_each_other_are_refused(er, tmp_path):
    write_journal(tmp_path, "nested", 12, nested_token=NESTED_TOKEN, starts=[], extra=_launch(TOKEN2), parent_pid=13)
    write_journal(tmp_path, "nested", 13, nested_token=TOKEN2, starts=[], extra=_launch(NESTED_TOKEN), parent_pid=12)

    summary = er.reduce_journals(tmp_path, [])

    assert sorted(p["token"] for p in summary["problems"] if p["kind"] == "nested-run-unreachable") == sorted(
        [NESTED_TOKEN, TOKEN2]
    )


def test_a_record_folder_that_cannot_be_listed_is_a_named_refusal_not_a_crash(er, tmp_path, monkeypatch):
    # 4431208517 (P2): EACCES or EIO while listing the held folder aborted reduce_journals.
    write_journal(tmp_path, "serial", 11, starts=[A_ROW])

    def broken(*args, **kwargs):
        raise PermissionError("synthetic listing failure")

    monkeypatch.setattr(os, "listdir", broken)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert summary["accepted"] is False and summary["status"] == "incomplete"
    assert "folder-unreadable" in kinds(summary["problems"])


def test_the_docstring_names_the_round_nineteen_problem_kinds(er):
    for kind in (
        "workers-collected-differently",
        "crash-report-outside-controller",
        "crash-report-unknown-item",
        "worker-evidence-missing",
        "nested-run-unreachable",
    ):
        assert kind in er.__doc__, kind


# --- round 20: the suffix check covers every reconciled process; the empty group name follows the writer -----------


def _nested_suffix_run(tmp_path, topology, raw, persisted="natural"):
    """A root launcher and one complete nested loadgroup run whose only item has requested group g and node id `raw`."""
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a", None)], extra=_launch(NESTED_TOKEN))
    row = (raw, "g")
    if topology == "serial":
        path = write_journal(tmp_path, "nested", 12, nested_token=NESTED_TOKEN, starts=[row])
    else:
        nodes = [("collection-manifest", manifest_fields([row], "node", "gw0"))]
        write_journal(tmp_path, "nested", 12, nested_token=NESTED_TOKEN, extra=nodes)
        path = write_journal(tmp_path, "worker", 13, worker="gw0", nested_token=NESTED_TOKEN, starts=[row])
    if persisted != "natural":
        damaged(path, "item-start", suffix_missing=persisted)


@pytest.mark.parametrize("persisted", [False, None, True], ids=["flag-false", "flag-omitted", "flag-true"])
@pytest.mark.parametrize("topology", ["serial", "worker"])
def test_n1_a_nested_item_missing_its_group_suffix_is_refused_like_a_root_item(er, tmp_path, topology, persisted):
    _nested_suffix_run(tmp_path, topology, "n.py::x", persisted)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    problem = next(p for p in summary["problems"] if p["kind"] == "suffix-missing")
    assert problem["nested_run"] == NESTED_TOKEN and problem["nodeid"] == "n.py::x"
    assert summary["accepted"] is False


@pytest.mark.parametrize("topology", ["serial", "worker"])
def test_n1_a_nested_item_with_its_group_suffix_is_accepted(er, tmp_path, topology):
    _nested_suffix_run(tmp_path, topology, "n.py::x@g")

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert summary["problems"] == [] and summary["accepted"] is True


@pytest.mark.parametrize("topology", ["serial", "worker"])
def test_n1_a_nested_persisted_flag_that_contradicts_the_node_id_is_refused(er, tmp_path, topology):
    _nested_suffix_run(tmp_path, topology, "n.py::x@g", persisted=True)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    problem = next(p for p in summary["problems"] if p["kind"] == "item-identity-mismatch")
    assert problem["nested_run"] == NESTED_TOKEN


EMPTY_GROUP = """
import pytest

pytestmark = pytest.mark.xdist_group("")


def test_x():
    assert True
"""


def test_n2_a_real_serial_run_with_an_empty_group_name_is_accepted(project, er, tmp_path):
    # The writer says suffix_missing = requested_group is not None and effective_group is None, so "" counts as a group.
    project.makepyfile(test_mod=EMPTY_GROUP)
    expected = collected_ids(project)
    rec = tmp_path / "rec"

    run_serial(project, rec).assert_outcomes(passed=1)

    (start,) = [r for _, r in records(rec, "item-start")]
    assert (start["requested_group"], start["effective_group"], start["suffix_missing"]) == ("", None, True)
    summary = er.reduce_journals(rec, expected)
    assert summary["accepted"], summary["problems"]


def test_n2_an_empty_group_name_in_a_hand_made_journal_follows_the_same_rule(er, tmp_path):
    quiet, loud = tmp_path / "quiet", tmp_path / "loud"
    quiet.mkdir()
    loud.mkdir()
    write_journal(quiet, "serial", 11, starts=[("t.py::a", "")], loadgroup=False)
    assert er.reduce_journals(quiet, ["t.py::a"])["problems"] == []
    write_journal(loud, "serial", 11, starts=[("t.py::a", "")], loadgroup=True)
    assert "suffix-missing" in kinds(er.reduce_journals(loud, ["t.py::a"])["problems"])


def test_n2_the_manifest_check_also_treats_an_empty_group_name_as_a_group(er, tmp_path):
    # a raw id ending in "@" with the requested group "": the effective group is "" and the canonical id drops the "@"
    write_journal(tmp_path, "serial", 11, starts=[("t.py::a@", "")], loadgroup=False)

    summary = er.reduce_journals(tmp_path, ["t.py::a"])

    assert summary["problems"] == [], summary["problems"]
    assert list(summary["items"]) == ["t.py::a"]


def _group_run(tmp_path, group, *, one_worker):
    """A controller and workers that all collected two members of `group` (node ids ending in its suffix); with two
    workers each runs one member, with one worker it runs both."""
    rows = [(f"t.py::a@{group}", group), (f"t.py::b@{group}", group)]
    workers = {"gw0": rows} if one_worker else {"gw0": rows[:1], "gw1": rows[1:]}
    nodes = [("collection-manifest", manifest_fields(rows, "node", w)) for w in workers]
    write_journal(tmp_path, "controller", 10, extra=nodes)
    for number, (worker, started) in enumerate(workers.items(), start=11):
        write_journal(tmp_path, "worker", number, worker=worker, starts=started, collected=rows)
    return [raw for raw, _ in rows]


@pytest.mark.parametrize("group", ["g", ""], ids=["named-group", "empty-group"])
def test_r21_members_of_a_group_that_ran_on_two_workers_are_a_split_whatever_the_group_name(er, tmp_path, group):
    # the empty name is a group too (the writer's rule): aggregation used a truthiness test and dropped it
    expected = [er.canonical_id(r, group) for r in _group_run(tmp_path, group, one_worker=False)]

    summary = er.reduce_journals(tmp_path, expected)

    assert "group-split" in kinds(summary["problems"]) and summary["accepted"] is False
    assert summary["groups"] == {group: ["gw0", "gw1"]}


@pytest.mark.parametrize("group", ["g", ""], ids=["named-group", "empty-group"])
def test_r21_members_of_a_group_on_one_worker_are_accepted_and_the_group_is_listed(er, tmp_path, group):
    expected = [er.canonical_id(r, group) for r in _group_run(tmp_path, group, one_worker=True)]

    summary = er.reduce_journals(tmp_path, expected)

    assert summary["problems"] == [] and summary["accepted"] is True
    assert summary["groups"] == {group: ["gw0"]}


EMPTY_GROUP_TWO = """
import pytest

pytestmark = pytest.mark.xdist_group("")


def test_x():
    assert True


def test_y():
    assert True
"""


def test_r22_a_real_two_worker_run_with_an_empty_group_name_joins_the_serial_collection(project, er, tmp_path):
    # xdist names these items `test_mod.py::test_x@`; the canonical id is the serial collection's id without it
    project.makepyfile(test_mod=EMPTY_GROUP_TWO)
    expected = collected_ids(project)
    rec = tmp_path / "rec"

    run_parallel(project, rec).assert_outcomes(passed=2)

    raw = sorted(r["nodeid"] for _, r in records(rec, "item-start"))
    assert raw == sorted(f"{i}@" for i in expected)
    summary = er.reduce_journals(rec, expected)
    assert summary["accepted"], summary["problems"]
    assert sorted(summary["items"]) == sorted(expected)


@pytest.mark.parametrize(
    ("raw", "group", "canonical"),
    [
        ("t.py::a@", "", "t.py::a"),
        ("t.py::a@g", "g", "t.py::a"),
        ("t.py::a", "", "t.py::a"),
        ("t.py::a@g", "other", "t.py::a@g"),
        ("t.py::a@g", None, "t.py::a@g"),
        ("t.py::x[a@b]", "", "t.py::x[a@b]"),
    ],
)
def test_r22_canonical_id_removes_the_suffix_exactly_when_it_equals_the_requested_group(er, raw, group, canonical):
    assert er.canonical_id(raw, group) == canonical
